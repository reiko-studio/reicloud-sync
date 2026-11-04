"""Exercise selection changes using real local rclone transfers."""

import unittest
import test_sync
from reicloud_sync.sync import read_state, write_state, MARKER


class ExclusionTests(unittest.TestCase):
    setUp = test_sync.SyncTests.setUp
    invoke = test_sync.SyncTests.invoke
    run_local = test_sync.SyncTests.run_local
    initialize = test_sync.SyncTests.initialize
    saved_status = test_sync.SyncTests.saved_status
    snapshot = test_sync.SyncTests.snapshot
    assertSuccess = test_sync.SyncTests.assertSuccess

    def test_initial_exclusions_apply_in_both_directions(self):
        (self.local / '.venv').mkdir()
        (self.local / '.venv' / 'local.tmp').write_text('keep local')
        (self.remote / '.venv').mkdir()
        (self.remote / '.venv' / 'remote.tmp').write_text('keep remote')
        self.assertSuccess('exclude', 'test', '.venv/**')
        self.initialize()
        self.assertFalse((self.remote / '.venv' / 'local.tmp').exists())
        self.assertFalse((self.local / '.venv' / 'remote.tmp').exists())
        self.assertTrue((self.local / '.venv' / 'local.tmp').exists())
        self.assertSuccess('sync', 'test')

    def test_staged_rules_block_sync_and_preview_preserves_history(self):
        self.initialize()
        self.assertSuccess('exclude', 'test', '*.tmp')
        self.assertTrue(self.saved_status()['filters_pending'])
        self.assertEqual(self.invoke('sync', 'test')[0], 1)
        before = self.snapshot(self.directory)
        self.assertSuccess('exclusions', 'test', '--apply', '--prefer', 'local', '--dry-run')
        self.assertEqual(before, self.snapshot(self.directory))
        self.assertTrue(self.saved_status()['filters_pending'])
        self.assertSuccess('exclusions', 'test', '--apply', '--prefer', 'local')
        self.assertFalse(self.saved_status()['filters_pending'])
        self.assertSuccess('sync', 'test')

    def test_excluded_existing_files_remain_and_can_be_reincluded(self):
        (self.local / 'cache.tmp').write_text('before')
        self.initialize()
        self.assertSuccess('exclude', 'test', '*.tmp')
        self.assertSuccess('exclusions', 'test', '--apply', '--prefer', 'local')
        (self.local / 'cache.tmp').write_text('local edited')
        self.assertSuccess('sync', 'test')
        self.assertEqual((self.remote / 'cache.tmp').read_text(), 'before')
        self.assertSuccess('include', 'test', '*.tmp')
        self.assertSuccess('exclusions', 'test', '--apply', '--prefer', 'local')
        self.assertEqual((self.remote / 'cache.tmp').read_text(), 'local edited')
        self.assertSuccess('sync', 'test')

    def test_access_marker_cannot_be_excluded(self):
        self.assertSuccess('exclude', 'test', '**')
        self.initialize()
        self.assertTrue((self.remote / MARKER).exists())
        self.assertFalse((self.remote / 'local.txt').exists())
        self.assertSuccess('sync', 'test')

    def test_invalid_rule_does_not_mutate_config(self):
        file = self.paths.config / 'pairs.toml'
        before = file.read_bytes()
        self.assertEqual(self.invoke('exclude', 'test', 'bad\nrule')[0], 1)
        self.assertEqual(file.read_bytes(), before)

    def test_legacy_unfiltered_state_can_keep_syncing(self):
        self.initialize()
        state = read_state(self.directory)
        state.pop('filters_hash')
        write_state(self.directory, state)
        for file in (self.directory / 'bisync').glob('filters.txt*'):
            file.unlink()
        self.assertSuccess('sync', 'test')
        self.assertSuccess('sync', 'test')
