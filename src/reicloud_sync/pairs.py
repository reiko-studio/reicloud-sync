"""Validated folder pairs stored independently of application settings."""

from contextlib import contextmanager
from dataclasses import asdict, dataclass
import fcntl
import json
from pathlib import Path
import re
import tomllib
import unicodedata

from .config import ConfigError, atomic_text
from .remotes import REMOTE_NAME


@dataclass(frozen=True)
class Pair:
    name: str
    local: str
    remote: str
    interval: int = 300
    exclusions: tuple[str, ...] = ()
    kind: str = "legacy"


def make_pair(name, local, remote, interval=300, exclusions=(), kind="legacy", *, require_directory=False):
    if kind not in ("legacy", "workspace", "external"):
        raise ConfigError("Pair kind must be legacy, workspace or external.")
    from .exclusions import validate_patterns
    exclusions = validate_patterns(exclusions)
    if type(interval) is not int or not 1 <= interval <= 604800:
        raise ConfigError("Interval must be an integer from 1 to 604800 seconds.")
    if not isinstance(name, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}', name):
        raise ConfigError('Pair names must be 1–64 letters, digits, underscores or hyphens, starting with a letter or digit.')
    if not isinstance(local, str) or not local.strip():
        raise ConfigError('Local path must be a nonempty string.')
    path = Path(local).expanduser().resolve()
    if require_directory and not path.is_dir():
        raise ConfigError(f'Local directory does not exist: {path}')
    connection, separator, raw = remote.partition(':') if isinstance(remote, str) else ('', '', '')
    if not separator or not REMOTE_NAME.fullmatch(connection):
        raise ConfigError('Remote must use NAME:PATH.')
    if '\\' in raw or any(ord(character) < 32 for character in raw):
        raise ConfigError('Remote path contains unsupported characters.')
    parts = [part for part in raw.split('/') if part and part != '.']
    if '..' in parts:
        raise ConfigError('Remote path must not contain .. components.')
    return Pair(name, str(path), connection + ':' + '/'.join(parts), interval, exclusions, kind)


def remote_parts(remote):
    return tuple(unicodedata.normalize('NFC', part).casefold() for part in remote.partition(':')[2].split('/') if part)


def overlaps(left, right):
    return left[:len(right)] == right or right[:len(left)] == left


def validate_pairs(pairs, config=None):
    if config and config['version'] == 2:
        from .workspace import external_remote
        workspaces = [pair for pair in pairs if pair.kind == 'workspace']
        if len(workspaces) != 1 or workspaces[0].name != 'workspace' or workspaces[0].remote != 'dropbox:':
            raise ConfigError('Exactly one workspace pair named workspace must target dropbox:.')
        for pair in pairs:
            if pair.kind != 'workspace' and (pair.kind != 'external' or pair.remote != external_remote(config, pair.name)):
                raise ConfigError('External mappings must target this computer namespace; arbitrary Dropbox root mappings are not allowed.')
    if config and config['version'] == 3:
        from .workspace import external_remote
        roots = {pair.remote[:-1] for pair in pairs if pair.kind == 'workspace'}
        if roots != set(config['remotes']):
            raise ConfigError('Each registered remote requires its canonical workspace.')
        for pair in pairs:
            name = pair.remote.partition(':')[0]
            if name not in roots:
                raise ConfigError(f'Unknown remote: {name}')
            if pair.kind == 'workspace':
                if pair.name != workspace_name(name) or pair.remote != name + ':' or pair.local != str(Path(config['remotes'][name]['workspace']).expanduser().resolve()):
                    raise ConfigError('Workspace does not match its registered remote.')
            elif pair.kind != 'external' or pair.remote != external_remote(config, pair.name, name):
                raise ConfigError('External mappings must use the managed computer namespace.')
    elif config and any(not pair.remote.startswith('dropbox:') for pair in pairs):
        raise ConfigError('Legacy configurations support only the dropbox connection.')
    for index, pair in enumerate(pairs):
        for other in pairs[:index]:
            if pair.name == other.name:
                raise ConfigError(f'Duplicate pair name: {pair.name}')
            if overlaps(Path(pair.local).parts, Path(other.local).parts):
                raise ConfigError(f'Local roots overlap: {pair.name} and {other.name}')
            managed_overlap = (config and config['version'] >= 2 and {pair.kind, other.kind} == {'workspace', 'external'})
            if (pair.remote.partition(':')[0] == other.remote.partition(':')[0]
                    and not managed_overlap and overlaps(remote_parts(pair.remote), remote_parts(other.remote))):
                raise ConfigError(f'Dropbox roots overlap: {pair.name} and {other.name}')


