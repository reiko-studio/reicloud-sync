"""Configuration discovery without creating directories during inspection."""

from dataclasses import dataclass
import os
import re
import tempfile
from pathlib import Path
import tomllib


class ConfigError(ValueError):
    pass


def absolute_path(value: str) -> Path:
    return Path(value).expanduser().absolute()


def xdg(name: str, fallback: str) -> Path:
    value = os.environ.get(name, "")
    return Path(value) if value and Path(value).is_absolute() else Path.home() / fallback


@dataclass(frozen=True)
class Paths:
    config: Path
    state: Path
    cache: Path

    @classmethod
    def discover(cls, override: str | None = None):
        selected = override or os.environ.get("REICLOUD_CONFIG_DIR")
        return cls(
            absolute_path(selected) if selected else xdg("XDG_CONFIG_HOME", ".config") / "reicloud-sync",
            xdg("XDG_STATE_HOME", ".local/state") / "reicloud-sync",
            xdg("XDG_CACHE_HOME", ".cache") / "reicloud-sync",
        )

    @property
    def settings(self):
        return self.config / "config.toml"

    @property
    def credentials(self):
        return self.config / "rclone.conf"

    def initialize(self):
        for directory in (self.config, self.state, self.cache):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            with self.settings.open("x", encoding="utf-8") as stream:
                stream.write('version = 1\n\n[dropbox]\nremote = "dropbox"\n')
        except FileExistsError:
            pass
        self.load()

    def load(self):
        if (self.config / "migration.json").exists():
            raise ConfigError("Incomplete migration. Run reicloud migrate --rollback.")
        try:
            with self.settings.open("rb") as stream:
                data = tomllib.load(stream)
        except FileNotFoundError:
            raise ConfigError("Setup is missing. Run 'reicloud setup'.") from None
        except tomllib.TOMLDecodeError as error:
            raise ConfigError(f"Invalid config.toml: {error}") from None
        if type(data.get("version")) is not int or data["version"] not in (1, 2, 3):
            raise ConfigError("config.toml must contain version = 1, 2 or 3.")
        if data['version'] < 3 and 'remotes' in data:
            raise ConfigError('Named remotes require config version 3.')
        section = data.get("dropbox")
        if data["version"] < 3 and (not isinstance(section, dict) or section.get("remote") != "dropbox"):
            raise ConfigError('config.toml must contain [dropbox] with remote = "dropbox".')
        if data['version'] >= 2:
            computer = data.get('computer')
            if not isinstance(computer, dict) or not isinstance(computer.get('name'), str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,55}-[0-9a-f]{6}', computer['name']):
                raise ConfigError('Workspace configuration requires a stable computer name with a six-character machine ID suffix.')
        if data['version'] == 3:
            from .remotes import REMOTE_NAME
            remotes = data.get('remotes')
            if section is not None or not isinstance(remotes, dict) or not remotes:
                raise ConfigError('Version 3 requires named remotes without a legacy [dropbox] section.')
            for name, settings in remotes.items():
                if (not REMOTE_NAME.fullmatch(name) or not isinstance(settings, dict)
                        or set(settings) - {'workspace', 'remote_watch'}
                        or not isinstance(settings.get('workspace'), str) or not settings['workspace'].strip()
                        or type(settings.get('remote_watch', False)) is not bool):
                    raise ConfigError(f'Invalid remote definition: {name}')
        return data


def atomic_text(file, text):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=file.parent, delete=False) as stream:
            temporary = stream.name
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, file)
        descriptor = os.open(file.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)
