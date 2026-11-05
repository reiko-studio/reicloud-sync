"""Remote event routing and scheduler behavior without cloud credentials."""

import sys
import time
import unittest
from unittest.mock import patch

import test_setup
import test_runner
from reicloud_sync.config import Paths
from reicloud_sync.pairs import find_pair, make_pair
from reicloud_sync.remote_watching import RemoteWatchManager, affects, event_path
from reicloud_sync.sync import state_dir, write_state


class RemoteWatcherTests(unittest.TestCase):
    invoke = test_setup.SetupTests.invoke

    def setUp(self):
        test_setup.SetupTests.setUp(self)
        self.paths = Paths.discover()
        code, output = self.invoke('setup', '--non-interactive', '--workspace', str(self.root / 'Dropbox'))
        self.assertEqual(code, 0, output)

    def test_event_format_and_namespace_routing(self):
        workspace = find_pair(self.paths, 'workspace-dropbox')
        external = make_pair('notes', str(self.root / 'notes'),
                             'dropbox:.reicloud-computers/pc/notes', kind='external')
        self.assertEqual(event_path(r'"notes/a \"quote\".txt": 1'), 'notes/a "quote".txt')
        self.assertIsNone(event_path('Waiting for changes, polling every 1s'))
        self.assertIsNone(event_path('"a/../b": 1'))
        self.assertTrue(affects(workspace, 'dropbox', 'notes/a.txt'))
        self.assertFalse(affects(workspace, 'dropbox', '.REICLOUD-COMPUTERS/pc/notes/a.txt'))
        self.assertFalse(affects(workspace, 'dropbox', '.reicloud-sync-access'))
        self.assertTrue(affects(external, 'dropbox', '.reicloud-computers/PC/notes/a.txt'))
        self.assertTrue(affects(external, 'dropbox', '.reicloud-computers/pc'))
        self.assertFalse(affects(external, 'dropbox', '.reicloud-computers/other/notes/a.txt'))
        self.assertFalse(affects(external, 'drive', '.reicloud-computers/pc/notes/a.txt'))

    def test_supervised_listener_starts_routes_and_stops(self):
        self.paths.settings.write_text(self.paths.settings.read_text().replace('remote_watch = false', 'remote_watch = true'))
        workspace = find_pair(self.paths, 'workspace-dropbox')
        fake = self.root / 'fake-rclone'
        fake.write_text(f'#!{sys.executable}\n'
                        'import json, sys, time\n'
                        'if "listremotes" in sys.argv:\n'
                        '    print(json.dumps([{"name": "dropbox", "type": "dropbox"}]))\n'
                        'else:\n'
                        '    print(json.dumps({"msg": "Waiting for changes, polling every 1s"}), flush=True)\n'
                        '    print(json.dumps({"msg": \'"notes/test.txt": 1\'}), flush=True)\n'
                        '    time.sleep(30)\n')
        fake.chmod(0o700)
        manager = RemoteWatchManager(self.paths)
        self.addCleanup(manager.close)
        with patch('reicloud_sync.remote_watching.shutil.which', return_value=str(fake)):
            manager.refresh([workspace])
            listener = manager.listeners['dropbox']
            deadline = time.monotonic() + 5
            changed = set()
            while not changed and time.monotonic() < deadline:
                changed = manager.drain()
                time.sleep(0.02)
        self.assertEqual(changed, {str(state_dir(self.paths, workspace))})
        self.assertEqual(manager.health(workspace), 'listening')
        manager.refresh([])
        self.assertFalse(listener.thread.is_alive())
        self.assertIsNotNone(listener.process.poll())


class RemoteHints:
    def __init__(self):
        self.pending = set()

    def drain(self):
        result, self.pending = self.pending, set()
        return result

    def refresh(self, pairs):
        self.pairs = pairs

    def health(self, pair):
        return 'listening'


class RemoteSchedulingTests(unittest.TestCase):
    invoke = test_runner.RunnerTests.invoke
    setUp = test_runner.RunnerTests.setUp
    worker = test_runner.RunnerTests.worker
    pair = test_runner.RunnerTests.pair

    def test_remote_hint_waits_for_cooldown_then_wakes_before_periodic_scan(self):
        pair = self.pair('notes', 300)
        watcher = RemoteHints()
        self.runner.remote_watcher = watcher
        self.runner.tick()
        self.started[-1][1].code = 0
        self.runner.tick()
        watcher.pending.add(str(state_dir(self.paths, pair)))
        self.time = 3
        self.runner.tick()
        self.time = 14
        self.runner.tick()
        self.assertEqual(len(self.started), 1)
        self.time = 15
        self.runner.tick()
        self.assertEqual(len(self.started), 2)

    def test_remote_hint_preserves_backoff_and_pause(self):
        pair = self.pair('notes', 300)
        watcher = RemoteHints()
        self.runner.remote_watcher = watcher
        self.runner.tick()
        write_state(state_dir(self.paths, pair), {'phase': 'retryable_error',
                    'last_success': None, 'automatic_retry': True})
        self.started[-1][1].code = 1
        self.runner.tick()
        watcher.pending.add(str(state_dir(self.paths, pair)))
        self.time = 10
        self.runner.tick()
        self.time = 20
        self.runner.tick()
        self.assertEqual(len(self.started), 1)
        self.assertEqual(self.invoke('pause', 'notes')[0], 0)
        self.time = 60
        self.runner.tick()
        self.assertEqual(len(self.started), 1)
        self.assertEqual(watcher.pairs, [])
