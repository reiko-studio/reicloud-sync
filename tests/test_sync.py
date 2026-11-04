"""Run real rclone against disposable local trees, never a cloud account."""

import json
from pathlib import Path
import shutil
import subprocess
import unittest
from unittest.mock import patch

import test_setup
from reicloud_sync.config import Paths
from reicloud_sync.pairs import find_pair
from reicloud_sync.sync import MARKER, pair_lock, state_dir, read_state, write_state


class SyncTests(unittest.TestCase):
    invoke = test_setup.SetupTests.invoke

    def setUp(self):
        test_setup.SetupTests.setUp(self)
        if not shutil.which('rclone'):
            self.skipTest('rclone is required for transfer integration tests')
        self.paths = Paths.discover()
        self.paths.initialize()
        self.local = self.root / 'local'
        self.remote = self.root / 'remote'
        self.local.mkdir()
        self.remote.mkdir()
        self.assertEqual(self.invoke('add', 'test', str(self.local), 'dropbox:Test')[0], 0)
        self.pair = find_pair(self.paths, 'test')
        self.directory = state_dir(self.paths, self.pair)
        self.calls = []
        self.mock = patch('reicloud_sync.cli.rclone', side_effect=self.run_local)
        self.mock.start()
        self.addCleanup(self.mock.stop)

    def run_local(self, paths, *args, capture=False, lock_fd=None):
        self.calls.append(args)
        if args[0] == 'listremotes':
            return subprocess.CompletedProcess([], 0, '[{"name":"dropbox","type":"dropbox"}]')
        replaced = [str(self.remote) + value[len('dropbox:Test'):] if value.startswith('dropbox:Test') else value for value in args]
        result = subprocess.run(['rclone', '--config', str(paths.credentials), *replaced],
                                text=True, capture_output=True,
                                pass_fds=() if lock_fd is None else (lock_fd,))
        if result.returncode:
            raise subprocess.CalledProcessError(result.returncode, ['rclone'], result.stdout, result.stderr)
        return result

    def initialize(self):
        (self.local / 'local.txt').write_text('local initial')
        (self.remote / 'remote.txt').write_text('remote initial')
        for index in range(20):
            (self.local / f'keep-{index}.txt').write_text('unchanged')
        self.assertSuccess('init', 'test', '--prefer', 'local')

    def assertSuccess(self, *args):
        code, output = self.invoke(*args)
        self.assertEqual(code, 0, output)

    def saved_status(self):
        return json.loads(self.invoke('status', 'test', '--json')[1])

    def snapshot(self, root):
        return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}

    def test_initialize_upload_download_and_restarted_sync(self):
        self.initialize()
        self.assertEqual(self.snapshot(self.local), self.snapshot(self.remote))
        self.assertEqual(self.saved_status()['phase'], 'ready')
        (self.local / 'upload.txt').write_text('upload')
        (self.remote / 'download.txt').write_text('download')
        (self.local / 'local.txt').write_text('local edit longer')
        (self.remote / 'remote.txt').write_text('remote edit longer')
        self.assertSuccess('sync', 'test')
        self.assertEqual(self.snapshot(self.local), self.snapshot(self.remote))
        self.assertEqual((self.remote / 'local.txt').read_text(), 'local edit longer')
        self.assertEqual((self.local / 'remote.txt').read_text(), 'remote edit longer')
        (self.local / 'upload.txt').unlink()
        (self.remote / 'download.txt').unlink()
        self.assertSuccess('sync', 'test')
        self.assertFalse((self.remote / 'upload.txt').exists())
        self.assertFalse((self.local / 'download.txt').exists())
        bisync = [call for call in self.calls if call[0] == 'bisync']
        self.assertIn('--resync-mode', bisync[0])
        self.assertTrue(all('--resync-mode' not in call for call in bisync[1:]))

    def test_sync_preview_preserves_history_and_trees(self):
        self.initialize()
        (self.local / 'new.txt').write_text('preview me')
        before = [self.snapshot(root) for root in (self.local, self.remote, self.directory)]
        self.assertSuccess('sync', 'test', '--dry-run')
        self.assertEqual(before, [self.snapshot(root) for root in (self.local, self.remote, self.directory)])

    def test_conflicts_preserve_both_versions(self):
        self.initialize()
        (self.local / 'local.txt').write_text('competing local edit')
        (self.remote / 'local.txt').write_text('competing remote edit of different size')
        self.assertSuccess('sync', 'test')
        for root in (self.local, self.remote):
            contents = [p.read_text() for p in root.glob('local.txt*')]
            self.assertIn('competing local edit', contents)
            self.assertIn('competing remote edit of different size', contents)

    def test_missing_remote_has_actionable_message_without_transfer(self):
        self.remote.rmdir()
        code, output = self.invoke('init', 'test', '--prefer', 'local')
        self.assertEqual(code, 1)
        self.assertIn('Create it in Dropbox', output)
        self.assertFalse(any(call[0] in ('bisync', 'copyto') for call in self.calls))

    def test_missing_marker_blocks_sync_and_requires_deliberate_recovery(self):
        self.initialize()
        success = self.saved_status()['last_success']
        (self.remote / MARKER).unlink()
        code, _ = self.invoke('sync', 'test')
        self.assertEqual(code, 1)
        self.assertEqual(self.saved_status()['phase'], 'needs_recovery')
        self.assertEqual(self.saved_status()['last_success'], success)
        count = len(self.calls)
        self.assertEqual(self.invoke('sync', 'test')[0], 1)
        self.assertEqual(len(self.calls), count)
        self.assertSuccess('recover', 'test', '--prefer', 'local', '--dry-run')
        self.assertEqual(self.saved_status()['phase'], 'needs_recovery')
        self.assertSuccess('recover', 'test', '--prefer', 'local')
        self.assertTrue(list(self.directory.glob('bisync-before-recovery-*')))
        self.assertEqual(self.saved_status()['phase'], 'ready')
        self.assertSuccess('sync', 'test')

    def test_mass_deletion_guard(self):
        self.initialize()
        for file in self.local.glob('keep-*'):
            file.unlink()
        self.assertEqual(self.invoke('sync', 'test')[0], 1)
        self.assertEqual(len(list(self.remote.glob('keep-*'))), 20)
        self.assertEqual(self.saved_status()['phase'], 'retryable_error')

    def test_locks_and_init_guard(self):
        with pair_lock(self.directory):
            self.assertEqual(self.invoke('init', 'test', '--prefer', 'local')[0], 1)
            self.assertTrue(self.saved_status()['active'])
        self.initialize()
        before = self.snapshot(self.directory)
        self.assertEqual(self.invoke('init', 'test', '--prefer', 'local')[0], 1)
        self.assertEqual(before, self.snapshot(self.directory))

    def test_lost_history_requires_recovery(self):
        self.initialize()
        shutil.rmtree(self.directory / 'bisync')
        self.assertEqual(self.invoke('sync', 'test')[0], 1)
        self.assertEqual(self.saved_status()['phase'], 'needs_recovery')
        self.assertSuccess('recover', 'test', '--prefer', 'local')
        self.assertSuccess('sync', 'test')

    def test_interrupt_is_persisted_and_not_retried(self):
        self.initialize()
        original = self.run_local
        def interrupt(paths, *args, **kwargs):
            if args[0] == 'bisync':
                raise KeyboardInterrupt()
            return original(paths, *args, **kwargs)
        with patch('reicloud_sync.cli.rclone', side_effect=interrupt):
            self.assertEqual(self.invoke('sync', 'test')[0], 130)
        self.assertEqual(self.saved_status()['phase'], 'needs_recovery')
        self.assertEqual(self.invoke('sync', 'test')[0], 1)

    def test_state_is_namespaced_by_configuration(self):
        other = Paths(self.root / 'other-config', self.paths.state, self.paths.cache)
        self.assertNotEqual(state_dir(self.paths, self.pair), state_dir(other, self.pair))

    def test_stale_running_record_is_reported_as_interrupted(self):
        self.initialize()
        state = read_state(self.directory)
        state['phase'] = 'running'
        write_state(self.directory, state)
        self.assertEqual(self.saved_status()['phase'], 'needs_recovery')
        self.assertFalse(self.saved_status()['active'])
        self.assertEqual(self.invoke('sync', 'test')[0], 1)
        self.assertSuccess('recover', 'test', '--prefer', 'local')

    def test_missing_local_root_does_not_propagate_deletions(self):
        self.initialize()
        before = self.snapshot(self.remote)
        self.local.rename(self.root / 'temporarily-unmounted')
        self.assertEqual(self.invoke('sync', 'test')[0], 1)
        self.assertEqual(before, self.snapshot(self.remote))

    def test_small_folder_deletion_and_legacy_retry(self):
        (self.local / 'one.txt').write_text('one')
        (self.remote / 'two.txt').write_text('two')
        self.assertSuccess('init', 'test', '--prefer', 'local')
        (self.local / 'two.txt').unlink()
        # Reproduce the former implementation's incorrect recovery lockout.
        state = read_state(self.directory)
        state.update(phase='needs_recovery', operation='sync', error='Operation failed (CalledProcessError)')
        state.pop('exit_code', None)
        write_state(self.directory, state)
        self.assertSuccess('sync', 'test', '--retry', '--dry-run')
        self.assertTrue((self.remote / 'two.txt').exists())
        self.assertSuccess('sync', 'test', '--retry')
        self.assertFalse((self.remote / 'two.txt').exists())
        self.assertEqual(self.saved_status()['phase'], 'ready')
        self.assertTrue(all('--resync-mode' not in call for call in [c for c in self.calls if c[0] == 'bisync'][1:]))

    def test_restore_after_deletion_abort_can_retry_without_recovery(self):
        self.initialize()
        original = self.snapshot(self.local)
        success = self.saved_status()['last_success']
        for file in self.local.glob('keep-*'):
            file.unlink()
        self.assertEqual(self.invoke('sync', 'test')[0], 1)
        self.assertEqual(self.saved_status()['phase'], 'retryable_error')
        self.assertEqual(self.saved_status()['exit_code'], 1)
        self.assertEqual(self.saved_status()['last_success'], success)
        for name, content in original.items():
            if not (self.local / name).exists():
                (self.local / name).write_bytes(content)
        self.assertSuccess('sync', 'test')
        self.assertEqual(self.snapshot(self.local), self.snapshot(self.remote))

    def test_explicit_deletion_limit_preserves_history(self):
        self.initialize()
        for file in self.local.glob('keep-*'):
            file.unlink()
        self.assertEqual(self.invoke('sync', 'test')[0], 1)
        self.assertSuccess('sync', 'test', '--dry-run', '--max-delete', '100')
        self.assertEqual(len(list(self.remote.glob('keep-*'))), 20)
        self.assertSuccess('sync', 'test', '--max-delete', '100')
        self.assertEqual(len(list(self.remote.glob('keep-*'))), 0)
        syncs = [call for call in self.calls if call[0] == 'bisync'][1:]
        self.assertTrue(all('--resync-mode' not in call and '--force' not in call for call in syncs))
