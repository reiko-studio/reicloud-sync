"""Shared read-only inspection of runner ownership and persisted schedules."""

from datetime import datetime
import fcntl
import hashlib
import json

from .config import ConfigError


def runner_lock_path(paths):
    key = hashlib.sha256(str(paths.config.resolve()).encode()).hexdigest()[:24]
    return paths.state / f'runner-{key}.lock'


def runner_active(paths):
    try:
        with runner_lock_path(paths).open('r') as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
    except FileNotFoundError:
        pass
    return False


def read_schedule(directory):
    try:
        data = json.loads((directory / 'schedule.json').read_text())
    except FileNotFoundError:
        return {}
    except (ValueError, OSError) as error:
        raise ConfigError(f'Cannot read schedule: {error}') from None
    if not isinstance(data, dict) or type(data.get('failures')) is not int or data['failures'] < 0:
        raise ConfigError('Invalid schedule.json; failures must be a nonnegative integer.')
    if data.get('next_run') is not None:
        try:
            parsed = datetime.fromisoformat(data['next_run'])
            if parsed.tzinfo is None:
                raise ValueError()
        except (TypeError, ValueError):
            raise ConfigError('Invalid next_run timestamp in schedule.json.') from None
    blocked = data.get('blocked_signature')
    if blocked is not None and (not isinstance(blocked, list) or len(blocked) != 4):
        raise ConfigError('Invalid blocked signature in schedule.json.')
    return data
