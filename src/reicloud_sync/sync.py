"""Manual bisync orchestration with durable state and conservative recovery."""

from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from .config import ConfigError
from .pairs import find_pair, make_pair
from .logging import get_cli_logger
from .scheduling import read_schedule, runner_active

MARKER = '.reicloud-sync-access'


def now():
    return datetime.now(timezone.utc).isoformat()


def state_dir(paths, pair):
    identity = json.dumps([str(paths.config.resolve()), pair.local, pair.remote])
    digest = hashlib.sha256(identity.encode()).hexdigest()[:24]
    return paths.state / 'pairs' / digest


def read_state(directory):
    try:
        data = json.loads((directory / 'status.json').read_text())
    except FileNotFoundError:
        return {'phase': 'uninitialized', 'last_success': None}
    except (ValueError, OSError) as error:
        raise ConfigError(f'Cannot read sync state: {error}') from None
    if not isinstance(data, dict) or data.get('phase') not in ('running', 'ready', 'retryable_error', 'needs_recovery'):
        raise ConfigError('Invalid sync state; inspect status.json before continuing.')
    return data


def write_state(directory, data, *, filename="status.json"):
    target_name = filename
    filename = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', dir=directory, delete=False) as stream:
            filename = stream.name
            json.dump(data, stream, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(filename, directory / target_name)
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if filename and os.path.exists(filename):
            os.unlink(filename)


@contextmanager
def pair_lock(directory):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / 'run.lock').open('a') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ConfigError('This pair already has a running operation.') from None
        yield stream.fileno()


def status(paths, name):
    pair = find_pair(paths, name)
    directory = state_dir(paths, pair)
    state = read_state(directory)
    active = False
    try:
        with (directory / 'run.lock').open('r') as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                active = True
    except FileNotFoundError:
        pass
    if state['phase'] == 'running' and not active:
        state = dict(state, phase='needs_recovery', error='Previous operation was interrupted.')
    control = read_control(directory)
    schedule = read_schedule(directory)
    running = runner_active(paths)
    next_run = schedule.get('next_run') if (running and not control['paused'] and not active
               and (state['phase'] == 'ready' or (state['phase'] == 'retryable_error' and state.get('automatic_retry')))
               and not schedule.get('blocked_signature')) else None
    return dict(state, name=pair.name, active=active, state_dir=str(directory),
                paused=control['paused'], interval_seconds=pair.interval,
                runner_active=running, next_run=next_run,
                scheduled_for=schedule.get('next_run'),
                consecutive_failures=schedule.get('failures', 0),
                last_error=state.get('last_error') or state.get('error') or schedule.get('error'),
                scheduler_error=schedule.get('error'))


def verify_roots(paths, pair, run):
    make_pair(pair.name, pair.local, pair.remote, require_directory=True)
    entries = json.loads(run(paths, 'listremotes', '--json', capture=True).stdout)
    if not any(entry['name'] == 'dropbox' and entry['type'] == 'dropbox' for entry in entries):
        raise ConfigError("Dropbox is not configured. Run 'reicloud login dropbox'.")
    try:
        result = run(paths, 'lsjson', pair.remote, '--stat', capture=True)
    except subprocess.CalledProcessError as error:
        if error.returncode in (3, 4):
            raise ConfigError(f'Remote directory is missing: {pair.remote}. Create it in Dropbox before continuing.') from None
        raise
    info = json.loads(result.stdout)
    if not isinstance(info, dict) or not info.get('IsDir'):
        raise ConfigError(f'Remote root is not a directory: {pair.remote}')


def failure_policy(error, *, preflight=False):
    if isinstance(error, KeyboardInterrupt):
        return 'interrupted', False
    if not isinstance(error, subprocess.CalledProcessError):
        return 'configuration', False
    code = error.returncode
    text = (error.stderr or '').lower()
    if code == 7 and not preflight:
        return 'critical', False
    safety = ('too many deletes', 'safety abort', 'check-access', 'access health',
              'all files have changed', 'lock file', 'check-sync', 'integrity')
    if any(word in text for word in safety):
        return 'safety', False
    if code == 2:
        return 'configuration', False
    transient = ('timeout', 'timed out', 'connection', 'network', 'temporary',
                 'temporarily', 'rate limit', 'too many requests', '429',
                 '502 bad gateway', '503 service', '504 gateway')
    if (preflight and code == 5) or (code == 1 and any(word in text for word in transient)):
        return 'transient', True
    return 'unknown', False


def record_failure(directory, state, error, operation, *, preflight=False):
    code = error.returncode if isinstance(error, subprocess.CalledProcessError) else None
    retryable = operation == 'sync' and (preflight or code in (1, 2))
    kind, automatic = failure_policy(error, preflight=preflight)
    if isinstance(error, ConfigError):
        message = str(error)
    elif retryable:
        message = ('Sync did not finish. Inspect diagnostics and retry sync; '
                   'for intended deletions above the limit, preview with --max-delete PERCENT.')
    else:
        message = f'Operation failed ({type(error).__name__}); inspect diagnostics before recovery.'
    state.update(phase='retryable_error' if retryable else 'needs_recovery',
                 operation=operation, finished=now(), exit_code=code, error=message,
                 failure_kind=kind, automatic_retry=automatic and retryable,
                 last_error=message, last_error_at=now())
    # Store classification and a safe message, never raw provider diagnostics.
    get_cli_logger().error('%s', message)
    write_state(directory, state)


