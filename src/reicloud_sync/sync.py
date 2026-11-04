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


def write_state(directory, data):
    filename = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', dir=directory, delete=False) as stream:
            filename = stream.name
            json.dump(data, stream, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(filename, directory / 'status.json')
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
    if directory.exists():
        with (directory / 'run.lock').open('a') as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                active = True
    if state['phase'] == 'running' and not active:
        state = dict(state, phase='needs_recovery', error='Previous operation was interrupted.')
    return dict(state, name=pair.name, active=active, state_dir=str(directory))


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
            verify_roots(paths, pair, run)
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
                code = error.returncode if isinstance(error, subprocess.CalledProcessError) else None
                retryable = operation == 'sync' and code in (1, 2)
                message = ('Sync failed without requiring resync. Inspect diagnostics and retry sync; '
                           'for intended deletions above the limit, preview with --max-delete PERCENT.'
                           if retryable else
                           f'Operation failed ({type(error).__name__}); inspect rclone diagnostics before recovery.')
                state.update(phase='retryable_error' if retryable else 'needs_recovery',
                             finished=now(), exit_code=code, error=message)
                logger.error('%s', message)
                write_state(directory, state)
                raise
            state.update(phase='ready', finished=now(), last_success=now(), error=None, exit_code=0)
            write_state(directory, state)
            logger.info('%s complete for %s. Last successful run: %s', operation, name, state['last_success'])