def load_pairs(paths):
    config = paths.load()
    file = paths.config / 'pairs.toml'
    try:
        with file.open('rb') as stream:
            data = tomllib.load(stream)
    except FileNotFoundError:
        if config['version'] == 2:
            raise ConfigError('Workspace pair configuration is missing.') from None
        data = {} if config['version'] == 3 else {'pairs': []}
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f'Invalid pairs.toml: {error}') from None
    if config['version'] == 3:
        pairs = load_v3_pairs(config, data)
        return validate_locations(paths, pairs, config)
    if set(data) != {'pairs'} or not isinstance(data['pairs'], list):
        raise ConfigError('pairs.toml must contain a pairs array.')
    pairs = []
    for entry in data['pairs']:
        if not isinstance(entry, dict) or not {'name', 'local', 'remote'} <= set(entry) or set(entry) - {'name', 'local', 'remote', 'interval', 'exclusions', 'kind'}:
            raise ConfigError('Each pair needs name, local and remote; interval and exclusions are optional.')
        if not isinstance(entry['local'], str) or not Path(entry['local']).is_absolute():
            raise ConfigError('Saved local paths must be absolute.')
        pair = make_pair(**entry)
        pairs.append(pair)
    return validate_locations(paths, pairs, config)


def validate_locations(paths, pairs, config):
    for pair in pairs:
        for directory in (paths.config, paths.state, paths.cache):
            if overlaps(Path(pair.local).parts, directory.resolve().parts):
                raise ConfigError('Local root must not overlap reicloud configuration, state or cache.')
    validate_pairs(pairs, config)
    return pairs


def workspace_name(remote):
    return 'workspace' if remote == 'dropbox' else f'workspace-{remote}'


def load_v3_pairs(config, data):
    from .workspace import external_remote
    options, external = data.get('workspaces', {}), data.get('pairs', {})
    if (set(data) - {'workspaces', 'pairs'} or not isinstance(options, dict)
            or not isinstance(external, dict) or set(options) - set(config['remotes'])):
        raise ConfigError('Invalid v3 pairs.toml or mixed configuration versions.')
    pairs = []
    for remote, settings in config['remotes'].items():
        overrides = options.get(remote, {})
        if not isinstance(overrides, dict) or set(overrides) - {'interval', 'exclusions'}:
            raise ConfigError(f'Invalid workspace options for {remote}')
        pairs.append(make_pair(workspace_name(remote), settings['workspace'], remote + ':',
                               kind='workspace', **overrides))
    for name, entry in external.items():
        if not isinstance(entry, dict) or not {'local', 'targets'} <= set(entry) or set(entry) - {'local', 'targets', 'interval', 'exclusions'}:
            raise ConfigError(f'Invalid pair definition: {name}')
        targets = entry['targets']
        if not isinstance(targets, list) or len(targets) != 1 or not isinstance(targets[0], str):
            raise ConfigError(f'Pair {name} must specify exactly one target in this release.')
        target = targets[0]
        if target not in config['remotes']:
            raise ConfigError(f'Unknown remote: {target}')
        options = {key: value for key, value in entry.items() if key != 'targets'}
        pairs.append(make_pair(name, remote=external_remote(config, name, target), kind='external', **options))
    return pairs


def find_pair(paths, name):
    for pair in load_pairs(paths):
        if pair.name == name:
            return pair
    raise ConfigError(f'Unknown pair: {name}')


@contextmanager
def edit_pairs(paths):
    paths.load()
    with (paths.config / 'pairs.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        pairs = load_pairs(paths)
        yield pairs
        config = paths.load()
        validate_locations(paths, pairs, config)
        atomic_text(paths.config / 'pairs.toml', serialize_pairs(pairs, config))


def serialize_pairs(pairs, config=None):
    if config and config['version'] == 3:
        lines = []
        for pair in pairs:
            remote = pair.remote.partition(':')[0]
            if pair.kind == 'workspace':
                lines.append(f'[workspaces.{json.dumps(remote)}]')
            else:
                lines.extend((f'[pairs.{json.dumps(pair.name)}]',
                              f'local = {json.dumps(pair.local, ensure_ascii=False)}',
                              f'targets = {json.dumps([remote])}'))
            lines.extend((f'interval = {pair.interval}',
                          f'exclusions = {json.dumps(pair.exclusions, ensure_ascii=False)}', ''))
        return '\n'.join(lines)

    lines = ['# Managed by reicloud; comments and formatting are regenerated.\n']
    if not pairs:
        lines.append('pairs = []\n')
    for pair in pairs:
        lines.append('\n[[pairs]]\n')
        for key, value in asdict(pair).items():
            lines.append(f'{key} = {json.dumps(value, ensure_ascii=False)}\n')
    return ''.join(lines)
