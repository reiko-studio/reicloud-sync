"""Provisioning and conflict-aware initialization through real local rclone."""

import unittest
import os
import test_sync
from reicloud_sync.sync import read_state


class InitializationTests(unittest.TestCase):
    setUp = test_sync.SyncTests.setUp
    invoke = test_sync.SyncTests.invoke
    run_local = test_sync.SyncTests.run_local
    snapshot = test_sync.SyncTests.snapshot
    assertSuccess = test_sync.SyncTests.assertSuccess

    def test_preview_missing_both_roots_creates_neither(self):
        self.local.rmdir()
        self.remote.rmdir()
        self.assertSuccess('init', 'test', '--dry-run')
        self.assertFalse(self.local.exists())
        self.assertFalse(self.remote.exists())
        self.assertFalse(any(call[0] in ('mkdir', 'copyto') for call in self.calls))
        self.assertEqual(read_state(self.directory)['phase'], 'uninitialized')

    def test_real_init_creates_missing_both_roots(self):
        self.local.rmdir()
        self.remote.rmdir()
        self.assertSuccess('init', 'test')
        self.assertTrue(self.local.is_dir())
        self.assertTrue(self.remote.is_dir())
        self.assertEqual(read_state(self.directory)['phase'], 'ready')

    def test_download_into_new_local_root(self):
        self.local.rmdir()
        (self.remote / 'from-web.txt').write_text('remote')
        self.assertSuccess('init', 'test', '--dry-run')
        self.assertFalse(self.local.exists())
        self.assertSuccess('init', 'test')
        self.assertEqual((self.local / 'from-web.txt').read_text(), 'remote')

    def test_disjoint_existing_files_need_no_preference(self):
        (self.local / 'local.txt').write_text('local')
        (self.remote / 'remote.txt').write_text('remote')
        self.assertSuccess('init', 'test')
        self.assertEqual(self.snapshot(self.local), self.snapshot(self.remote))

    def test_actual_conflict_requires_preference_before_writes(self):
        (self.local / 'same.txt').write_text('left')
        (self.remote / 'same.txt').write_text('rght')
        os.utime(self.local / 'same.txt', (1700000000, 1700000000))
        os.utime(self.remote / 'same.txt', (1600000000, 1600000000))
        before = [self.snapshot(self.local), self.snapshot(self.remote)]
        code, output = self.invoke('init', 'test')
        self.assertEqual(code, 1)
        self.assertIn('conflicting file', output)
        self.assertEqual(before, [self.snapshot(self.local), self.snapshot(self.remote)])
        self.assertSuccess('init', 'test', '--prefer', 'local')
        self.assertEqual((self.remote / 'same.txt').read_text(), 'left')

    def test_identical_same_path_files_need_no_preference(self):
        for root in (self.local, self.remote):
            (root / 'same.txt').write_text('identical')
            os.utime(root / 'same.txt', (1700000000, 1700000000))
        self.assertSuccess('init', 'test')
