"""Two independent clients sharing a disposable local Dropbox stand-in."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from reicloud_sync.config import Paths
from reicloud_sync.workspace import migrate
from reicloud_sync.pairs import make_pair, edit_pairs, find_pair
from reicloud_sync.sync import transfer, state_dir, MARKER


class TwoComputerTests(unittest.TestCase):
    def setUp(self):
        if not shutil.which('rclone'):
            self.skipTest('rclone is required')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.account = self.root / 'account'
        self.account.mkdir()
        self.marker(self.account)
        self.clients = []
        for name in ('one', 'two'):
            home = self.root / name
            paths = Paths(home / 'config', home / 'state', home / 'cache')
            paths.initialize()
            workspace = home / 'Dropbox'
            workspace.mkdir()
            self.marker(workspace)
            with patch('reicloud_sync.workspace.computer_name', return_value=f'{name}-abcdef'):
                migrate(paths, str(workspace))
            self.clients.append((paths, workspace))

    def marker(self, root):
        (root / MARKER).write_text('reicloud-sync access check\n')
        os.utime(root / MARKER, (1600000000, 1600000000))

    def run_local(self, paths, *args, capture=False, lock_fd=None):
        if args[0] == 'listremotes':
            return subprocess.CompletedProcess([], 0, '[{"name":"dropbox","type":"dropbox"}]')
        values = [str(self.account / value[8:].lstrip('/')) if value.startswith('dropbox:') else value for value in args]
        result = subprocess.run(['rclone', '--config', str(paths.credentials), *values], text=True, capture_output=True)
        if result.returncode:
            raise subprocess.CalledProcessError(result.returncode, ['rclone'], result.stdout, result.stderr)
        return result

    def run_pair(self, paths, name, operation='sync'):
        try:
            transfer(paths, name, operation, None, False, self.run_local)
        except subprocess.CalledProcessError as error:
            self.fail(error.stderr)

    def test_shared_workspace_and_computer_specific_isolation(self):
        first, local_a = self.clients[0]
        second, local_b = self.clients[1]
        for index in range(10):
            (local_a / f'shared-{index}.txt').write_text(f'shared {index}')
        external = self.root / 'one' / 'results'
        external.mkdir()
        (external / 'private.txt').write_text('computer one only')
        remote = 'dropbox:.reicloud-computers/one-abcdef/results'
        with edit_pairs(first) as pairs:
            pairs.append(make_pair('results', str(external), remote, kind='external'))
        self.run_pair(first, 'results', 'init')
        external_b = self.root / 'two' / 'results'
        external_b.mkdir()
        (external_b / 'private.txt').write_text('computer two only')
        with edit_pairs(second) as pairs:
            pairs.append(make_pair('results', str(external_b), 'dropbox:.reicloud-computers/two-abcdef/results', kind='external'))
        self.run_pair(second, 'results', 'init')
        self.run_pair(first, 'workspace', 'init')
        self.run_pair(second, 'workspace', 'init')
        self.assertEqual((local_b / 'shared-0.txt').read_text(), 'shared 0')
        self.assertTrue((self.account / '.reicloud-computers/one-abcdef/results/private.txt').exists())
        self.assertFalse((local_a / '.reicloud-computers').exists())
        self.assertFalse((local_b / '.reicloud-computers').exists())
        (local_b / 'from-two.txt').write_text('two')
        self.run_pair(second, 'workspace')
        self.run_pair(first, 'workspace')
        self.assertEqual((local_a / 'from-two.txt').read_text(), 'two')
        (local_b / 'from-two.txt').unlink()
        self.run_pair(second, 'workspace')
        self.run_pair(first, 'workspace')
        self.assertFalse((local_a / 'from-two.txt').exists())
        (local_a / 'shared-0.txt').write_text('edited on first computer')
        os.utime(local_a / 'shared-0.txt', (1700000100, 1700000100))
        self.run_pair(first, 'workspace')
        self.run_pair(second, 'workspace')
        self.assertEqual((local_b / 'shared-0.txt').read_text(), 'edited on first computer')
        (local_a / 'shared-1.txt').write_text('competing edit on first')
        (local_b / 'shared-1.txt').write_text('different competing edit on second')
        os.utime(local_a / 'shared-1.txt', (1700000200, 1700000200))
        os.utime(local_b / 'shared-1.txt', (1700000300, 1700000300))
        self.run_pair(first, 'workspace')
        self.run_pair(second, 'workspace')
        self.run_pair(first, 'workspace')
        for local in (local_a, local_b):
            contents = [file.read_text() for file in local.glob('shared-1.txt*')]
            self.assertIn('competing edit on first', contents)
            self.assertIn('different competing edit on second', contents)
        self.assertEqual((self.account / '.reicloud-computers/two-abcdef/results/private.txt').read_text(), 'computer two only')
        self.assertNotEqual(state_dir(first, find_pair(first, 'workspace')), state_dir(second, find_pair(second, 'workspace')))
