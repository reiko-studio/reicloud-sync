"""Workspace policy and legacy migrations without touching account data."""

import json
from pathlib import Path
import unittest
from unittest.mock import patch

import test_setup
from reicloud_sync.config import Paths, ConfigError
from reicloud_sync.pairs import load_pairs, find_pair
from reicloud_sync.workspace import migrate, computer_name
from reicloud_sync.exclusions import filter_text, selection


class WorkspaceTests(unittest.TestCase):
    invoke = test_setup.SetupTests.invoke

    def setUp(self):
        test_setup.SetupTests.setUp(self)
        self.paths = Paths.discover()
        self.paths.initialize()
        self.identity = patch('reicloud_sync.workspace.computer_name', return_value='p16v-abcdef')
        self.identity.start()
        self.addCleanup(self.identity.stop)
        self.workspace = self.root / 'Dropbox'

    def configure(self):
        return migrate(self.paths, str(self.workspace))

    def test_new_profile_derives_external_destination_and_protects_namespace(self):
        self.configure()
        self.assertEqual(self.paths.load()['version'], 2)
        self.assertEqual(self.invoke('add', 'results', str(self.root / 'results'))[0], 0)
        pair = find_pair(self.paths, 'results')
        self.assertEqual(pair.remote, 'dropbox:.reicloud-computers/p16v-abcdef/results')
        workspace = find_pair(self.paths, 'workspace')
        self.assertIn('- /.reicloud-computers/**', filter_text(selection(workspace)))
        self.assertEqual(self.invoke('include', 'workspace', '/.reicloud-computers/**')[0], 1)
        self.assertFalse(self.workspace.exists())

    def test_forbid_workspace_children_root_targets_and_foreign_machine(self):
        self.configure()
        for args in [('nested', self.workspace / 'inside', None),
                     ('bad', self.root / 'outside', 'dropbox:root-folder'),
                     ('bad', self.root / 'outside', 'dropbox:.reicloud-computers/other-123abc/bad')]:
            command = ['add', args[0], str(args[1])]
            if args[2]:
                command.append(args[2])
            self.assertEqual(self.invoke(*command)[0], 1)
        self.assertEqual(len(load_pairs(self.paths)), 1)

    def test_explicit_retirement_keeps_files_and_state(self):
        local = self.root / 'old'
        local.mkdir()
        (local / 'file').write_text('keep')
        self.assertEqual(self.invoke('add', 'old', str(local), 'dropbox:Old')[0], 0)
        before = self.paths.settings.read_bytes()
        preview = migrate(self.paths, str(self.workspace), dry_run=True)
        self.assertTrue(preview['requires_retirement'])
        self.assertEqual(preview['retired_pairs'][0]['name'], 'old')
        with self.assertRaises(ConfigError):
            self.configure()
        self.assertEqual(self.paths.settings.read_bytes(), before)
        result = migrate(self.paths, str(self.workspace), retire_legacy=True)
        self.assertTrue((Path(result['backup']) / 'config.toml').is_file())
        self.assertEqual((local / 'file').read_text(), 'keep')

    def test_failed_publish_restores_original_configuration(self):
        import os
        original = os.replace
        failed = False
        before = self.paths.settings.read_bytes()
        def replace(source, target):
            nonlocal failed
            if Path(target) == self.paths.settings and not failed:
                failed = True
                raise OSError('simulated failure')
            return original(source, target)
        with patch('os.replace', side_effect=replace), self.assertRaises(OSError):
            self.configure()
        self.assertEqual(self.paths.settings.read_bytes(), before)
        self.assertFalse((self.paths.config / 'migration.json').exists())
        self.assertEqual(self.paths.load()['version'], 1)

    def test_manual_pair_edit_cannot_escape_policy(self):
        self.configure()
        self.assertEqual(self.invoke('add', 'results', str(self.root / 'results'))[0], 0)
        file = self.paths.config / 'pairs.toml'
        file.write_text(file.read_text().replace('.reicloud-computers/p16v-abcdef/results', 'results'))
        self.assertEqual(self.invoke('config', 'check')[0], 1)


class IdentityTests(unittest.TestCase):
    def test_suisave_machine_prefix_convention(self):
        with patch('socket.gethostname', return_value='My-PC'), patch.object(Path, 'read_text', return_value='abcdef1234567890'):
            self.assertEqual(computer_name(), 'my-pc-abcdef')
            self.assertEqual(computer_name('Research'), 'research-abcdef')
