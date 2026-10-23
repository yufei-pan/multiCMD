import _thread
import os
import signal
import subprocess
import sys
import textwrap
import threading
import time

import pytest

import multiCMD
from conftest import PACKAGE_ROOT, pid_gone, sigint_main_after
from multiCMD import AsyncExecutor, run_command, run_commands

linux_only = pytest.mark.skipif(not sys.platform.startswith('linux'), reason='checks processes through /proc')


@linux_only
@pytest.mark.slow
def test_timeout_kills_background_grandchild_and_returns_promptly(launcher):
	start = time.monotonic()
	task = run_command(['sh', '-c', 'sleep 30 & echo $!; wait'], timeout=1, quiet=True, return_object=True)
	assert task.returncode == 124
	assert 'Timeout!' in task.stderr
	assert time.monotonic() - start < 3.5
	assert pid_gone(int(task.stdout[0]))


@pytest.mark.slow
def test_timeout_does_not_wait_for_a_foreground_grandchild(launcher):
	start = time.monotonic()
	task = run_command(['sh', '-c', 'sleep 13; echo done'], timeout=1, quiet=True, return_object=True)
	assert task.returncode == 124
	assert time.monotonic() - start < 3.5


@pytest.mark.slow
def test_progress_redraws_count_as_activity():
	task = run_command(['sh', '-c', 'for i in 1 2 3 4 5; do printf "\\r$i"; sleep 0.4; done; echo'],
		timeout=1, quiet=True, return_object=True)
	assert task.returncode == 0
	assert task.stdout == ['5']


@pytest.mark.slow
def test_partial_line_output_counts_as_activity():
	task = run_command(['sh', '-c', 'for i in 1 2 3 4 5; do printf .; sleep 0.4; done'],
		timeout=1, quiet=True, return_object=True)
	assert task.returncode == 0
	assert task.stdout == ['.....']


@linux_only
@pytest.mark.slow
def test_executor_stop_kills_grandchildren_promptly():
	ex = AsyncExecutor(quiet=True)
	task = ex.run_command(['sh', '-c', 'sleep 30 & echo $!; wait'])
	deadline = time.monotonic() + 5
	while not task.stdout and time.monotonic() < deadline:
		time.sleep(0.02)
	start = time.monotonic()
	ex.stop(timeout=10)
	assert time.monotonic() - start < 3.5
	assert task.returncode is not None
	assert pid_gone(int(task.stdout[0]))


def test_timing_fields():
	task = run_command(['sleep', '0.2'], quiet=True, return_object=True)
	assert task.start_time <= task.end_time
	assert task.duration >= 0.2
	assert multiCMD.Task(['x']).duration is None


@pytest.mark.slow
def test_fire_and_forget_reports_exit_before_background_output_ends():
	task = run_command(['sh', '-c', '(sleep 2; echo late) & echo started'],
		quiet=True, wait_for_return=False, return_object=True)
	deadline = time.monotonic() + 1.5
	while task.returncode is None and time.monotonic() < deadline:
		time.sleep(0.02)
	assert task.returncode == 0
	assert 'late' not in task.stdout
	# the leader is reaped and dropped from the registry, so kill_all() leaves the background job alone
	assert not multiCMD._live_procs
	multiCMD.join_threads(timeout=5)
	assert task.stdout == ['started', 'late']


@linux_only
def test_ctrl_c_kills_the_running_command_and_reraises(tmp_path):
	pidfile = tmp_path / 'pid'
	timer = threading.Timer(0.5, _thread.interrupt_main)
	timer.start()
	start = time.monotonic()
	with pytest.raises(KeyboardInterrupt):
		run_command(['sh', '-c', f'sleep 30 & echo $! > {pidfile}; wait'], quiet=True)
	assert time.monotonic() - start < 3.5
	assert pid_gone(int(pidfile.read_text()))


@linux_only
def test_ctrl_c_while_draining_keeps_the_output_already_read(monkeypatch):
	# the command exits, a background job holds its stdout ( stderr is closed ): the wait on the stdout reader takes the SIGINT
	real = multiCMD._read_stream
	seen = []
	def late_reader(stream, target, task, *args):
		seen.append(task)
		got = []
		real(stream, got, task, *args)
		time.sleep(0.3)
		target.extend(got)
	monkeypatch.setattr(multiCMD, '_read_stream', late_reader)
	with pytest.raises(KeyboardInterrupt), sigint_main_after(0.5):
		run_command(['sh', '-c', 'echo out; exec 2>&-; sleep 30 & exit 0'], quiet=True)
	assert seen[0].stdout == ['out']


