"""Named rclone connections and supported backend capabilities."""

import json
import re

from .config import ConfigError

REMOTE_NAME = re.compile(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,53}')


def select_remote(config, name=None):
    remotes = config.get('remotes', {'dropbox': {}})
    if name is None:
        if len(remotes) != 1:
            raise ConfigError('Select a registered remote with --remote NAME.')
        return next(iter(remotes))
    if name not in remotes:
        raise ConfigError(f'Unknown remote: {name}')
    return name


def backend(paths, name, run):
    entries = json.loads(run(paths, 'listremotes', '--json', capture=True).stdout)
    return next((entry['type'] for entry in entries if entry['name'] == name), None)


def require_dropbox(paths, name, run):
    kind = backend(paths, name, run)
    if kind is None:
        raise ConfigError(f"Remote {name} is not connected. Run 'reicloud login {name}'.")
    if kind != 'dropbox':
        raise ConfigError(f'Remote {name} uses unsupported backend {kind}. Only Dropbox is supported.')
