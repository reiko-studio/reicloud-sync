"""Desktop incident handling with mocked notification delivery."""

import unittest
from unittest.mock import patch
import test_setup
from reicloud_sync.config import Paths
from reicloud_sync.pairs import find_pair
from reicloud_sync.notifications import configure, attention, recovered, enabled, _seen


class NotificationTests(unittest.TestCase):
    invoke = test_setup.SetupTests.invoke

    def setUp(self):
        test_setup.SetupTests.setUp(self)
        self.paths = Paths.discover()
        self.paths.initialize()
        local = self.root / 'notes'
        local.mkdir()
        self.assertEqual(self.invoke('add', 'notes', str(local), 'dropbox:Notes')[0], 0)
        self.pair = find_pair(self.paths, 'notes')

    def test_default_disabled_and_persistent_opt_in(self):
        self.assertFalse(enabled(self.paths))
        configure(self.paths, True)
        self.assertTrue(enabled(self.paths))
        configure(self.paths, False)
        self.assertFalse(enabled(self.paths))

    def test_incident_dedup_survives_restart_and_success_rearms(self):
        configure(self.paths, True)
        state = {'failure_kind': 'safety', 'error': 'too many deletes'}
        with patch('reicloud_sync.notifications.send') as send:
            attention(self.paths, self.pair, state)
            _seen.clear()
            attention(self.paths, self.pair, state)
            self.assertEqual(send.call_count, 1)
            recovered(self.paths, self.pair)
            self.assertEqual(send.call_count, 2)
            attention(self.paths, self.pair, state)
            self.assertEqual(send.call_count, 3)

    def test_failed_delivery_does_not_break_sync_or_spam(self):
        configure(self.paths, True)
        with patch('reicloud_sync.notifications.send', side_effect=OSError('no desktop')) as send:
            attention(self.paths, self.pair, {'error': 'failure'})
            attention(self.paths, self.pair, {'error': 'failure'})
            self.assertEqual(send.call_count, 1)

    def test_test_command_does_not_enable_notifications(self):
        with patch('reicloud_sync.cli.send_notification') as send:
            self.assertEqual(self.invoke('notifications', 'test')[0], 0)
        send.assert_called_once()
        self.assertFalse(enabled(self.paths))
