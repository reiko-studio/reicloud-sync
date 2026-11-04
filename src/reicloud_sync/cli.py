"""Personal Dropbox synchronization powered by rclone."""

from dataclasses import asdict, replace
from pathlib import Path

import argparse
import json
import os
import re
import shutil
import subprocess
import sys

from . import __version__
from .config import ConfigError, Paths
from .sync import transfer, status, set_paused
from .runner import run_forever
from .processes import run_locked
from .logging import get_cli_logger
from .pairs import edit_pairs, find_pair, load_pairs, make_pair


def rclone(paths, *args, capture=False, lock_fd=None):
    executable = shutil.which("rclone")
    if not executable:
        raise ConfigError("rclone is missing. Install it and put it on PATH.")
    # Keep ambient rclone configuration from silently selecting another account.
    environment = {key: value for key, value in os.environ.items() if not key.startswith("RCLONE_")}
    command = [executable, "--config", str(paths.credentials), *args]
    if lock_fd is None:
        try:
            return subprocess.run(command, env=environment, check=True, text=True,
                                  stdout=subprocess.PIPE if capture else None,
                                  stderr=subprocess.PIPE, pass_fds=())
        except subprocess.CalledProcessError as error:
            if error.stderr:
                sys.stderr.write(error.stderr)
            raise
    return run_locked(command, environment, capture, lock_fd, bisync=args[0] == 'bisync')



def remotes(paths):
    return json.loads(rclone(paths, "listremotes", "--json", capture=True).stdout)


def check_dropbox(paths):
    entries = remotes(paths)
    if not any(entry["name"] == "dropbox" and entry["type"] == "dropbox" for entry in entries):
        raise ConfigError("Dropbox is not configured. Run 'reicloud login dropbox'.")
    # Discard directory names: this verifies access without exposing account contents.
    rclone(paths, "lsf", "dropbox:", "--max-depth", "1", capture=True)
    get_cli_logger().info("Dropbox access verified. No files were transferred.")


def delete_percent(value):
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError('Deletion percentage must be an integer from 0 to 100.') from None
    if not 0 <= number <= 100:
        raise argparse.ArgumentTypeError('Deletion percentage must be an integer from 0 to 100.')
    return number


def duration(value):
    match = re.fullmatch(r'(\d+)([smh]?)', value)
    if not match:
        raise argparse.ArgumentTypeError('Use a duration such as 30s, 5m or 1h.')
    seconds = int(match[1]) * {'': 1, 's': 1, 'm': 60, 'h': 3600}[match[2]]
    if not 1 <= seconds <= 604800:
        raise argparse.ArgumentTypeError('Interval must be between 1 second and 7 days.')
    return seconds


