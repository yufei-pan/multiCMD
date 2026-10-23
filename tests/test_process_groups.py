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


def _jam(proc):
	'''Leave proc's Popen lock held by nobody, as a KeyboardInterrupt right after poll()'s acquire(False) does'''
	thread = threading.Thread(target=proc._waitpid_lock.acquire, daemon=True)
	thread.start()
	thread.join(5)
	assert proc._waitpid_lock.locked()


@pytest.mark.skipif(not multiCMD._CAN_PEEK, reason='needs os.waitid() with WNOWAIT')
@pytest.mark.parametrize('how', ['exits', 'killed'])
def test_a_command_whose_popen_lock_a_ctrl_c_left_held_is_still_reaped(launcher, how):
	# ( seen on free-threaded 3.13t / 3.14t; review of round 10, finding 4 ) a Ctrl-C right after Popen.poll()'s
	# acquire(False) of _waitpid_lock, before its try: the lock stays held, and every later poll() returns None
	proc, pgid = multiCMD._spawn(['true'] if how == 'exits' else ['sleep', '30'], subprocess.DEVNULL, subprocess.DEVNULL)
	multiCMD._register(proc, pgid)
	try:
		_jam(proc)
		started = time.monotonic()
		if how == 'exits':
			assert multiCMD._wait_all([(proc, pgid)], 5)
			assert proc.returncode == 0
		else:
			multiCMD._kill_commands([(proc, pgid)], signal.SIGINT)
			assert proc.returncode == -signal.SIGINT
		# before ( fdc45b9's RLock: a lock left held by another thread ): None for good, every wait to its timeout
		assert time.monotonic() - started < multiCMD._JAMMED_AFTER + 1
		multiCMD._unregister(proc)
		assert proc not in multiCMD._live_procs and proc.poll() == proc.returncode
	finally:
		multiCMD._live_procs.pop(proc, None)
		if proc.returncode is None:
			proc.kill()


def test_a_signal_handler_that_stops_the_commands_inside_a_popen_wait_leaves_it_the_returncode(launcher):
	# ( review of round 10, finding 4; probe r10probes/rlock_reenter.py ) a program's SIGTERM handler calls kill_all()
	# ( as its docstring suggests ) and returns, landing in the main thread's Popen.wait() right after it took its
	# lock: a reap from inside the handler leaves that wait ECHILD, and Popen records 0 for a killed command
	proc, pgid = multiCMD._spawn(['sleep', '30'], subprocess.DEVNULL, subprocess.DEVNULL)
	multiCMD._register(proc, pgid)
	try:
		multiCMD._send(proc, pgid, signal.SIGTERM)
		assert pid_gone(proc.pid)
		real = proc._try_wait
		seen = []
		def try_wait(flags):
			if not seen:
				# what the handler does as it lands here, on the same thread
				multiCMD.kill_all()
				seen.append(proc.returncode)
			return real(flags)
		proc._try_wait = try_wait
		proc.wait(timeout=5)
		# before ( fdc45b9's RLock ): the handler reaped it, and the wait then recorded 0
		assert seen == [None] and proc.returncode == -signal.SIGTERM
	finally:
		multiCMD._live_procs.pop(proc, None)
		if proc.returncode is None:
			proc.kill()


@pytest.mark.parametrize('quiet', [False, True])
def test_a_command_no_signal_stops_is_dropped_after_a_full_kill(monkeypatch, capfd, quiet):
	# ( review of round 10, finding 7 ) one in uninterruptible sleep, or not ours to signal ( EPERM, which _send
	# swallows ): here no signal is ever sent. It stayed registered for good, and every later kill_all() and the exit
	# waited their full 2 s for it
	proc, pgid = multiCMD._spawn(['sleep', '30'], subprocess.DEVNULL, subprocess.DEVNULL)
	multiCMD._register(proc, pgid)
	try:
		with monkeypatch.context() as m:
			m.setattr(multiCMD, '_send', lambda *args: None)
			if quiet:
				multiCMD._stop_command(proc, pgid, quiet=True)
			else:
				multiCMD.kill_all()
			assert proc.poll() is None
			# before ( fdc45b9 ): still registered
			assert proc not in multiCMD._live_procs
			started = time.monotonic()
			multiCMD.kill_all()
			multiCMD._kill_running_at_exit()
			assert time.monotonic() - started < 0.5
		err = capfd.readouterr().err
		assert err.count(f'pid {proc.pid} is still running') == (0 if quiet else 1), err
	finally:
		multiCMD._live_procs.pop(proc, None)
		proc.kill()
		proc.wait(5)


def test_a_command_given_up_on_is_named_once_whoever_gives_up_on_it():
	# ( review of round 11, finding 10 ) two kill_all() at once ( from two threads ) both took it from the registry's
	# snapshot, and the "show once" guard stored proc itself, so it always said "first": the pid was named twice
	proc, pgid = multiCMD._spawn(['sleep', '30'], subprocess.DEVNULL, subprocess.DEVNULL)
	try:
		lines = []
		real = multiCMD._write_error
		multiCMD._write_error = lines.append
		try:
			multiCMD._give_up_on([(proc, pgid)])
			multiCMD._give_up_on([(proc, pgid)])
		finally:
			multiCMD._write_error = real
		# before ( fc0c782 ): twice
		assert lines == [f'multiCMD: pid {proc.pid} is still running after SIGKILL: no longer tracked']
	finally:
		proc.kill()
		proc.wait(5)


