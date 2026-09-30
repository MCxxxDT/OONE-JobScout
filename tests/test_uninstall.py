"""Uninstaller tests use disposable installations, never the user's app/data."""
import ast
import contextlib
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from boss_apply import uninstall as u


def fixture(path):
    path.mkdir(parents=True)
    (path / 'boss_apply').mkdir()
    (path / 'boss_apply/config.py').write_text('')
    (path / 'pyproject.toml').write_text('[project]\nname = "oone-jobscout"\n')
    (path / 'start.sh').write_text('')
    (path / 'start.bat').write_text('')
    (path / 'state/browser-profile').mkdir(parents=True)
    (path / 'state/browser-profile/Cookies').write_text('fixture cookies')
    (path / 'config.local.json').write_text('fixture key')
    return path


class UninstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='oone test 中文 ')
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.root = fixture(self.base / 'OONE JobScout 中文')
        self.home = self.base / 'home'
        self.home.mkdir()
        self.home_patch = patch.object(Path, 'home', return_value=self.home)
        self.home_patch.start()
        self.addCleanup(self.home_patch.stop)

    def test_root_and_foreign_directory_guards(self):
        for path in (self.home, self.base, Path('/'), Path(sys.base_prefix)):
            with self.subTest(path=path), self.assertRaises(u.UninstallError):
                u.checkout(path)
        if os.name != 'nt':
            link = self.base / 'linked root'
            link.symlink_to(self.root, target_is_directory=True)
            with self.assertRaises(u.UninstallError):
                u.checkout(link)

    def test_complete_delete_preserves_shared_symlink_target(self):
        shared = self.base / 'shared venv'
        shared.mkdir()
        (shared / 'keep.txt').write_text('keep')
        if os.name != 'nt':
            (self.root / '.venv').symlink_to(shared, target_is_directory=True)
        plan = u.make_plan(self.root)
        with patch.object(u, 'processes', return_value=[]), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(u.execute(plan, {os.getpid()}), 0)
        self.assertFalse(self.root.exists())
        self.assertEqual((shared / 'keep.txt').read_text(), 'keep')

    def test_legacy_requires_explicit_opt_in_and_reports_incomplete(self):
        old = self.home / 'chrome-cdp-profile'
        old.mkdir()
        (old / 'Cookies').write_text('legacy')
        plan = u.make_plan(self.root)
        self.assertNotIn(str(old), [t.path for t in plan.targets])
        with patch.object(u, 'processes', return_value=[]), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(u.execute(plan, {os.getpid()}), 2)
        self.assertTrue(old.exists())

    def test_legacy_link_is_unlinked_without_deleting_destination(self):
        if os.name == 'nt':
            self.skipTest('POSIX symlink test')
        real = self.base / 'ordinary browser'
        real.mkdir()
        (real / 'Cookies').write_text('keep')
        old = self.home / 'chrome-cdp-profile'
        old.symlink_to(real, target_is_directory=True)
        plan = u.make_plan(self.root, legacy=True)
        with patch.object(u, 'processes', return_value=[]), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(u.execute(plan, {os.getpid()}), 0)
        self.assertFalse(os.path.lexists(old))
        self.assertTrue((real / 'Cookies').exists())

    def test_replaced_target_is_rejected_before_stopping_processes(self):
        plan = u.make_plan(self.root)
        self.root.rename(self.base / 'old')
        fixture(self.root)
        with patch.object(u, 'stop_processes') as stop:
            with self.assertRaises(u.UninstallError):
                u.execute(plan, {os.getpid()})
            stop.assert_not_called()
        self.assertTrue(self.root.exists())

    def test_dry_run_keeps_files_and_does_not_spawn_worker(self):
        with patch.object(u, 'processes', return_value=[]), patch.object(u, 'worker') as worker:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(u.main(['--root', str(self.root), '--dry-run']), 0)
            worker.assert_not_called()
        self.assertTrue((self.root / 'config.local.json').exists())

    def test_cancel_and_noninteractive_require_yes(self):
        with patch.object(u, 'processes', return_value=[]), patch.object(u.sys.stdin, 'isatty', return_value=True), patch('builtins.input', return_value='NO'), patch.object(u, 'worker') as worker, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(u.main(['--root', str(self.root)]), 0)
            worker.assert_not_called()
        with patch.object(u, 'processes', return_value=[]), patch.object(u.sys.stdin, 'isatty', return_value=False), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(u.main(['--root', str(self.root)]), 1)
        self.assertTrue(self.root.exists())

    def test_process_ownership_uses_profile_or_module_and_scope(self):
        plan = u.make_plan(self.root)
        profile = str(self.root / 'state/browser-profile')
        make = lambda args, cwd='': u.Process(123, 1, 'started', tuple(args), cwd)
        self.assertTrue(u.owned(make(['chrome', '--user-data-dir=' + profile]), plan))
        self.assertTrue(u.owned(make(['chrome', '--user-data-dir=' + str(self.root / 'state/console-profile')]), plan))
        self.assertFalse(u.owned(make(['chrome', '--remote-debugging-port=9335']), plan))
        self.assertFalse(u.owned(make(['chrome', '--user-data-dir=' + profile + '-other']), plan))
        self.assertTrue(u.owned(make(['python', '-m', 'boss_apply.web_server'], str(self.root)), plan))
        self.assertFalse(u.owned(make(['python', '-m', 'boss_apply.web_server'], str(self.base)), plan))
        self.assertTrue(u.owned(make(['python', str(self.root / 'scripts/daemon_auto_reply.py')]), plan))
        self.assertFalse(u.owned(make(['python', str(self.base / 'other/scripts/daemon_auto_reply.py')]), plan))

    def test_external_sensitive_link_reports_remaining_data(self):
        if os.name == 'nt':
            self.skipTest('POSIX symlink test')
        external = self.base / 'external state'
        (self.root / 'state').rename(external)
        (self.root / 'state').symlink_to(external, target_is_directory=True)
        plan = u.make_plan(self.root)
        with patch.object(u, 'processes', return_value=[]), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(u.execute(plan, {os.getpid()}), 2)
        self.assertTrue((external / 'browser-profile/Cookies').exists())

    def test_unscoped_module_is_not_guessed_or_silently_ignored(self):
        plan = u.make_plan(self.root)
        unknown = u.Process(33, 1, 'start', ('python', '-m', 'boss_apply.web_server'))
        with patch.object(u, 'processes', return_value=[unknown]):
            with self.assertRaises(u.UninstallError):
                u.matches(plan, set())
        self.assertTrue(self.root.exists())

    def test_latest_console_launcher_uses_dedicated_visible_profile(self):
        module = ast.parse((u.SOURCE / 'scripts/approval_web.py').read_text(encoding='utf-8'))
        function = next(n for n in module.body if isinstance(n, ast.FunctionDef) and
                        n.name == '_launch_browser_when_ready')
        namespace = {'os': os, 'cfgmod': SimpleNamespace(STATE_DIR=str(self.root / 'state'))}
        exec(compile(ast.Module(body=[function], type_ignores=[]), '<console launcher>', 'exec'), namespace)
        with patch('socket.create_connection'), patch('subprocess.Popen') as spawn, patch('os.path.exists', return_value=True), patch('os.makedirs') as mkdir:
            namespace['_launch_browser_when_ready'](8788, 'fixture-token')
        expected = str(self.root / 'state/console-profile')
        self.assertIn('--user-data-dir=' + expected, spawn.call_args.args[0])
        mkdir.assert_called_once_with(expected, exist_ok=True)

    def test_stop_descendants_and_exclude_uninstaller(self):
        plan = u.make_plan(self.root)
        parent = u.Process(10, 1, 'start', ('python', '-m', 'boss_apply.web_server'), str(self.root))
        child = u.Process(11, 10, 'start', ('python', '-m', 'boss_apply.web_server'))
        unrelated = u.Process(12, 1, 'start', ('cloudflared', 'tunnel'))
        with patch.object(u, 'processes', return_value=[parent, child, unrelated]):
            self.assertEqual({p.pid for p in u.matches(plan, set())}, {10, 11})
            self.assertEqual(u.matches(plan, {10}), [])

    def test_pid_reuse_is_not_killed(self):
        old = u.Process(123, 1, 'old', ('python',))
        current = u.Process(123, 1, 'new', ('chrome',))
        with patch.object(u, 'processes', return_value=[current]), patch.object(os, 'kill') as kill:
            u.terminate(old, force=True)
            kill.assert_not_called()

    def test_process_failure_and_locked_file_never_report_success(self):
        plan = u.make_plan(self.root)
        with patch.object(u, 'processes', side_effect=u.UninstallError('cannot read processes')):
            with self.assertRaises(u.UninstallError):
                u.execute(plan, {os.getpid()})
        self.assertTrue(self.root.exists())
        with patch.object(u, 'processes', return_value=[]), patch.object(u, 'remove', side_effect=PermissionError('locked')), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(u.execute(plan, {os.getpid()}), 1)
        self.assertTrue(self.root.exists())

    @unittest.skipUnless(shutil.which('git'), 'requires git')
    def test_external_and_nested_git_worktrees_are_included(self):
        def git(*args):
            subprocess.run(['git', '-C', str(self.root), *args], check=True, capture_output=True)
        git('init')
        git('add', '.')
        git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-m', 'fixture')
        external = self.base / 'external worktree 中文'
        nested = self.root / '.worktrees/nested'
        git('worktree', 'add', '--detach', str(external))
        git('worktree', 'add', '--detach', str(nested))
        plan = u.make_plan(nested)
        self.assertEqual(set(plan.roots), {str(self.root), str(external), str(nested)})
        with patch.object(u, 'processes', return_value=[]), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(u.execute(plan, {os.getpid()}), 0)
        self.assertFalse(external.exists())
        self.assertFalse(self.root.exists())

    def test_uv_uses_package_manager_and_package_only_cache_cleanup(self):
        tool = self.base / 'tools/oone-jobscout'
        tool.mkdir(parents=True)
        (tool / 'uv-receipt.toml').write_text('fixture')
        commands = []
        def fake_run(args, timeout=30):
            commands.append(args)
            if args[-2:] == ['tool', 'dir']:
                return str(tool.parent)
            if args[-2:] == ['tool', 'list']:
                return 'oone-jobscout v1.0.0' if tool.exists() else ''
            if 'uninstall' in args:
                shutil.rmtree(tool)
            return ''
        with patch.object(shutil, 'which', return_value='/test/uv'), patch.object(u, 'run', side_effect=fake_run), patch.object(u, 'processes', return_value=[]), contextlib.redirect_stdout(io.StringIO()):
            plan = u.make_plan(self.root, uv_tools=True)
            self.assertEqual(u.execute(plan, {os.getpid()}), 2)
        self.assertIn(['/test/uv', '--no-config', 'tool', 'uninstall', 'oone-jobscout'], commands)
        self.assertIn(['/test/uv', '--no-config', 'cache', 'clean', 'oone-jobscout'], commands)
        self.assertFalse(tool.exists())


if __name__ == '__main__':
    unittest.main()
