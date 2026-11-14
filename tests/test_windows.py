import ast
import builtins
import io
import subprocess
import sys
import types

import pytest

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


def test_input_countdown_uses_msvcrt_when_present(monkeypatch, fake_clock):
	monkeypatch.setitem(sys.modules, 'msvcrt', types.SimpleNamespace(kbhit=lambda: True))
	monkeypatch.setattr(builtins, 'input', lambda: ' yes ')
	assert multiCMD.input_with_timeout_and_countdown(3, 'pick') == 'yes'
	assert fake_clock.now == 1000.0


def test_input_countdown_runs_out_at_the_end_of_stdin_when_msvcrt_says_a_key_is_waiting(monkeypatch, fake_clock):
	# Ctrl-Z at a console, or a stdin that ended: input() raises EOFError ( 1.64 let it out ); no answer, so the countdown runs
	# out, and the console is asked again each second and once more at the end
	asked = []
	def at_its_end():
		asked.append('input')
		raise EOFError
	def key_waiting():
		asked.append('kbhit')
		return True
	monkeypatch.setitem(sys.modules, 'msvcrt', types.SimpleNamespace(kbhit=key_waiting))
	monkeypatch.setattr(builtins, 'input', at_its_end)
	assert multiCMD.input_with_timeout_and_countdown(3, 'pick') is None
	assert asked == ['kbhit', 'input'] * 4
	assert fake_clock.slept == [1, 1, 1]


def test_input_countdown_on_msvcrt_takes_a_float_timeout(monkeypatch, fake_clock):
	# by the clock ( 1.65: TypeError ): no key, asked at 0, 0.5 and 1.5 s and at the end
	asked = []
	def no_key():
		asked.append(fake_clock.now)
		return False
	monkeypatch.setitem(sys.modules, 'msvcrt', types.SimpleNamespace(kbhit=no_key))
	assert multiCMD.input_with_timeout_and_countdown(2.5, 'pick') is None
	assert asked == [1000.0, 1000.5, 1001.5, 1002.5]
	assert fake_clock.slept == [0.5, 1, 1]


@pytest.mark.parametrize('stdin', ['missing', 'closed'])
def test_input_countdown_on_msvcrt_does_not_ask_a_missing_stdin(monkeypatch, fake_clock, stdin):
	# a key waiting at the console, but no stdin to read it from: input() would raise RuntimeError ( "lost sys.stdin" )
	def no_input():
		raise AssertionError('input() called')
	monkeypatch.setitem(sys.modules, 'msvcrt', types.SimpleNamespace(kbhit=lambda: True))
	monkeypatch.setattr(builtins, 'input', no_input)
	if stdin == 'missing':
		monkeypatch.setattr(sys, 'stdin', None)
	else:
		closed = io.StringIO()
		closed.close()
		monkeypatch.setattr(sys, 'stdin', closed)
	assert multiCMD.input_with_timeout_and_countdown(2, 'pick') is None
	assert fake_clock.slept == [1, 1]


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
