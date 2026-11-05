"""Read-only remote listings annotated with workspace ownership."""

import json
import re
import subprocess

from .config import ConfigError
from .remotes import select_remote, require_dropbox
from .pairs import load_pairs
from .workspace import NAMESPACE


def relative_path(value):
    if '\\' in value or any(ord(character) < 32 for character in value):
        raise ConfigError('Unsupported remote path.')
    parts = [part for part in value.split('/') if part and part != '.']
    if '..' in parts:
        raise ConfigError('Browse path cannot contain .. components.')
    return '/'.join(parts)


def listing(paths, path, run, *, computer=None, computers=False, connection=None):
    config = paths.load()
    pairs = load_pairs(paths)
    if ':' in path:
        prefix, _, path = path.partition(':')
        if connection is not None and prefix != connection:
            raise ConfigError('Browse path and --remote select different connections.')
        connection = prefix
    connection = select_remote(config, connection)
    if config['version'] == 3:
        require_dropbox(paths, connection, run)
    pairs = [pair for pair in pairs if pair.remote.startswith(connection + ':')]
    path = relative_path(path or '')
    if computers and (computer or path):
        raise ConfigError('--computers cannot be combined with a path or --computer.')
    if computers:
        path = NAMESPACE
    if computer:
        if not re.fullmatch(r'[a-zA-Z0-9_-]+', computer):
            raise ConfigError('Invalid computer name.')
        candidates = json.loads(run(paths, 'lsjson', f'{connection}:{NAMESPACE}', '--dirs-only', '--max-depth', '1', capture=True).stdout)
        matching = [entry['Name'] for entry in candidates if entry['Name'] == computer or entry['Name'].startswith(computer + '-')]
        if len(matching) != 1:
            raise ConfigError('Computer name is missing or ambiguous; use browse --computers to select its full name.')
        path = '/'.join(part for part in (NAMESPACE, matching[0], path) if part)
    remote = connection + ':' + path
    try:
        entries = json.loads(run(paths, 'lsjson', remote, '--max-depth', '1', capture=True).stdout)
    except subprocess.CalledProcessError as error:
        if path != NAMESPACE or error.returncode not in (3, 4):
            raise
        entries = []
    current = config.get('computer', {}).get('name')
    workspace = next((pair for pair in pairs if pair.kind == 'workspace'), None)
    rows = []
    for entry in entries:
        full = '/'.join(part for part in (path, entry['Name']) if part)
        parts = full.split('/')
        matched = next((pair for pair in pairs if pair.kind != 'workspace' and
                        (full.casefold() == pair.remote.partition(':')[2].casefold() or full.casefold().startswith(pair.remote.partition(':')[2].casefold().rstrip('/') + '/'))), None)
        if parts[0].casefold() == NAMESPACE:
            scope = 'computer-namespaces' if len(parts) == 1 else ('this-computer' if parts[1] == current else 'other-computer')
            selection = 'excluded-from-workspace'
        elif workspace:
            scope = 'shared-workspace'
            selection = 'subject-to-user-exclusions' if workspace.exclusions else 'included'
        else:
            scope, selection = 'legacy' if matched else 'unconfigured', 'not-evaluated'
        rows.append({'name': entry['Name'], 'directory': entry.get('IsDir', False),
                     'size': entry.get('Size'), 'scope': scope, 'selection': selection,
                     'mapping': matched.name if matched else None})
    return {'remote': remote, 'entries': rows,
            'workspace_exclusions': list(workspace.exclusions) if workspace else []}
