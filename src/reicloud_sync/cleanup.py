"""Remove the managed service and optionally local setup, never synced content."""

from contextlib import ExitStack
import fcntl
import json
from pathlib import Path
import re
import shutil
import tomllib

from .config import ConfigError
from .pairs import Pair, load_pairs
from .runner import runner_lock
from .scheduling import runner_lock_path
from .service import HEADER, unit_path, unit_name, systemctl
from .sync import state_dir

CONFIG_FILES = ('config.toml', 'pairs.toml', 'rclone.conf', 'notifications.json', 'pairs.lock', 'migration.json')


def owned_states(paths):
    try:
        known = {state_dir(paths, pair) for pair in load_pairs(paths)}
    except (ConfigError, OSError):
        known = set()
    saved = [paths.config / 'pairs.toml']
    backups = paths.config / 'backups'
    if backups.is_dir() and not backups.is_symlink():
        saved.extend(directory / 'pairs.toml' for directory in backups.iterdir() if directory.is_dir() and not directory.is_symlink())
    for file in saved:
        if file.is_symlink():
            continue
        try:
            pairs = tomllib.loads(file.read_text()).get('pairs', [])
            for value in pairs:
                pair = Pair(value['name'], value['local'], value['remote'])
                known.add(state_dir(paths, pair))
        except (FileNotFoundError, ValueError, KeyError, TypeError):
            continue
    base = paths.state / 'pairs'
    if base.is_dir() and not base.is_symlink():
        for directory in base.iterdir():
            if directory.is_symlink() or not directory.is_dir():
                continue
            try:
                value = json.loads((directory / 'status.json').read_text())['pair']
                pair = Pair(value['name'], value['local'], value['remote'])
                if state_dir(paths, pair) == directory:
                    known.add(directory)
            except (FileNotFoundError, ValueError, KeyError, TypeError):
                pass
    return sorted(directory for directory in known if directory.exists() and not directory.is_symlink())


def plan(paths, *, wipe=False):
    if paths.config.is_symlink() or paths.state.is_symlink() or paths.cache.is_symlink() or (paths.state / 'pairs').is_symlink():
        raise ConfigError('Cleanup refuses symlinked application roots; resolve their ownership manually.')
    unit = unit_path(paths)
    if unit.is_symlink() or (unit.exists() and not unit.read_text().startswith(HEADER)):
        raise ConfigError('Refusing to remove an unmanaged service file.')
    files = [paths.config / name for name in CONFIG_FILES if (paths.config / name).exists() or (paths.config / name).is_symlink()] if wipe else []
    if wipe:
        backups = paths.config / 'backups'
        if backups.is_dir() and not backups.is_symlink():
            for directory in backups.iterdir():
                if directory.is_dir() and not directory.is_symlink() and re.fullmatch(r'\d{8}T\d{12}', directory.name):
                    files.extend(directory / name for name in ('config.toml', 'pairs.toml') if (directory / name).is_file())
    states = owned_states(paths) if wipe else []
    return {'service': str(unit) if unit.exists() else None,
            'remove_files': [str(file) for file in files],
            'remove_state_directories': [str(directory) for directory in states],
            'preserve': 'All synchronized local/Dropbox folders, unknown files, other profiles and the installed CLI/environment.'}


def uninstall(paths, *, wipe=False, yes=False, dry_run=False):
    result = plan(paths, wipe=wipe)
    if dry_run:
        return result
    if wipe and not yes:
        raise ConfigError('Wipe removes local configuration, credentials and owned history. Review uninstall --wipe --dry-run, then use --wipe --yes.')
    if result['service']:
        systemctl('disable', '--now', unit_name(paths))
    with runner_lock(paths), ExitStack() as stack:
        if paths.config.is_dir():
            lock = stack.enter_context((paths.config / 'pairs.lock').open('a'))
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = plan(paths, wipe=wipe)
        for directory in map(Path, result['remove_state_directories']):
            file = directory / 'run.lock'
            if file.exists():
                lock = stack.enter_context(file.open('r'))
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise ConfigError('A manual sync is still active. Stop it before wiping setup.') from None
        if result['service']:
            Path(result['service']).unlink()
            systemctl('daemon-reload')
        if wipe:
            for directory in map(Path, result['remove_state_directories']):
                shutil.rmtree(directory)
            for file in map(Path, result['remove_files']):
                file.unlink(missing_ok=True)
            runner_lock_path(paths).unlink(missing_ok=True)
    if wipe:
        # Only prune empty directories; unknown files and other profiles survive.
        backups = paths.config / 'backups'
        if backups.is_dir() and not backups.is_symlink():
            for directory in backups.iterdir():
                try:
                    directory.rmdir()
                except OSError:
                    pass
        for directory in (backups, paths.config, paths.state / 'pairs', paths.state, paths.cache):
            try:
                directory.rmdir()
            except OSError:
                pass
    return result
