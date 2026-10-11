import os
import struct

import pytest

import multiCMD
from multiCMD import run_command

pytestmark = pytest.mark.skipif(os.name != 'posix', reason='pty mode is POSIX only')

TTY_CHECK = ['sh', '-c', '[ -t 1 ] && echo tty || echo pipe']


def test_pty_command_sees_a_terminal():
	assert run_command(TTY_CHECK, quiet=True, pty=True) == ['tty']
	assert run_command(TTY_CHECK, quiet=True) == ['pipe']


def test_pty_merges_stderr_into_stdout():
	task = run_command(['sh', '-c', 'echo out; echo err >&2'], quiet=True, pty=True, return_object=True)
	assert task.stdout == ['out', 'err']
	assert task.stderr == []
	assert task._pty is True


def test_pty_sets_pager_to_cat():
	assert run_command(['sh', '-c', 'echo "$PAGER $GIT_PAGER"'], quiet=True, pty=True) == ['cat cat']


def test_pty_turns_off_output_processing():
	import termios
	master, slave = multiCMD._open_pty(0)
	try:
		assert not termios.tcgetattr(slave)[1] & termios.OPOST
	finally:
		os.close(master)
		os.close(slave)


def test_pty_window_size_leaves_room_for_the_prefix(monkeypatch):
	import fcntl
	import termios
	monkeypatch.setattr(multiCMD, 'get_terminal_size', lambda: (100, 40))
	master, slave = multiCMD._open_pty(4)
	try:
		rows, columns = struct.unpack('HHHH', fcntl.ioctl(slave, termios.TIOCGWINSZ, b'\0' * 8))[:2]
		assert (rows, columns) == (40, 96)
	finally:
		os.close(master)
		os.close(slave)


def test_pty_falls_back_to_pipes_when_none_can_be_opened(monkeypatch, capsys):
	import pty
	def no_pty():
		raise OSError('out of ptys')
	monkeypatch.setattr(pty, 'openpty', no_pty)
	monkeypatch.setattr(multiCMD, '_PTY_WARNED', False)
	assert run_command(TTY_CHECK, quiet=True, pty=True) == ['pipe']
	assert 'cannot open a pty' in capsys.readouterr().err


def test_pty_is_ignored_with_a_warning_off_posix(monkeypatch, capsys):
	monkeypatch.setattr(multiCMD, '_IS_POSIX', False)
	monkeypatch.setattr(multiCMD, '_PTY_WARNED', False)
	assert multiCMD._resolve_pty(True) is False
	assert 'needs a POSIX system' in capsys.readouterr().err


@pytest.mark.slow
def test_pty_timeout_still_kills():
	task = run_command(['sh', '-c', 'echo hi; sleep 10'], quiet=True, pty=True, timeout=1, return_object=True)
	assert task.returncode == 124
	assert task.stdout == ['hi']
