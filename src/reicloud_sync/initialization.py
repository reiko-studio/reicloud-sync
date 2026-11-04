"""Read-only initialization planning, followed by explicit root provisioning."""

from dataclasses import dataclass
import json
from pathlib import Path
import subprocess
import tempfile

from .config import ConfigError
from .exclusions import filter_text, selection


@dataclass
class InitPlan:
    local_exists: bool
    remote_exists: bool
    preference: str | None

    def describe(self, pair):
        return {'local': pair.local, 'remote': pair.remote,
                'create_local': not self.local_exists, 'create_remote': not self.remote_exists,
                'prefer': self.preference or 'no conflicting files detected'}


def remote_directory(paths, pair, run, *, missing_ok=False):
    entries = json.loads(run(paths, 'listremotes', '--json', capture=True).stdout)
    if not any(entry['name'] == 'dropbox' and entry['type'] == 'dropbox' for entry in entries):
        raise ConfigError("Dropbox is not configured. Run 'reicloud login dropbox'.")
    try:
        info = json.loads(run(paths, 'lsjson', pair.remote, '--stat', capture=True).stdout)
    except subprocess.CalledProcessError as error:
        if error.returncode not in (3, 4):
            raise
        info = None
    if info is None:
        if missing_ok:
            return False
        raise ConfigError(f'Remote directory is missing: {pair.remote}. Restore it in Dropbox before continuing; sync never recreates roots.')
    if not isinstance(info, dict) or not info.get('IsDir'):
        raise ConfigError(f'Remote root is not a directory: {pair.remote}')
    return True


def plan_init(paths, pair, run, preference):
    local = Path(pair.local)
    if local.exists() and not local.is_dir():
        raise ConfigError(f'Local root is not a directory: {local}')
    present = local.is_dir()
    remote = remote_directory(paths, pair, run, missing_ok=True)
    if present and remote and preference is None and any(local.iterdir()):
        with tempfile.TemporaryDirectory(prefix='reicloud-check-') as temporary:
            root = Path(temporary)
            rules = root / 'filters.txt'
            rules.write_text(filter_text(selection(pair)))
            report, errors = root / 'comparison.txt', root / 'errors.txt'
            try:
                run(paths, 'check', pair.local, pair.remote, '--combined', str(report),
                    '--error', str(errors), '--filter-from', str(rules),
                    '--log-file', str(root / 'check.log'), capture=True)
            except subprocess.CalledProcessError as error:
                lines = report.read_text().splitlines() if report.exists() else []
                valid_report = lines and all(line[:2] in ('= ', '+ ', '- ', '* ') for line in lines)
                if error.returncode != 1 or not valid_report or (errors.exists() and errors.read_text().strip()):
                    raise
            lines = report.read_text().splitlines() if report.exists() else []
            conflicts = [line[2:] for line in lines if line.startswith('* ')]
            if conflicts:
                raise ConfigError(f'{len(conflicts)} conflicting file(s), including {conflicts[0]}. Preview with --prefer local or --prefer remote explicitly.')
    return InitPlan(present, remote, preference)


def create_roots(paths, pair, plan, run, lock_fd):
    if not plan.local_exists:
        Path(pair.local).mkdir(parents=True, exist_ok=True)
    if not plan.remote_exists:
        run(paths, 'mkdir', pair.remote, lock_fd=lock_fd)


def preview_roots(pair, plan, temporary):
    local, remote = pair.local, pair.remote
    if not plan.local_exists:
        path = Path(temporary) / 'empty-local'
        path.mkdir()
        local = str(path)
    if not plan.remote_exists:
        path = Path(temporary) / 'empty-remote'
        path.mkdir()
        remote = str(path)
    return local, remote
