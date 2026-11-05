"""Small terminal wizard for configuration and login, never file transfers."""

from dataclasses import dataclass
from pathlib import Path
import socket
import sys

import questionary

from .config import ConfigError
from .pairs import load_pairs
from .workspace import computer_name


@dataclass(frozen=True)
class SetupAnswers:
    workspace: str | None
    computer: str | None
    login: bool


def terminal_available():
    return sys.stdin.isatty() and sys.stdout.isatty() and sys.stderr.isatty()


def workspace_error(paths, value):
    if not value.strip() or any(ord(character) < 32 for character in value):
        return 'Enter a directory path.'
    try:
        directory = Path(value).expanduser().resolve()
        if directory.exists() and not directory.is_dir():
            return 'Choose a directory, not a file.'
        for protected in (paths.config, paths.state, paths.cache):
            protected = protected.resolve()
            if directory.is_relative_to(protected) or protected.is_relative_to(directory):
                return 'Keep Dropbox separate from application configuration, state and cache.'
    except (OSError, ValueError) as error:
        return str(error)
    return True


def computer_error(value):
    try:
        computer_name(value)
    except (ConfigError, ValueError) as error:
        return str(error)
    return True


def ask(question):
    try:
        result = question.unsafe_ask()
    except EOFError:
        raise KeyboardInterrupt() from None
    if result is None:
        raise KeyboardInterrupt()
    return result


def wizard(paths, *, workspace=None, computer=None, login=False):
    questionary.print('Reicloud setup', style='bold')
    questionary.print('Choose where shared Dropbox files live. Setup does not transfer files.')
    config = paths.load() if paths.settings.exists() else None
    pairs = load_pairs(paths) if config else []
    existing = next((pair for pair in pairs if pair.kind == 'workspace'), None)
    legacy = bool(config and config['version'] == 1 and pairs)
    if existing:
        questionary.print(f'Workspace: {existing.local}\nComputer: {config["computer"]["name"]}')
        questionary.print('Existing workspace and computer identity will be preserved.')
    elif legacy:
        questionary.print('Existing legacy pairs will be preserved. Use migrate --dry-run to plan a workspace migration.')
    else:
        if workspace is None:
            workspace = ask(questionary.path('Local Dropbox folder:', default='~/Dropbox',
                                            only_directories=True,
                                            validate=lambda value: workspace_error(paths, value)))
        valid = workspace_error(paths, workspace)
        if valid is not True:
            raise ConfigError(valid)
        if computer is None:
            computer = ask(questionary.text('Computer label:', default=socket.gethostname(), validate=computer_error))
        identity = computer_name(computer)
        questionary.print(f'Workspace: {Path(workspace).expanduser().resolve()}\nComputer: {identity}')
        questionary.print('External folders will use .reicloud-computers/' + identity + '/NAME.')
    if not login:
        login = ask(questionary.confirm('Connect/check Dropbox now?', default=not paths.credentials.exists()))
    questionary.print('Dropbox directories are created later by init. No service will be installed or started.')
    if not ask(questionary.confirm('Save setup' + (' and open Dropbox login?' if login else '?'), default=True)):
        raise KeyboardInterrupt()
    return SetupAnswers(workspace, computer, login)