def positive_int(value):
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError('Expected a positive integer.') from None
    if number < 1:
        raise argparse.ArgumentTypeError('Expected a positive integer.')
    return number


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
    add = commands.add_parser("add", help="Register a folder pair without transferring files")
    add.add_argument("name")
    add.add_argument("local")
    add.add_argument("remote")
    add.add_argument("--interval", type=duration, default=300, help="Sync interval (default: 5m)")
    listing = commands.add_parser("list", help="List saved folder pairs")
    listing.add_argument("--json", action="store_true")
    show = commands.add_parser("show", help="Inspect a saved folder pair")
    show.add_argument("name")
    show.add_argument("--json", action="store_true")
    remove = commands.add_parser("remove", help="Unregister a pair; leave files untouched")
    remove.add_argument("name")
    init = commands.add_parser("init", help="Initialize two-way synchronization (use --dry-run to preview)")
    init.add_argument("name")
    init.add_argument("--dry-run", action="store_true")
    init.add_argument("--prefer", choices=["local", "remote"], required=True,
                      help="Which side would win when the same file differs during initialization")
    sync = commands.add_parser("sync", help="Synchronize an initialized pair")
    sync.add_argument("name")
    sync.add_argument("--dry-run", action="store_true")
    sync.add_argument("--max-delete", type=delete_percent, default=50, metavar="PERCENT",
                      help="Maximum deletion percentage on either side for this run (default: 50)")
    sync.add_argument("--retry", action="store_true",
                      help="Retry a previous failed sync using existing history; never resync")
    recover = commands.add_parser("recover", help="Explicitly reinitialize after a failed/interrupted operation")
    recover.add_argument("name")
    recover.add_argument("--prefer", choices=["local", "remote"], required=True)
    recover.add_argument("--dry-run", action="store_true")
    state = commands.add_parser("status", help="Show persistent state and last successful run")
    state.add_argument("name")
    state.add_argument("--json", action="store_true")
    run = commands.add_parser("run", help="Run periodic syncs in the foreground")
    run.add_argument("--once", action="store_true", help="Sync eligible pairs once and exit")
    run.add_argument("--jobs", type=positive_int, default=2, help="Concurrent pairs (default: 2)")
    interval = commands.add_parser("interval", help="Change a pair's sync interval")
    interval.add_argument("name")
    interval.add_argument("duration", type=duration)
    for action in ('pause', 'resume'):
        control = commands.add_parser(action, help=f'{action.capitalize()} future syncs for a pair')
        control.add_argument('name')
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
        if args.command in ('pause', 'resume'):
            set_paused(paths, args.name, args.command == 'pause')
            return 0
        if args.command == "run":
            return run_forever(paths, args.jobs, once=args.once)
        if args.command == "interval":
            with edit_pairs(paths) as pairs:
                index = next((i for i, pair in enumerate(pairs) if pair.name == args.name), None)
                if index is None:
                    raise ConfigError(f"Unknown pair: {args.name}")
                pairs[index] = replace(pairs[index], interval=args.duration)
            logger.info("Interval for %s: %s seconds", args.name, args.duration)
            return 0
        if args.command == "add":
            pair = make_pair(args.name, args.local, args.remote, interval=args.interval, require_directory=True)
            for directory in (paths.config, paths.state, paths.cache):
                if directory.resolve().is_relative_to(pair.local) or Path(pair.local).is_relative_to(directory.resolve()):
                    raise ConfigError("Local root must not overlap reicloud configuration, state or cache.")
            with edit_pairs(paths) as pairs:
                pairs.append(pair)
            logger.info("Registered pair %s. No files were transferred.", pair.name)
            return 0
        if args.command in ("list", "show"):
            pairs = [find_pair(paths, args.name)] if args.command == "show" else load_pairs(paths)
            data = [asdict(pair) for pair in pairs]
            if args.json:
                print(json.dumps(data[0] if args.command == "show" else data, indent=2))
            elif not pairs:
                print("No folder pairs configured.")
            else:
                for pair in pairs:
                    print(f"{pair.name}: {pair.local} <-> {pair.remote}")
            return 0
        if args.command == "remove":
            with edit_pairs(paths) as pairs:
                pair = next((pair for pair in pairs if pair.name == args.name), None)
                if pair is None:
                    raise ConfigError(f"Unknown pair: {args.name}")
                pairs.remove(pair)
            logger.info("Unregistered %s. Local and remote files were left untouched.", args.name)
            return 0
        if args.command in ("init", "sync", "recover"):
            paths.load()
            transfer(paths, args.name, args.command, getattr(args, 'prefer', None), args.dry_run, rclone,
                     retry=getattr(args, 'retry', False), max_delete=getattr(args, 'max_delete', 50))
            return 0
        if args.command == "status":
            data = status(paths, args.name)
            print(json.dumps(data, indent=2) if args.json else
                  f"{data['name']}: {data['phase']}\nActive: {data['active']}\nLast success: {data.get('last_success') or 'never'}\nError: {data.get('error') or 'none'}")
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
            load_pairs(paths)
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
