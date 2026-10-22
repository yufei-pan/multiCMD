import os
import shlex
import shutil
import signal
import subprocess
import sys
import textwrap
import threading
import time

import pytest

import multiCMD
from conftest import PACKAGE_ROOT, pid_gone

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
		stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=60)
	assert 'RESULT rc=1' in result.stdout.replace('\r', '')


def _state(pid):
	try:
		with open(f'/proc/{pid}/stat') as f:
			return f.read().rsplit(')', 1)[1].split()[0]
	except OSError:
		return '?'


def test_kill_commands_reaches_grandchildren(launcher):
	proc, pgid = multiCMD._spawn(['sh', '-c', 'sleep 30 & echo $!; wait'], subprocess.PIPE, subprocess.DEVNULL)
	child = int(proc.stdout.readline())
	start = time.monotonic()
	multiCMD._kill_commands([(proc, pgid)], signal.SIGINT)
	assert proc.poll() is not None
	assert pid_gone(child)
	assert time.monotonic() - start < 3
	proc.stdout.close()


def test_kill_commands_continues_stopped_commands(launcher):
	proc, pgid = multiCMD._spawn(['sh', '-c', 'kill -STOP $$; sleep 30'], subprocess.DEVNULL, subprocess.DEVNULL)
	deadline = time.monotonic() + 5
	while _state(proc.pid) != 'T' and time.monotonic() < deadline:
		time.sleep(0.02)
	assert _state(proc.pid) == 'T'
	multiCMD._kill_commands([(proc, pgid)], signal.SIGINT)
	assert proc.poll() is not None


def test_kill_commands_escalates_to_sigkill(launcher):
	proc, pgid = multiCMD._spawn(['sh', '-c', 'trap "" INT TERM; echo ready; while :; do sleep 0.1; done'],
		subprocess.PIPE, subprocess.DEVNULL)
	proc.stdout.readline()
	start = time.monotonic()
	multiCMD._kill_commands([(proc, pgid)], signal.SIGINT)
	elapsed = time.monotonic() - start
	assert proc.returncode == -signal.SIGKILL
	assert 0.9 < elapsed < 3
	proc.stdout.close()


def test_kill_all_stops_registered_commands():
	proc, pgid = multiCMD._spawn(['sh', '-c', 'sleep 30 & echo $!; wait'], subprocess.PIPE, subprocess.DEVNULL)
	multiCMD._register(proc, pgid)
	try:
		child = int(proc.stdout.readline())
		multiCMD.kill_all()
		assert proc.poll() is not None
		assert pid_gone(child)
	finally:
		multiCMD._unregister(proc)
		proc.stdout.close()


def test_kill_all_with_nothing_running_returns_at_once():
	start = time.monotonic()
	multiCMD.kill_all()
	assert time.monotonic() - start < 0.5


EXIT_PROBE = textwrap.dedent(r"""
	import subprocess, sys
	sys.path.insert(0, {root!r})
	import multiCMD
	proc, pgid = multiCMD._spawn(['sh', '-c', 'sleep 30 & echo $!; wait'], subprocess.PIPE, subprocess.DEVNULL)
	multiCMD._register(proc, pgid)
	print(proc.pid, proc.stdout.readline().decode().strip(), flush=True)
""")


