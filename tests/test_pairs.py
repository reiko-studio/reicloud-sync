"""Folder configuration and preview safety checks."""

import contextlib
import io
import json
from pathlib import Path
import shutil
import subprocess
from unittest.mock import patch

import test_setup
import unittest
from reicloud_sync.cli import main
from reicloud_sync.config import Paths
from reicloud_sync.pairs import load_pairs


class PairTests(unittest.TestCase):
    invoke = test_setup.SetupTests.invoke
    def setUp(self):
        test_setup.SetupTests.setUp(self)
        Paths.discover().initialize()
        self.local = self.root / 'documents'
        self.local.mkdir()
        self.other = self.root / 'other'
        self.other.mkdir()

    def add(self, name='notes', local=None, remote='dropbox:Notes'):
        return self.invoke('add', name, str(local or self.local), remote)

    def test_roundtrip_and_removal_leave_files(self):
        file = self.local / 'keep.txt'
        file.write_text('preserve')
        self.assertEqual(self.add()[0], 0)
        code, output = self.invoke('show', 'notes', '--json')
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)['local'], str(self.local))
        self.assertEqual(len(json.loads(self.invoke('list', '--json')[1])), 1)
        self.assertEqual(self.invoke('remove', 'notes')[0], 0)
        self.assertEqual(load_pairs(Paths.discover()), [])
        self.assertEqual(file.read_text(), 'preserve')
        self.assertEqual(self.invoke('remove', 'notes')[0], 1)

    def test_reject_duplicate_local_nested_remote_case_and_symlink(self):
        self.assertEqual(self.add()[0], 0)
        original = (Paths.discover().config / 'pairs.toml').read_bytes()
        child = self.local / 'child'
        child.mkdir()
        alias = self.root / 'alias'
        alias.symlink_to(self.local, target_is_directory=True)
        for name, local, remote in [
            ('notes', self.other, 'dropbox:Other'),
            ('second', child, 'dropbox:Other'),
            ('second', alias, 'dropbox:Other'),
            ('second', self.other, 'dropbox:/notes/child/'),
            ('second', self.other, 'dropbox:'),
        ]:
            with self.subTest(local=local, remote=remote):
                self.assertEqual(self.add(name, local, remote)[0], 1)
                self.assertEqual((Paths.discover().config / 'pairs.toml').read_bytes(), original)

    def test_invalid_paths_and_unknown_pairs(self):
        for remote in ('drive:Notes', 'dropbox:../Notes', 'dropbox:one\\two'):
            self.assertEqual(self.add(remote=remote)[0], 1)
        self.assertEqual(self.add(local=self.root / 'missing')[0], 1)
        self.assertEqual(self.add(name='../bad')[0], 1)
        self.assertEqual(self.invoke('show', 'absent')[0], 1)
        self.assertEqual(self.add(local=self.root)[0], 1)

    def test_malformed_saved_pairs_fail_config_check_without_rewrite(self):
        file = Paths.discover().config / 'pairs.toml'
        file.write_text('pairs = "invalid"\n')
        self.assertEqual(self.invoke('config', 'check')[0], 1)
        self.assertEqual(self.add()[0], 1)
        self.assertEqual(file.read_text(), 'pairs = "invalid"\n')

    def test_preview_is_always_dry_and_discards_history(self):
        self.assertEqual(self.add()[0], 0)
        replies = [subprocess.CompletedProcess([], 0, '[{"name":"dropbox","type":"dropbox"}]'),
                   subprocess.CompletedProcess([], 0, '{"IsDir":true}'),
                   subprocess.CompletedProcess([], 0)]
        with patch('reicloud_sync.cli.rclone', side_effect=replies) as run:
            self.assertEqual(self.invoke('init', 'notes', '--dry-run', '--prefer', 'remote')[0], 0)
        args = run.call_args.args
        self.assertIn('--dry-run', args)
        self.assertEqual(args[args.index('--resync-mode') + 1], 'path2')
        self.assertFalse(Path(args[args.index('--workdir') + 1]).exists())
        self.assertEqual(len(load_pairs(Paths.discover())), 1)

    def test_uninitialized_sync_is_still_rejected(self):
        self.add()
        with patch('reicloud_sync.cli.rclone') as run:
            self.assertEqual(self.invoke('sync', 'notes')[0], 1)
        run.assert_not_called()

    def test_real_rclone_preview_preserves_both_trees(self):
        if not shutil.which('rclone'):
            self.skipTest('rclone is not installed')
        self.add()
        (self.local / 'local.txt').write_text('local')
        (self.other / 'remote.txt').write_text('remote')
        (self.local / 'conflict.txt').write_text('left')
        (self.other / 'conflict.txt').write_text('right')
        before = {str(p): p.read_bytes() for root in (self.local, self.other) for p in root.rglob('*') if p.is_file()}

        def fake_remote(paths, *args, **kwargs):
            if args[0] == 'listremotes':
                return subprocess.CompletedProcess([], 0, '[{"name":"dropbox","type":"dropbox"}]')
            args = [str(self.other) if arg == 'dropbox:Notes' else arg for arg in args]
            return subprocess.run(['rclone', '--config', str(paths.credentials), *args],
                                  text=True, capture_output=True, check=True)

        with patch('reicloud_sync.cli.rclone', side_effect=fake_remote):
            code, output = self.invoke('init', 'notes', '--dry-run', '--prefer', 'local')
        self.assertEqual(code, 0, output)
        after = {str(p): p.read_bytes() for root in (self.local, self.other) for p in root.rglob('*') if p.is_file()}
        self.assertEqual(before, after)
