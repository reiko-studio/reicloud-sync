"""Validated folder pairs stored independently of application settings."""

from contextlib import contextmanager
from dataclasses import asdict, dataclass
import fcntl
import json
import os
from pathlib import Path
import re
import tempfile
import tomllib
import unicodedata

from .config import ConfigError


@dataclass(frozen=True)
class Pair:
    name: str
    local: str
    remote: str
    interval: int = 300


def make_pair(name, local, remote, interval=300, *, require_directory=False):
    if type(interval) is not int or not 1 <= interval <= 604800:
        raise ConfigError("Interval must be an integer from 1 to 604800 seconds.")
    if not isinstance(name, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}', name):
        raise ConfigError('Pair names must be 1–64 letters, digits, underscores or hyphens, starting with a letter or digit.')
    if not isinstance(local, str) or not local.strip():
        raise ConfigError('Local path must be a nonempty string.')
    path = Path(local).expanduser().resolve()
    if require_directory and not path.is_dir():
        raise ConfigError(f'Local directory does not exist: {path}')
    if not isinstance(remote, str) or not remote.startswith('dropbox:'):
        raise ConfigError('Remote must use dropbox:PATH; only Dropbox is supported yet.')
    raw = remote.removeprefix('dropbox:')
    if '\\' in raw or any(ord(character) < 32 for character in raw):
        raise ConfigError('Remote path contains unsupported characters.')
    parts = [part for part in raw.split('/') if part and part != '.']
    if '..' in parts:
        raise ConfigError('Remote path must not contain .. components.')
    return Pair(name, str(path), 'dropbox:' + '/'.join(parts), interval)


def remote_parts(remote):
    return tuple(unicodedata.normalize('NFC', part).casefold() for part in remote[8:].split('/') if part)


def overlaps(left, right):
    return left[:len(right)] == right or right[:len(left)] == left


def validate_pairs(pairs):
    for index, pair in enumerate(pairs):
        for other in pairs[:index]:
            if pair.name == other.name:
                raise ConfigError(f'Duplicate pair name: {pair.name}')
            if overlaps(Path(pair.local).parts, Path(other.local).parts):
                raise ConfigError(f'Local roots overlap: {pair.name} and {other.name}')
            if overlaps(remote_parts(pair.remote), remote_parts(other.remote)):
                raise ConfigError(f'Dropbox roots overlap: {pair.name} and {other.name}')


def load_pairs(paths):
    paths.load()
    file = paths.config / 'pairs.toml'
    try:
        with file.open('rb') as stream:
            data = tomllib.load(stream)
    except FileNotFoundError:
        return []
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f'Invalid pairs.toml: {error}') from None
    if set(data) != {'pairs'} or not isinstance(data['pairs'], list):
        raise ConfigError('pairs.toml must contain a pairs array.')
    pairs = []
    for entry in data['pairs']:
        if not isinstance(entry, dict) or not {'name', 'local', 'remote'} <= set(entry) or set(entry) - {'name', 'local', 'remote', 'interval'}:
            raise ConfigError('Each pair needs name, local and remote; interval is optional.')
        if not isinstance(entry['local'], str) or not Path(entry['local']).is_absolute():
            raise ConfigError('Saved local paths must be absolute.')
        pair = make_pair(**entry)
        for directory in (paths.config, paths.state, paths.cache):
            if overlaps(Path(pair.local).parts, directory.resolve().parts):
                raise ConfigError('Local root must not overlap reicloud configuration, state or cache.')
        pairs.append(pair)
    validate_pairs(pairs)
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
        validate_pairs(pairs)
        lines = ['# Managed by reicloud; comments and formatting are regenerated.\n']
        if not pairs:
            lines.append('pairs = []\n')
        for pair in pairs:
            lines.append('\n[[pairs]]\n')
            for key, value in asdict(pair).items():
                lines.append(f'{key} = {json.dumps(value, ensure_ascii=False)}\n')
        filename = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=paths.config, delete=False) as stream:
                filename = stream.name
                stream.write(''.join(lines))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(filename, paths.config / 'pairs.toml')
        finally:
            if filename and os.path.exists(filename):
                os.unlink(filename)