@pytest.mark.skipif(not hasattr(signal, 'SIGKILL'), reason='POSIX signals')
def test_kill_all_says_nothing_of_a_command_a_quiet_run_started(monkeypatch, capfd):
	# ( review of round 11, finding 9 ) kill_all() and the exit hook passed no quiet down, so a command no signal stops
	# was named on stderr even when every multiCMD call was quiet ( statblk at exit )
	ran = multiCMD.run_commands([['sleep', '30']], quiet=True, wait_for_return=False, return_object=True)
	deadline = time.monotonic() + 5
	while not multiCMD._live_procs and time.monotonic() < deadline:
		time.sleep(0.01)
	(proc, pgid), = multiCMD._live_procs.copy().items()
	try:
		with monkeypatch.context() as m:
			m.setattr(multiCMD, '_send', lambda *args: None)
			multiCMD.kill_all()
			assert proc not in multiCMD._live_procs and proc.poll() is None
		# before ( fc0c782 ): "multiCMD: pid N is still running after SIGKILL: no longer tracked"
		assert 'still running' not in capfd.readouterr().err
	finally:
		proc.kill()
		multiCMD.join_threads(timeout=5)
	assert ran[0].returncode == -signal.SIGKILL


@pytest.mark.skipif(not multiCMD._CAN_PEEK, reason='needs os.waitid() with WNOWAIT')
def test_a_command_reaped_outside_popen_while_its_lock_is_jammed_is_recorded_as_popen_does(launcher):
	# ( review of round 11, finding 4; probe echild_probe.py ) SIGCHLD ignored has the kernel reap the command: with
	# its Popen lock jammed, os.waitid() then raised ECHILD and _poll() returned None for ever, so every wait ran to
	# its timeout and every kill_all() cost 2 s
	previous = signal.signal(signal.SIGCHLD, signal.SIG_IGN)
	try:
		proc, pgid = multiCMD._spawn(['sleep', '0.2'], subprocess.DEVNULL, subprocess.DEVNULL)
		multiCMD._register(proc, pgid)
		try:
			_jam(proc)
			# ( the kernel has reaped it by then )
			assert pid_gone(proc.pid)
			time.sleep(0.3)
			started = time.monotonic()
			# before ( fc0c782 ): False after 2 s, returncode None
			assert multiCMD._wait_all([(proc, pgid)], 2) and proc.returncode == 0
			assert time.monotonic() - started < multiCMD._JAMMED_AFTER + 0.5
			started = time.monotonic()
			multiCMD.kill_all()
			# before: still registered, and each kill_all() took 2 s or more
			assert proc not in multiCMD._live_procs and time.monotonic() - started < 0.5
		finally:
			multiCMD._live_procs.pop(proc, None)
	finally:
		signal.signal(signal.SIGCHLD, previous)


@pytest.mark.skipif(not multiCMD._CAN_PEEK, reason='needs os.waitid() with WNOWAIT')
def test_a_poll_whose_signal_handler_runs_long_keeps_other_threads_from_reaping_its_command(launcher):
	# ( review of round 11, finding 11 ) a signal handler ( kill_all(), some 2 s ) lands in the main thread's poll() as
	# it holds the Popen lock: another thread's _poll() saw the lock held past _JAMMED_AFTER and reaped the command,
	# and the interrupted poll() then got ECHILD, which Popen records as returncode 0
	proc, pgid = multiCMD._spawn(['sleep', '30'], subprocess.DEVNULL, subprocess.DEVNULL)
	multiCMD._register(proc, pgid)
	try:
		multiCMD._send(proc, pgid, signal.SIGTERM)
		assert pid_gone(proc.pid)
		other = []
		real_lock = proc._waitpid_lock
		class Lock:
			'''The Popen lock, whose first acquire runs what a signal handler landing right after it would'''
			def acquire(self, *args):
				got = real_lock.acquire(*args)
				if got and not other:
					# the handler, inside Popen's own poll(), the lock held: meanwhile another thread waits for the command
					thread = threading.Thread(target=lambda: other.append(multiCMD._wait_all([(proc, pgid)], multiCMD._JAMMED_AFTER + 0.5)))
					thread.start()
					thread.join(10)
				return got
			def release(self):
				real_lock.release()
			def locked(self):
				return real_lock.locked()
			__enter__ = acquire
			def __exit__(self, *exc_info):
				self.release()
		proc._waitpid_lock = Lock()
		# before ( fc0c782 ): the other thread reaped it ( [True] ), and the interrupted poll() recorded 0
		assert multiCMD._poll(proc) == -signal.SIGTERM and other == [False]
		assert not proc._multicmd_inside
	finally:
		multiCMD._live_procs.pop(proc, None)
		if proc.returncode is None:
			proc.kill()