def test_kill_all_runs_when_the_interpreter_exits(tmp_path):
	probe = tmp_path / 'probe.py'
	probe.write_text(EXIT_PROBE.format(root=str(PACKAGE_ROOT)))
	out = subprocess.run([sys.executable, str(probe)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=30).stdout
	leader, child = (int(pid) for pid in out.split())
	assert pid_gone(leader)
	assert pid_gone(child)


def test_spawn_restores_default_sigpipe_and_sigxfsz(launcher):
	# the exec wrapper is a Python interpreter, which ignores SIGPIPE and SIGXFSZ; commands must not inherit that
	proc, _ = multiCMD._spawn(['grep', 'SigIgn', '/proc/self/status'], subprocess.PIPE, subprocess.DEVNULL)
	out, _ = proc.communicate(timeout=10)
	ignored = int(out.split()[1], 16)
	assert not ignored & (1 << (signal.SIGPIPE - 1))
	assert not ignored & (1 << (signal.SIGXFSZ - 1))


def test_a_command_stopped_before_the_exec_wrapper_made_its_group_is_still_stopped(monkeypatch, tmp_path):
	# ( review of round 9, finding 14 ) before 3.11 the group exists only once the wrapper's Python has called
	# setpgid: a kill right after the start found no group ( ESRCH, swallowed ), took the command for gone and
	# dropped it, and it then ran on, untracked. Here the wrapper takes 1.5 s to get there.
	monkeypatch.setattr(multiCMD, '_HAS_PROCESS_GROUP', False)
	monkeypatch.setattr(multiCMD, '_PGROUP_EXEC', 'import time\ntime.sleep(1.5)\n' + multiCMD._PGROUP_EXEC)
	marker = tmp_path / 'ran'
	proc, pgid = multiCMD._spawn(['sh', '-c', f'touch {marker}; exec sleep 30'], subprocess.DEVNULL, subprocess.DEVNULL)
	multiCMD._register(proc, pgid)
	try:
		multiCMD._kill_commands([(proc, pgid)], signal.SIGINT)
		multiCMD._unregister(proc)
		time.sleep(1)
		# before ( 0e4d015 ): still running, and the command ran once the wrapper got to it
		assert proc.poll() is not None and not marker.exists()
		assert proc not in multiCMD._live_procs
	finally:
		multiCMD._live_procs.pop(proc, None)
		try:
			os.killpg(proc.pid, signal.SIGKILL)
		except OSError:
			pass
		proc.wait(5)


def test_a_command_still_running_stays_registered():
	proc, pgid = multiCMD._spawn(['sleep', '30'], subprocess.DEVNULL, subprocess.DEVNULL)
	multiCMD._register(proc, pgid)
	try:
		multiCMD._unregister(proc)
		assert proc in multiCMD._live_procs
		multiCMD._kill_commands([(proc, pgid)], signal.SIGINT)
		multiCMD._unregister(proc)
		assert proc not in multiCMD._live_procs
	finally:
		multiCMD._live_procs.pop(proc, None)
		multiCMD._kill_commands([(proc, pgid)], signal.SIGKILL)


def test_a_poll_cut_short_holding_popens_lock_does_not_keep_the_command_from_being_reaped(launcher):
	# ( seen on free-threaded 3.13t / 3.14t ) a Ctrl-C right after Popen.poll()'s acquire(False) of _waitpid_lock,
	# before its try: the lock stays held, and every later poll() returned None
	proc, pgid = multiCMD._spawn(['true'], subprocess.DEVNULL, subprocess.DEVNULL)
	multiCMD._register(proc, pgid)
	try:
		assert proc._waitpid_lock.acquire(False)
		deadline = time.monotonic() + 5
		while proc.poll() is None and time.monotonic() < deadline:
			time.sleep(0.01)
		# before: None for good, and the command stayed registered
		assert proc.poll() == 0
		multiCMD._unregister(proc)
		assert proc not in multiCMD._live_procs
	finally:
		multiCMD._live_procs.pop(proc, None)


def test_the_registry_takes_no_lock_a_ctrl_c_could_leave_held():
	# ( found with the free-threaded stress loops ) a Ctrl-C right after a `with lock:` acquire on the main thread
	# leaves the lock held on Python 3.6 - 3.9 and 3.14 ( r10/withlock.py: about a third of such Ctrl-Cs ); held,
	# _live_procs_lock hung every registration, kill_all() and the exit hook
	assert multiCMD._live_procs_lock.acquire(False)
	try:
		done = []
		def run():
			done.append(multiCMD.run_command(['true'], quiet=True, return_code_only=True))
			multiCMD.kill_all()
			done.append('killed')
		worker = threading.Thread(target=run, daemon=True)
		worker.start()
		worker.join(10)
		# before: blocked for good on the lock
		assert done == [0, 'killed']
	finally:
		multiCMD._live_procs_lock.release()
