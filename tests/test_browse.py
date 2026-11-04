"""Read-only browse and destructive-cleanup scope checks in temporary profiles."""

import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

import test_setup
from reicloud_sync.config import Paths
from reicloud_sync.workspace import migrate
from reicloud_sync.pairs import find_pair
from reicloud_sync.sync import state_dir, write_state


class BrowseTests(unittest.TestCase):
    invoke = test_setup.SetupTests.invoke

    def setUp(self):
        test_setup.SetupTests.setUp(self)
        self.paths = Paths.discover()
        self.paths.initialize()
        self.local = self.root / 'Dropbox'
        with patch('reicloud_sync.workspace.computer_name', return_value='pc-abcdef'):
            migrate(self.paths, str(self.local))

    def test_browse_reports_namespace_without_making_directories(self):
        rows = [{'Name': '.reicloud-computers', 'IsDir': True}, {'Name': 'notes', 'IsDir': True}]
        with patch('reicloud_sync.cli.rclone', return_value=subprocess.CompletedProcess([], 0, json.dumps(rows))) as run:
            code, output = self.invoke('browse', '--json')
        self.assertEqual(code, 0)
        result = json.loads(output)
        self.assertEqual(result['entries'][0]['selection'], 'excluded-from-workspace')
        self.assertEqual(result['entries'][1]['scope'], 'shared-workspace')
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[1], 'lsjson')
        self.assertFalse(self.local.exists())