def test_missing_command_is_127(launcher):
	assert run_command(['no-such-command-xyz'], quiet=True, return_code_only=True) == 127


def test_command_that_is_not_executable_is_126(launcher, tmp_path):
	script = tmp_path / 'script.sh'
	script.write_text('#!/bin/sh\necho hi\n')
	script.chmod(0o644)
	assert run_command([str(script)], quiet=True, return_code_only=True) == 126


def test_sudo_validation_runs_in_the_foreground(monkeypatch):
	calls = []
	monkeypatch.setattr(multiCMD, 'SUDO_PATH', '/usr/bin/sudo')
	monkeypatch.setattr(subprocess, 'run', lambda *args, **kwargs: calls.append((args, kwargs)))
	multiCMD._validate_sudo()
	assert calls == [((['/usr/bin/sudo', '-v'],), {})]


def test_stopped_task_is_skipped_before_it_starts():
	task = multiCMD.Task(['true'])
	task.stop = True
	multiCMD.__dict__['__run_command'](task, threading.Semaphore(1), quiet=True)
	assert task.returncode is None
	assert task.start_time is None
	assert not task._finalized


def test_pipelines_stop_when_the_reader_goes_away(launcher):
	# with SIGPIPE ignored, `yes` complains and an endless writer never stops
	task = run_command('yes | head -1', shell=True, quiet=True, return_object=True)
	assert task.stdout == ['y']
	assert task.stderr == []
	start = time.monotonic()
	task = run_command('while :; do echo x; done | head -1', shell=True, quiet=True, return_object=True)
	assert task.stdout == ['x']
	assert time.monotonic() - start < 5


# the command's own process exits at once, but the background sleep keeps its output open
LEAVES_A_BACKGROUND_JOB = ['sh', '-c', 'sleep 30 & echo $!']


@linux_only
@pytest.mark.slow
def test_timeout_reaches_what_a_finished_command_left_running():
	start = time.monotonic()
	task = run_command(LEAVES_A_BACKGROUND_JOB, timeout=1, quiet=True, return_object=True)
	assert time.monotonic() - start < 3.5
	assert task.returncode == 124
	assert pid_gone(int(task.stdout[0]))


@linux_only
@pytest.mark.slow
def test_executor_stop_reaches_what_a_finished_command_left_running():
	ex = AsyncExecutor(quiet=True)
	task = ex.run_command(LEAVES_A_BACKGROUND_JOB)
	deadline = time.monotonic() + 5
	while not task.stdout and time.monotonic() < deadline:
		time.sleep(0.02)
	time.sleep(0.2)
	start = time.monotonic()
	ex.stop(timeout=10)
	assert time.monotonic() - start < 3.5
	assert pid_gone(int(task.stdout[0]))


@linux_only
@pytest.mark.slow
def test_pool_ctrl_c_reaches_what_finished_commands_left_running(tmp_path):
	pidfiles = [tmp_path / f'pid{i}' for i in range(3)]
	commands = [['sh', '-c', f'sleep 30 & echo $! > {path}'] for path in pidfiles]
	timer = threading.Timer(0.7, _thread.interrupt_main)
	timer.start()
	start = time.monotonic()
	with pytest.raises(KeyboardInterrupt):
		run_commands(commands, max_threads=3, quiet=True)
	assert time.monotonic() - start < 4
	for path in pidfiles:
		assert pid_gone(int(path.read_text()))


EXIT_PROBE = textwrap.dedent(r"""
	import sys, time
	sys.path.insert(0, {root!r})
	import multiCMD
	task = multiCMD.run_command(['sh', '-c', 'sleep 30 & echo $!'], quiet=True, wait_for_return=False, return_object=True)
	while task.returncode is None or not task.stdout:
		time.sleep(0.02)
	print(task.stdout[0], flush=True)
""")


