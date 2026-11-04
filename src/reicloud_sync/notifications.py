"""Optional desktop notifications for background sync incidents."""

import hashlib
import json
import shutil
import subprocess

from .config import ConfigError
from .logging import get_cli_logger
from .sync import state_dir, write_state

_seen = set()
_warned = set()


def enabled(paths):
    try:
        data = json.loads((paths.config / 'notifications.json').read_text())
    except FileNotFoundError:
        return False
    except ValueError:
        raise ConfigError('Invalid notifications.json.') from None
    if not isinstance(data, dict) or type(data.get('enabled')) is not bool:
        raise ConfigError('notifications.json must contain a boolean enabled field.')
    return data['enabled']


def configure(paths, value):
    paths.load()
    write_state(paths.config, {'enabled': value}, filename='notifications.json')


def send(title, body, *, urgent=False):
    executable = shutil.which('notify-send')
    if not executable:
        raise ConfigError('notify-send is missing. Install your desktop libnotify tools first.')
    subprocess.run([executable, '--app-name=reicloud-sync',
                    '--urgency=critical' if urgent else '--urgency=normal', '--', title, body],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=5)


def read_delivery(directory):
    try:
        data = json.loads((directory / 'notification.json').read_text())
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, ValueError):
        return {}


def attention(paths, pair, state):
    directory = state_dir(paths, pair)
    try:
        if not enabled(paths):
            return
        signature = hashlib.sha256(json.dumps([state.get('failure_kind'), state.get('error')]).encode()).hexdigest()
        key = (str(directory), signature)
        if key in _seen:
            return
        _seen.add(key)
        previous = read_delivery(directory)
        if previous.get('kind') == 'attention' and previous.get('signature') == signature:
            return
        delivered = False
        try:
            send(f'Reicloud: {pair.name} needs attention', f'Run reicloud status {pair.name} for details.', urgent=True)
            delivered = True
        except (ConfigError, OSError, subprocess.SubprocessError) as error:
            get_cli_logger().warning('Desktop notification unavailable: %s', error)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        write_state(directory, {'kind': 'attention', 'signature': signature, 'sent': delivered}, filename='notification.json')
    except (ConfigError, OSError, ValueError) as error:
        key = (str(directory), str(error))
        if key not in _warned:
            _warned.add(key)
            get_cli_logger().warning('Desktop notifications skipped: %s', error)


def recovered(paths, pair):
    directory = state_dir(paths, pair)
    try:
        previous = read_delivery(directory)
        if previous.get('kind') != 'attention':
            return
        if previous.get('sent') and enabled(paths):
            try:
                send(f'Reicloud: {pair.name} is syncing again', 'The latest background sync completed successfully.')
            except (ConfigError, OSError, subprocess.SubprocessError) as error:
                get_cli_logger().warning('Desktop recovery notification unavailable: %s', error)
        write_state(directory, {'kind': 'clear'}, filename='notification.json')
        _seen.difference_update({key for key in _seen if key[0] == str(directory)})
    except (ConfigError, OSError, ValueError):
        pass
