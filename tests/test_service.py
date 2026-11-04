"""User-unit generation and controls without changing the real user manager."""

import os
from pathlib import Path
import shutil
import subprocess
import unittest
from unittest.mock import patch

import test_setup
from reicloud_sync.config import Paths, ConfigError
from reicloud_sync.service import render_unit, unit_path, unit_name, install, control, quote, systemctl


class ServiceTests(unittest.TestCase):
    invoke = test_setup.SetupTests.invoke

    def setUp(self):
        test_setup.SetupTests.setUp(self)
        self.paths = Paths.discover()
        self.paths.initialize()

    def test_install_dry_run_has_no_side_effects(self):
        with patch('reicloud_sync.service.systemctl') as systemctl:
            code, output = self.invoke('service', 'install', '--dry-run')
        self.assertEqual(code, 0)
        self.assertIn('ExecStart=', output)
        self.assertIn('KillMode=mixed', output)
        self.assertFalse(unit_path(self.paths).exists())
        systemctl.assert_not_called()

    def test_install_enables_but_never_starts_then_control_is_explicit(self):
        with patch('reicloud_sync.service.systemctl') as systemctl:
            self.assertEqual(self.invoke('service', 'install')[0], 0)
            self.assertEqual(systemctl.call_args_list[0].args, ('daemon-reload',))
            self.assertEqual(systemctl.call_args_list[1].args, ('enable', unit_name(self.paths)))
            self.assertEqual(len(systemctl.call_args_list), 2)
            self.assertEqual(self.invoke('service', 'start')[0], 0)
            self.assertEqual(systemctl.call_args.args, ('start', unit_name(self.paths)))
            self.assertEqual(self.invoke('service', 'stop')[0], 0)
            self.assertEqual(systemctl.call_args.args, ('stop', unit_name(self.paths)))
        self.assertIn(str(self.paths.config), unit_path(self.paths).read_text())

    def test_independent_configs_have_different_units_and_fixed_runtime(self):
        other = Paths(self.root / 'other', self.paths.state, self.paths.cache)
        self.assertNotEqual(unit_name(other), unit_name(self.paths))
        content = render_unit(self.paths)
        self.assertIn('-m', content)
        self.assertIn('XDG_STATE_HOME=', content)
        self.assertNotIn('RCLONE_CONFIG', content)
        self.assertNotIn('token', content)

    def test_refuses_to_replace_unmanaged_unit(self):
        file = unit_path(self.paths)
        file.parent.mkdir(parents=True)
        file.write_text('manual settings')
        with patch('reicloud_sync.service.systemctl') as systemctl:
            self.assertEqual(self.invoke('service', 'install')[0], 1)
        self.assertEqual(file.read_text(), 'manual settings')
        systemctl.assert_not_called()

    def test_escapes_systemd_specifiers_and_rejects_newlines(self):
        self.assertEqual(quote('space %n $HOME', command=True), '"space %%n $$HOME"')
        self.assertEqual(quote('HOME=$HOME'), '"HOME=$HOME"')
        with self.assertRaises(ConfigError):
            quote('line\nbreak')

    def test_generated_unit_passes_systemd_verification(self):
        if not shutil.which('systemd-analyze'):
            self.skipTest('systemd-analyze is unavailable')
        file = self.root / unit_name(self.paths)
        file.write_text(render_unit(self.paths))
        result = subprocess.run(['systemd-analyze', 'verify', '--man=no', str(file)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_service_has_actionable_error(self):
        code, output = self.invoke('service', 'start')
        self.assertEqual(code, 1)
        self.assertIn('service install', output)

    def test_controls_always_target_user_manager(self):
        with patch('reicloud_sync.service.shutil.which', return_value='/usr/bin/systemctl'), patch('subprocess.run') as run:
            systemctl('stop', unit_name(self.paths))
        self.assertEqual(run.call_args.args[0], ['/usr/bin/systemctl', '--user', 'stop', unit_name(self.paths)])
