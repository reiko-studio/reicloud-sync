"""Shared workspace configuration and explicit legacy migration."""

from dataclasses import asdict, replace
from datetime import datetime, timezone
import fcntl
import json
from pathlib import Path
import re
import shutil
import socket

from .config import ConfigError, atomic_text
from .pairs import load_pairs, make_pair, serialize_pairs, validate_pairs, workspace_name
from .runner import runner_lock

NAMESPACE = '.reicloud-computers'


def computer_name(label=None):
    label = label or socket.gethostname()
    label = re.sub(r'[^a-z0-9_-]+', '-', label.lower()).strip('-_')[:56]
    if not label:
        raise ConfigError('Computer label must contain letters or digits.')
    try:
        prefix = Path('/etc/machine-id').read_text().strip()[:6].lower()
    except OSError:
        raise ConfigError('Cannot read /etc/machine-id to build a stable computer name.') from None
    if not re.fullmatch('[0-9a-f]{6}', prefix):
        raise ConfigError('/etc/machine-id does not contain a valid ID prefix.')
    return f'{label}-{prefix}'


def external_remote(config, name, remote="dropbox"):
    return f'{remote}:{NAMESPACE}/{config["computer"]["name"]}/{name}'


def plan(paths, local, label=None, *, retire_legacy=False):
    config = paths.load()
    if config['version'] != 1:
        raise ConfigError('Workspace is already configured. Existing workspace/computer identity cannot be silently changed.')
    existing = load_pairs(paths)
    computer = computer_name(label)
    new_config = {'version': 2, 'dropbox': {'remote': 'dropbox'}, 'computer': {'name': computer}}
    root = str(Path(local).expanduser().resolve())
    workspace = next((pair for pair in existing if pair.local == root and pair.remote == 'dropbox:'), None)
    workspace = replace(workspace, name='workspace', kind='workspace') if workspace else make_pair('workspace', root, 'dropbox:', kind='workspace')
    pairs, retired = [workspace], []
    for pair in existing:
        if pair.local == workspace.local and pair.remote == workspace.remote:
            continue
        if pair.name != 'workspace' and pair.remote == external_remote(new_config, pair.name):
            pairs.append(replace(pair, kind='external'))
        else:
            retired.append(pair)
    for pair in pairs:
        for directory in (paths.config, paths.state, paths.cache):
            root_path = Path(pair.local)
            if directory.resolve().is_relative_to(root_path) or root_path.is_relative_to(directory.resolve()):
                raise ConfigError('Workspace/mapping must not overlap application configuration, state or cache.')
    validate_pairs(pairs, new_config)
    return {'computer': computer, 'workspace': root,
            'retired_pairs': [asdict(pair) for pair in retired],
            'requires_retirement': bool(retired) and not retire_legacy,
            'pairs': [asdict(pair) for pair in pairs]}, new_config, pairs


def registry_pairs(pairs):
    return [replace(pair, name=workspace_name(pair.remote.partition(':')[0]))
            if pair.kind == 'workspace' else pair for pair in pairs]


def migrate(paths, local, label=None, *, retire_legacy=False, dry_run=False, registry=False):
    result, config, pairs = plan(paths, local, label, retire_legacy=retire_legacy)
    if registry:
        config = registry_config(config['computer'], result['workspace'])
        pairs = registry_pairs(pairs)
        validate_pairs(pairs, config)
        result.update(version=3, pairs=[asdict(pair) for pair in pairs])
    if dry_run:
        return result
    if result['requires_retirement']:
        raise ConfigError('Existing pairs need an explicit migration decision. Preview migrate --dry-run; use --retire-legacy-pairs to unregister them without moving or deleting any files.')
    # Runner exclusion plus the config lock prevents workers seeing half a migration.
    with runner_lock(paths), (paths.config / 'pairs.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        result, config, pairs = plan(paths, local, label, retire_legacy=retire_legacy)
        if result['requires_retirement']:
            raise ConfigError('Pair configuration changed; preview migration again.')
        if registry:
            config = registry_config(config['computer'], result['workspace'])
            pairs = registry_pairs(pairs)
            validate_pairs(pairs, config)
            result.update(version=3, pairs=[asdict(pair) for pair in pairs])
        result['backup'] = publish(paths, config, pairs)
    return result


def settings_text(config):
    lines = [f'version = {config["version"]}', '', '[computer]',
             f'name = {json.dumps(config["computer"]["name"])}']
    if config['version'] == 2:
        lines.extend(('', '[dropbox]', 'remote = "dropbox"'))
    else:
        for name, remote in config['remotes'].items():
            lines.extend(('', f'[remotes.{json.dumps(name)}]',
                          f'workspace = {json.dumps(remote["workspace"], ensure_ascii=False)}',
                          f'remote_watch = {str(remote.get("remote_watch", False)).lower()}'))
    return '\n'.join(lines) + '\n'


def publish(paths, config, pairs):
    backup = paths.config / 'backups' / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')
    backup.mkdir(parents=True, mode=0o700)
    for name in ('config.toml', 'pairs.toml'):
        file = paths.config / name
        if file.exists():
            shutil.copy2(file, backup / name)
    marker = paths.config / 'migration.json'
    atomic_text(marker, json.dumps({'backup': str(backup)}))
    try:
        atomic_text(paths.config / 'pairs.toml', serialize_pairs(pairs, config))
        atomic_text(paths.settings, settings_text(config))
    except BaseException:
        rollback(paths)
        raise
    marker.unlink()
    return str(backup)


def registry_config(computer, local):
    return {'version': 3, 'computer': computer,
            'remotes': {'dropbox': {'workspace': local, 'remote_watch': False}}}


def upgrade(paths, *, dry_run=False):
    def prepare():
        old = paths.load()
        if old['version'] != 2:
            raise ConfigError('Registry upgrade requires a v2 workspace.')
        pairs = load_pairs(paths)
        workspace = next(pair for pair in pairs if pair.kind == 'workspace')
        config = registry_config(old['computer'], workspace.local)
        pairs = registry_pairs(pairs)
        validate_pairs(pairs, config)
        return config, pairs

    config, pairs = prepare()
    result = {'version': 3, 'remotes': config['remotes'], 'pairs': [asdict(pair) for pair in pairs]}
    if not dry_run:
        with runner_lock(paths), (paths.config / 'pairs.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            config, pairs = prepare()
            result.update(remotes=config['remotes'], pairs=[asdict(pair) for pair in pairs])
            result['backup'] = publish(paths, config, pairs)
    return result


def rollback(paths):
    marker = paths.config / 'migration.json'
    try:
        backup = Path(json.loads(marker.read_text())['backup'])
    except (FileNotFoundError, ValueError, KeyError, TypeError):
        raise ConfigError('No valid interrupted migration to roll back.') from None
    if not backup.resolve().is_relative_to((paths.config / 'backups').resolve()) or backup.is_symlink():
        raise ConfigError('Invalid migration backup directory.')
    if not (backup / 'config.toml').is_file() or (backup / 'config.toml').is_symlink():
        raise ConfigError('Migration backup is missing its original settings.')
    for name in ('config.toml', 'pairs.toml'):
        saved = backup / name
        if saved.is_file():
            atomic_text(paths.config / name, saved.read_text())
        else:
            (paths.config / name).unlink(missing_ok=True)
    marker.unlink()


def rollback_command(paths):
    with runner_lock(paths), (paths.config / 'pairs.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        rollback(paths)
