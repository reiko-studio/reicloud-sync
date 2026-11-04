"""Read-only browse and destructive-cleanup scope checks in temporary profiles."""

import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

import test_setup
from reicloud_sync.config import Paths
from reicloud_sync.workspace import migrate
from reicloud_sync.pairs import find_pair
from reicloud_sync.sync import state_dir, write_state
from reicloud_sync.cleanup import uninstall


class BrowseCleanupTests(unittest.TestCase):
    invoke = test_setup.SetupTests.invoke

    def setUp(self):
        test_setup.SetupTests.setUp(self)
        self.paths = Paths.discover()
        self.paths.initialize()
        self.local = self.root / 'Dropbox'
        with patch('reicloud_sync.workspace.computer_name', return_value='pc-abcdef'):
            migrate(self.paths, str(self.local))

    def test_wipe_requires_confirmation_and_preserves_synced_and_unknown_files(self):
        self.local.mkdir()
        (self.local / 'project.txt').write_text('keep synced')
        unknown = self.paths.config / 'personal.txt'
        unknown.write_text('keep unknown')
        pair = find_pair(self.paths, 'workspace')
        directory = state_dir(self.paths, pair)
        directory.mkdir(parents=True)
        write_state(directory, {'phase': 'ready', 'pair': {'name': pair.name, 'local': pair.local, 'remote': pair.remote}})
        credentials = self.paths.credentials
        credentials.write_text('secret fixture')
        self.assertEqual(self.invoke('uninstall', '--wipe')[0], 1)
        self.assertTrue(credentials.exists())
        self.assertEqual(self.invoke('uninstall', '--wipe', '--dry-run')[0], 0)
        self.assertTrue(credentials.exists())
        self.assertEqual(self.invoke('uninstall', '--wipe', '--yes')[0], 0)
        self.assertFalse(credentials.exists())
        self.assertFalse(directory.exists())
        self.assertEqual((self.local / 'project.txt').read_text(), 'keep synced')
        self.assertEqual(unknown.read_text(), 'keep unknown')

    def test_wipe_preserves_other_profile_state(self):
        other = Paths(self.root / 'other-config', self.paths.state, self.paths.cache)
        other.initialize()
        with patch('reicloud_sync.workspace.computer_name', return_value='other-123abc'):
            migrate(other, str(self.root / 'other-dropbox'))
        pair = find_pair(other, 'workspace')
        directory = state_dir(other, pair)
        directory.mkdir(parents=True)
        write_state(directory, {'phase': 'ready', 'pair': {'name': pair.name, 'local': pair.local, 'remote': pair.remote}})
        uninstall(self.paths, wipe=True, yes=True)
        self.assertTrue((directory / 'status.json').exists())
        self.assertTrue(other.settings.exists())

    def test_wipe_refuses_symlinked_pair_store(self):
        outside = self.root / 'outside'
        outside.mkdir()
        (self.paths.state / 'pairs').symlink_to(outside, target_is_directory=True)
        self.assertEqual(self.invoke('uninstall', '--wipe', '--yes')[0], 1)
        self.assertTrue(outside.exists())

    def test_uninstall_removes_only_managed_service_without_wipe(self):
        from reicloud_sync.service import unit_path, render_unit
        file = unit_path(self.paths)
        file.parent.mkdir(parents=True)
        file.write_text(render_unit(self.paths))
        with patch('reicloud_sync.cleanup.systemctl') as systemctl:
            uninstall(self.paths)
        self.assertEqual(systemctl.call_args_list[0].args[:2], ('disable', '--now'))
        self.assertEqual(systemctl.call_args_list[-1].args, ('daemon-reload',))
        self.assertFalse(file.exists())
        self.assertTrue(self.paths.settings.exists())

    def test_running_foreground_runner_prevents_wipe(self):
        from reicloud_sync.runner import runner_lock
        with runner_lock(self.paths):
            self.assertEqual(self.invoke('uninstall', '--wipe', '--yes')[0], 1)
        self.assertTrue(self.paths.settings.exists())
