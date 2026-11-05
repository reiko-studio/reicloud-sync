"""Interactive setup decisions with no browser, account or real-home writes."""

from pathlib import Path
from contextlib import ExitStack, contextmanager
import threading
import questionary
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
import unittest
from unittest.mock import Mock, patch

import test_setup
from reicloud_sync.config import Paths
from reicloud_sync.setup_ui import wizard, workspace_error
from reicloud_sync.workspace import migrate


class SetupUITests(unittest.TestCase):
    invoke = test_setup.SetupTests.invoke

    def setUp(self):
        test_setup.SetupTests.setUp(self)
        self.paths = Paths.discover()

    @contextmanager
    def prompts(self, answers):
        with ExitStack() as stack:
            asked = stack.enter_context(patch('reicloud_sync.setup_ui.ask', side_effect=answers))
            for name in ('select', 'path', 'text', 'confirm'):
                stack.enter_context(patch('questionary.' + name, return_value=Mock()))
            yield asked

    def test_blank_wizard_previews_identity_without_writes(self):
        with self.prompts(['Dropbox', str(self.root / 'Dropbox'), 'laptop', False, True]), patch('questionary.print'):
            result = wizard(self.paths)
        self.assertEqual(result.computer, 'laptop')
        self.assertFalse(result.login)
        self.assertFalse(self.paths.config.exists())
        self.assertFalse((self.root / 'Dropbox').exists())

    def test_declining_summary_cancels_without_setup_files(self):
        with self.prompts(['Dropbox', str(self.root / 'Dropbox'), 'laptop', False, False]), patch('questionary.print'):
            with self.assertRaises(KeyboardInterrupt):
                wizard(self.paths)
        self.assertFalse(self.paths.config.exists())

    def test_bare_setup_auto_opens_wizard_in_terminal(self):
        from reicloud_sync.setup_ui import SetupAnswers
        with patch('reicloud_sync.cli.terminal_available', return_value=True), patch('reicloud_sync.cli.setup_wizard', return_value=SetupAnswers(str(self.root / 'Dropbox'), 'laptop', False)) as ui:
            self.assertEqual(self.invoke('setup')[0], 0)
        ui.assert_called_once()
        self.assertEqual(self.paths.load()['version'], 3)
        self.assertFalse((self.root / 'Dropbox').exists())

    def test_flags_and_noninteractive_mode_never_prompt(self):
        with patch('reicloud_sync.cli.terminal_available', return_value=True), patch('reicloud_sync.cli.setup_wizard') as ui:
            self.assertEqual(self.invoke('setup', '--non-interactive')[0], 0)
            self.assertEqual(self.invoke('setup', '--workspace', '~/Dropbox')[0], 0)
        ui.assert_not_called()

    def test_existing_profile_does_not_prompt_for_new_identity(self):
        self.paths.initialize()
        migrate(self.paths, str(self.root / 'Dropbox'))
        before = self.paths.settings.read_bytes()
        with self.prompts([False, True]) as prompts, patch('questionary.path') as path_prompt, patch('questionary.text') as name_prompt, patch('questionary.print'):
            result = wizard(self.paths)
        path_prompt.assert_not_called()
        name_prompt.assert_not_called()
        self.assertIsNone(result.workspace)
        self.assertIsNone(result.computer)
        self.assertEqual(before, self.paths.settings.read_bytes())

    def test_cancellation_in_cli_returns_130_and_creates_nothing(self):
        with patch('reicloud_sync.cli.terminal_available', return_value=True), patch('reicloud_sync.cli.setup_wizard', side_effect=KeyboardInterrupt):
            self.assertEqual(self.invoke('setup')[0], 130)
        self.assertFalse(self.paths.config.exists())

    def test_forced_interactive_requires_a_terminal(self):
        with patch('reicloud_sync.cli.terminal_available', return_value=False):
            code, output = self.invoke('setup', '--interactive')
        self.assertEqual(code, 1)
        self.assertIn('requires a terminal', output)
        self.assertFalse(self.paths.config.exists())

    def test_invalid_workspace_rejected_before_writes(self):
        self.assertIsNot(workspace_error(self.paths, str(self.root)), True)
        file = self.root / 'file'
        file.touch()
        self.assertIsNot(workspace_error(self.paths, str(file)), True)

    def test_actual_prompt_flow_with_terminal_keystrokes(self):
        factories = {name: getattr(questionary, name) for name in ('select', 'path', 'text', 'confirm')}
        target = str(self.root / 'Dropbox')
        with create_pipe_input() as pipe, ExitStack() as stack:
            for name, factory in factories.items():
                def build(*args, _factory=factory, **kwargs):
                    return _factory(*args, input=pipe, output=DummyOutput(), **kwargs)
                stack.enter_context(patch('questionary.' + name, side_effect=build))
            stack.enter_context(patch('questionary.print'))
            timeout = threading.Timer(5, lambda: pipe.send_text('\x03'))
            timeout.start()
            try:
                pipe.send_text('\r\x15' + target + '\r\x15ui-test\rny')
                result = wizard(self.paths)
            finally:
                timeout.cancel()
        self.assertEqual(result.workspace, target)
        self.assertEqual(result.computer, 'ui-test')
        self.assertFalse(result.login)
        self.assertFalse(self.paths.config.exists())
