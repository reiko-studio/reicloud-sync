"""Periodic foreground scheduling of independent manual-sync workers."""

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import os
import signal
import subprocess
import sys
import threading
import time

from .config import ConfigError
from .logging import get_cli_logger
from .pairs import load_pairs
from .sync import state_dir, status, read_control, write_state
from .scheduling import runner_lock_path, read_schedule


def start_worker(paths, pair):
    environment = dict(os.environ, XDG_STATE_HOME=str(paths.state.parent),
                       XDG_CACHE_HOME=str(paths.cache.parent))
    return subprocess.Popen(
        [sys.executable, '-m', 'reicloud_sync', '--config-dir', str(paths.config), 'sync', pair.name],
        env=environment, start_new_session=True,
    )


@dataclass
class Job:
    pair: object
    process: object


class Runner:
    def __init__(self, paths, jobs=2, *, clock=time.monotonic, worker=start_worker, wall_clock=time.time):
        self.paths = paths
        self.jobs = jobs
        self.clock = clock
        self.worker = worker
        self.wall_clock = wall_clock
        self.loaded = set()
        self.schedules = {}
        self.intervals = {}
        self.active = {}
        self.due = {}
        self.attempted = set()
        self.controls = {}
        self.failures = {}
        self.blocked = {}
        self.had_failures = False
        self.errors = {}
        self.logger = get_cli_logger()

    def load_schedule(self, key, pair):
        if key in self.loaded:
            return
        data = read_schedule(state_dir(self.paths, pair))
        self.schedules[key] = data
        self.failures[key] = data.get('failures', 0)
        if data.get('next_run'):
            remaining = datetime.fromisoformat(data['next_run']).timestamp() - self.wall_clock()
            self.due[key] = self.clock() + max(0, remaining)
        if data.get('blocked_signature') is not None:
            self.blocked[key] = tuple(data['blocked_signature'])
        self.loaded.add(key)

    def save_schedule(self, key, pair):
        due = self.due.get(key)
        next_run = None if due is None or key in self.active or key in self.blocked else datetime.fromtimestamp(
            self.wall_clock() + due - self.clock(), timezone.utc).isoformat()
        previous = self.schedules.get(key, {})
        if next_run and previous.get('next_run'):
            if abs(datetime.fromisoformat(next_run).timestamp() - datetime.fromisoformat(previous['next_run']).timestamp()) < 0.5:
                next_run = previous['next_run']
        data = {'next_run': next_run, 'failures': self.failures.get(key, 0),
                'blocked_signature': list(self.blocked[key]) if key in self.blocked else None,
                'error': self.errors.get(key)}
        if data != self.schedules.get(key):
            directory = state_dir(self.paths, pair)
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            write_state(directory, data, filename='schedule.json')
            self.schedules[key] = data

    @staticmethod
    def signature(state):
        return tuple(state.get(key) for key in ('phase', 'last_success', 'finished', 'error'))

    def completed(self, key, job, code, pair):
        self.logger.info('Scheduled sync %s exited with code %s.', job.pair.name, code)
        if code == 0:
            self.failures.pop(key, None)
            self.blocked.pop(key, None)
            self.due[key] = self.clock() + pair.interval
            return
        self.had_failures = True
        state = status(self.paths, pair.name)
        if state['active']:
            # A manual run may have won the per-pair lock just before our worker.
            self.due[key] = self.clock() + 5
        elif state['phase'] == 'retryable_error' and state.get('automatic_retry'):
            count = self.failures.get(key, 0) + 1
            self.failures[key] = count
            delay = min(30 * 2 ** min(count - 1, 7), 3600)
            self.due[key] = self.clock() + delay
            self.logger.warning('%s: transient failure; retry in %s seconds.', pair.name, delay)
        else:
            self.failures[key] = self.failures.get(key, 0) + 1
            self.blocked[key] = self.signature(state)
            self.logger.error('%s needs manual attention; other pairs will continue.', pair.name)

    def tick(self, *, once=False):
        pairs = load_pairs(self.paths)
        known = {str(state_dir(self.paths, pair)): pair for pair in pairs}
        for key, pair in known.items():
            try:
                self.load_schedule(key, pair)
            except (ConfigError, OSError, ValueError) as error:
                if self.errors.get(key) != str(error):
                    self.logger.error('%s: %s', pair.name, error)
                    self.errors[key] = str(error)
        known = {key: pair for key, pair in known.items() if key in self.loaded}
        for key, job in list(self.active.items()):
            code = job.process.poll()
            if code is not None:
                del self.active[key]
                if key in known:
                    try:
                        self.completed(key, job, code, known[key])
                    except (ConfigError, OSError, ValueError) as error:
                        self.logger.error('%s: %s', job.pair.name, error)
                        self.errors[key] = str(error)
        self.due = {key: value for key, value in self.due.items() if key in known}
        pending = False
        for key, pair in sorted(known.items(), key=lambda entry: self.due.get(entry[0], 0)):
            try:
                previous_interval = self.intervals.get(key)
                self.intervals[key] = pair.interval
                if (previous_interval is not None and previous_interval != pair.interval
                        and key in self.due and not self.failures.get(key)):
                    self.due[key] += pair.interval - previous_interval
                control = read_control(state_dir(self.paths, pair))
                previous = self.controls.get(key)
                self.controls[key] = control
                if previous is not None and previous != control and not control['paused']:
                    self.due.pop(key, None)
                if control['paused'] or key in self.active or (once and key in self.attempted):
                    continue
                state = status(self.paths, pair.name)
                signature = self.signature(state)
                if key in self.blocked:
                    if self.blocked[key] == signature:
                        continue
                    self.blocked.pop(key)
                    self.failures.pop(key, None)
                    self.due.pop(key, None)
                if state['active']:
                    continue
                if state['phase'] not in ('ready', 'retryable_error'):
                    continue
                if state['phase'] == 'retryable_error' and not state.get('automatic_retry'):
                    continue
                if not once and self.due.get(key, 0) > self.clock():
                    continue
                pending = True
                if len(self.active) >= self.jobs:
                    continue
                self.logger.info('Starting scheduled sync: %s', pair.name)
                self.attempted.add(key)
                try:
                    self.active[key] = Job(pair, self.worker(self.paths, pair))
                except OSError:
                    self.blocked[key] = signature
                    raise
                self.errors.pop(key, None)
            except (ConfigError, OSError, ValueError) as error:
                self.had_failures = True
                if self.errors.get(key) != str(error):
                    self.logger.error('%s: %s', pair.name, error)
                    self.errors[key] = str(error)
        for key, pair in known.items():
            try:
                self.save_schedule(key, pair)
            except OSError as error:
                self.logger.error('%s: cannot save schedule: %s', pair.name, error)
                self.had_failures = True
        return bool(self.active) or pending

    def stop(self):
        for job in self.active.values():
            if job.process.poll() is None:
                try:
                    os.killpg(job.process.pid, signal.SIGINT)
                except ProcessLookupError:
                    pass
        deadline = time.monotonic() + 100
        for job in self.active.values():
            try:
                job.process.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(job.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                job.process.wait()


@contextmanager
def runner_lock(paths):
    paths.state.mkdir(parents=True, exist_ok=True, mode=0o700)
    with runner_lock_path(paths).open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ConfigError('A runner for this configuration is already active.') from None
        yield


def run_forever(paths, jobs=2, *, once=False):
    paths.load()
    stopped = threading.Event()
    previous = {}
    runner = Runner(paths, jobs)
    with runner_lock(paths):
        try:
            for sig in (signal.SIGINT, signal.SIGTERM):
                previous[sig] = signal.signal(sig, lambda *_: stopped.set())
            runner.logger.info('Runner started. Only initialized pairs are synchronized; Ctrl+C stops it.')
            while not stopped.is_set():
                pending = runner.tick(once=once)
                if once and not pending:
                    break
                stopped.wait(1)
        finally:
            runner.stop()
            for sig, handler in previous.items():
                signal.signal(sig, handler)
            runner.logger.info('Runner stopped.')
    return 1 if once and runner.had_failures else 0
