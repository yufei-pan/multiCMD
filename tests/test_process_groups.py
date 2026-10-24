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
		assert err.count(f'pid {proc.pid} not seen to exit') == (0 if quiet else 1), err
	finally:
		multiCMD._live_procs.pop(proc, None)
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
		assert f'pid {proc.pid}' not in capfd.readouterr().err
	finally:
		proc.kill()
		multiCMD.join_threads(timeout=5)
	assert ran[0].returncode == -signal.SIGKILL


def test_every_registry_entry_holds_a_real_process_group_id_or_none(launcher, monkeypatch):
	# ( the incident of 2026-10-23: a prototype stored a "grouped" flag where the group id belongs, and killpg(True, sig)
	# is kill(-1, sig), every process the user may signal ) whatever multiCMD registers, as a command starts, drains, or
	# is stopped after a Ctrl-C, holds the command's pid as its group id, or None
	seen = []
	class Watched(dict):
		def __setitem__(self, proc, pgid):
			seen.append((proc, pgid))
			super().__setitem__(proc, pgid)
	monkeypatch.setattr(multiCMD, '_live_procs', Watched())
	monkeypatch.setattr(multiCMD, '_draining', Watched())
	multiCMD.run_commands([['true'], ['sh', '-c', 'sleep 0.3 & echo a']], quiet=True)
	multiCMD.run_commands([['true']] * 3, max_threads=3, quiet=True)
	multiCMD.run_command(['sleep', '5'], timeout=0.2, quiet=True, pty=os.name != 'nt')
	multiCMD.run_commands([['true']] * 2, quiet=True, wait_for_return=False)
	multiCMD.join_threads()
	ex = multiCMD.AsyncExecutor(max_threads=2, quiet=True)
	ex.run_commands([['true']] * 2)
	ex.wait()
	# a Ctrl-C before the registration ( the handler registers it ), and one that cuts the stop short ( re-registered )
	real_register, real_stop = multiCMD._register, multiCMD._stop_command
	for what in ('register', 'stop'):
		def interrupt(*args, **kwargs):
			raise KeyboardInterrupt
		monkeypatch.setattr(multiCMD, '_register' if what == 'register' else '_stop_command', interrupt)
		with pytest.raises(KeyboardInterrupt):
			multiCMD.run_command(['sleep', '5'], timeout=0.2, quiet=True)
		monkeypatch.setattr(multiCMD, '_register', real_register)
		monkeypatch.setattr(multiCMD, '_stop_command', real_stop)
		multiCMD.kill_all(signal.SIGKILL)
	assert len(seen) >= 10
	bad = [(proc.pid, pgid) for proc, pgid in seen if not (pgid is None or (type(pgid) is int and pgid > 1 and pgid == proc.pid))]
	assert bad == []
