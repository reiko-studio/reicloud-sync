"""Configuration discovery without creating directories during inspection."""

from dataclasses import dataclass
import os
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
        try:
            with self.settings.open("rb") as stream:
                data = tomllib.load(stream)
        except FileNotFoundError:
            raise ConfigError("Setup is missing. Run 'reicloud setup'.") from None
        except tomllib.TOMLDecodeError as error:
            raise ConfigError(f"Invalid config.toml: {error}") from None
        if type(data.get("version")) is not int or data["version"] != 1:
            raise ConfigError("config.toml must contain version = 1.")
        section = data.get("dropbox")
        if not isinstance(section, dict) or section.get("remote") != "dropbox":
            raise ConfigError('config.toml must contain [dropbox] with remote = "dropbox".')
        return data
