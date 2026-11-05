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
from .service import install as install_service, control as control_service, installed_python
from .logging import get_cli_logger
from .notifications import enabled as notifications_enabled, configure as configure_notifications, send as send_notification
from .activity import read_entries, follow, current_recorder, sanitize
from .sync import state_dir
from .exclusions import change as change_exclusion
from .browse import listing as browse_listing
from .cleanup import uninstall
from .setup_ui import wizard as setup_wizard, terminal_available
from .workspace import migrate, rollback_command as rollback_migration, computer_name, external_remote
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
                recorder = current_recorder()
                if recorder:
                    for line in error.stderr.splitlines():
                        recorder.record('rclone', message=sanitize(line))
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
    setup.add_argument("--workspace", help="Canonical local Dropbox folder (default for new setup: ~/Dropbox)")
    setup.add_argument("--computer", help="Computer label; a machine-ID suffix is appended")
    mode = setup.add_mutually_exclusive_group()
    mode.add_argument('--interactive', action='store_true', help='Open the setup wizard')
    mode.add_argument('--non-interactive', action='store_true', help='Use flags/defaults without prompts')
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
    add.add_argument("remote", nargs="?", help="Legacy destination; workspace mappings derive their destination")
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
    init.add_argument("--prefer", choices=["local", "remote"],
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
    state.add_argument("name", nargs="?", help="Omit to show all pairs")
    state.add_argument("--json", action="store_true")
    run = commands.add_parser("run", help="Run periodic syncs in the foreground")
    run.add_argument("--watch", action="store_true", help="Trigger earlier syncs on local changes")
    run.add_argument("--once", action="store_true", help="Sync eligible pairs once and exit")
    run.add_argument("--jobs", type=positive_int, default=2, help="Concurrent pairs (default: 2)")
    interval = commands.add_parser("interval", help="Change a pair's sync interval")
    interval.add_argument("name")
    interval.add_argument("duration", type=duration)
    for action in ('pause', 'resume'):
        control = commands.add_parser(action, help=f'{action.capitalize()} future syncs for a pair')
        control.add_argument('name')
    service = commands.add_parser('service', help='Install or control the systemd user service')
    actions = service.add_subparsers(dest='service_action', required=True)
    installation = actions.add_parser('install', help='Write and enable the user unit without starting it')
    installation.add_argument('--dry-run', action='store_true', help='Print unit without writing or enabling it')
    installation.add_argument('--jobs', type=positive_int, default=2)
    installation.add_argument('--watch', action='store_true')
    for action in ('start', 'stop', 'status', 'path', 'enable', 'disable'):
        actions.add_parser(action)
    for action in ('exclude', 'include'):
        exclusions = commands.add_parser(action, help='Stage an exclusion rule change')
        exclusions.add_argument('name')
        exclusions.add_argument('pattern')
    exclusions = commands.add_parser('exclusions', help='List or deliberately apply staged exclusion changes')
    exclusions.add_argument('name')
    exclusions.add_argument('--json', action='store_true')
    exclusions.add_argument('--apply', action='store_true')
    exclusions.add_argument('--prefer', choices=['local', 'remote'])
    exclusions.add_argument('--dry-run', action='store_true')
    logs = commands.add_parser('logs', help='Read a pair activity log')
    logs.add_argument('name')
    logs.add_argument('--lines', type=positive_int, default=50)
    logs.add_argument('--follow', action='store_true')
    logs.add_argument('--json', action='store_true', help='Output JSON lines')
    notifications = commands.add_parser('notifications', help='Configure optional desktop alerts')
    notifications.add_argument('action', choices=['on', 'off', 'status', 'test'])
    migration = commands.add_parser('migrate', help='Preview or apply explicit legacy workspace migration')
    migration.add_argument('--workspace')
    migration.add_argument('--computer')
    migration.add_argument('--retire-legacy-pairs', action='store_true')
    migration.add_argument('--dry-run', action='store_true')
    migration.add_argument('--rollback', action='store_true')
    browse = commands.add_parser('browse', help='Inspect Dropbox without changing its contents')
    browse.add_argument('path', nargs='?', default='')
    browse.add_argument('--computer')
    browse.add_argument('--computers', action='store_true')
    browse.add_argument('--json', action='store_true')
    cleanup = commands.add_parser('uninstall', help='Remove the service; optionally wipe local setup')
    cleanup.add_argument('--wipe', action='store_true')
    cleanup.add_argument('--yes', action='store_true')
    cleanup.add_argument('--dry-run', action='store_true')
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
        if args.command == 'uninstall':
            print(json.dumps(uninstall(paths, wipe=args.wipe, yes=args.yes, dry_run=args.dry_run), indent=2))
            return 0
        if args.command == 'browse':
            result = browse_listing(paths, args.path, rclone, computer=args.computer, computers=args.computers)
            if args.json:
                print(json.dumps(result, indent=2))
            else:
                print(result['remote'])
                for row in result['entries']:
                    print(f"{row['name']}{'/' if row['directory'] else ''}  [{row['scope']}; {row['selection']}]"
                          + (f"  mapping={row['mapping']}" if row['mapping'] else ''))
            return 0
        if args.command == 'migrate':
            if args.rollback:
                rollback_migration(paths)
                logger.info('Interrupted migration rolled back.')
            else:
                result = migrate(paths, args.workspace or '~/Dropbox', args.computer,
                                 retire_legacy=args.retire_legacy_pairs, dry_run=args.dry_run)
                print(json.dumps(result, indent=2))
            return 0
        if args.command == 'notifications':
            paths.load()
            if args.action in ('on', 'off'):
                configure_notifications(paths, args.action == 'on')
                logger.info('Desktop notifications %s.', args.action)
            elif args.action == 'test':
                send_notification('Reicloud notification test', 'Desktop notifications are working.')
            else:
                print('enabled' if notifications_enabled(paths) else 'disabled')
            return 0
        if args.command == 'logs':
            pair = find_pair(paths, args.name)
            directory = state_dir(paths, pair)
            entries = follow(directory, args.lines) if args.follow else read_entries(directory, args.lines)
            for entry in entries:
                print(json.dumps(entry) if args.json else
                      f"{entry['time']} {entry['event']}: {entry.get('message') or entry.get('operation', '')}", flush=True)
            return 0
        if args.command in ('exclude', 'include'):
            change_exclusion(paths, args.name, args.pattern, remove=args.command == 'include')
            logger.info('Exclusion change saved for %s. Existing history requires explicit application.', args.name)
            return 0
        if args.command == 'exclusions':
            if args.apply:
                if not args.prefer:
                    raise ConfigError('--apply requires --prefer local or --prefer remote. Preview with --dry-run first.')
                transfer(paths, args.name, 'filters', args.prefer, args.dry_run, rclone)
            else:
                if args.dry_run or args.prefer:
                    raise ConfigError('--dry-run and --prefer require --apply.')
                data = status(paths, args.name)
                result = {'managed': data['managed_exclusions'], 'exclusions': data['exclusions'], 'pending': data['filters_pending']}
                print(json.dumps(result, indent=2) if args.json else
                      '\n'.join(['Managed: ' + rule for rule in result['managed']] + result['exclusions']) + f"\nPending application: {result['pending']}")
            return 0
        if args.command == 'service':
            if args.service_action == 'install':
                install_service(paths, args.jobs, dry_run=args.dry_run, watching=args.watch)
            else:
                control_service(paths, args.service_action)
            return 0
        if args.command in ('pause', 'resume'):
            set_paused(paths, args.name, args.command == 'pause')
            return 0
        if args.command == "run":
            return run_forever(paths, args.jobs, once=args.once, watching=args.watch)
        if args.command == "interval":
            with edit_pairs(paths) as pairs:
                index = next((i for i, pair in enumerate(pairs) if pair.name == args.name), None)
                if index is None:
                    raise ConfigError(f"Unknown pair: {args.name}")
                pairs[index] = replace(pairs[index], interval=args.duration)
            logger.info("Interval for %s: %s seconds", args.name, args.duration)
            return 0
        if args.command == "add":
            config = paths.load()
            if config['version'] == 2:
                if args.name == 'workspace':
                    raise ConfigError('workspace is reserved for the canonical Dropbox folder.')
                remote = external_remote(config, args.name)
                if args.remote is not None and args.remote != remote:
                    raise ConfigError(f'External mappings must target {remote}; shared content belongs inside the workspace.')
                pair = make_pair(args.name, args.local, remote, interval=args.interval, kind='external')
            else:
                if args.remote is None:
                    raise ConfigError('Legacy setup requires a remote; migrate to use automatic destinations.')
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
            names = [args.name] if args.name else [pair.name for pair in load_pairs(paths)]
            rows = []
            for name in names:
                try:
                    rows.append(status(paths, name))
                except (ConfigError, OSError, ValueError) as error:
                    if args.name:
                        raise
                    rows.append({'name': name, 'phase': 'invalid', 'error': str(error)})
            if args.json:
                print(json.dumps(rows[0] if args.name else rows, indent=2))
            elif not rows:
                print("No folder pairs configured.")
            else:
                for data in rows:
                    print(f"{data['name']}: {data['phase']}"
                          f"\nPaused: {data.get('paused', False)}"
                          f"\nExclusion changes pending: {data.get('filters_pending', False)}"
                          f"\nActive: {data.get('active', False)}"
                          f"\nRunner: {'running' if data.get('runner_active') else 'stopped'}"
                          f"\nInterval: {data.get('interval_seconds', 'unknown')} seconds"
                          f"\nLast success: {data.get('last_success') or 'never'}"
                          f"\nNext run: {data.get('next_run') or 'not scheduled'}"
                          f"\nConsecutive failures: {data.get('consecutive_failures', 0)}"
                          f"\nError: {data.get('error') or data.get('scheduler_error') or 'none'}"
                          f"\nLast error: {data.get('last_error') or 'none'}")
            return 0
        if args.command == "setup":
            bare_setup = not (args.workspace or args.computer or args.login)
            use_wizard = args.interactive or (not args.non_interactive and bare_setup and terminal_available())
            if use_wizard:
                if not terminal_available():
                    raise ConfigError('Interactive setup requires a terminal. Use --non-interactive with flags instead.')
                answers = setup_wizard(paths, workspace=args.workspace, computer=args.computer, login=args.login)
                args.workspace, args.computer, args.login = answers.workspace, answers.computer, answers.login
            fresh = not paths.settings.exists()
            paths.initialize()
            config = paths.load()
            if fresh or (config['version'] == 1 and (args.workspace or not load_pairs(paths))):
                migrate(paths, args.workspace or '~/Dropbox', args.computer)
            elif config['version'] == 2 and (args.workspace or args.computer):
                workspace = find_pair(paths, 'workspace')
                if ((args.workspace and str(Path(args.workspace).expanduser().resolve()) != workspace.local)
                        or (args.computer and computer_name(args.computer) != config['computer']['name'])):
                    raise ConfigError('Setup identity/root is already saved; it cannot be changed implicitly.')
            logger.info("Configuration ready: %s", paths.settings)
            current = paths.load()
            if current['version'] == 2:
                logger.info('Computer: %s; workspace: %s', current['computer']['name'], find_pair(paths, 'workspace').local)
            else:
                logger.info('Legacy pairs retained. Preview reicloud migrate --dry-run before switching workflows.')
            if not args.login:
                logger.info("Next: reicloud login dropbox")
                if current['version'] == 2:
                    logger.info('After login: reicloud init workspace --dry-run')
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
            if args.command == 'setup' and paths.load()['version'] == 2:
                logger.info('Next: reicloud init workspace --dry-run')
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
            if paths.settings.exists() or (paths.config / 'migration.json').exists():
                load_pairs(paths)
            print(f"Reicloud: {__version__}\nPython: {sys.executable}")
            service_python = installed_python(paths)
            if service_python:
                suffix = ' (missing)' if not service_python.is_file() else ''
                print(f"Service Python: {service_python}{suffix}")
                if service_python != Path(sys.executable).absolute():
                    print('Reinstall the service from this installation to switch interpreters.')
            else:
                print('Service Python: no managed unit found')
            version = rclone(paths, "version", capture=True).stdout.splitlines()[0]
            print(f"Configuration: {paths.settings}\nEngine: {version}")
            print("Credentials: present" if paths.credentials.is_file() else "Credentials: absent (run reicloud login dropbox)")
        return 0
    except (ConfigError, OSError, ValueError) as error:
        logger.error("%s", error)
        return 1
    except subprocess.CalledProcessError as error:
        program = Path(error.cmd[0]).name if isinstance(error.cmd, (list, tuple)) else 'Command'
        logger.error("%s failed (exit %s). Check the diagnostic above; the operation did not complete.", program, error.returncode)
        return 1
    except KeyboardInterrupt:
        logger.warning("Cancelled.")
        return 130
