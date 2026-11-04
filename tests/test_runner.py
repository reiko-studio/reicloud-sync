"""Deterministic scheduler tests with no cloud access or clock sleeps."""

from dataclasses import replace
import unittest
import json

import test_setup
from reicloud_sync.config import Paths, ConfigError
from reicloud_sync.pairs import find_pair, edit_pairs
from reicloud_sync.runner import Runner, runner_lock
from reicloud_sync.sync import state_dir, write_state, set_paused, read_control, pair_lock, failure_policy


class Process:
    def __init__(self):
        self.code = None

    def poll(self):
        return self.code


class RunnerTests(unittest.TestCase):
    invoke = test_setup.SetupTests.invoke

    def setUp(self):
        test_setup.SetupTests.setUp(self)
        self.paths = Paths.discover()
        self.paths.initialize()
        self.time = 0
        self.started = []
        self.runner = Runner(self.paths, jobs=2, clock=lambda: self.time, worker=self.worker, wall_clock=lambda: 1700000000 + self.time)

    def worker(self, paths, pair):
        process = Process()
        self.started.append((pair.name, process))
        return process

    def pair(self, name, interval=30, phase='ready'):
        local = self.root / name
        local.mkdir()
        self.assertEqual(self.invoke('add', name, str(local), f'dropbox:{name}', '--interval', str(interval))[0], 0)
        pair = find_pair(self.paths, name)
        if phase != 'uninitialized':
            directory = state_dir(self.paths, pair)
            directory.mkdir(parents=True)
            write_state(directory, {'phase': phase, 'last_success': None})
        return pair

    def test_each_pair_uses_its_interval_from_completion(self):
        self.pair('first', 10)
        self.pair('second', 60)
        self.runner.tick()
        self.assertEqual([name for name, _ in self.started], ['first', 'second'])
        for _, process in self.started:
            process.code = 0
        self.runner.tick()
        self.time = 9
        self.runner.tick()
        self.assertEqual(len(self.started), 2)
        self.time = 10
        self.runner.tick()
        self.assertEqual([name for name, _ in self.started], ['first', 'second', 'first'])
        self.time = 60
        self.runner.tick()
        self.assertEqual(self.started[-1][0], 'second')

    def test_jobs_limit_and_once_mode(self):
        for name in ('one', 'two', 'three'):
            self.pair(name)
        self.runner.tick(once=True)
        self.assertEqual(len(self.started), 2)
        self.started[0][1].code = 0
        self.runner.tick(once=True)
        self.assertEqual(len(self.started), 3)
        for _, process in self.started:
            process.code = 0
        self.assertFalse(self.runner.tick(once=True))
        self.assertEqual(len(self.started), 3)

    def test_uninitialized_and_critical_pairs_are_never_started(self):
        self.pair('new', phase='uninitialized')
        self.pair('broken', phase='needs_recovery')
        self.assertFalse(self.runner.tick())
        self.assertEqual(self.started, [])

    def test_existing_pairs_default_to_five_minutes(self):
        pair = self.pair('notes')
        file = self.paths.config / 'pairs.toml'
        file.write_text(file.read_text().replace('interval = 30\n', ''))
        self.assertEqual(find_pair(self.paths, pair.name).interval, 300)
        self.assertEqual(self.invoke('interval', pair.name, '2m')[0], 0)
        self.assertEqual(find_pair(self.paths, pair.name).interval, 120)

    def test_only_one_runner_per_configuration(self):
        with runner_lock(self.paths):
            with self.assertRaises(ConfigError):
                with runner_lock(self.paths):
                    pass

    def test_pause_and_resume_are_persistent_and_resume_is_immediate(self):
        pair = self.pair('notes', 60)
        self.runner.tick()
        self.started[0][1].code = 0
        self.runner.tick()
        self.assertEqual(self.invoke('pause', 'notes')[0], 0)
        self.time = 1
        self.runner.tick()
        self.assertEqual(len(self.started), 1)
        self.assertTrue(read_control(state_dir(self.paths, pair))['paused'])
        self.assertEqual(self.invoke('resume', 'notes')[0], 0)
        self.runner.tick()
        self.assertEqual(len(self.started), 2)

    def test_pause_does_not_wait_for_transfer_lock(self):
        pair = self.pair('notes')
        with pair_lock(state_dir(self.paths, pair)):
            self.assertEqual(self.invoke('pause', pair.name)[0], 0)
        self.runner.tick()
        self.assertEqual(self.started, [])

    def test_paused_pair_is_rejected_before_manual_transfer(self):
        pair = self.pair('notes')
        set_paused(self.paths, pair.name, True)
        code, output = self.invoke('sync', pair.name)
        self.assertEqual(code, 1)
        self.assertIn('is paused', output)

    def test_unknown_pause_does_not_create_state(self):
        self.assertEqual(self.invoke('pause', 'missing')[0], 1)
        self.assertFalse((self.paths.state / 'pairs').exists())

    def test_transient_backoff_grows_and_success_resets_it(self):
        pair = self.pair('network', interval=5)
        directory = state_dir(self.paths, pair)
        self.runner.tick()
        write_state(directory, {'phase': 'retryable_error', 'last_success': None,
                                'automatic_retry': True, 'error': 'network', 'finished': 'one'})
        self.started[-1][1].code = 1
        self.runner.tick()
        self.time = 29
        self.runner.tick()
        self.assertEqual(len(self.started), 1)
        self.time = 30
        self.runner.tick()
        self.assertEqual(len(self.started), 2)
        self.started[-1][1].code = 1
        self.runner.tick()
        self.time = 89
        self.runner.tick()
        self.assertEqual(len(self.started), 2)
        self.time = 90
        self.runner.tick()
        self.assertEqual(len(self.started), 3)
        write_state(directory, {'phase': 'ready', 'last_success': 'success'})
        self.started[-1][1].code = 0
        self.runner.tick()
        self.time = 95
        self.runner.tick()
        self.assertEqual(len(self.started), 4)
        self.assertEqual(self.runner.failures, {})

    def test_safety_failure_blocks_only_that_pair(self):
        broken = self.pair('safety')
        self.pair('healthy', interval=1)
        self.runner.tick()
        write_state(state_dir(self.paths, broken), {'phase': 'retryable_error',
                    'last_success': None, 'automatic_retry': False, 'error': 'too many deletes'})
        self.started[0][1].code = 1
        self.started[1][1].code = 0
        self.runner.tick()
        self.time = 100
        self.runner.tick()
        self.assertEqual([name for name, _ in self.started], ['safety', 'healthy', 'healthy'])

    def test_per_pair_corrupt_state_does_not_block_healthy_pair(self):
        broken = self.pair('broken')
        self.pair('healthy')
        (state_dir(self.paths, broken) / 'status.json').write_text('bad json')
        self.runner.tick()
        self.assertEqual([name for name, _ in self.started], ['healthy'])

    def test_failure_classification_never_auto_retries_critical_or_safety(self):
        import subprocess
        for code, message, expected in [(1, 'connection timed out', True),
                                         (1, 'Safety abort: too many deletes', False),
                                         (2, 'usage error', False),
                                         (7, 'connection timed out', False),
                                         (1, 'unclassified failure', False)]:
            with self.subTest(code=code, message=message):
                error = subprocess.CalledProcessError(code, ['rclone'], stderr=message)
                self.assertEqual(failure_policy(error)[1], expected)

    def test_worker_launch_failure_is_not_retried_in_a_busy_loop(self):
        self.pair('launch')
        def fail(*args):
            raise OSError('cannot start worker')
        self.runner.worker = fail
        self.runner.tick(once=True)
        self.assertFalse(self.runner.tick(once=True))
        self.assertTrue(self.runner.had_failures)

    def test_status_shows_pause_and_only_live_next_run(self):
        self.pair('notes', 60)
        self.runner.tick()
        self.started[-1][1].code = 0
        with runner_lock(self.paths):
            self.runner.tick()
            row = json.loads(self.invoke('status', 'notes', '--json')[1])
            self.assertTrue(row['runner_active'])
            self.assertIsNotNone(row['next_run'])
            self.assertEqual(row['interval_seconds'], 60)
            self.assertEqual(self.invoke('pause', 'notes')[0], 0)
            row = json.loads(self.invoke('status', 'notes', '--json')[1])
            self.assertTrue(row['paused'])
            self.assertIsNone(row['next_run'])
        row = json.loads(self.invoke('status', 'notes', '--json')[1])
        self.assertFalse(row['runner_active'])
        self.assertIsNone(row['next_run'])

    def test_restart_preserves_backoff_deadline_and_failure_count(self):
        pair = self.pair('network')
        self.runner.tick()
        write_state(state_dir(self.paths, pair), {'phase': 'retryable_error',
                    'last_success': None, 'automatic_retry': True, 'finished': 'failed'})
        self.started[-1][1].code = 1
        self.runner.tick()
        restarted = Runner(self.paths, clock=lambda: self.time, worker=self.worker,
                           wall_clock=lambda: 1700000000 + self.time)
        self.time = 20
        restarted.tick()
        self.assertEqual(len(self.started), 1)
        self.time = 30
        restarted.tick()
        self.assertEqual(len(self.started), 2)
        self.started[-1][1].code = 1
        restarted.tick()
        row = json.loads(self.invoke('status', 'network', '--json')[1])
        self.assertEqual(row['consecutive_failures'], 2)
        self.assertIsNotNone(row['scheduled_for'])

    def test_status_all_is_json_and_isolates_invalid_pair_state(self):
        pair = self.pair('bad')
        self.pair('good')
        (state_dir(self.paths, pair) / 'status.json').write_text('invalid')
        code, output = self.invoke('status', '--json')
        self.assertEqual(code, 0)
        rows = json.loads(output)
        self.assertEqual([row['phase'] for row in rows], ['invalid', 'ready'])

    def test_interval_change_updates_existing_schedule(self):
        self.pair('notes', 60)
        self.runner.tick()
        self.started[-1][1].code = 0
        self.runner.tick()
        self.time = 10
        self.assertEqual(self.invoke('interval', 'notes', '5s')[0], 0)
        self.runner.tick()
        self.assertEqual(len(self.started), 2)
