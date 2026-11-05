"""Supervise rclone Dropbox notifications as hints for the sync scheduler."""

from dataclasses import dataclass, field
import json
import os
from queue import Empty, Full, Queue
import re
import shutil
import subprocess
import threading
import time

from .config import ConfigError
from .logging import get_cli_logger
from .pairs import remote_parts
from .sync import state_dir, MARKER
from .workspace import NAMESPACE

_EVENT = re.compile(r'("(?:[^"\\]|\\.)*"): (?:0|1|directory|object)')


def event_path(message):
    match = _EVENT.fullmatch(message)
    if not match:
        return None
    try:
        path = json.loads(match[1])
    except ValueError:
        return None
    parts = path.strip('/').split('/')
    if not parts[0] or any(part in ('', '.', '..') for part in parts):
        return None
    return path


def affects(pair, remote, path):
    if pair.remote.partition(':')[0] != remote:
        return False
    changed = remote_parts(remote + ':' + path)
    root = remote_parts(pair.remote)
    if not changed or changed[-1] == MARKER.casefold():
        return False
    if pair.kind == 'workspace' and changed[0] == NAMESPACE:
        return False
    # A deleted ancestor can affect every pair beneath it.
    return changed[:len(root)] == root or root[:len(changed)] == changed


@dataclass
class Listener:
    stop: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    process: subprocess.Popen | None = None
    health: str = 'starting'


class RemoteWatchManager:
    def __init__(self, paths):
        self.paths = paths
        self.listeners = {}
        self.retry_after = {}
        self.events = Queue(maxsize=4096)
        self.pairs = []
        self.logger = get_cli_logger()

    def monitor(self, name, listener):
        try:
            executable = shutil.which('rclone')
            if not executable:
                raise ConfigError('rclone unavailable')
            command = [executable, '--config', str(self.paths.credentials)]
            env = {key: value for key, value in os.environ.items() if not key.startswith('RCLONE_')}
            result = subprocess.run([*command, 'listremotes', '--json'], env=env,
                                    capture_output=True, text=True, check=True, timeout=5)
            if not any(entry['name'] == name and entry['type'] == 'dropbox'
                       for entry in json.loads(result.stdout)):
                listener.health = 'unsupported or disconnected'
                return
            if listener.stop.is_set():
                return
            with subprocess.Popen([*command, 'test', 'changenotify', name + ':',
                                   '--poll-interval', '1s', '--use-json-log', '--log-level', 'INFO'],
                                  env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True, errors='replace') as process:
                listener.process = process
                if listener.stop.is_set():
                    process.terminate()
                for line in process.stdout:
                    if listener.stop.is_set():
                        break
                    try:
                        message = json.loads(line).get('msg', '')
                    except (ValueError, AttributeError):
                        continue
                    if not isinstance(message, str):
                        continue
                    if message.startswith('Waiting for changes,'):
                        if listener.health != 'degraded':
                            listener.health = 'listening'
                    elif 'Change notify listener failure' in message or 'Failed to get StartCursor' in message:
                        listener.health = 'degraded'
                    path = event_path(message)
                    if path is not None:
                        listener.health = 'listening'
                        try:
                            self.events.put_nowait((name, path))
                        except Full:
                            # Periodic scans reconcile events lost during bursts.
                            pass
                if process.poll() is None and listener.stop.is_set():
                    process.terminate()
                process.wait()
            if not listener.stop.is_set():
                listener.health = 'retrying'
        except (OSError, ValueError, KeyError, TypeError, ConfigError, subprocess.SubprocessError):
            listener.health = 'retrying'
        finally:
            if not listener.stop.is_set():
                self.retry_after[name] = time.monotonic() + 30
                self.logger.warning('Remote watcher %s: %s. Periodic scans continue.', name, listener.health)

    def refresh(self, pairs):
        self.pairs = pairs
        configured = self.paths.load().get('remotes', {})
        wanted = {pair.remote.partition(':')[0] for pair in pairs}
        wanted = {name for name in wanted if configured.get(name, {}).get('remote_watch')}
        for name, listener in list(self.listeners.items()):
            if name not in wanted:
                self.stop(listener)
                del self.listeners[name]
        for name in sorted(wanted):
            listener = self.listeners.get(name)
            if listener and listener.thread.is_alive():
                continue
            if self.retry_after.get(name, 0) > time.monotonic():
                continue
            listener = Listener()
            listener.thread = threading.Thread(target=self.monitor, args=(name, listener), daemon=True)
            self.listeners[name] = listener
            listener.thread.start()

    def drain(self):
        keys = set()
        configured = self.paths.load().get('remotes', {})
        for _ in range(self.events.maxsize):
            try:
                remote, path = self.events.get_nowait()
            except Empty:
                return keys
            if remote not in self.listeners or not configured.get(remote, {}).get('remote_watch'):
                continue
            keys.update(str(state_dir(self.paths, pair)) for pair in self.pairs if affects(pair, remote, path))

        return keys

    def health(self, pair):
        listener = self.listeners.get(pair.remote.partition(':')[0])
        return listener.health if listener else 'idle'

    @staticmethod
    def stop(listener):
        listener.stop.set()
        process = listener.process
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
        listener.thread.join(timeout=2)

    def close(self):
        for listener in self.listeners.values():
            self.stop(listener)
        self.listeners.clear()
