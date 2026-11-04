"""Bounded logs and token redaction without account access."""

import io
import json
import subprocess
import sys
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

import test_setup
from reicloud_sync.activity import activity_scope, append, read_entries, sanitize, follow
from reicloud_sync.processes import run_locked


class ActivityTests(unittest.TestCase):
    setUp = test_setup.SetupTests.setUp

    def test_transfer_diagnostics_are_recorded_and_redacted(self):
        with (self.root / 'lock').open('w') as lock:
            command = [sys.executable, '-c', 'import sys; print("file.txt copied", file=sys.stderr); print("access_token=secret", file=sys.stderr)']
            with redirect_stderr(io.StringIO()), activity_scope(self.root, 'test', 'sync', False):
                run_locked(command, None, False, lock.fileno())
        entries = read_entries(self.root)
        self.assertEqual(entries[0]['event'], 'started')
        self.assertEqual(entries[-1]['event'], 'completed')
        serialized = json.dumps(entries)
        self.assertIn('file.txt copied', serialized)
        self.assertNotIn('secret', serialized)
        self.assertEqual((self.root / 'activity.jsonl').stat().st_mode & 0o777, 0o600)

    def test_rotation_and_tail(self):
        with patch('reicloud_sync.activity.LIMIT', 1):
            for index in range(6):
                append(self.root, {'id': str(index), 'event': 'test'})
        self.assertEqual([entry['id'] for entry in read_entries(self.root, 2)], ['4', '5'])
        self.assertEqual(len(list(self.root.glob('activity.jsonl*'))), 4)

    def test_follow_survives_rotation(self):
        append(self.root, {'id': 'first'})
        stream = follow(self.root)
        self.assertEqual(next(stream)['id'], 'first')
        with patch('reicloud_sync.activity.LIMIT', 1):
            append(self.root, {'id': 'second'})
        self.assertEqual(next(stream)['id'], 'second')
        stream.close()

    def test_logging_failure_does_not_abort_operation(self):
        with patch('reicloud_sync.activity.append', side_effect=OSError('unavailable')):
            with redirect_stderr(io.StringIO()), activity_scope(self.root, 'test', 'sync', False):
                pass
