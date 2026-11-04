"""Rclone exclusion rules and deliberate activation of changed file selections."""

from dataclasses import replace
import hashlib

from .config import ConfigError
from .pairs import edit_pairs

ACCESS_MARKER = '.reicloud-sync-access'


def validate_patterns(patterns):
    if not isinstance(patterns, (list, tuple)):
        raise ConfigError('Exclusions must be a list of rclone glob patterns.')
    for pattern in patterns:
        if (not isinstance(pattern, str) or not pattern.strip() or pattern != pattern.strip()
                or any(ord(character) < 32 for character in pattern)):
            raise ConfigError('Each exclusion must be a nonempty single-line pattern without outer whitespace.')
    return tuple(dict.fromkeys(patterns))


def filter_text(patterns):
    return f'+ /{ACCESS_MARKER}\n' + ''.join(f'- {pattern}\n' for pattern in patterns)


def fingerprint(patterns):
    return hashlib.sha256(filter_text(patterns).encode()).hexdigest()


def managed_patterns(pair):
    return ('/.reicloud-computers/**',) if pair.kind == 'workspace' else ()


def selection(pair):
    return managed_patterns(pair) + pair.exclusions


def pending(pair, state):
    return (state['phase'] != 'uninitialized'
            and state.get('filters_hash', fingerprint(())) != fingerprint(selection(pair)))


def filter_arguments(workdir, pair, state, operation):
    # Old unfiltered histories can keep syncing until the user changes selection.
    if not selection(pair) and 'filters_hash' not in state and operation == 'sync':
        return []
    file = workdir / 'filters.txt'
    file.write_text(filter_text(selection(pair)), encoding='utf-8')
    return ['--filters-file', str(file)]


def change(paths, name, pattern, *, remove=False):
    validate_patterns([pattern])
    with edit_pairs(paths) as pairs:
        index = next((i for i, pair in enumerate(pairs) if pair.name == name), None)
        if index is None:
            raise ConfigError(f'Unknown pair: {name}')
        patterns = list(pairs[index].exclusions)
        if remove:
            if pattern not in patterns:
                raise ConfigError(f'Exclusion not found: {pattern}')
            patterns.remove(pattern)
        elif pattern not in patterns:
            patterns.append(pattern)
        pairs[index] = replace(pairs[index], exclusions=tuple(patterns))
