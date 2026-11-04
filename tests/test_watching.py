"""Real filesystem notifications and deterministic scheduling of watcher hints."""

import time
import unittest
import test_setup
import test_runner
from reicloud_sync.config import Paths
from reicloud_sync.pairs import find_pair
from reicloud_sync.watching import WatchManager
from reicloud_sync.sync import state_dir, write_state


class WatchTests(unittest.TestCase):
    invoke = test_setup.SetupTests.invoke

    def setUp(self):
        test_setup.SetupTests.setUp(self)
        self.paths = Paths.discover()
        self.paths.initialize()
        self.local = self.root / 'notes'
        self.local.mkdir()
        self.assertEqual(self.invoke('add', 'notes', str(self.local), 'dropbox:Notes')[0], 0)
        self.pair = find_pair(self.paths, 'notes')

    def test_real_file_events_and_clean_stop(self):
        manager = WatchManager(self.paths)
        self.addCleanup(manager.close)
        manager.refresh([self.pair])
        handle = next(iter(manager.handles.values()))
        self.assertTrue(handle.ready.wait(5), 'watcher did not become ready')
        (self.local / 'file.txt').write_text('changed')
        deadline = time.monotonic() + 5
        changes = set()
        while time.monotonic() < deadline and not changes:
            changes.update(manager.drain())
            time.sleep(0.05)
        self.assertIn(str(state_dir(self.paths, self.pair)), changes)
        manager.close()
        self.assertFalse(handle.thread.is_alive())

    def test_service_watch_option_is_explicit(self):
        code, output = self.invoke('service', 'install', '--watch', '--dry-run')
        self.assertEqual(code, 0)
        self.assertIn('"--watch"', output)


class FakeWatcher:
    def __init__(self):
        self.pending = set()
        self.watched = []

    def drain(self):
        result, self.pending = self.pending, set()
        return result

    def refresh(self, pairs):
        self.watched = pairs


class WatchSchedulingTests(unittest.TestCase):
    invoke = test_runner.RunnerTests.invoke
    setUp = test_runner.RunnerTests.setUp
    worker = test_runner.RunnerTests.worker
    pair = test_runner.RunnerTests.pair

    def test_burst_is_debounced_and_periodic_scan_remains(self):
        pair = self.pair('notes', 300)
        watcher = FakeWatcher()
        self.runner.watcher = watcher
        self.runner.tick()
        self.started[-1][1].code = 0
        self.runner.tick()
        key = str(state_dir(self.paths, pair))
        self.time = 10
        watcher.pending.add(key)
        self.runner.tick()
        self.time = 11
        watcher.pending.add(key)
        self.runner.tick()
        self.time = 12
        self.runner.tick()
        self.assertEqual(len(self.started), 1)
        self.time = 13
        self.runner.tick()
        self.assertEqual(len(self.started), 2)
        self.started[-1][1].code = 0
        self.runner.tick()
        self.time = 313
        self.runner.tick()
        self.assertEqual(len(self.started), 3)

    def test_events_cannot_bypass_backoff_or_pause(self):
        pair = self.pair('notes', 300)
        watcher = FakeWatcher()
        self.runner.watcher = watcher
        self.runner.tick()
        write_state(state_dir(self.paths, pair), {'phase': 'retryable_error',
                    'last_success': None, 'automatic_retry': True})
        self.started[-1][1].code = 1
        self.runner.tick()
        self.time = 5
        watcher.pending.add(str(state_dir(self.paths, pair)))
        self.runner.tick()
        self.time = 10
        self.runner.tick()
        self.assertEqual(len(self.started), 1)
        self.assertEqual(self.invoke('pause', 'notes')[0], 0)
        self.time = 60
        self.runner.tick()
        self.assertEqual(len(self.started), 1)
        self.assertEqual(watcher.watched, [])

    def test_events_during_transfer_coalesce_into_one_followup(self):
        pair = self.pair('notes', 300)
        watcher = FakeWatcher()
        self.runner.watcher = watcher
        self.runner.tick()
        self.time = 5
        watcher.pending.add(str(state_dir(self.paths, pair)))
        self.runner.tick()
        self.time = 10
        self.started[-1][1].code = 0
        self.runner.tick()
        self.assertEqual(len(self.started), 2)
        self.started[-1][1].code = 0
        self.runner.tick()
        self.assertEqual(len(self.started), 2)

    def test_events_do_not_apply_staged_exclusions(self):
        pair = self.pair('notes', 300)
        watcher = FakeWatcher()
        self.runner.watcher = watcher
        self.runner.tick()
        self.started[-1][1].code = 0
        self.runner.tick()
        self.assertEqual(self.invoke('exclude', 'notes', '*.tmp')[0], 0)
        self.time = 500
        watcher.pending.add(str(state_dir(self.paths, pair)))
        self.runner.tick()
        self.assertEqual(len(self.started), 1)
        self.assertEqual(watcher.watched, [])
