import os
import shlex
import shutil
import signal
import subprocess
import sys
import textwrap

import pytest

import multiCMD
from conftest import PACKAGE_ROOT

pytestmark = pytest.mark.skipif(not sys.platform.startswith('linux'), reason='process groups are checked on Linux')


def test_spawn_puts_command_in_its_own_group(launcher):
	proc, pgid = multiCMD._spawn(['sh', '-c', 'ps -o pgid= -p $$'], subprocess.PIPE, subprocess.DEVNULL)
	out, _ = proc.communicate(timeout=10)
	assert pgid == proc.pid
	assert int(out) == proc.pid
	assert int(out) != os.getpgrp()


def test_spawn_missing_command_raises_file_not_found(launcher):
	with pytest.raises(FileNotFoundError):
		multiCMD._spawn(['no-such-command-xyz'], subprocess.PIPE, subprocess.PIPE)


def test_spawn_blocks_tty_signals_in_the_command(launcher):
	# read by the command itself: dash clears the mask before it execs what it runs
	proc, _ = multiCMD._spawn(['grep', 'SigBlk', '/proc/self/status'], subprocess.PIPE, subprocess.DEVNULL)
	out, _ = proc.communicate(timeout=10)
	blocked = int(out.split()[1], 16)
	assert blocked & (1 << (signal.SIGTTIN - 1))
	assert blocked & (1 << (signal.SIGTTOU - 1))


def test_spawn_restores_the_callers_signal_mask(launcher):
	proc, _ = multiCMD._spawn(['true'], subprocess.DEVNULL, subprocess.DEVNULL)
	proc.wait(timeout=10)
	assert signal.SIGTTIN not in signal.pthread_sigmask(signal.SIG_BLOCK, set())


TTY_PROBE = textwrap.dedent(r"""
	import os, signal, subprocess, sys
	sys.path.insert(0, {root!r})
	import multiCMD
	multiCMD._HAS_PROCESS_GROUP = {native!r}
	proc, pgid = multiCMD._spawn(['sh', '-c', 'read x </dev/tty; echo rc=$?'], subprocess.PIPE, subprocess.STDOUT)
	try:
		print('RESULT', proc.communicate(timeout=5)[0].decode().strip())
	except subprocess.TimeoutExpired:
		os.killpg(pgid, signal.SIGKILL)
		print('RESULT STOPPED')
""")


@pytest.mark.skipif(shutil.which('script') is None, reason='needs util-linux script for a controlling terminal')
def test_reading_the_terminal_fails_instead_of_stopping(launcher, tmp_path):
	probe = tmp_path / 'probe.py'
	probe.write_text(TTY_PROBE.format(root=str(PACKAGE_ROOT), native=launcher == 'native'))
	command = f'{shlex.quote(sys.executable)} {shlex.quote(str(probe))}'
	result = subprocess.run(['script', '-qec', command, '/dev/null'],
		stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
	assert 'RESULT rc=1' in result.stdout.replace('\r', '')
