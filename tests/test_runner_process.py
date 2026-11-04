"""Exercise real foreground-process ownership and graceful worker signals."""

import contextlib
import io
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import unittest

import test_setup
from reicloud_sync.config import Paths
from reicloud_sync.processes import run_locked
from reicloud_sync.runner import Runner, Job
from reicloud_sync.scheduling import runner_active


class RunnerProcessTests(unittest.TestCase):
    def setUp(self):
        test_setup.SetupTests.setUp(self)
        self.paths = Paths.discover()
        self.paths.initialize()

    def wait_for(self, predicate):
        deadline = time.monotonic() + 5
        while not predicate():
            if time.monotonic() >= deadline:
                self.fail('Child process did not become ready within five seconds')
            time.sleep(0.01)

    def cleanup_process(self, process):
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)
        if process.stderr:
            process.stderr.close()

    def test_runner_rejects_duplicate_and_stops_on_sigterm(self):
        command = [sys.executable, '-m', 'reicloud_sync', '--config-dir', str(self.paths.config), 'run']
        process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                   text=True, start_new_session=True)
        self.addCleanup(self.cleanup_process, process)
        self.wait_for(lambda: runner_active(self.paths))
        duplicate = subprocess.run([*command, '--once'], text=True, capture_output=True, timeout=5)
        self.assertEqual(duplicate.returncode, 1)
        self.assertIn('already active', duplicate.stderr)
        process.terminate()
        self.assertEqual(process.wait(timeout=5), 0)
        self.assertFalse(runner_active(self.paths))

    def test_stop_signals_worker_group_and_waits_for_exit(self):
        ready = self.root / 'ready'
        finished = self.root / 'finished'
        script = """
import signal, sys, time
from pathlib import Path
def stop(*args):
    Path(sys.argv[2]).write_text('graceful')
    sys.exit(0)
signal.signal(signal.SIGINT, stop)
Path(sys.argv[1]).touch()
while True:
    time.sleep(1)
"""
        process = subprocess.Popen([sys.executable, '-c', script, str(ready), str(finished)],
                                   start_new_session=True, stderr=subprocess.PIPE)
        self.addCleanup(self.cleanup_process, process)
        self.wait_for(ready.exists)
        runner = Runner(self.paths)
        runner.active['worker'] = Job(None, process)
        runner.stop()
        self.assertEqual(process.returncode, 0)
        self.assertEqual(finished.read_text(), 'graceful')

    def test_locked_process_relays_and_retains_failure_diagnostics(self):
        with (self.root / 'lock').open('w') as lock:
            output = io.StringIO()
            command = [sys.executable, '-c', 'import sys; print("connection timed out", file=sys.stderr); sys.exit(1)']
            with contextlib.redirect_stderr(output), self.assertRaises(subprocess.CalledProcessError) as failure:
                run_locked(command, dict(os.environ), False, lock.fileno())
        self.assertIn('connection timed out', output.getvalue())
        self.assertIn('connection timed out', failure.exception.stderr)
