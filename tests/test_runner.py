"""Deterministic scheduler tests with no cloud access or clock sleeps."""

from dataclasses import replace
import unittest

import test_setup
from reicloud_sync.config import Paths, ConfigError
from reicloud_sync.pairs import find_pair, edit_pairs
from reicloud_sync.runner import Runner, runner_lock
from reicloud_sync.sync import state_dir, write_state


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
        self.runner = Runner(self.paths, jobs=2, clock=lambda: self.time, worker=self.worker)

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