def transfer(paths, name, operation, prefer, dry_run, run, *, retry=False, max_delete=50):
    logger = get_cli_logger()
    # Hold the config lock while using a pair so remove/add cannot race a transfer.
    paths.load()
    with (paths.config / 'pairs.lock').open('a') as config_lock:
        fcntl.flock(config_lock, fcntl.LOCK_SH)
        pair = find_pair(paths, name)
        directory = state_dir(paths, pair)
        with pair_lock(directory) as lock_fd:
            state = read_state(directory)
            if operation == 'sync' and read_control(directory)['paused'] and not dry_run:
                raise ConfigError(f'Pair {name} is paused. Run reicloud resume {name} first.')
            if operation == 'init' and state['phase'] != 'uninitialized':
                raise ConfigError('Pair already has sync state. Use sync, or explicitly recover after inspecting the previous failure.')
            retry_old_failure = (retry and state['phase'] == 'needs_recovery'
                                 and state.get('operation') == 'sync' and state.get('last_success'))
            if operation == 'sync' and state['phase'] not in ('ready', 'retryable_error') and not retry_old_failure:
                raise ConfigError('Pair is not ready. Initialize it first, or inspect status before using recover. For a previous failed sync, --retry attempts existing history without resync.')
            if operation == 'recover' and state['phase'] == 'uninitialized':
                raise ConfigError('Pair has no sync state. Use init first.')
            if operation == 'recover' and state['phase'] == 'ready':
                raise ConfigError('Pair is ready; use sync. Recovery is reserved for interrupted or failed operations.')
            try:
                verify_roots(paths, pair, run)
            except (ConfigError, OSError, ValueError, subprocess.CalledProcessError) as error:
                if operation == 'sync' and not dry_run:
                    record_failure(directory, state, error, operation, preflight=True)
                raise
            workdir = directory / 'bisync'
            if operation == 'sync':
                if not workdir.is_dir() or not list(workdir.glob('*.path1.lst')) or not list(workdir.glob('*.path2.lst')):
                    if not dry_run:
                        state.update(phase='needs_recovery', error='Bisync history is missing.')
                        write_state(directory, state)
                    raise ConfigError('Bisync history is missing. Inspect state and recover explicitly; no automatic resync was attempted.')
            arguments = ['--verbose', '--create-empty-src-dirs',
                         '--conflict-resolve', 'none', '--conflict-loser', 'num', '--max-delete', str(max_delete)]
            if operation != 'sync':
                arguments += ['--resync-mode', 'path1' if prefer == 'local' else 'path2']
                logger.warning('%s %s: differing files prefer %s during initialization; unique files are combined.', operation, name, prefer)
            if operation == 'sync' or not dry_run:
                arguments += ['--check-access', '--check-filename', MARKER]
            if dry_run:
                logger.info('Preview only. No file contents will be transferred.')
                with tempfile.TemporaryDirectory(prefix='reicloud-preview-') as temporary:
                    preview = Path(temporary) / 'bisync'
                    if operation == 'sync':
                        shutil.copytree(workdir, preview)
                    else:
                        preview.mkdir()
                    run(paths, 'bisync', pair.local, pair.remote, *arguments,
                        '--workdir', str(preview), '--dry-run', lock_fd=lock_fd)
                logger.info('Preview complete. Persistent sync history was not changed.')
                return
            state = dict(state, phase='running', operation=operation,
                         started=now(), pair=asdict(pair), error=None, exit_code=None)
            write_state(directory, state)
            try:
                if operation != 'sync':
                    marker = Path(pair.local) / MARKER
                    if marker.is_symlink() or (marker.exists() and not marker.is_file()):
                        raise ConfigError(f'Reserved access marker is not a regular file: {marker}')
                    try:
                        with marker.open('x') as stream:
                            stream.write('reicloud-sync access check\n')
                    except FileExistsError:
                        pass
                    run(paths, 'copyto', str(marker), pair.remote.rstrip('/') + '/' + MARKER,
                        lock_fd=lock_fd)
                workdir.mkdir(exist_ok=True, mode=0o700)
                # Explicit recovery gets fresh history, retaining failed history for inspection.
                if operation == 'recover':
                    suffix = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')
                    workdir.rename(directory / f'bisync-before-recovery-{suffix}')
                    workdir.mkdir(mode=0o700)
                run(paths, 'bisync', pair.local, pair.remote, *arguments,
                    '--workdir', str(workdir), lock_fd=lock_fd)
            except BaseException as error:
                record_failure(directory, state, error, operation)
                raise
            state.update(phase='ready', finished=now(), last_success=now(), error=None, exit_code=0,
                         failure_kind=None, automatic_retry=False)
            write_state(directory, state)
            logger.info('%s complete for %s. Last successful run: %s', operation, name, state['last_success'])


def read_control(directory):
    try:
        data = json.loads((directory / 'control.json').read_text())
    except FileNotFoundError:
        return {'paused': False, 'updated': None}
    except (ValueError, OSError) as error:
        raise ConfigError(f'Cannot read pause state: {error}') from None
    if not isinstance(data, dict) or type(data.get('paused')) is not bool:
        raise ConfigError('Invalid control.json; paused must be a boolean.')
    return data


def set_paused(paths, name, paused):
    pair = find_pair(paths, name)
    directory = state_dir(paths, pair)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / 'control.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        write_state(directory, {'paused': paused, 'updated': now()}, filename='control.json')
    get_cli_logger().info('%s %s. Active transfers are allowed to finish.',
                          'Paused' if paused else 'Resumed', name)
