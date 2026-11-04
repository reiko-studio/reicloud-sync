"""Setup and authentication commands; transfers are not implemented yet."""

import argparse
import json
import os
import shutil
import subprocess

from . import __version__
from .config import ConfigError, Paths
from .logging import get_cli_logger


def rclone(paths, *args, capture=False):
    executable = shutil.which("rclone")
    if not executable:
        raise ConfigError("rclone is missing. Install it and put it on PATH.")
    # Keep ambient rclone configuration from silently selecting another account.
    environment = {key: value for key, value in os.environ.items() if not key.startswith("RCLONE_")}
    return subprocess.run(
        [executable, "--config", str(paths.credentials), *args],
        env=environment, check=True, text=True,
        stdout=subprocess.PIPE if capture else None,
    )


def remotes(paths):
    return json.loads(rclone(paths, "listremotes", "--json", capture=True).stdout)


def check_dropbox(paths):
    entries = remotes(paths)
    if not any(entry["name"] == "dropbox" and entry["type"] == "dropbox" for entry in entries):
        raise ConfigError("Dropbox is not configured. Run 'reicloud login dropbox'.")
    # Discard directory names: this verifies access without exposing account contents.
    rclone(paths, "lsf", "dropbox:", "--max-depth", "1", capture=True)
    get_cli_logger().info("Dropbox access verified. No files were transferred.")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="reicloud", description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--config-dir", help="Override REICLOUD_CONFIG_DIR and the XDG config directory")
    commands = parser.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("setup", help="Create directories and initial configuration")
    setup.add_argument("--login", action="store_true", help="Also start Dropbox login")
    config = commands.add_parser("config", help="Inspect configuration without displaying credentials")
    config.add_argument("action", choices=["paths", "check"])
    config.add_argument("--json", action="store_true")
    commands.add_parser("doctor", help="Check configuration and rclone availability")
    login = commands.add_parser("login", help="Connect Dropbox through rclone's browser OAuth")
    login.add_argument("provider", choices=["dropbox"])
    check = commands.add_parser("check", help="Verify read-only remote access")
    check.add_argument("provider", choices=["dropbox"])
    args = parser.parse_args(argv)
    logger = get_cli_logger()
    paths = Paths.discover(args.config_dir)
    try:
        if args.command == "config" and args.action == "paths":
            data = {"config_dir": str(paths.config), "config_file": str(paths.settings),
                    "rclone_config": str(paths.credentials), "state_dir": str(paths.state),
                    "cache_dir": str(paths.cache)}
            print(json.dumps(data, indent=2) if args.json else "\n".join(f"{key}: {value}" for key, value in data.items()))
            return 0
        if args.command == "setup":
            paths.initialize()
            logger.info("Configuration ready: %s", paths.settings)
            if not args.login:
                logger.info("Next: reicloud login dropbox")
                return 0
        if args.command in ("setup", "login"):
            paths.load()
            if any(entry["name"] == "dropbox" for entry in remotes(paths)):
                logger.info("Dropbox remote already exists; checking access.")
            else:
                logger.info("Opening Dropbox login through rclone. Complete authorization in your browser.")
                try:
                    rclone(paths, "config", "create", "dropbox", "dropbox", "--no-output")
                finally:
                    if paths.credentials.exists():
                        paths.credentials.chmod(0o600)
            check_dropbox(paths)
        elif args.command == "check":
            paths.load()
            check_dropbox(paths)
        elif args.command == "config":
            paths.load()
            if args.json:
                print(json.dumps({"valid": True}))
            else:
                logger.info("Configuration is valid.")
        elif args.command == "doctor":
            paths.load()
            version = rclone(paths, "version", capture=True).stdout.splitlines()[0]
            print(f"Configuration: {paths.settings}\nEngine: {version}")
            print("Credentials: present" if paths.credentials.is_file() else "Credentials: absent (run reicloud login dropbox)")
        return 0
    except (ConfigError, OSError, ValueError) as error:
        logger.error("%s", error)
        return 1
    except subprocess.CalledProcessError as error:
        logger.error("rclone failed (exit %s). Check the diagnostic above; the operation did not complete.", error.returncode)
        return 1
    except KeyboardInterrupt:
        logger.warning("Cancelled.")
        return 130
