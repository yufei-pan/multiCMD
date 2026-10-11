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
