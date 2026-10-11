import ast
import builtins
import subprocess
import sys
import types

import multiCMD
from conftest import PACKAGE_ROOT


def _capture_commands(monkeypatch):
	seen = []
	def fake_run_commands(commands, **kwargs):
		seen.extend(commands)
		return [0] * len(commands)
	monkeypatch.setattr(multiCMD, 'run_commands', fake_run_commands)
	return seen


def test_ping_uses_dash_n_on_windows(monkeypatch):
	seen = _capture_commands(monkeypatch)
	monkeypatch.setattr(multiCMD, '_IS_POSIX', False)
	assert multiCMD.ping(['h1', 'h2']) == [True, True]
	assert seen == ['ping -n 1 h1', 'ping -n 1 h2']


def test_ping_uses_dash_c_on_posix(monkeypatch):
	seen = _capture_commands(monkeypatch)
	multiCMD.ping('h1')
	assert seen == ['ping -c 1 h1']


class FakeProc:
	'''A Windows Popen: terminate() and kill(), no POSIX signals'''
	def __init__(self, dies_on_terminate):
		self.dies_on_terminate = dies_on_terminate
		self.calls = []
		self.returncode = None

	def poll(self):
		return self.returncode

	def terminate(self):
		self.calls.append('terminate')
		if self.dies_on_terminate:
			self.returncode = 1

	def kill(self):
		self.calls.append('kill')
		self.returncode = 9

	def wait(self, timeout=None):
		if self.returncode is None:
			raise subprocess.TimeoutExpired('fake', timeout)
		return self.returncode

	def send_signal(self, sig):
		raise AssertionError('no POSIX signals on Windows')


def test_kill_commands_on_windows_terminates_then_kills(monkeypatch):
	monkeypatch.setattr(multiCMD, '_IS_POSIX', False)
	polite, stubborn = FakeProc(True), FakeProc(False)
	multiCMD._kill_commands([(polite, None), (stubborn, None)], 2)
	assert polite.calls == ['terminate']
	assert stubborn.calls == ['terminate', 'kill']


def test_spawn_on_windows_has_no_group(monkeypatch):
	monkeypatch.setattr(multiCMD, '_IS_POSIX', False)
	proc, pgid = multiCMD._spawn(['true'], subprocess.DEVNULL, subprocess.DEVNULL)
	proc.wait(timeout=10)
	assert pgid is None


def test_input_countdown_uses_msvcrt_when_present(monkeypatch):
	monkeypatch.setitem(sys.modules, 'msvcrt', types.SimpleNamespace(kbhit=lambda: True))
	monkeypatch.setattr(builtins, 'input', lambda: ' yes ')
	assert multiCMD.input_with_timeout_and_countdown(3, 'pick') == 'yes'


def test_posix_only_modules_are_imported_lazily():
	tree = ast.parse((PACKAGE_ROOT / 'multiCMD.py').read_text())
	top_level = set()
	for node in tree.body:
		if isinstance(node, ast.Import):
			top_level.update(alias.name for alias in node.names)
		elif isinstance(node, ast.ImportFrom):
			top_level.add(node.module)
	assert not top_level & {'pty', 'termios', 'fcntl', 'msvcrt'}


def test_cli_leaves_pty_mode_off_without_posix(monkeypatch):
	seen = {}
	def fake_run_commands(commands, **kwargs):
		seen.update(kwargs)
		task = multiCMD.Task(['true'])
		task.returncode = 0
		return [task]
	monkeypatch.setattr(multiCMD, 'run_commands', fake_run_commands)
	monkeypatch.setattr(multiCMD, '_install_cli_signal_handlers', lambda: None)
	monkeypatch.setattr(multiCMD, '_IS_POSIX', False)
	monkeypatch.setattr(multiCMD, '_stdout_isatty', lambda: True)
	monkeypatch.setattr(sys, 'argv', ['mcmd', 'true'])
	assert multiCMD.main() == 0
	assert seen['pty'] is False


def test_frozen_executable_is_never_used_as_the_exec_wrapper(monkeypatch):
	# a PyInstaller-style app would receive "-I -S -c ..." as its own arguments
	monkeypatch.setattr(multiCMD, '_HAS_PROCESS_GROUP', False)
	monkeypatch.setattr(sys, 'frozen', True, raising=False)
	proc, pgid = multiCMD._spawn(['sh', '-c', 'echo $0'], subprocess.PIPE, subprocess.DEVNULL)
	out, _ = proc.communicate(timeout=10)
	assert out.strip() == b'sh'
	assert pgid is None
