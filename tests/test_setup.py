"""Exercise config boundaries and authentication delegation without cloud access."""

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from reicloud_sync.cli import main, rclone
from reicloud_sync.config import Paths


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.environment = patch.dict(os.environ, {
            'XDG_CONFIG_HOME': str(self.root / 'config'),
            'XDG_STATE_HOME': str(self.root / 'state'),
            'XDG_CACHE_HOME': str(self.root / 'cache'),
            'REICLOUD_CONFIG_DIR': '',
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def invoke(self, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            code = main(list(args))
        return code, output.getvalue()

    def test_paths_are_read_only_and_overrides_have_precedence(self):
        with patch.dict(os.environ, {'REICLOUD_CONFIG_DIR': str(self.root / 'override')}):
            code, output = self.invoke('--config-dir', str(self.root / 'cli'), 'config', 'paths', '--json')
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)['config_dir'], str(self.root / 'cli'))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_logging_goes_to_stderr_and_json_stays_clean(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            self.assertEqual(main(['setup']), 0)
        self.assertEqual(stdout.getvalue(), '')
        self.assertIn('INFO', stderr.getvalue())
        self.assertIn('Configuration ready', stderr.getvalue())
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            self.assertEqual(main(['config', 'check', '--json']), 0)
        self.assertEqual(json.loads(stdout.getvalue()), {'valid': True})
        self.assertEqual(stderr.getvalue(), '')

    def test_setup_is_idempotent_and_private(self):
        self.assertEqual(self.invoke('setup')[0], 0)
        paths = Paths.discover()
        paths.settings.write_text(paths.settings.read_text() + '\n# keep my edits\n')
        self.assertEqual(self.invoke('setup')[0], 0)
        self.assertIn('# keep my edits', paths.settings.read_text())
        self.assertEqual(paths.config.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.invoke('config', 'check')[0], 0)

    def test_bad_config_is_not_overwritten(self):
        paths = Paths.discover()
        paths.initialize()
        paths.settings.write_text('version = 99\n')
        self.assertEqual(self.invoke('setup')[0], 1)
        self.assertEqual(paths.settings.read_text(), 'version = 99\n')

    def test_missing_setup_has_actionable_error(self):
        code, output = self.invoke('login', 'dropbox')
        self.assertEqual(code, 1)
        self.assertIn('reicloud setup', output)

    def test_login_suppresses_credentials_and_checks_access(self):
        Paths.discover().initialize()
        replies = [subprocess.CompletedProcess([], 0, '[]'),
                   subprocess.CompletedProcess([], 0),
                   subprocess.CompletedProcess([], 0, '[{"name":"dropbox","type":"dropbox"}]'),
                   subprocess.CompletedProcess([], 0, 'private-folder\n')]
        with patch('reicloud_sync.cli.rclone', side_effect=replies) as run:
            code, output = self.invoke('login', 'dropbox')
        self.assertEqual(code, 0)
        self.assertIn('--no-output', run.call_args_list[1].args)
        self.assertIn('verified', output)
        self.assertNotIn('private-folder', output)

    def test_existing_remote_is_not_overwritten(self):
        Paths.discover().initialize()
        reply = subprocess.CompletedProcess([], 0, '[{"name":"dropbox","type":"dropbox"}]')
        with patch('reicloud_sync.cli.rclone', return_value=reply) as run:
            self.assertEqual(self.invoke('login', 'dropbox')[0], 0)
        self.assertFalse(any('create' in call.args for call in run.call_args_list))

    def test_auth_failure_does_not_report_success(self):
        Paths.discover().initialize()
        with patch('reicloud_sync.cli.rclone', side_effect=subprocess.CalledProcessError(1, ['rclone'])):
            code, output = self.invoke('check', 'dropbox')
        self.assertEqual(code, 1)
        self.assertNotIn('access verified', output)

    def test_rclone_uses_explicit_config_and_ignores_ambient_credentials(self):
        with patch.dict(os.environ, {'RCLONE_CONFIG': '/elsewhere', 'RCLONE_CONFIG_DROPBOX_TOKEN': 'secret'}):
            with patch('shutil.which', return_value='/usr/bin/rclone'), patch('subprocess.run') as run:
                rclone(Paths.discover(), 'version', capture=True)
        args, kwargs = run.call_args
        self.assertEqual(args[0][1:3], ['--config', str(Paths.discover().credentials)])
        self.assertFalse(any(key.startswith('RCLONE_') for key in kwargs['env']))


if __name__ == '__main__':
    unittest.main()