@linux_only
def test_exit_leaves_alone_what_a_finished_fire_and_forget_command_started(tmp_path):
	# statbtrfs starts `btrfs scrub start`, which leaves the scrub running in the background on purpose
	probe = tmp_path / 'probe.py'
	probe.write_text(EXIT_PROBE.format(root=str(PACKAGE_ROOT)))
	out = subprocess.run([sys.executable, str(probe)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=30).stdout
	child = int(out.split()[0])
	try:
		assert not pid_gone(child, wait=0.5)
	finally:
		os.kill(child, signal.SIGKILL)


@pytest.mark.parametrize('exc', [KeyboardInterrupt, RuntimeError], ids=['ctrl-c', 'cannot-start'])
def test_a_reader_start_that_fails_still_stops_the_command(monkeypatch, exc):
	real_start = multiCMD._Thread.start
	count = [0]
	def start(self):
		if getattr(self, '_target', None) is multiCMD._read_stream:
			count[0] += 1
			if count[0] == 2:
				raise exc("can't start new thread")
		real_start(self)
	monkeypatch.setattr(multiCMD._Thread, 'start', start)
	procs = []
	real_register = multiCMD._register
	monkeypatch.setattr(multiCMD, '_register', lambda proc, pgid: (procs.append(proc), real_register(proc, pgid)))
	try:
		if exc is KeyboardInterrupt:
			with pytest.raises(KeyboardInterrupt):
				run_command(['sleep', '30'], quiet=True)
		else:
			task = run_command(['sleep', '30'], quiet=True, return_object=True)
			assert task.returncode == -1 and task._error and "can't start new thread" in task.stderr[0]
		assert not multiCMD._live_procs
		assert procs and procs[0].poll() is not None
	finally:
		multiCMD.kill_all(signal.SIGKILL)


def test_a_command_that_exits_just_past_the_inactivity_timeout_keeps_its_exit_code(monkeypatch):
	# it exits after the threshold but before _watch() looks again: its output ends a moment later
	real_watch, real_read, real_join = multiCMD._watch, multiCMD._read_stream, multiCMD._join
	go = threading.Event()
	def watch(task, proc, timeout):
		reason = real_watch(task, proc, 0)
		task._last_output -= timeout + 1
		return reason
	def held_read(stream, target, task, *args):
		real_read(stream, target, task, *args)
		go.wait(10)
	def join(thread, timeout=None):
		# the drain's first step: the output ends now, within it
		if timeout == 0.05 and not go.is_set():
			go.set()
			timeout = 10
		return real_join(thread, timeout)
	monkeypatch.setattr(multiCMD, '_watch', watch)
	monkeypatch.setattr(multiCMD, '_read_stream', held_read)
	monkeypatch.setattr(multiCMD, '_join', join)
	task = run_command(['sh', '-c', 'echo hi; exit 3'], timeout=1, quiet=True, return_object=True)
	assert (task.returncode, task.stdout, task.stderr) == (3, ['hi'], [])


@pytest.mark.parametrize('exc', [KeyboardInterrupt, RuntimeError], ids=['ctrl-c', 'failure'])
def test_a_ctrl_c_or_a_failure_right_after_the_spawn_still_stops_the_command(monkeypatch, exc):
	# between _spawn() returning and the readers starting: before the fix the command ran on, unread and registered
	procs = []
	real_register = multiCMD._register
	def register(proc, pgid):
		procs.append(proc)
		real_register(proc, pgid)
		raise exc('right after the spawn')
	monkeypatch.setattr(multiCMD, '_register', register)
	try:
		if exc is KeyboardInterrupt:
			with pytest.raises(KeyboardInterrupt):
				run_command(['sleep', '30'], quiet=True)
		else:
			task = run_command(['sleep', '30'], quiet=True, return_object=True)
			assert task.returncode == -1 and task._error and 'right after the spawn' in task.stderr[0]
		assert not multiCMD._live_procs
		assert procs and procs[0].poll() is not None
		assert procs[0].stdout.closed and procs[0].stderr.closed
	finally:
		multiCMD.kill_all(signal.SIGKILL)


_EXIT_ON_SIGTERM = r'''
import os, signal, subprocess, sys, threading, time
sys.path.insert(0, sys.argv[1])
import multiCMD
pids, how = sys.argv[2], sys.argv[3]
real = subprocess.Popen._execute_child
def execute_child(self, *args, **kwargs):
	real(self, *args, **kwargs)
	with open(pids, 'a') as f:
		f.write('%d\n' % self.pid)
subprocess.Popen._execute_child = execute_child
def handler(signum, frame):
	# as kill_all()'s docstring suggests
	multiCMD.kill_all()
	sys.exit(143)
signal.signal(signal.SIGTERM, handler)
def sender():
	deadline = time.monotonic() + 10
	while not multiCMD._live_procs and time.monotonic() < deadline:
		time.sleep(0.01)
	os.kill(os.getpid(), signal.SIGTERM)
threading.Thread(target=sender, daemon=True).start()
commands = [['sleep', '30']] * 20
if how == 'join_threads':
	multiCMD.run_commands(commands, quiet=True, wait_for_return=False)
	multiCMD.join_threads()
elif how == 'executor':
	ex = multiCMD.AsyncExecutor(max_threads=1, quiet=True)
	ex.run_commands(commands)
	ex.wait()
else:
	# the program waits on something of its own: only the exit hook is left to stop them
	multiCMD.run_commands(commands, quiet=True, wait_for_return=False)
	threading.Event().wait(30)
'''


@pytest.mark.skipif(not hasattr(signal, 'SIGTERM') or os.name == 'nt', reason='POSIX signals')
@pytest.mark.parametrize('how', ['join_threads', 'executor', 'its own wait'])
def test_a_sigterm_handlers_exit_leaves_no_command_running(tmp_path, how):
	# ( review of round 10, finding 8; probe r10probes/ff_exit2.py ) a program's SIGTERM handler calls kill_all() and
	# sys.exit(): join_threads() and AsyncExecutor.wait() stopped the commands only on KeyboardInterrupt, so on that
	# SystemExit the queued commands went on starting, after kill_all() and after the exit hook's snapshot
	pids = tmp_path / 'pids'
	proc = subprocess.run([sys.executable, '-c', _EXIT_ON_SIGTERM, str(PACKAGE_ROOT), str(pids), how],
		stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=60)
	assert proc.returncode == 143, proc.stderr
	started = [int(pid) for pid in pids.read_text().split()]
	left = [pid for pid in started if not pid_gone(pid, wait=2)]
	for pid in left:
		try:
			os.kill(pid, signal.SIGKILL)
		except OSError:
			pass
	# before ( fdc45b9 ): one more started after the exit hook had looked, and ran on
	assert started and left == [], (started, proc.stderr)
	# and no more start once the exit hook has begun ( without that check, all 20 started, each killed in turn )
	assert len(started) <= 5, started


_LATE_AT_EXIT = r'''
import atexit, sys, weakref
sys.path.insert(0, sys.argv[1])
def late(how):
	import multiCMD
	task = multiCMD.run_command(['echo', 'hi'], quiet=True, return_object=True)
	print(how, 'serial', task.returncode, task.stdout, task.stop, flush=True)
	codes = multiCMD.run_commands([['echo', 'a'], ['echo', 'b']], max_threads=2, quiet=True, return_code_only=True)
	print(how, 'pool', codes, flush=True)
	try:
		multiCMD.run_command(['false'], quiet=True, raise_error=True)
	except multiCMD.CommandError:
		print(how, 'raised', flush=True)
class Late:
	pass
holder = Late()
# registered before multiCMD is imported: both run after its exit hook
atexit.register(late, 'atexit')
weakref.finalize(holder, late, 'finalize')
import multiCMD
'''


def test_a_command_run_from_an_atexit_handler_after_the_exit_hook_runs(tmp_path):
	# ( review of round 11, finding 1 ) the exit hook's _exiting gated the main thread too, and was never cleared: a
	# command run from an atexit handler or a weakref.finalize callback that runs after the hook was skipped silently
	# ( returncode None, task.stop set, raise_error not raising ), where 1.54 ran it
	proc = subprocess.run([sys.executable, '-c', _LATE_AT_EXIT, str(PACKAGE_ROOT)],
		stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=60)
	assert proc.returncode == 0, proc.stderr
	lines = proc.stdout.splitlines()
	for how in ('finalize', 'atexit'):
		# before ( fc0c782 ): "<how> serial None [] True", the pool's [None, None], and no CommandError
		assert f"{how} serial 0 ['hi'] False" in lines, (lines, proc.stderr)
		assert f'{how} pool [0, 0]' in lines and f'{how} raised' in lines, (lines, proc.stderr)


_REFUSED_AT_EXIT = r'''
import atexit, sys
sys.path.insert(0, sys.argv[1])
def late():
	multiCMD.join_threads(timeout=10)
	for task in tasks:
		print(task.returncode, task._error, any('the interpreter is exiting' in line for line in task.stderr), flush=True)
atexit.register(late)
import multiCMD
# queued on daemon threads: the first runs as the program ends, the others start after the exit hook began
tasks = multiCMD.run_commands([['sleep', '30']] * 3, max_threads=1, quiet=True, wait_for_return=False, return_object=True)
import time
deadline = time.monotonic() + 10
while not multiCMD._live_procs and time.monotonic() < deadline:
	time.sleep(0.01)
'''


def test_a_command_the_exit_hook_keeps_from_starting_fails_with_that_reason(tmp_path):
	# ( review of round 11, finding 1 ) never skipped silently: a daemon thread's command that would start after the
	# exit hook began fails, -1 with the reason in its stderr
	proc = subprocess.run([sys.executable, '-c', _REFUSED_AT_EXIT, str(PACKAGE_ROOT)],
		stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=60)
	assert proc.returncode == 0, proc.stderr
	first, *rest = proc.stdout.splitlines()
	# the first one killed by the hook; before ( fc0c782 ): the others None, False, False
	assert first.split()[0] not in ('0', 'None') and first.split()[1:] == ['False', 'False'], proc.stdout
	assert rest == ['-1 True True'] * 2, (proc.stdout, proc.stderr)


_IN_FLIGHT_AT_EXIT = r'''
import sys, time
sys.path.insert(0, sys.argv[1])
import multiCMD
pids, count = sys.argv[2], int(sys.argv[3])
real = multiCMD._spawn
def spawn(*args, **kwargs):
	# the command runs, and its thread is held up before it registers it: the program exits meanwhile
	proc, pgid = real(*args, **kwargs)
	with open(pids, 'a') as f:
		f.write('%d\n' % proc.pid)
	time.sleep(0.3)
	return proc, pgid
multiCMD._spawn = spawn
multiCMD.run_commands([['sleep', '30']] * count, max_threads=0, quiet=True, wait_for_return=False)
deadline = time.monotonic() + 10
while time.monotonic() < deadline:
	try:
		with open(pids) as f:
			if len(f.read().split()) == count:
				break
	except OSError:
		pass
	time.sleep(0.005)
'''


@linux_only
def test_a_command_started_as_the_exit_hook_begins_is_still_stopped(tmp_path):
	# ( review of round 11, finding 2; probe exit_inflight.py ) a daemon thread past the start's check of _exiting
	# that had not registered its command yet registered it after the hook's last look at the registry, and was then
	# frozen with the interpreter: the command outlived the program
	pids = tmp_path / 'pids'
	proc = subprocess.run([sys.executable, '-c', _IN_FLIGHT_AT_EXIT, str(PACKAGE_ROOT), str(pids), '3'],
		stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=60)
	assert proc.returncode == 0, proc.stderr
	started = [int(pid) for pid in pids.read_text().split()]
	left = [pid for pid in started if not pid_gone(pid, wait=2)]
	for pid in left:
		try:
			os.kill(pid, signal.SIGKILL)
		except OSError:
			pass
	# before ( fc0c782 ): all three ran on
	assert len(started) == 3 and left == [], (started, left, proc.stderr)


@linux_only
def test_a_sigterm_handlers_exit_while_a_serial_run_drains_output_stops_what_holds_it(tmp_path, request):
	# ( review of round 11, finding 3; probe drain_exit.py ) the command exited, and what it started in the background
	# holds its output open: _drain() stopped that on KeyboardInterrupt only, so a SIGTERM handler's sys.exit() left
	# the background group running
	previous = signal.signal(signal.SIGTERM, lambda *args: sys.exit(143))
	request.addfinalizer(lambda: signal.signal(signal.SIGTERM, previous))
	pidfile = tmp_path / 'pid'
	main = threading.main_thread().ident
	def sender():
		deadline = time.monotonic() + 10
		while not (multiCMD._draining or time.monotonic() > deadline):
			time.sleep(0.01)
		time.sleep(0.2)
		signal.pthread_kill(main, signal.SIGTERM)
	threading.Thread(target=sender, daemon=True).start()
	with pytest.raises(SystemExit) as raised:
		run_command(['sh', '-c', f'sleep 30 & echo $! > {pidfile}; echo hi'], quiet=True)
	assert raised.value.code == 143
	pid = int(pidfile.read_text())
	gone = pid_gone(pid, wait=2)
	if not gone:
		os.kill(pid, signal.SIGKILL)
	# before ( fc0c782 ): the background sleep ran on
	assert gone and not multiCMD._draining
