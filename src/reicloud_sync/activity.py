"""Bounded per-pair activity logs, separate from trusted synchronization state."""

from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import fcntl
import json
import os
import re
import time
import uuid

from .logging import get_cli_logger

LIMIT = 1024 * 1024
BACKUPS = 3
_current = ContextVar('reicloud_activity', default=None)
_sensitive = re.compile(r'(?i)(access.?token|refresh.?token|authorization|bearer\s|client.?secret|password|"token"|\btoken\s*[:=])')


def sanitize(message):
    if _sensitive.search(message):
        return '[redacted credential-related diagnostic]'
    return message[:8192].rstrip()


class Recorder:
    def __init__(self, directory, name):
        self.directory, self.name = directory, name
        self.failed = False

    def record(self, event, **fields):
        if self.failed:
            return
        data = dict(fields, id=uuid.uuid4().hex, time=datetime.now(timezone.utc).isoformat(),
                    pair=self.name, event=event)
        try:
            append(self.directory, data)
        except OSError as error:
            self.failed = True
            get_cli_logger().warning('Activity log unavailable: %s', error)


def append(directory, data):
    file = directory / 'activity.jsonl'
    with (directory / 'activity.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if file.exists() and file.stat().st_size >= LIMIT:
            oldest = directory / f'activity.jsonl.{BACKUPS}'
            oldest.unlink(missing_ok=True)
            for index in range(BACKUPS - 1, 0, -1):
                source = directory / f'activity.jsonl.{index}'
                if source.exists():
                    source.replace(directory / f'activity.jsonl.{index + 1}')
            file.replace(directory / 'activity.jsonl.1')
        descriptor = os.open(file, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            stream.write(json.dumps(data, ensure_ascii=False) + '\n')


@contextmanager
def activity_scope(directory, name, operation, dry_run):
    recorder = Recorder(directory, name)
    token = _current.set(recorder)
    recorder.record('started', operation=operation, dry_run=dry_run)
    try:
        yield
    except BaseException as error:
        recorder.record('failed', operation=operation, dry_run=dry_run,
                        error_type=type(error).__name__, exit_code=getattr(error, 'returncode', None))
        raise
    else:
        recorder.record('completed', operation=operation, dry_run=dry_run)
    finally:
        _current.reset(token)


def current_recorder():
    return _current.get()


def read_entries(directory, limit=50):
    entries = deque(maxlen=limit)
    for name in [f'activity.jsonl.{i}' for i in range(BACKUPS, 0, -1)] + ['activity.jsonl']:
        try:
            with (directory / name).open(encoding='utf-8') as stream:
                for line in stream:
                    try:
                        entries.append(json.loads(line))
                    except ValueError:
                        continue
        except FileNotFoundError:
            pass
    return list(entries)


def follow(directory, limit=50):
    file = directory / 'activity.jsonl'
    stream = None
    seen = deque(maxlen=1000)
    try:
        if file.exists():
            stream = file.open(encoding='utf-8')
            stream.seek(0, os.SEEK_END)
        for entry in read_entries(directory, limit):
            seen.append(entry['id'])
            yield entry
        while True:
            if stream is None and file.exists():
                stream = file.open(encoding='utf-8')
            if stream:
                position = stream.tell()
                line = stream.readline()
                if line.endswith('\n'):
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    if entry['id'] not in seen:
                        seen.append(entry['id'])
                        yield entry
                    continue
                stream.seek(position)
                if file.exists() and os.fstat(stream.fileno()).st_ino != file.stat().st_ino:
                    stream.close()
                    stream = None
                    continue
            time.sleep(0.2)
    finally:
        if stream:
            stream.close()
