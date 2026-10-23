'''
A Ctrl-C while multiCMD settles its bookkeeping: the step runs again to its end and the Ctrl-C is raised after it,
never lost; the failure messages a step shows appear once, after it
'''
import dis
import functools
import gc
import os
import signal
import subprocess
import sys
import threading
import time
import weakref
import _thread

import pytest

import multiCMD
from conftest import PACKAGE_ROOT, pid_gone, sigint_main_after
from multiCMD import AsyncExecutor, run_commands
from test_async_executor import _fail_nth_start, _global_threads, _join_threads_returns_within, _runs


def _ctrl_c():
	'''A real Ctrl-C to the whole process ( any thread not blocking SIGINT may take it ), and time for it to land'''
	os.kill(os.getpid(), signal.SIGINT)
	deadline = time.monotonic() + 0.2
	while time.monotonic() < deadline:
		pass


@pytest.fixture
def another_thread():
	'''A thread alive throughout: a SIGINT the main thread blocked would be taken by it'''
	release = threading.Event()
	thread = threading.Thread(target=release.wait, daemon=True)
	thread.start()
	yield thread
	release.set()
	thread.join(5)


def _sigint_bits(pid):
	'''Whether SIGINT is blocked / ignored in a process ( from /proc/<pid>/status )'''
	bits = {}
	with open(f'/proc/{pid}/status') as f:
		for line in f:
			name, _, value = line.partition(':')
			if name in ('SigBlk', 'SigIgn'):
				bits[name] = bool(int(value, 16) & (1 << (signal.SIGINT - 1)))
	return bits


# ---- _settle(): a Ctrl-C that cuts the step short runs it again, and is raised right after it

@pytest.mark.skipif(not hasattr(signal, 'pthread_sigmask'), reason='POSIX signals')
def test_a_ctrl_c_inside_a_settled_step_is_raised_right_after_it(another_thread):
	runs, done = [], []
	def step():
		runs.append(1)
		if len(runs) == 1:
			_ctrl_c()
		done.append(1)
	with pytest.raises(KeyboardInterrupt):
		multiCMD._settle(step)
	# the Ctrl-C cut the step short and it ran again to its end ( nothing is held: see review of round 9, finding
	# 5 ); before round 8 the Ctrl-C was then lost
	assert runs == [1, 1] and done == [1]
	assert signal.getsignal(signal.SIGINT) is signal.default_int_handler


@pytest.mark.skipif(not hasattr(signal, 'pthread_sigmask'), reason='POSIX signals')
def test_a_ctrl_c_inside_a_settled_step_is_raised_when_the_caller_handles_one_already(another_thread):
	# ( round 8 took it then, guessing from sys.exc_info() that the caller was on its way out: it is always handed on )
	runs = []
	def step():
		runs.append(1)
		if len(runs) == 1:
			_ctrl_c()
	first = KeyboardInterrupt('first')
	with pytest.raises(KeyboardInterrupt) as raised:
		try:
			raise first
		except KeyboardInterrupt:
			multiCMD._settle(step)
	assert runs == [1, 1]
	assert raised.value is not first and raised.value.__context__ is first
	assert signal.getsignal(signal.SIGINT) is signal.default_int_handler


def test_a_keyboard_interrupt_that_cuts_a_step_short_runs_it_again_and_is_raised_after():
	# not a Ctrl-C ( another signal's handler, or injected ): the step runs again, and the interrupt is not lost
	runs = []
	def step():
		runs.append(1)
		if len(runs) == 1:
			raise KeyboardInterrupt
	with pytest.raises(KeyboardInterrupt):
		multiCMD._settle(step)
	assert runs == [1, 1]


@pytest.mark.skipif(not hasattr(signal, 'pthread_sigmask'), reason='POSIX signals')
def test_a_settled_step_on_another_thread_sees_the_handler_as_it_is(another_thread):
	# Python runs signal handlers on the main thread only: a Ctrl-C meanwhile is the main thread's
	seen = []
	def step():
		seen.append(signal.getsignal(signal.SIGINT))
	worker = threading.Thread(target=multiCMD._settle, args=(step,))
	worker.start()
	worker.join(5)
	assert seen == [signal.default_int_handler]


@pytest.mark.skipif(not hasattr(signal, 'pthread_sigmask'), reason='POSIX signals')
def test_a_ctrl_c_during_a_settled_step_goes_to_the_programs_handler(another_thread):
	# as it lands: nothing is held, and a handler that does not raise does not cut the step short
	calls = []
	def handler(signum, frame):
		calls.append(signum)
	previous = signal.signal(signal.SIGINT, handler)
	try:
		def step():
			_ctrl_c()
			calls.append('step')
		multiCMD._settle(step)
		assert calls == [signal.SIGINT, 'step'] and signal.getsignal(signal.SIGINT) is handler
	finally:
		signal.signal(signal.SIGINT, previous)


@pytest.mark.skipif(not hasattr(signal, 'pthread_sigmask'), reason='POSIX signals')
def test_a_settled_step_touches_no_signal_state(monkeypatch, another_thread):
	# ( review of round 9, finding 5 ) the hold swapped SIGINT's handler with signal.signal(), which also puts back
	# CPython's own C-level one; a step cut short by a Ctrl-C now only runs again
	calls = []
	real = signal.signal
	monkeypatch.setattr(signal, 'signal', lambda signum, handler: (calls.append(signum), real(signum, handler))[1])
	runs = []
	def step():
		runs.append(1)
		if len(runs) == 1:
			_ctrl_c()
	with pytest.raises(KeyboardInterrupt):
		multiCMD._settle(step)
	assert runs == [1, 1] and calls == []
	assert not hasattr(multiCMD, '_HoldCtrlC')


# ---- the messages a settled step shows: once, after it

def test_messages_shown_in_a_settled_step_appear_once_after_it(monkeypatch):
	written = []
	monkeypatch.setattr(multiCMD, '_write_error', lambda text: written.append((text, signal.getsignal(signal.SIGINT))))
	task = multiCMD.Task(['x'])
	runs = []
	def step():
		multiCMD._show_error('first', task)
		runs.append(1)
		assert written == []
		if len(runs) == 1:
			raise KeyboardInterrupt
	# ( the interrupt that cut the step short is raised after it: round 8 took it, the caller being on its way out )
	with pytest.raises(KeyboardInterrupt):
		multiCMD._settle(step)
	assert runs == [1, 1]
	# shown once, after the step
	assert written == [('first', signal.default_int_handler)]
	multiCMD._show_error('now')
	assert written[-1][0] == 'now'


def test_a_fire_and_forget_start_failure_cut_short_shows_its_message_once(monkeypatch, capfd):
	# the fail cut short once, then the starts' settling cut short too: before, each restart ran every fail again
	_fail_nth_start(monkeypatch, getattr(multiCMD, '__run_tracked_command'), 2)
	real_fail = multiCMD._fail_task
	armed = {'fail': True}
	def fail(*args, **kwargs):
		real_fail(*args, **kwargs)
		if armed['fail']:
			armed['fail'] = False
			raise KeyboardInterrupt
	monkeypatch.setattr(multiCMD, '_fail_task', fail)
	real_stop = multiCMD._stop_tasks
	armed['stop'] = True
	def stop(tasks):
		real_stop(tasks)
		if armed['stop']:
			armed['stop'] = False
			raise KeyboardInterrupt
	monkeypatch.setattr(multiCMD, '_stop_tasks', stop)
	with pytest.raises(KeyboardInterrupt):
		run_commands([['sleep', '30']] * 3, max_threads=0, wait_for_return=False)
	assert _join_threads_returns_within(5)
	assert capfd.readouterr().err.count('Error running command') == 1


# ---- a Ctrl-C while the bookkeeping of a failure is done is not lost

@pytest.mark.skipif(not hasattr(signal, 'pthread_sigmask'), reason='POSIX signals')
def test_a_ctrl_c_while_a_serial_run_cleans_up_a_failure_stops_the_run(monkeypatch):
	# the output readers of the first command cannot start; a Ctrl-C lands while that is cleaned up
	real_start = multiCMD._Thread.start
	def start(self):
		if getattr(self, '_target', None) is multiCMD._read_stream:
			raise RuntimeError("can't start new thread")
		real_start(self)
	monkeypatch.setattr(multiCMD._Thread, 'start', start)
	real_abandon = multiCMD._Thread._abandon
	sent = []
	def abandon(self):
		if not sent and sys._getframe(1).f_code.co_name == 'settle':
			sent.append(1)
			_ctrl_c()
		return real_abandon(self)
	monkeypatch.setattr(multiCMD._Thread, '_abandon', abandon)
	tasks = []
	real_build = multiCMD._build_tasks
	monkeypatch.setattr(multiCMD, '_build_tasks', lambda *a, **k: (lambda built: (tasks.extend(built[0]), built)[1])(real_build(*a, **k)))
	start_time = time.monotonic()
	# before: the Ctrl-C was taken in the settling, the first command failed with -1 and the others ran
	with pytest.raises(KeyboardInterrupt):
		run_commands([['sleep', '30'], ['true'], ['true']], quiet=True)
	assert sent and time.monotonic() - start_time < 5
	assert tasks[0].returncode not in (0, None) and [task.returncode for task in tasks[1:]] == [None, None]
	assert all(task.stop for task in tasks) and not multiCMD._live_procs


@pytest.mark.skipif(not hasattr(signal, 'pthread_sigmask'), reason='POSIX signals')
def test_a_ctrl_c_while_the_executor_stops_its_commands_is_raised_after(another_thread):
	ex = AsyncExecutor(max_threads=1, quiet=True)
	sent = []
	class Stopping(multiCMD.Task):
		def __setattr__(self, name, value):
			super().__setattr__(name, value)
			if name == 'stop' and value and not sent:
				sent.append(1)
				_ctrl_c()
	ex.tasks.append(Stopping(['x']))
	ex.tasks.append(multiCMD.Task(['y']))
	with pytest.raises(KeyboardInterrupt):
		ex.stop()
	# every task was asked to stop before the Ctrl-C was raised
	assert all(task.stop for task in ex.tasks)


def test_a_second_ctrl_c_while_join_threads_stops_its_commands_ends_the_wait():
	release = threading.Event()
	sent = []
	class Thread(multiCMD._Thread):
		@property
		def task(self):
			if threading.current_thread() is threading.main_thread() and not sent:
				sent.append(1)
				# read in the settled stop step: a second Ctrl-C lands then
				_ctrl_c()
			return self._task
		@task.setter
		def task(self, value):
			self._task = value
	thread = Thread(release.wait, (10,), multiCMD.Task(['x']))
	thread.start()
	try:
		start = time.monotonic()
		with pytest.raises(KeyboardInterrupt), sigint_main_after(0.2):
			multiCMD.join_threads(threads=[thread])
		# before: the second Ctrl-C was taken by the stop and the wait went on for 5 s
		assert sent and time.monotonic() - start < 3
		assert thread._task.stop
	finally:
		release.set()
	assert multiCMD._join(thread, 5)


# ---- fire-and-forget: the whole batch is tracked before any starts, and none is left tracked that never runs

def test_a_join_threads_meanwhile_sees_the_whole_fire_and_forget_batch(monkeypatch):
	seen = []
	real_start = multiCMD._Thread.start
	def start(self):
		if getattr(self, '_target', None) is getattr(multiCMD, '__run_tracked_command') and not seen:
			seen.append(set(_global_threads()))
		real_start(self)
	monkeypatch.setattr(multiCMD._Thread, 'start', start)
	tasks = run_commands([['true']] * 3, quiet=True, wait_for_return=False, return_object=True)
	# before: only the thread about to start was registered, and a join_threads() could return before the rest
	assert seen and {task.thread for task in tasks} <= seen[0]
	multiCMD.join_threads()
	assert [task.returncode for task in tasks] == [0, 0, 0]


def test_a_second_ctrl_c_before_the_starts_settle_leaves_no_thread_tracked(monkeypatch):
	# ( review probe ff_escape.py ) the first Ctrl-C as the second thread starts, the second as _start_threads()
	# begins to settle: before the fix a registered thread that never started stayed alive, and join_threads() hung
	tasks = []
	real_build = multiCMD._build_tasks
	monkeypatch.setattr(multiCMD, '_build_tasks', lambda *a, **k: (lambda built: (tasks.extend(built[0]), built)[1])(real_build(*a, **k)))
	_fail_nth_start(monkeypatch, getattr(multiCMD, '__run_tracked_command'), 2, KeyboardInterrupt())
	real_settle = multiCMD._settle
	armed = [True]
	def settle(step):
		if armed[0] and step.__qualname__ == '_start_threads.<locals>.settle':
			armed[0] = False
			raise KeyboardInterrupt('second Ctrl-C')
		return real_settle(step)
	monkeypatch.setattr(multiCMD, '_settle', settle)
	with pytest.raises(KeyboardInterrupt):
		run_commands([['sleep', '30']] * 3, max_threads=0, quiet=True, wait_for_return=False)
	assert not armed[0]
	assert _join_threads_returns_within(3)
	assert not any(thread.is_alive() for thread in _global_threads())
	assert all(task.stop and not task.thread.is_alive() for task in tasks) and not _runs(tasks[2].thread)
	assert not multiCMD._live_procs


# ---- a command interrupted as it starts is always registered while it is killed

def test_a_ctrl_c_before_the_registration_still_registers_the_command_while_it_is_killed(monkeypatch):
	real_register = multiCMD._register
	armed = [True]
	def register(proc, pgid):
		if armed[0]:
			armed[0] = False
			raise KeyboardInterrupt
		real_register(proc, pgid)
	monkeypatch.setattr(multiCMD, '_register', register)
	registered = []
	real_kill = multiCMD._kill_commands
	def kill(commands, first_signal, *args):
		commands = list(commands)
		registered.extend(proc in multiCMD._live_procs for proc, _ in commands)
		real_kill(commands, first_signal, *args)
	monkeypatch.setattr(multiCMD, '_kill_commands', kill)
	with pytest.raises(KeyboardInterrupt):
		multiCMD.run_command(['sleep', '30'], quiet=True)
	# before: a second Ctrl-C during the kill left a command neither dead nor known to kill_all()
	assert registered == [True]
	assert not multiCMD._live_procs


@pytest.mark.skipif(not sys.platform.startswith('linux'), reason='checks processes through /proc')
def test_a_ctrl_c_while_the_command_starts_still_stops_it(monkeypatch, tmp_path):
	pidfile = tmp_path / 'pid'
	real_spawn = multiCMD._spawn
	def spawn(*args, **kwargs):
		result = real_spawn(*args, **kwargs)
		deadline = time.monotonic() + 5
		while not pidfile.exists() and time.monotonic() < deadline:
			time.sleep(0.01)
		# lands once the command runs, before the spawn has even returned
		_ctrl_c()
		return result
	monkeypatch.setattr(multiCMD, '_spawn', spawn)
	with pytest.raises(KeyboardInterrupt):
		multiCMD.run_command(['sh', '-c', f'echo $$ > {pidfile}.tmp; mv {pidfile}.tmp {pidfile}; exec sleep 30'], quiet=True)
	# before ( 5f7468d ): the Ctrl-C lost the command, which ran on in its own process group, unknown to kill_all()
	assert pid_gone(int(pidfile.read_text()))
	assert not multiCMD._live_procs


# ---- a command that cannot be run is recorded whole, whatever cuts the recording short

def test_a_missing_command_cut_short_while_it_is_recorded_still_gets_127(monkeypatch):
	class Cut(list):
		armed = True
		def append(self, item):
			super().append(item)
			if Cut.armed:
				Cut.armed = False
				raise KeyboardInterrupt
	tasks = []
	real_build = multiCMD._build_tasks
	def build(*args, **kwargs):
		built = real_build(*args, **kwargs)
		for task in built[0]:
			task.stderr = Cut()
		tasks.extend(built[0])
		return built
	monkeypatch.setattr(multiCMD, '_build_tasks', build)
	with pytest.raises(KeyboardInterrupt):
		multiCMD.run_command(['mcmd-no-such-command-xyz'], quiet=True)
	# before: the error text was recorded with return code None
	assert tasks[0].returncode == 127 and len(tasks[0].stderr) == 1


# ---- the pool: a failed start leaves the tasks to the workers that start, and failing every task is settled

def _failing_worker_starts(monkeypatch, failing):
	'''Make the pool's worker starts whose 1-based number is in failing raise; returns the list of starts tried'''
	calls = []
	real_start = multiCMD._Thread.start
	def start(self):
		if getattr(self, '_target', None) is not None and self._target.__name__ == 'worker':
			calls.append(self)
			if len(calls) in failing:
				raise RuntimeError("can't start new thread")
		real_start(self)
	monkeypatch.setattr(multiCMD._Thread, 'start', start)
	return calls


def test_the_pool_runs_the_tasks_on_the_workers_that_start_when_the_first_cannot(monkeypatch, capfd):
	# ( review round 8, finding 2 ) a passing failure ( EAGAIN ) of the very first start
	calls = _failing_worker_starts(monkeypatch, {1})
	tasks = run_commands([['true']] * 4, max_threads=4, quiet=False, return_object=True)
	# before ( a96b922 ): no other worker was tried, and every task failed with -1
	assert len(calls) == 4
	assert [task.returncode for task in tasks] == [0] * 4
	assert "multiCMD: started 3 of 4 threads: can't start new thread" in capfd.readouterr().err


def test_the_pool_stops_starting_workers_after_two_in_a_row_cannot_start(monkeypatch, capfd):
	calls = _failing_worker_starts(monkeypatch, {2, 3, 4, 5})
	tasks = run_commands([['true']] * 6, max_threads=5, quiet=False, return_object=True)
	# before ( 5f7468d ): every one of the 5 workers was tried
	assert len(calls) == 3
	assert [task.returncode for task in tasks] == [0] * 6
	assert "multiCMD: started 1 of 5 threads: can't start new thread" in capfd.readouterr().err


def test_a_pool_that_cannot_start_two_workers_in_a_row_fails_every_task(monkeypatch):
	calls = _failing_worker_starts(monkeypatch, set(range(1, 10)))
	tasks = run_commands([['true']] * 4, max_threads=4, quiet=True, return_object=True)
	assert len(calls) == 2
	assert [task.returncode for task in tasks] == [-1] * 4 and all(task._error for task in tasks)


def test_a_pool_whose_workers_cannot_start_fails_every_task_whatever_cuts_it_short(monkeypatch):
	real_start = multiCMD._Thread.start
	def start(self):
		if getattr(self, '_target', None) is not None and self._target.__name__ == 'worker':
			raise RuntimeError("can't start new thread")
		real_start(self)
	monkeypatch.setattr(multiCMD._Thread, 'start', start)
	real_fail = multiCMD._fail_task
	count = [0]
	def fail(*args, **kwargs):
		real_fail(*args, **kwargs)
		count[0] += 1
		if count[0] == 2:
			raise KeyboardInterrupt
	monkeypatch.setattr(multiCMD, '_fail_task', fail)
	tasks = []
	real_build = multiCMD._build_tasks
	monkeypatch.setattr(multiCMD, '_build_tasks', lambda *a, **k: (lambda built: (tasks.extend(built[0]), built)[1])(real_build(*a, **k)))
	with pytest.raises(KeyboardInterrupt):
		run_commands([['true']] * 4, max_threads=2, quiet=True)
	# before: the tasks after the interrupted one were left with return code None
	assert [task.returncode for task in tasks] == [-1] * 4 and all(task._finalized for task in tasks)


# ---- review round 8: a normal run touches no signal state

def _normal_runs():
	'''A successful run through every API, the failure-free paths only'''
	assert multiCMD.run_command(['echo', 'a'], quiet=True) == ['a']
	assert multiCMD.run_command(['sh', '-c', 'echo $MCMD_X'], quiet=True, extra_env={'MCMD_X': 'x'}) == ['x']
	assert multiCMD.run_command('echo b | cat', quiet=True, shell=True) == ['b']
	if os.name != 'nt':
		assert multiCMD.run_command(['echo', 'c'], quiet=True, pty=True) == ['c']
	assert run_commands([['true']] * 4, max_threads=2, quiet=True, return_code_only=True) == [0] * 4
	assert run_commands([['true']] * 3, max_threads=0, quiet=True, return_code_only=True) == [0] * 3
	tasks = run_commands([['true']] * 3, quiet=True, wait_for_return=False, return_object=True)
	multiCMD.join_threads()
	assert [task.returncode for task in tasks] == [0] * 3
	ex = AsyncExecutor(max_threads=2, quiet=True)
	ex.run_commands([['true']] * 3)
	ex.wait()
	assert [task.returncode for task in ex.tasks] == [0] * 3
	ex.cleanup()
	# a timeout kills the command: still no signal handler set
	assert multiCMD.run_command(['sleep', '5'], quiet=True, timeout=0.3, return_code_only=True) == 124
	# ( review of round 9, finding 4 ) nothing to run: statbtrfs.execute_commands([]) and hpcp do this
	assert run_commands([], max_threads=4, quiet=True) == [] and run_commands([], quiet=True) == []
	# the executor stopped, and cleaned up, while a command runs and others are queued
	for finish in ('stop', 'cleanup'):
		ex = AsyncExecutor(max_threads=1, quiet=True)
		ex.run_commands([['sleep', '5']] * 3)
		deadline = time.monotonic() + 5
		while not multiCMD._live_procs and time.monotonic() < deadline:
			time.sleep(0.01)
		getattr(ex, finish)()
		assert not multiCMD._live_procs


def test_a_normal_run_never_sets_a_signal_handler(monkeypatch):
	# ( review round 8, finding 1 ) round 8 held a Ctrl-C around every spawn, swapping SIGINT's handler each time
	calls = []
	real = signal.signal
	def record(signum, handler):
		calls.append((signum, handler))
		return real(signum, handler)
	monkeypatch.setattr(signal, 'signal', record)
	before = signal.getsignal(signal.SIGINT)
	_normal_runs()
	assert calls == []
	assert signal.getsignal(signal.SIGINT) is before


@pytest.mark.skipif(not hasattr(signal, 'pthread_sigmask'), reason='POSIX signals')
def test_the_rare_paths_never_set_a_signal_handler_either(monkeypatch):
	# ( review of round 9, finding 5 ) a missing command, a thread that cannot start, a Ctrl-C while commands run
	calls = []
	real = signal.signal
	monkeypatch.setattr(signal, 'signal', lambda signum, handler: (calls.append(signum), real(signum, handler))[1])
	assert multiCMD.run_command(['mcmd-no-such-command-xyz'], quiet=True, return_code_only=True) == 127
	_failing_worker_starts(monkeypatch, {1})
	assert run_commands([['true']] * 3, max_threads=3, quiet=True, return_code_only=True) == [0] * 3
	monkeypatch.undo()
	monkeypatch.setattr(signal, 'signal', lambda signum, handler: (calls.append(signum), real(signum, handler))[1])
	for max_threads in (1, 2):
		with pytest.raises(KeyboardInterrupt), sigint_main_after(0.3):
			run_commands([['sleep', '5']] * 2, max_threads=max_threads, quiet=True)
	with pytest.raises(KeyboardInterrupt), sigint_main_after(0.3):
		ex = AsyncExecutor(max_threads=2, quiet=True)
		ex.run_commands([['sleep', '5']] * 3)
		ex.wait()
	assert calls == [] and not multiCMD._live_procs


_C_LEVEL_PROBE = r'''
import ctypes, faulthandler, json, os, platform, signal, struct, sys, time
sys.path.insert(0, sys.argv[1])
sys.path.insert(0, sys.argv[2])
import multiCMD
from test_settle import _normal_runs
def sigaction():
	# glibc x86_64 / aarch64: sa_handler ( 8 bytes ), sa_mask ( 128 ), sa_flags ( int )
	buf = ctypes.create_string_buffer(256)
	assert ctypes.CDLL(None, use_errno=True).sigaction(signal.SIGINT, None, buf) == 0
	return struct.unpack_from('Q', buf.raw, 0)[0], struct.unpack_from('i', buf.raw, 8 + 128)[0]
dump = open(sys.argv[3], 'w')
faulthandler.register(signal.SIGINT, file=dump, chain=True)
signal.siginterrupt(signal.SIGINT, False)
before = sigaction()
_normal_runs()
after = sigaction()
try:
	os.kill(os.getpid(), signal.SIGINT)
	deadline = time.monotonic() + 2
	while time.monotonic() < deadline:
		pass
	interrupted = False
except KeyboardInterrupt:
	interrupted = True
dump.flush()
print(json.dumps({'before': before, 'after': after, 'interrupted': interrupted}))
sys.stdout.flush()
# ( CPython 3.6 crashes at exit after a Ctrl-C that faulthandler chained, with or without multiCMD )
os._exit(0)
'''


def _glibc_sigaction_layout():
	'''Whether struct sigaction is laid out as the probe reads it: glibc on x86_64 / aarch64 Linux'''
	import platform
	return sys.platform.startswith('linux') and platform.libc_ver()[0] == 'glibc' \
		and platform.machine() in ('x86_64', 'aarch64') and hasattr(signal, 'siginterrupt')


@pytest.mark.skipif(not _glibc_sigaction_layout(), reason="reads glibc's struct sigaction ( x86_64 / aarch64 ) and uses signal.siginterrupt")
def test_a_normal_run_leaves_a_c_level_sigint_handler_and_siginterrupt_in_place(tmp_path):
	# ( review round 8, finding 1 ) each signal.signal() put CPython's own C handler back: faulthandler's was dropped,
	# and SA_RESTART with it, by the first serial run_command
	import json
	import subprocess
	dump = tmp_path / 'dump'
	out = subprocess.run([sys.executable, '-c', _C_LEVEL_PROBE, os.path.dirname(multiCMD.__file__), os.path.dirname(__file__), str(dump)],
		stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60, universal_newlines=True)
	assert out.returncode == 0, out.stderr
	result = json.loads(out.stdout.strip().splitlines()[-1])
	SA_RESTART = 0x10000000
	assert result['before'][1] & SA_RESTART
	assert result['after'] == result['before']
	# faulthandler still dumps on a Ctrl-C, then chains to Python's handler
	assert result['interrupted'] and 'most recent call first' in dump.read_text()


# ---- review round 8: a Ctrl-C while multiCMD stops its commands is always raised, never absorbed

@pytest.mark.skipif(not hasattr(signal, 'pthread_sigmask'), reason='POSIX signals')
def test_a_ctrl_c_while_the_executor_stops_inside_the_programs_own_handler_is_raised(another_thread):
	# ( finding 3 ) the program handles a first Ctrl-C itself and stops its executor; a second lands meanwhile
	ex = AsyncExecutor(max_threads=1, quiet=True)
	sent = []
	class Stopping(multiCMD.Task):
		def __setattr__(self, name, value):
			super().__setattr__(name, value)
			if name == 'stop' and value and not sent:
				sent.append(1)
				_ctrl_c()
	ex.tasks.append(Stopping(['x']))
	first = KeyboardInterrupt('the program\'s')
	with pytest.raises(KeyboardInterrupt) as raised:
		try:
			raise first
		except KeyboardInterrupt:
			ex.stop()
	# before ( a96b922 ): taken for a second Ctrl-C of multiCMD's own, and lost: ex.stop() returned
	assert sent and raised.value.__context__ is first


def test_the_programs_handler_gets_every_ctrl_c(another_thread):
	# ( finding 9, review probe p_custom.py ) e.g. "first Ctrl-C: stop gracefully; second: force quit"
	calls = []
	def handler(signum, frame):
		calls.append(signum)
		raise KeyboardInterrupt(len(calls))
	previous = signal.signal(signal.SIGINT, handler)
	release = threading.Event()
	sent = []
	class Thread(multiCMD._Thread):
		@property
		def task(self):
			if threading.current_thread() is threading.main_thread() and not sent:
				sent.append(1)
				_ctrl_c()
			return self._task
		@task.setter
		def task(self, value):
			self._task = value
	thread = Thread(release.wait, (10,), multiCMD.Task(['x']))
	thread.start()
	try:
		with pytest.raises(KeyboardInterrupt), sigint_main_after(0.2):
			multiCMD.join_threads(threads=[thread])
		# before ( a96b922 ): the second Ctrl-C never reached the program's handler
		assert sent and calls == [signal.SIGINT, signal.SIGINT]
	finally:
		signal.signal(signal.SIGINT, previous)
		release.set()
	assert multiCMD._join(thread, 5)


def _in_frames(predicate, depth=6):
	frame = sys._getframe(2)
	for _ in range(depth):
		if frame is None:
			return False
		if predicate(frame.f_code):
			return True
		frame = frame.f_back
	return False


@pytest.mark.skipif(not hasattr(signal, 'pthread_sigmask'), reason='POSIX signals')
@pytest.mark.parametrize('mode', ['fire-and-forget', 'pool'])
def test_a_second_ctrl_c_while_the_starts_are_settled_ends_the_wait(monkeypatch, mode, another_thread):
	# ( finding 4, review probe p_lost2.py ) the first Ctrl-C as the second thread starts, the second while
	# _start_threads() settles: the wait for the command already running must end at once
	target = getattr(multiCMD, '__run_tracked_command') if mode == 'fire-and-forget' else None
	real_start = multiCMD._Thread.start
	starts = []
	first = KeyboardInterrupt('first')
	def start(self):
		mine = getattr(self, '_target', None)
		if mine is not None and (mine is target or (target is None and mine.__name__ == 'worker')):
			starts.append(time.monotonic())
			if len(starts) == 2:
				deadline = time.monotonic() + 5
				while not multiCMD._live_procs and time.monotonic() < deadline:
					time.sleep(0.01)
				starts.append(time.monotonic())
				raise first
		real_start(self)
	monkeypatch.setattr(multiCMD._Thread, 'start', start)
	real_abandon = multiCMD._Thread._abandon
	sent = []
	def abandon(self):
		if not sent and threading.current_thread() is threading.main_thread() \
			and _in_frames(lambda code: code.co_name == 'settle' and 'failing' in code.co_freevars):
			sent.append(1)
			_ctrl_c()
		return real_abandon(self)
	monkeypatch.setattr(multiCMD._Thread, '_abandon', abandon)
	# a command that only SIGKILL ends: stopping it takes over 1 s
	command = ['sh', '-c', 'trap "" INT TERM; sleep 30']
	with pytest.raises(KeyboardInterrupt) as raised:
		if mode == 'fire-and-forget':
			run_commands([command] * 3, max_threads=0, quiet=True, wait_for_return=False)
		else:
			run_commands([command] * 3, max_threads=3, quiet=True)
	elapsed = time.monotonic() - starts[2]
	assert sent and raised.value is not first and raised.value.__context__ is first
	# before ( a96b922 ): the second Ctrl-C was taken, and the wait went on until the command was dead ( > 1 s )
	assert elapsed < 0.9
	monkeypatch.undo()
	# the command is still stopped, by its thread
	deadline = time.monotonic() + 5
	while multiCMD._live_procs and time.monotonic() < deadline:
		time.sleep(0.05)
	assert not multiCMD._live_procs


@pytest.mark.skipif(not hasattr(signal, 'pthread_sigmask'), reason='POSIX signals')
def test_a_second_ctrl_c_while_a_serial_run_deals_with_the_first_ends_the_wait_for_the_command(monkeypatch):
	# ( finding 5 ) the first Ctrl-C while the command runs, the second while _execute() settles: no wait for the kill
	real_abandon = multiCMD._Thread._abandon
	sent = []
	def abandon(self):
		if not sent and threading.current_thread() is threading.main_thread() \
			and _in_frames(lambda code: code.co_name == 'settle' and 'readers' in code.co_freevars):
			sent.append(time.monotonic())
			_ctrl_c()
		return real_abandon(self)
	monkeypatch.setattr(multiCMD._Thread, '_abandon', abandon)
	procs = []
	real_register = multiCMD._register
	monkeypatch.setattr(multiCMD, '_register', lambda proc, pgid: (procs.append(proc), real_register(proc, pgid)))
	try:
		with pytest.raises(KeyboardInterrupt) as raised, sigint_main_after(0.3):
			multiCMD.run_command(['sh', '-c', 'trap "" INT TERM; sleep 30'], quiet=True)
		# before ( a96b922 ): the second Ctrl-C was taken, and the kill waited over 1 s for the command to die
		assert sent and time.monotonic() - sent[0] < 0.9
		assert isinstance(raised.value.__context__, KeyboardInterrupt)
		# left registered, for kill_all() and the exit hook
		assert procs and procs[0] in multiCMD._live_procs
	finally:
		multiCMD.kill_all()
		for proc in procs:
			proc.wait(5)
			multiCMD._unregister(proc)


# ---- review round 8: what a settled step records comes before the Ctrl-C it raises

@pytest.mark.skipif(not hasattr(signal, 'pthread_sigmask'), reason='POSIX signals')
def test_a_ctrl_c_while_a_missing_command_is_recorded_is_raised_once_it_is(monkeypatch, another_thread):
	# ( finding 7 ) the command could not be run ( 127 ), and a Ctrl-C lands as that is recorded
	real_cannot = multiCMD._cannot_run
	sent = []
	def cannot(*args, **kwargs):
		if not sent:
			sent.append(1)
			_ctrl_c()
		return real_cannot(*args, **kwargs)
	monkeypatch.setattr(multiCMD, '_cannot_run', cannot)
	tasks = []
	real_build = multiCMD._build_tasks
	monkeypatch.setattr(multiCMD, '_build_tasks', lambda *a, **k: (lambda built: (tasks.extend(built[0]), built)[1])(real_build(*a, **k)))
	with pytest.raises(KeyboardInterrupt) as raised:
		multiCMD.run_command(['mcmd-no-such-command-xyz'], quiet=True)
	assert tasks[0].returncode == 127 and len(tasks[0].stderr) == 1 and tasks[0]._finalized
	assert isinstance(raised.value.__context__, FileNotFoundError)


@pytest.mark.skipif(not hasattr(signal, 'pthread_sigmask'), reason='POSIX signals')
def test_a_ctrl_c_while_a_pool_fails_its_tasks_is_raised_once_every_one_is_recorded(monkeypatch, another_thread):
	_failing_worker_starts(monkeypatch, set(range(1, 10)))
	real_fail = multiCMD._fail_task
	count = [0]
	def fail(*args, **kwargs):
		count[0] += 1
		if count[0] == 2:
			_ctrl_c()
		real_fail(*args, **kwargs)
	monkeypatch.setattr(multiCMD, '_fail_task', fail)
	tasks = []
	real_build = multiCMD._build_tasks
	monkeypatch.setattr(multiCMD, '_build_tasks', lambda *a, **k: (lambda built: (tasks.extend(built[0]), built)[1])(real_build(*a, **k)))
	with pytest.raises(KeyboardInterrupt):
		run_commands([['true']] * 4, max_threads=2, quiet=True)
	assert [task.returncode for task in tasks] == [-1] * 4 and all(task._finalized for task in tasks)


# ---- review of round 9: a Ctrl-C while the command starts never loses it, and a second one still stops it

def _children():
	'''The pids of this process's children, whichever of its threads started them'''
	pids = set()
	for tid in os.listdir('/proc/self/task'):
		try:
			with open(f'/proc/self/task/{tid}/children') as f:
				pids.update(int(pid) for pid in f.read().split())
		except OSError:
			pass
	return pids


def _zombie(pid):
	try:
		with open(f'/proc/{pid}/stat') as f:
			return f.read().rsplit(')', 1)[1].split()[0] == 'Z'
	except OSError:
		return True


def _describe(pid):
	'''A child left behind, for the failure message: its state, process group, command line, and whether it is registered'''
	try:
		with open(f'/proc/{pid}/stat') as f:
			fields = f.read().rsplit(')', 1)[1].split()
		with open(f'/proc/{pid}/cmdline', 'rb') as f:
			cmdline = f.read().replace(b'\0', b' ')[:80]
	except OSError as e:
		return (pid, repr(e))
	registered = [(proc.returncode, pgid) for proc, pgid in multiCMD._live_procs.copy().items() if proc.pid == pid]
	return (pid, 'state', fields[0], 'pgrp', fields[2], cmdline, 'registered', registered)


def _left_running(before, wait=6.0):
	'''
	The children started since before that still run after up to wait seconds, or that are dead but neither
	reaped nor registered ( kill_all() and the exit hook reap what is registered ). A command cut short as it starts
	is stopped within about 2 s ( the kill ); wait leaves room for load.
	'''
	deadline = time.monotonic() + wait
	while True:
		registered = {proc.pid for proc in multiCMD._live_procs.copy()}
		left = {pid for pid in _children() - before if not _zombie(pid) or pid not in registered}
		if not left or time.monotonic() > deadline:
			return left
		time.sleep(0.02)


def _hook_the_fork(monkeypatch, in_fork, forked):
	'''
	Have in_fork[0] say whether the main thread is inside the C call that forks a command: _fork_exec ( Python 3.11+ ),
	_posixsubprocess.fork_exec ( 3.6 - 3.10 ) or os.posix_spawn. Set before it, cleared once it has returned ( and its
	pid is appended to forked ), so a Ctrl-C whose handler sees it set was raised as that call returned, before Popen
	could store the pid. Nothing after the clear can raise it before Popen has the pid.
	'''
	import _posixsubprocess
	import subprocess
	def hooked(real):
		def fork(*args, **kwargs):
			in_fork[0] = True
			try:
				pid = real(*args, **kwargs)
				forked.append(pid)
			finally:
				in_fork[0] = False
			return pid
		return fork
	if hasattr(subprocess, '_fork_exec'):
		monkeypatch.setattr(subprocess, '_fork_exec', hooked(subprocess._fork_exec))
	else:
		monkeypatch.setattr(_posixsubprocess, 'fork_exec', hooked(_posixsubprocess.fork_exec))
	if hasattr(os, 'posix_spawn'):
		monkeypatch.setattr(os, 'posix_spawn', hooked(os.posix_spawn))


def _collection_phase(collecting, name, info):
	'''The gc.callbacks callback of _collection_watch ( partial()-bound to its dict )'''
	collecting[threading.get_ident()] = name == 'start'


def _collection_watch():
	'''
	A function that says whether a garbage collection runs on this thread now ( from its gc.callbacks start to its stop ),
	one that stops watching, and one that forgets what it saw on this thread, for a caller to call as each run begins,
	when no collection runs. A collection runs finalizers and weakref callbacks from C, wherever it was triggered,
	and CPython drops what is raised there: reported to the unraisable hook, or to stderr before 3.8, or not at all
	( an io object's close() as it is collected, up to 3.12 ). Free-threaded 3.13t also runs a signal handler from within
	a collection, outside any finalizer, with the frame that triggered it as the current one, and drops the
	KeyboardInterrupt it raises silently. So is one raised in the watch's own callback ( _collection_phase ): one that
	landed in its 'stop' before it recorded the end would leave it saying a collection runs, and excuse every Ctrl-C
	after it, without the reset
	'''
	collecting = {}
	phase = functools.partial(_collection_phase, collecting)
	gc.callbacks.append(phase)
	return ((lambda: collecting.get(threading.get_ident(), False)), (lambda: gc.callbacks.remove(phase)),
		(lambda: collecting.pop(threading.get_ident(), None)))


# on Python 3.6 - 3.9 and 3.14 a Ctrl-C right after a C lock's acquire in a `with lock:` leaves the lock held, and
# Thread.start() takes threading's own _active_limbo_lock so: every thread then hangs as it ends ( CPython's own, see
# the README ). The stress test below keeps its Ctrl-Cs out of the output readers' Thread.start() there: one that would
# land inside it is taken right after it
_WITH_MAY_JAM = sys.version_info < (3, 10) or sys.version_info >= (3, 14)


@pytest.mark.skipif(not sys.platform.startswith('linux'), reason='checks processes through /proc')
@pytest.mark.parametrize('ctrl_cs', [1, 2])
def test_a_ctrl_c_while_the_command_starts_loses_it_only_inside_the_fork(launcher, monkeypatch, request, ctrl_cs):
	# a real SIGINT from another thread lands anywhere in the start ( 0-0.5 ms after the spawn begins ), and in the
	# second variant another one 0-2 ms later, while the first is dealt with. A command is lost ( it runs on in its
	# own process group, unknown to kill_all() ) only when the Ctrl-C was raised as the fork returned, before Popen
	# stored the pid, as in 1.54 ( see _execute ). Anywhere else, inside Popen after the fork included ( see _Popen ),
	# it is registered and stopped: before ( 0e4d015 had that; the helper start of fe8fa40 is reverted ) nothing
	# else may be lost, and what a second Ctrl-C left registered is dead
	import random
	main = threading.main_thread().ident
	in_fork = [False]
	forked = []
	_hook_the_fork(monkeypatch, in_fork, forked)
	# per Ctrl-C: whether it landed in a finalizer ( a __del__ or a weakref callback, or pytest's report of one
	# dropped, or anything else that runs during a collection, see _collection_watch ), whether inside the fork, and where
	landed = []
	# whether a garbage collection runs on this thread now ( see _collection_watch )
	collecting, unwatch, reset_watch = _collection_watch()
	request.addfinalizer(unwatch)
	# whether a Ctrl-C raises: only while a run goes on ( one that lands after it is only recorded )
	armed = [False]
	def handler(signum, frame):
		# a finalizer or weakref callback ( a lambda one of the standard library's, as threading has ) that the garbage
		# collector ran wherever it ran, or anything else that runs during a collection, which CPython drops what it
		# raises from ( see _collection_watch ): on 3.6, with no unraisable hook to count it, two Ctrl-Cs landed in
		# functions a collection ran inside os.path.isfile(), _remove and reset, and the second was taken for one
		# multiCMD lost; on 3.13t so was one whose handler a collection itself ran, in Popen.__init__ or _execute_child
		innermost = frame is not None and frame.f_code.co_name == '<lambda>' and frame.f_code.co_filename != multiCMD.__file__
		names = []
		while frame is not None:
			names.append(frame.f_code.co_name if 'unraisable' not in frame.f_code.co_filename else 'unraisable')
			frame = frame.f_back
		landed.append((innermost or collecting() or bool({'__del__', '_remove', 'unraisable'} & set(names)), in_fork[0], names[:12]))
		if armed[0]:
			raise KeyboardInterrupt
	previous = signal.signal(signal.SIGINT, handler)
	request.addfinalizer(lambda: signal.signal(signal.SIGINT, previous))
	# the KeyboardInterrupts CPython dropped as unraisable ( from Python 3.8 )
	unraisable = []
	if hasattr(sys, 'unraisablehook'):
		# those dropped KeyboardInterrupts are expected here: not pytest's to report ( a second Ctrl-C landing in
		# its report fails the test )
		monkeypatch.setattr(sys, 'unraisablehook', lambda dropped: unraisable.append(dropped.exc_type))
	if _WITH_MAY_JAM:
		real_start = threading.Thread.start
		def start(self):
			if getattr(self, '_target', None) is multiCMD._read_stream:
				# ( unblocked whatever happens: a Ctrl-C that landed before the block is raised right after it )
				try:
					signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT})
					real_start(self)
				finally:
					signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGINT})
			else:
				real_start(self)
		monkeypatch.setattr(threading.Thread, 'start', start)
	spawning = threading.Event()
	# per run: whether _spawn returned
	spawned = []
	real_spawn = multiCMD._spawn
	def spawn(*args, **kwargs):
		spawning.set()
		result = real_spawn(*args, **kwargs)
		spawned.append(True)
		return result
	monkeypatch.setattr(multiCMD, '_spawn', spawn)
	before = _children()
	# the children lost and not excused; those lost inside the fork; the runs cut short between the fork and the end of
	# the spawn ( where _Popen hands the command over )
	lost, forks, recovered = [], [], []
	request.addfinalizer(lambda: (multiCMD.kill_all(), [multiCMD._live_procs.pop(proc, None) for proc in multiCMD._live_procs.copy()]))
	for _ in range(100):
		# ( a spawn takes some 60 - 150 us: the timer's own wake-up takes about as long )
		first = random.choice([0, random.uniform(0, 0.0005)])
		delays = [first] + ([first + random.uniform(0, 0.002)] if ctrl_cs == 2 else [])
		spawning.clear()
		del spawned[:], forked[:]
		sent = []
		def send():
			try:
				# from the moment the spawn begins ( however long getting there took, under load )
				spawning.wait(1)
				start = time.monotonic()
				for delay in delays:
					time.sleep(max(0, start + delay - time.monotonic()))
					signal.pthread_kill(main, signal.SIGINT)
			finally:
				sent.append(1)
		del landed[:], unraisable[:]
		reset_watch()
		try:
			try:
				# ( a bare thread: Thread.start() and join() take threading's own locks with `with`, which a Ctrl-C
				# can leave held on 3.6 - 3.9 and 3.14, and every thread then hangs as it ends )
				armed[0] = True
				_thread.start_new_thread(send, ())
				multiCMD.run_command(['sleep', '3'], quiet=True)
			finally:
				# a Ctrl-C from here on is only recorded ( none raises outside the run, where nothing would take it );
				# wait until every SIGINT is sent and its handler has run ( two sent close together may come as one: up
				# to 1 s )
				armed[0] = False
				waited = time.monotonic() + 1
				while not sent or (len(landed) < len(delays) and time.monotonic() < waited):
					time.sleep(0.001)
		except KeyboardInterrupt:
			pass
		else:
			# CPython drops a KeyboardInterrupt its handler raises inside a __del__ or a weakref callback ( a Popen or
			# a Thread collected on the main thread, a lambda weakref callback of threading's ): that Ctrl-C never
			# reached multiCMD, and it is the only excuse ( told by the unraisable hook, or by the frames: not every
			# such drop reaches the hook on free-threaded 3.13t, see _collection_watch )
			assert landed and (unraisable.count(KeyboardInterrupt) >= len(landed)
				or all(dropped for dropped, _, _ in landed)), (landed, unraisable)
		inside = any(inside for _, inside, _ in landed)
		left = _left_running(before, wait=0 if inside else 6.0)
		if inside and len(left) <= 1:
			# the one window that loses a command: kill it, and reap it ( nothing else holds it )
			forks.extend(left)
			for pid in left:
				try:
					os.killpg(pid, signal.SIGKILL)
				except OSError:
					os.kill(pid, signal.SIGKILL)
				os.waitpid(pid, 0)
		else:
			lost.extend(left)
		if forked and not spawned and not inside and landed:
			# cut short after the fork, before _spawn returned ( see _Popen ), and still stopped
			recovered.append(1)
		# what a second Ctrl-C left registered is dead by now: reap it, as kill_all() would ( _wait_all(), not
		# proc.wait(): that Ctrl-C may have landed inside a poll(), and left its Popen lock held, see _poll )
		left_registered = list(multiCMD._live_procs.copy())
		assert ctrl_cs == 2 or not left_registered
		assert multiCMD._wait_all([(proc, None) for proc in left_registered], 5), left_registered
		for proc in left_registered:
			multiCMD._unregister(proc)
		if lost:
			break
	described = [_describe(pid) for pid in lost]
	for pid in lost:
		for kill in (os.killpg, os.kill):
			try:
				kill(pid, signal.SIGKILL)
			except OSError:
				pass
	# before ( this test with 0e4d015's _Popen gone ): a command cut short inside Popen after the fork, or before the
	# spawn returned, left running unknown to kill_all()
	assert described == [], (len(forks), len(recovered))


def test_the_stress_tests_excuse_is_only_what_runs_during_a_collection():
	# ( matrix of round 12, finding 4 ) on 3.6 two Ctrl-Cs landed in functions run from C inside os.path.isfile(), a
	# WeakSet's _remove and one named reset, and on 3.13t one's handler ran in Popen.__init__ and _execute_child with no
	# finalizer frame above it, and CPython dropped them all; the stress test knew only __del__, _remove and lambdas,
	# and took them for interrupts multiCMD lost. They ran during a garbage collection, which _collection_watch() tells
	# from gc.callbacks: a weakref callback a collection runs is excused, and the same callback run otherwise is not,
	# nor is a function called by the run itself
	collecting, unwatch, _ = _collection_watch()
	seen = []
	try:
		class Box:
			pass
		def reset(ref):
			seen.append(('reset', collecting()))
		def on_the_path():
			seen.append(('on_the_path', collecting()))
		# a cycle only a collection frees, its weakref callback run by it
		gc.disable()
		try:
			box = Box()
			box.cycle = box
			ref = weakref.ref(box, reset)
			del box
			gc.collect()
			# the same callback as the last reference goes, outside any collection
			other = Box()
			other_ref = weakref.ref(other, reset)
			del other
		finally:
			gc.enable()
		on_the_path()
	finally:
		unwatch()
	assert seen == [('reset', True), ('reset', False), ('on_the_path', False)]
	assert ref() is None and other_ref() is None


@pytest.mark.skipif(not sys.platform.startswith('linux'), reason='reads /proc/<pid>/status')
def test_a_second_ctrl_c_while_a_serial_run_deals_with_the_first_still_stops_the_command(monkeypatch, tmp_path):
	# ( review of round 9, blocker 2 ) it ends the wait, but the command is sent SIGTERM before it is re-raised
	marker = tmp_path / 'term'
	real_abandon = multiCMD._Thread._abandon
	sent = []
	def abandon(self):
		if not sent and threading.current_thread() is threading.main_thread() \
			and _in_frames(lambda code: code.co_name == 'settle' and 'readers' in code.co_freevars):
			sent.append(time.monotonic())
			_ctrl_c()
		return real_abandon(self)
	monkeypatch.setattr(multiCMD._Thread, '_abandon', abandon)
	procs = []
	real_register = multiCMD._register
	monkeypatch.setattr(multiCMD, '_register', lambda proc, pgid: (procs.append(proc), real_register(proc, pgid)))
	try:
		with pytest.raises(KeyboardInterrupt), sigint_main_after(0.3):
			multiCMD.run_command(['sh', '-c', f'trap "" INT; trap "echo term > {marker}; exit 0" TERM; sleep 30 & wait'], quiet=True)
		assert sent and time.monotonic() - sent[0] < 0.9
		# before ( 0e4d015 ): never signalled, it ran on until kill_all() or the exit hook
		assert procs and procs[0] in multiCMD._live_procs
		assert procs[0].wait(5) == 0 and marker.read_text().strip() == 'term'
	finally:
		multiCMD.kill_all()
		for proc in procs:
			proc.wait(5)
			multiCMD._unregister(proc)


@pytest.mark.parametrize('mode', ['fire-and-forget', 'pool'])
def test_a_ctrl_c_right_after_the_starts_is_a_first_one_and_is_waited_for(monkeypatch, mode):
	# ( review of round 9, finding 7 ) one that lands as _start_threads() returns, before the caller knows the
	# starts are over, was taken for one the starts had dealt with: the commands were not waited for
	real_start_threads = multiCMD._start_threads
	def start_threads(threads, *args, **kwargs):
		running = real_start_threads(threads, *args, **kwargs)
		# ( the workers' own, of their output readers, are left alone )
		if threading.current_thread() is threading.main_thread():
			deadline = time.monotonic() + 5
			while len(multiCMD._live_procs) < 2 and time.monotonic() < deadline:
				time.sleep(0.01)
			raise KeyboardInterrupt
		return running
	monkeypatch.setattr(multiCMD, '_start_threads', start_threads)
	with pytest.raises(KeyboardInterrupt):
		if mode == 'fire-and-forget':
			run_commands([['sleep', '30']] * 2, max_threads=0, quiet=True, wait_for_return=False)
		else:
			run_commands([['sleep', '30']] * 2, max_threads=2, quiet=True)
	# before ( 0e4d015 ): returned at once, the commands still being stopped by their threads
	assert not multiCMD._live_procs
	monkeypatch.undo()
	assert _join_threads_returns_within(5)


def test_a_ctrl_c_that_thread_start_turns_into_another_error_is_still_raised(monkeypatch):
	# ( seen on free-threaded 3.13t ) a Ctrl-C inside Condition.wait()'s _acquire_restore(), as Thread.start() waits
	# for an output reader: the Condition's release then raises RuntimeError, the reader runs all the same, and
	# _start_threads() took the error for a start that worked after all: the Ctrl-C was lost and the run went on
	real_start = threading.Thread.start
	armed = [True]
	def start(self):
		real_start(self)
		if armed[0] and getattr(self, '_target', None) is multiCMD._read_stream:
			armed[0] = False
			try:
				raise KeyboardInterrupt
			except KeyboardInterrupt:
				raise RuntimeError('release unlocked lock')
	monkeypatch.setattr(threading.Thread, 'start', start)
	started = time.monotonic()
	with pytest.raises(KeyboardInterrupt):
		multiCMD.run_command(['sleep', '2'], quiet=True)
	# before: the command ran to its end and the call returned
	assert time.monotonic() - started < 1.5 and not multiCMD._live_procs


# ---- review of round 10

@pytest.mark.skipif(not hasattr(signal, 'pthread_sigmask'), reason='POSIX signals')
def test_a_thread_start_error_while_the_caller_handles_a_ctrl_c_is_not_that_ctrl_c(monkeypatch, another_thread):
	# ( finding 3; probe r10probes/stale_ctx.py ) a program cleans up after a real Ctrl-C it caught, running commands
	# from its except block, and one pool worker cannot start ( EAGAIN ): that RuntimeError has the Ctrl-C being
	# handled as its context, and was taken for a Ctrl-C that landed during the start
	real_start = threading.Thread.start
	calls = []
	def start(self):
		if getattr(self, '_target', None) is not None and getattr(self._target, '__name__', '') == 'worker':
			calls.append(self)
			if len(calls) == 1:
				raise RuntimeError("can't start new thread")
		real_start(self)
	monkeypatch.setattr(threading.Thread, 'start', start)
	try:
		_ctrl_c()
	except KeyboardInterrupt:
		try:
			tasks = run_commands([['true']] * 4, max_threads=4, quiet=True, return_object=True)
		except KeyboardInterrupt:
			# before ( fdc45b9 ): the Ctrl-C being handled was raised again, and the tasks never ran
			pytest.fail('the Ctrl-C the caller handles was raised again')
	assert len(calls) == 4 and [task.returncode for task in tasks] == [0] * 4


@pytest.mark.skipif(not sys.platform.startswith('linux'), reason='reads /proc/<pid>/status')
def test_a_second_ctrl_c_pending_as_a_serial_run_settles_the_first_still_has_it_settled(monkeypatch):
	# ( review of round 10, finding 9: _execute settles through _settle ) the first Ctrl-C before the command is
	# registered, the second pending as _settle is called, so that it is raised from the call before the step runs:
	# the step runs all the same, knowing of it, and the command is registered and sent SIGTERM before it is re-raised
	procs = []
	real_register = multiCMD._register
	def register(proc, pgid):
		procs.append(proc)
		raise KeyboardInterrupt('first')
	monkeypatch.setattr(multiCMD, '_register', register)
	real_settle = multiCMD._settle
	calls = []
	def settle(step, *args, **kwargs):
		calls.append(step)
		if len(calls) == 1:
			raise KeyboardInterrupt('second, pending')
		return real_settle(step, *args, **kwargs)
	monkeypatch.setattr(multiCMD, '_settle', settle)
	try:
		with pytest.raises(KeyboardInterrupt) as raised:
			multiCMD.run_command(['sleep', '30'], quiet=True)
		assert str(raised.value) == 'second, pending' and len(calls) >= 2
		# left registered, for kill_all() and the exit hook, and sent SIGTERM ( not the kill's SIGINT: no wait for it )
		assert procs and procs[0] in multiCMD._live_procs
		assert procs[0].wait(5) == -signal.SIGTERM
	finally:
		monkeypatch.undo()
		multiCMD.kill_all()
		for proc in procs:
			proc.wait(5)
			multiCMD._unregister(proc)


class _WatchedLock:
	'''A lock for multiCMD that records each acquire made by a settled step, or any at all with always ( see below )'''
	def __init__(self, taken, real, always=False):
		self._real = real
		self._taken = taken
		self._always = always

	def acquire(self, *args, **kwargs):
		if self._always or getattr(_in_settled_step, 'depth', 0):
			# ( for the old registry locks, any acquire at all )
			import traceback
			self._taken.append(''.join(traceback.format_stack(limit=6)))
		return self._real.acquire(*args, **kwargs)

	def __enter__(self):
		self.acquire()
		return self

	def __exit__(self, *exc_info):
		self._real.release()

	def release(self):
		self._real.release()

	def locked(self):
		return self._real.locked()


class _WatchedThreading:
	'''multiCMD's threading module: its locks and semaphores are _WatchedLocks, the rest is threading's own'''
	def __init__(self, taken):
		self._taken = taken

	def Lock(self):
		return _WatchedLock(self._taken, threading.Lock())

	def RLock(self):
		return _WatchedLock(self._taken, threading.RLock())

	def Semaphore(self, value=1):
		return _WatchedLock(self._taken, threading.Semaphore(value))

	def __getattr__(self, name):
		return getattr(threading, name)


_in_settled_step = threading.local()


@pytest.mark.skipif(not hasattr(signal, 'pthread_sigmask'), reason='POSIX signals')
def test_no_settled_step_takes_a_lock(monkeypatch, capfd):
	# ( review of round 10, finding 5; probe r10probes/settle_jam.py ) a settled step runs again after any interrupt
	# that cuts it short, so one that takes a lock with `with` on the main thread, which a Ctrl-C right after the
	# acquire leaves held ( 3.6 - 3.9, 3.14 ), blocks on it every time it runs again: a hang only SIGKILL ends. Every
	# lock and semaphore multiCMD makes here is watched, and every path that settles is taken
	taken, old = [], []
	monkeypatch.setattr(multiCMD, 'threading', _WatchedThreading(taken))
	monkeypatch.setattr(multiCMD, '_display', multiCMD._Display())
	# the old registry locks: multiCMD never takes them at all
	for name in ('__running_threads_lock', '_live_procs_lock'):
		monkeypatch.setitem(multiCMD.__dict__, name, _WatchedLock(old, threading.Lock(), always=True))
	real_settle = multiCMD._settle
	steps = []
	def settle(step, *args, **kwargs):
		def watched():
			steps.append(step)
			_in_settled_step.depth = getattr(_in_settled_step, 'depth', 0) + 1
			try:
				step()
			finally:
				_in_settled_step.depth -= 1
		return real_settle(watched, *args, **kwargs)
	monkeypatch.setattr(multiCMD, '_settle', settle)
	# a command that cannot be run, and one multiCMD fails to run
	assert multiCMD.run_command(['mcmd-no-such-command-xyz'], quiet=False, return_code_only=True) == 127
	with monkeypatch.context() as m:
		m.setattr(multiCMD, '_spawn', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('cannot spawn')))
		assert multiCMD.run_command(['true'], quiet=False, return_code_only=True) == -1
	# pool workers that cannot start: one, and all of them
	with monkeypatch.context() as m:
		_failing_worker_starts(m, {1})
		assert run_commands([['true']] * 3, max_threads=3, quiet=True, return_code_only=True) == [0] * 3
	with monkeypatch.context() as m:
		_failing_worker_starts(m, set(range(1, 10)))
		assert run_commands([['true']] * 3, max_threads=3, quiet=True, return_code_only=True) == [-1] * 3
	# a fire-and-forget thread that cannot start, and a Ctrl-C while they start
	with monkeypatch.context() as m:
		_fail_nth_start(m, getattr(multiCMD, '__run_tracked_command'), 2)
		run_commands([['true']] * 3, quiet=True, wait_for_return=False)
		multiCMD.join_threads()
	with monkeypatch.context() as m:
		_fail_nth_start(m, getattr(multiCMD, '__run_tracked_command'), 2, KeyboardInterrupt())
		with pytest.raises(KeyboardInterrupt):
			run_commands([['sleep', '5']] * 3, quiet=True, wait_for_return=False)
	# a Ctrl-C before an executor's dispatcher runs, and its stop() while commands run and wait
	ex = AsyncExecutor(max_threads=1, quiet=True)
	with monkeypatch.context() as m:
		_fail_nth_start(m, ex._dispatch, 1, KeyboardInterrupt())
		with pytest.raises(KeyboardInterrupt):
			ex.run_commands([['sleep', '5']] * 2)
	ex.run_commands([['sleep', '5']] * 2)
	ex.stop()
	# a Ctrl-C while a serial run ( pipes and pty ), a pool, join_threads() and an executor's wait() wait
	for kwargs in ({}, {'pty': True}, {'max_threads': 2}):
		with pytest.raises(KeyboardInterrupt), sigint_main_after(0.3):
			run_commands([['sleep', '5']] * 2, quiet=True, **kwargs)
	run_commands([['sleep', '5']] * 2, quiet=True, wait_for_return=False)
	with pytest.raises(KeyboardInterrupt), sigint_main_after(0.3):
		multiCMD.join_threads()
	ex = AsyncExecutor(max_threads=2, quiet=True)
	with pytest.raises(KeyboardInterrupt), sigint_main_after(0.3):
		ex.run_commands([['sleep', '5']] * 3)
		ex.wait()
	# and kill_all() and the exit hook with a command running ( for the old registry locks )
	run_commands([['sleep', '5']], quiet=True, wait_for_return=False)
	deadline = time.monotonic() + 5
	while not multiCMD._live_procs and time.monotonic() < deadline:
		time.sleep(0.01)
	multiCMD.kill_all()
	multiCMD._kill_running_at_exit()
	monkeypatch.undo()
	assert _join_threads_returns_within(5) and not multiCMD._live_procs
	assert len(steps) >= 15
	# before ( fdc45b9 ): an executor's _untrack took its _lock, and a fire-and-forget thread's forget took
	# __running_threads_lock, which run_commands() and join_threads() took too
	assert taken == [], taken[0]
	assert old == [], old[0]


_SETTLE_JAM = r'''
import os, signal, sys, threading, time, _thread
sys.path.insert(0, sys.argv[1])
import multiCMD
mode = sys.argv[2]
if mode == 'fire-and-forget':
	# its forget took __running_threads_lock
	lock = multiCMD.__dict__['__running_threads_lock']
	target = multiCMD.__dict__['__run_tracked_command']
else:
	ex = multiCMD.AsyncExecutor(max_threads=1, quiet=True)
	# its _untrack took the executor's _lock
	lock = ex._lock
	target = ex._dispatch
real_start = multiCMD._Thread.start
def start(self):
	if getattr(self, '_target', None) == target:
		# the first Ctrl-C lands as the thread starts, and a second one right after the lock's acquire left it held
		lock.acquire()
		raise KeyboardInterrupt
	real_start(self)
multiCMD._Thread.start = start
main = threading.main_thread().ident
def sender():
	for i in range(6):
		time.sleep(0.3)
		signal.pthread_kill(main, signal.SIGINT)
	time.sleep(0.5)
	print('still stuck after 6 Ctrl-Cs', flush=True)
	os._exit(3)
_thread.start_new_thread(sender, ())
try:
	if mode == 'fire-and-forget':
		multiCMD.run_commands([['true']] * 2, quiet=True, wait_for_return=False)
	else:
		ex.run_commands([['true']] * 2)
except BaseException as e:
	print('returned with', type(e).__name__, flush=True)
	os._exit(0)
'''


@pytest.mark.skipif(not hasattr(signal, 'pthread_kill'), reason='POSIX signals')
@pytest.mark.parametrize('mode', ['fire-and-forget', 'executor'])
def test_a_lock_a_ctrl_c_left_held_never_hangs_a_settled_step(mode):
	# ( review of round 10, finding 5; probe r10probes/settle_jam.py ) with the lock a settled step took jammed, every
	# later Ctrl-C ( a real SIGINT from another thread, 6 of them ) ran the step again into the held lock
	proc = subprocess.run([sys.executable, '-c', _SETTLE_JAM, str(PACKAGE_ROOT), mode], stdout=subprocess.PIPE,
		stderr=subprocess.PIPE, universal_newlines=True, timeout=30)
	# before ( fdc45b9 ): still stuck after 6 Ctrl-Cs
	assert (proc.returncode, proc.stdout.strip()) == (0, 'returned with KeyboardInterrupt'), proc.stderr


class _Cancelled(Exception):
	'''What a program's own SIGINT handler raises instead of KeyboardInterrupt'''


@pytest.fixture
def cancelling_handler(request):
	'''A program's SIGINT handler that raises _Cancelled, an Exception; it counts the Ctrl-Cs it gets'''
	calls = []
	def handler(signum, frame):
		calls.append(signum)
		raise _Cancelled(len(calls))
	previous = signal.signal(signal.SIGINT, handler)
	request.addfinalizer(lambda: signal.signal(signal.SIGINT, previous))
	return calls


@pytest.mark.skipif(not hasattr(signal, 'pthread_sigmask'), reason='POSIX signals')
def test_whatever_a_programs_handler_raises_into_a_settled_step_runs_it_again(cancelling_handler, another_thread):
	# ( review of round 10, finding 6 ) a real Ctrl-C while the executor's stop() asks its tasks to stop, its handler
	# raising an Exception: the step was cut short, and the tasks after the first were never asked
	ex = AsyncExecutor(max_threads=1, quiet=True)
	sent = []
	class Stopping(multiCMD.Task):
		def __setattr__(self, name, value):
			super().__setattr__(name, value)
			if name == 'stop' and value and not sent:
				sent.append(1)
				_ctrl_c()
	ex.tasks.append(Stopping(['x']))
	ex.tasks.extend(multiCMD.Task(['y']) for _ in range(3))
	with pytest.raises(_Cancelled):
		ex.stop()
	# before ( fdc45b9 ): only the first was asked to stop
	assert sent and cancelling_handler == [signal.SIGINT]
	assert all(task.stop for task in ex.tasks)


@pytest.mark.skipif(not hasattr(signal, 'pthread_sigmask'), reason='POSIX signals')
def test_whatever_a_programs_handler_raises_while_a_pool_fails_its_tasks_leaves_none_unrecorded(monkeypatch, cancelling_handler, another_thread):
	# ( review of round 10, finding 6 ) no pool worker can start, and a real Ctrl-C lands as the second of the tasks
	# is failed, its handler raising an Exception: before, the step was cut short there, and the tasks after it were
	# left with returncode None, never run nor failed
	_failing_worker_starts(monkeypatch, set(range(1, 10)))
	real_fail = multiCMD._fail_task
	count = [0]
	def fail(*args, **kwargs):
		count[0] += 1
		if count[0] == 2:
			_ctrl_c()
		real_fail(*args, **kwargs)
	monkeypatch.setattr(multiCMD, '_fail_task', fail)
	tasks = []
	real_build = multiCMD._build_tasks
	monkeypatch.setattr(multiCMD, '_build_tasks', lambda *a, **k: (lambda built: (tasks.extend(built[0]), built)[1])(real_build(*a, **k)))
	with pytest.raises(_Cancelled):
		run_commands([['true']] * 4, max_threads=2, quiet=True)
	assert cancelling_handler == [signal.SIGINT]
	# before ( fdc45b9 ): [-1, None, None, None]
	assert [task.returncode for task in tasks] == [-1] * 4 and all(task._finalized for task in tasks)


def test_a_settled_step_that_fails_on_its_own_is_not_run_for_ever():
	# what it raises every time is raised after a few runs, the first one
	runs = []
	def step():
		runs.append(1)
		raise ValueError(len(runs))
	with pytest.raises(ValueError) as raised:
		multiCMD._settle(step)
	assert len(runs) == multiCMD._STEP_FAILURES and raised.value.args == (1,)
	# an interrupt among them is raised first, and does not count as a failure
	runs[:] = []
	def step2():
		runs.append(1)
		if len(runs) == 1:
			raise KeyboardInterrupt
		if len(runs) == 2:
			raise ValueError
	with pytest.raises(KeyboardInterrupt):
		multiCMD._settle(step2)
	assert len(runs) == 3


@pytest.mark.skipif(os.name == 'nt', reason='POSIX fork')
def test_a_popen_is_handed_over_before_it_forks(launcher, monkeypatch):
	# ( review of round 11, finding 7 ) _Popen appended itself to started once Popen had returned or raised, after a
	# check of its pid: on Python 3.6 - 3.9 a Ctrl-C can be raised between nearly any two instructions, so one landing
	# between that check and the append dropped a command that runs. It is in started before the fork now, and the pid
	# Popen stores on it is all the caller needs
	started = []
	at_fork = []
	import _posixsubprocess
	import subprocess
	def hooked(real):
		def fork(*args, **kwargs):
			at_fork.append(list(started))
			return real(*args, **kwargs)
		return fork
	if hasattr(subprocess, '_fork_exec'):
		monkeypatch.setattr(subprocess, '_fork_exec', hooked(subprocess._fork_exec))
	else:
		monkeypatch.setattr(_posixsubprocess, 'fork_exec', hooked(_posixsubprocess.fork_exec))
	if hasattr(os, 'posix_spawn'):
		monkeypatch.setattr(os, 'posix_spawn', hooked(os.posix_spawn))
	proc, pgid = multiCMD._spawn_process(['sleep', '30'], subprocess.DEVNULL, subprocess.DEVNULL, started=started)
	try:
		# before ( fc0c782 ): nothing in started yet as it forked
		assert len(at_fork) == 1 and [entry[0] for entry in at_fork[0]] == [proc]
		assert multiCMD._started_command(started) == (proc, pgid) and pgid == proc.pid
	finally:
		proc.kill()
		proc.wait(5)


def test_a_settled_step_that_fails_on_its_own_once_is_not_raised_once_it_completes(capfd):
	# ( review of round 11, finding 5 ) a failure of the step's own that a run again got past was raised all the same
	runs = []
	def step():
		runs.append(1)
		if len(runs) == 1:
			raise OSError('transient')
	# before ( fc0c782 ): raised OSError
	multiCMD._settle(step)
	assert len(runs) == 2
	err = capfd.readouterr().err
	assert err.count('OSError: transient') == 1, err
	runs[:] = []
	multiCMD._settle(step, quiet=True)
	assert len(runs) == 2 and capfd.readouterr().err == ''


def test_a_ctrl_c_whose_clean_up_fails_on_its_own_once_is_still_a_ctrl_c(monkeypatch, capfd):
	# ( review of round 11, finding 5 ) _settle() put the step's own failures in cuts with the interrupts: in _execute,
	# a clean-up after a Ctrl-C that failed once on its own was taken for a second Ctrl-C ( the SIGTERM shortcut, no
	# wait ), and `raise cut` raised that failure instead of the KeyboardInterrupt, which __run_command recorded as -1
	# and swallowed: the run went on as if no Ctrl-C had come
	procs = []
	real_register = multiCMD._register
	def register(proc, pgid):
		procs.append(proc)
		real_register(proc, pgid)
		if len(procs) == 1:
			raise KeyboardInterrupt
	monkeypatch.setattr(multiCMD, '_register', register)
	real_settle = multiCMD._settle
	def settle(step, *args, **kwargs):
		if getattr(step, '__name__', '') == 'settle' and 'readers' in step.__code__.co_freevars:
			failed = []
			def flaky():
				if not failed:
					failed.append(1)
					raise OSError('flaky clean-up')
				step()
			return real_settle(flaky, *args, **kwargs)
		return real_settle(step, *args, **kwargs)
	monkeypatch.setattr(multiCMD, '_settle', settle)
	tasks = []
	real_build = multiCMD._build_tasks
	monkeypatch.setattr(multiCMD, '_build_tasks', lambda *a, **k: (lambda built: (tasks.extend(built[0]), built)[1])(real_build(*a, **k)))
	try:
		# before ( fc0c782 ): the OSError was recorded as -1 and the run returned, the second command run
		with pytest.raises(KeyboardInterrupt):
			run_commands([['sleep', '30'], ['true']], quiet=True)
		# the Ctrl-C's own stop: the command killed by the kill sequence, waited for, and the next one skipped
		assert procs[0].returncode == -signal.SIGINT and tasks[0].returncode == -signal.SIGINT and not tasks[0]._error
		assert tasks[1].returncode is None and tasks[1].stop
		assert procs[0] not in multiCMD._live_procs
	finally:
		multiCMD.kill_all(signal.SIGKILL)


def _builds(monkeypatch):
	'''The tasks every run_commands() call builds, in a list'''
	tasks = []
	real_build = multiCMD._build_tasks
	monkeypatch.setattr(multiCMD, '_build_tasks', lambda *a, **k: (lambda built: (tasks.extend(built[0]), built)[1])(real_build(*a, **k)))
	return tasks


@pytest.mark.skipif(not sys.platform.startswith('linux'), reason='checks processes through /proc')
@pytest.mark.parametrize('where', ['waiting', 'draining'])
def test_what_a_programs_handler_raises_into_a_serial_run_stops_the_run(monkeypatch, tmp_path, cancelling_handler, where):
	# ( review of round 11, finding 6 ) a program's SIGINT handler that raises an Exception of its own while a serial run
	# waits for its command: _execute stopped the command, and __run_command's `except Exception` recorded the
	# handler's exception as multiCMD's own failure ( -1 ) and swallowed it, so the run went on with the next command
	tasks = _builds(monkeypatch)
	pidfile = tmp_path / 'pid'
	first = ['sleep', '30'] if where == 'waiting' else ['sh', '-c', f'sleep 30 & echo $! > {pidfile}; echo hi']
	main = threading.main_thread().ident
	timer = threading.Timer(0.5, signal.pthread_kill, (main, signal.SIGINT))
	timer.start()
	try:
		# before ( fc0c782 ): nothing raised, [-1, 0]
		with pytest.raises(_Cancelled):
			run_commands([first, ['true']], quiet=True)
	finally:
		timer.cancel()
	assert cancelling_handler == [signal.SIGINT]
	if where == 'waiting':
		# recorded as stopped ( killed by the kill sequence's SIGINT ), not as a failure of multiCMD's
		assert tasks[0].returncode == -signal.SIGINT and not tasks[0]._error
	else:
		# the command itself had exited; what held its output is stopped
		assert tasks[0].returncode == 0 and tasks[0].stdout == ['hi']
		pid = int(pidfile.read_text())
		gone = pid_gone(pid, wait=2)
		if not gone:
			os.kill(pid, signal.SIGKILL)
		assert gone
	assert tasks[0]._finalized and tasks[1].returncode is None and tasks[1].stop
	assert not multiCMD._live_procs and not multiCMD._draining


@pytest.mark.skipif(not sys.platform.startswith('linux'), reason='POSIX commands')
def test_no_generator_of_multicmds_is_closed_on_the_main_threads_paths(launcher, tmp_path):
	# ( found by the free-threaded stress loop of this round ) a generator left suspended is closed as it is dropped,
	# by GeneratorExit thrown into its frame, and CPython drops as unraisable an interrupt raised there: a Ctrl-C landing
	# as _spawn() returned from its loop over _files_named(), or as _drain() / _wait_all() / AsyncExecutor.wait() left
	# their any() / all() over a generator expression, was lost
	closed = []
	def tracer(frame, event, arg):
		if event == 'exception' and arg[0] is GeneratorExit and frame.f_code.co_filename == multiCMD.__file__:
			closed.append(frame.f_code.co_name)
		return tracer
	sys.settrace(tracer)
	try:
		# a bare name ( looked up in PATH ), output drained after the command exits, a kill, an executor's wait
		assert run_commands([['echo', 'a'], ['sh', '-c', 'sleep 0.3 & echo b']], quiet=True) == [['a'], ['b']]
		proc, pgid = multiCMD._spawn(['sleep', '30'], subprocess.DEVNULL, subprocess.DEVNULL)
		other, other_pgid = multiCMD._spawn(['sleep', '30'], subprocess.DEVNULL, subprocess.DEVNULL)
		multiCMD._kill_commands([(proc, pgid), (other, other_pgid)], signal.SIGTERM)
		ex = AsyncExecutor(max_threads=2, quiet=True)
		ex.run_commands([['sleep', '0.2'], ['true']])
		assert ex.wait(timeout=10) == []
	finally:
		sys.settrace(None)
	# before ( fc0c782 ): _files_named and the <genexpr>s ( on 3.9, 3.13.17, 3.14: Debian's 3.13.5 closes such a
	# generator without resuming its frame, and passes either way )
	assert closed == []


# ---- matrix of round 12: an interrupt at any point where Python raises one, in turn ( see _interrupt_at )

# the points _interrupt_at puts a Ctrl-C at: calls and returns, or loop back-jumps ( opcode events: from Python 3.7 )
_POINTS = ['calls', pytest.param('jumps', marks=pytest.mark.skipif(sys.version_info < (3, 7), reason='opcode events need Python 3.7+'))]

# code -> its loop back-jumps that are check points ( see _back_jumps )
_JUMPS = {}

def _back_jumps(code):
	'''
	code's loop back-jumps where Python raises a pending interrupt, as { offset: the jump's offset }, for the jump and
	for the EXTENDED_ARG before it, if any ( up to 3.10 the opcode event of such a jump comes at that EXTENDED_ARG only,
	from 3.11 at both ): JUMP_BACKWARD from 3.11 ( not JUMP_BACKWARD_NO_INTERRUPT ), a JUMP_ABSOLUTE to an earlier
	offset before. 3.10 and 3.11 also raise one at a conditional jump back ( POP_JUMP_IF_FALSE,
	POP_JUMP_BACKWARD_IF_FALSE ... ), only when it is taken, which an opcode event cannot tell ( it comes before the
	jump ): those are left out. On 3.6 - 3.9 Python raises one after most instructions, so after a JUMP_ABSOLUTE, at
	its target, with the try blocks of the jump in force
	'''
	jumps = _JUMPS.get(code)
	if jumps is None:
		jumps = _JUMPS[code] = {}
		prefix = None
		for x in dis.get_instructions(code):
			if x.opname == 'EXTENDED_ARG':
				prefix = x.offset if prefix is None else prefix
				continue
			if x.opname == 'JUMP_BACKWARD' or x.opname == 'JUMP_ABSOLUTE' and x.argval < x.offset:
				jumps[x.offset] = x.offset
				if prefix is not None:
					jumps[prefix] = x.offset
			prefix = None
	return jumps


def _interrupt_at(state, counted, points='calls', collecting=None):
	'''
	A hook ( for _hook ) that raises KeyboardInterrupt at the state['k']-th point counted(frame, event) accepts, counting
	from 0. With points 'calls' a profile function, at a Python function's 'call' ( Python raises a pending Ctrl-C as a
	function starts ), its 'return' ( on 3.6 - 3.10 also as the caller gets the result ), and a C function's 'c_return'
	( as its caller gets the result ); with 'jumps' a trace function, at the 'opcode' event of a loop back-jump ( see
	_back_jumps ). That is where Python itself raises a Ctrl-C ( from 3.10 on, only there ), so k from 0 up to the last
	point puts one at each of them in turn. A hook that raises is unset by Python: one per run. It sets state['n']
	( points counted so far ), state['where'] ( the one it raised at ) and state['injected']. Nothing is counted inside a
	finalizer or weakref callback ( a __del__ or a _remove further up the stack ), or while a garbage collection runs
	( collecting: see _collection_watch, reset here as the run begins ): CPython drops what is raised there.
	'''
	state.update(n=0, where=None, injected=False)
	if collecting is not None:
		collecting[2]()
	def excused(frame):
		if collecting is not None and collecting[0]():
			return True
		if frame.f_code is _hook.__code__ or frame.f_code is _unhook.__code__:
			# ( the test's own: setting the hook, and unsetting it )
			return True
		while frame is not None:
			if frame.f_code.co_name in ('__del__', '_remove') or frame.f_code is _collection_phase.__code__:
				return True
			frame = frame.f_back
		return False
	def inject(frame, event, what):
		if state['n'] == state['k']:
			state['injected'] = True
			state['where'] = (event, frame.f_code.co_name, frame.f_lineno, what)
			raise KeyboardInterrupt('injected')
		state['n'] += 1
	def profile(frame, event, arg):
		if event in ('call', 'return', 'c_return') and counted(frame, event) and not excused(frame):
			inject(frame, event, getattr(arg, '__name__', None) if event == 'c_return' else None)
	# the frame and the jump whose EXTENDED_ARG was counted: its own event, which follows from 3.11, is the same point
	prefixed = [None]
	def trace(frame, event, arg):
		# ( at every event: from 3.12 one set as the frame's 'call' comes, before it has a trace function, does nothing )
		frame.f_trace_opcodes = True
		if event != 'opcode':
			return trace
		jump = _back_jumps(frame.f_code).get(frame.f_lasti)
		if jump is None or (frame.f_lasti == jump and prefixed[0] == (frame, jump)):
			return trace
		prefixed[0] = (frame, jump) if frame.f_lasti != jump else None
		if counted(frame, event) and not excused(frame):
			inject(frame, event, jump)
		return trace
	return profile if points == 'calls' else trace


def _hook(hook):
	'''
	Set a hook from _interrupt_at on this thread: a profile function for every frame from here on, a trace function for
	those and for the frames running already, up to the test's own ( a loop of theirs is traced too )
	'''
	if hook.__name__ == 'profile':
		sys.setprofile(hook)
		return
	sys.settrace(hook)
	frame = sys._getframe(1)
	while frame is not None and not frame.f_code.co_name.startswith('test_'):
		frame.f_trace = hook
		frame.f_trace_opcodes = True
		frame = frame.f_back


def _unhook():
	sys.setprofile(None)
	sys.settrace(None)


@pytest.fixture
def collecting():
	'''A _collection_watch() for _interrupt_at, for the test's length'''
	watch = _collection_watch()
	yield watch
	watch[1]()


@pytest.mark.parametrize('points', _POINTS)
def test_a_ctrl_c_anywhere_in_a_settled_step_leaves_no_state_behind_for_later_ones(capfd, collecting, points):
	# ( matrix of round 12, finding 1 ) _settle() set its per-thread state as it began and reset it in its finally; a
	# Ctrl-C landing in that finally before the reset ( as getattr() returned ) left it set, so every later _settle() on
	# that thread took itself for a nested one and never showed the messages of its step: _show_error() dropped them
	# for good ( seen on 3.13t after the start stress test ). A Ctrl-C at each point of a _settle() whose step shows a
	# message, in turn, and then a step that fails once on its own: its failure is shown
	code = multiCMD._settle.__code__
	def counted(frame, event):
		return frame.f_code is code or frame.f_back is not None and frame.f_back.f_code is code
	def step():
		multiCMD._show_error('shown by the step')
	state = {'k': 0}
	while True:
		_hook(_interrupt_at(state, counted, points, collecting))
		try:
			multiCMD._settle(step)
		except KeyboardInterrupt:
			pass
		finally:
			_unhook()
		capfd.readouterr()
		runs = []
		def fails_once():
			runs.append(1)
			if len(runs) == 1:
				raise OSError('transient')
		multiCMD._settle(fails_once)
		err = capfd.readouterr().err
		# before ( f891332 ): nothing shown after a Ctrl-C as getattr() returned in the finally
		assert len(runs) == 2 and err.count('OSError: transient') == 1, (state['where'], err)
		if not state['injected']:
			break
		state['k'] += 1
	assert state['k'] > (10 if points == 'calls' else 1)


@pytest.mark.skipif(not hasattr(signal, 'pthread_sigmask'), reason='POSIX signals')
def test_a_ctrl_c_anywhere_in_a_start_leaves_the_signal_mask_as_it_was(launcher):
	# ( matrix of round 12, finding 2 ) _spawn_process blocked SIGTTIN / SIGTTOU with signal.pthread_sigmask(), whose
	# Python wrapper turns the C call's result into a set in a generator expression once the mask is set: a Ctrl-C
	# landing there left them blocked on the main thread for good ( old_mask never assigned, the finally never entered:
	# 45 % of the Ctrl-Cs raised in that wrapper, by a probe ), and one landing as the restoring call began left them
	# blocked too. A Ctrl-C at each point of a start, in turn: in _spawn_process, and as what it calls begins and
	# returns ( it calls _signal.pthread_sigmask(), the C function, and no code of the signal module's )
	code = multiCMD._spawn_process.__code__
	def counted(frame, event):
		return frame.f_code is code or frame.f_back is not None and frame.f_back.f_code is code
	before = signal.pthread_sigmask(signal.SIG_BLOCK, [])
	state = {'k': 0}
	while True:
		started = []
		_hook(_interrupt_at(state, counted))
		try:
			multiCMD._spawn_process(['true'], subprocess.DEVNULL, subprocess.DEVNULL, started=started)
		except KeyboardInterrupt:
			pass
		finally:
			_unhook()
		mask = signal.pthread_sigmask(signal.SIG_BLOCK, [])
		signal.pthread_sigmask(signal.SIG_SETMASK, before)
		for proc, _ in started:
			if getattr(proc, 'pid', None) is not None and proc.returncode is None:
				proc.wait(10)
		# before ( f891332 ): SIGTTIN and SIGTTOU left blocked by a Ctrl-C in the wrapper's generator expression, or as
		# the restoring call began
		assert mask == before, state['where']
		if not state['injected']:
			break
		state['k'] += 1
	assert state['k'] > 5


@pytest.mark.skipif(not sys.platform.startswith('linux'), reason='POSIX commands')
@pytest.mark.parametrize('points', _POINTS)
def test_a_second_ctrl_c_anywhere_before_the_command_is_registered_never_loses_it(launcher, monkeypatch, collecting, points):
	# ( matrix of round 12, finding 3 ) _execute() began its handling of an interrupt with _is_interrupt(first), a call
	# before the loop that settles: a second Ctrl-C raised as it began ( the next point where Python raises one, so
	# where a signal delivered twice lands ) left _execute before settle had registered the command, which ran on in
	# its own process group, unknown to kill_all() and the exit hook, and was left a zombie ( seen on 3.13t and 3.14t
	# in the start stress test, outside the fork ). A first Ctrl-C once Popen has the pid, and a second one at each
	# point of multiCMD's code in turn until the command is registered: it is registered, or it is gone
	state = {'k': 0}
	def counted(frame, event):
		proc = state.get('proc')
		if proc is not None and proc in multiCMD._live_procs:
			# registered: nothing after this can lose it
			state['registered'] = True
			_unhook()
			return False
		mine = multiCMD.__file__
		return frame.f_code.co_filename == mine or frame.f_back is not None and frame.f_back.f_code.co_filename == mine
	real_execute_child = subprocess.Popen._execute_child
	def execute_child(self, *args, **kwargs):
		real_execute_child(self, *args, **kwargs)
		# the first Ctrl-C, inside Popen once it has the pid; the second one from here on
		state['proc'] = self
		_hook(_interrupt_at(state, counted, points, collecting))
		raise KeyboardInterrupt('first')
	monkeypatch.setattr(subprocess.Popen, '_execute_child', execute_child)
	lost = []
	while True:
		state.pop('proc', None)
		state['registered'] = False
		try:
			with pytest.raises(KeyboardInterrupt):
				multiCMD.run_command(['sleep', '30'], quiet=True)
		finally:
			_unhook()
		proc = state['proc']
		if proc in multiCMD._live_procs:
			# what a second Ctrl-C left registered: kill_all() and the exit hook reach it
			multiCMD.kill_all(signal.SIGKILL)
		elif proc.returncode is None:
			# before ( f891332 ): lost by a second Ctrl-C as _is_interrupt() began, returned, or as its isinstance() did
			lost.append(state['where'])
			proc.kill()
			proc.wait(10)
		if not state['injected']:
			break
		state['k'] += 1
	assert lost == []
	assert state['k'] > (5 if points == 'calls' else 0) and state['registered']


@pytest.mark.skipif(not sys.platform.startswith('linux'), reason='POSIX commands')
@pytest.mark.parametrize('points', _POINTS)
def test_a_second_ctrl_c_anywhere_after_the_first_still_has_the_command_stopped(launcher, monkeypatch, collecting, points):
	# ( matrix of round 12, with finding 3 ) a second Ctrl-C ends the wait for the command, which is sent SIGTERM at
	# least: _stop_command() did that in a try of its own, and one that landed as the call to it began, before that try,
	# left the command registered but running, unsignalled. A first Ctrl-C once Popen has the pid, and a second one at
	# each point of multiCMD's code in turn, to the end of the run: the command is dead soon after either way. ( review
	# of round 13, B2 ) at the loop back-jumps too: one landing on _settle's inner loop's way back, right after
	# `done = True` ( a check point on 3.14, and on 3.6 - 3.9 ), was taken by its outer loop and raised as cuts[0], but
	# settle had completed without knowing of it and never sent the SIGTERM; and one landing on the way back of
	# _execute's outer loop, as its inner one had ended, left _execute before the command was stopped
	state = {'k': 0}
	def counted(frame, event):
		mine = multiCMD.__file__
		return frame.f_code.co_filename == mine or frame.f_back is not None and frame.f_back.f_code.co_filename == mine
	real_execute_child = subprocess.Popen._execute_child
	def execute_child(self, *args, **kwargs):
		real_execute_child(self, *args, **kwargs)
		state['proc'] = self
		_hook(_interrupt_at(state, counted, points, collecting))
		raise KeyboardInterrupt('first')
	monkeypatch.setattr(subprocess.Popen, '_execute_child', execute_child)
	running = []
	while True:
		try:
			with pytest.raises(KeyboardInterrupt):
				multiCMD.run_command(['sleep', '30'], quiet=True)
		finally:
			_unhook()
		proc = state['proc']
		if not multiCMD._wait_all([(proc, None)], 2):
			# before ( f891332 ): left running by a second Ctrl-C as _stop_command() began, registered; or lost. Before
			# ( cf4a7c1 ): by one on _settle's inner loop's way back after `done = True`, or on _execute's outer loop's
			# way back, registered
			running.append((state['where'], proc in multiCMD._live_procs))
			proc.kill()
		multiCMD._wait_all([(proc, None)], 5)
		multiCMD._unregister(proc)
		if not state['injected']:
			break
		state['k'] += 1
	assert running == []
	assert state['k'] > (40 if points == 'calls' else 5) and not multiCMD._live_procs


# ( a Ctrl-C inside Thread.start()'s wait can leave that thread's Event inconsistent, CPython's own: the thread, which
# multiCMD abandons then, fails as it comes up, and CPython reports that as unraisable )
@pytest.mark.filterwarnings('ignore::pytest.PytestUnraisableExceptionWarning')
@pytest.mark.skipif(not sys.platform.startswith('linux'), reason='POSIX commands')
@pytest.mark.parametrize('points', _POINTS)
@pytest.mark.parametrize('command', [['true'], ['mcmd-no-such-command-xyz']], ids=['true', 'missing'])
def test_a_ctrl_c_at_any_point_of_a_serial_run_is_raised(collecting, points, command):
	# ( matrix of round 12, finding 4 ) on 3.13t the start stress test saw run_command() return normally after a
	# Ctrl-C whose handler ran in the standard library's frames of the start ( a generator expression of
	# signal.pthread_sigmask(), Thread.__init__, Popen.__init__, _execute_child ), and multiCMD's _execute never saw an
	# exception. One KeyboardInterrupt at each call, return and C-call return of a serial run in turn, and at each loop
	# back-jump, in every frame of the main thread ( subprocess's, contextlib's, os's ... ), outside the finalizers,
	# weakref callbacks and collections CPython drops what is raised in ( see _interrupt_at ): run_command() raises it
	# every time, so a Ctrl-C that Python raises there is never lost by multiCMD, for a command that runs and for one
	# that is not found ( review of round 13, B1: on 3.14 lost on the way back of _execute's loop, see the test below ).
	# threading's own frames are left out where a Ctrl-C right after a `with lock:` acquire leaves the lock held
	# ( 3.6 - 3.9, 3.14: CPython's own, see the README )
	skipped = {threading.__file__} if _WITH_MAY_JAM else set()
	def counted(frame, event):
		return frame.f_code.co_filename not in skipped
	state = {'k': 0}
	returned = []
	while True:
		_hook(_interrupt_at(state, counted, points, collecting))
		try:
			multiCMD.run_command(command, quiet=True)
			if state['injected']:
				returned.append(state['where'])
		except KeyboardInterrupt:
			pass
		finally:
			_unhook()
		multiCMD.kill_all(signal.SIGKILL)
		if not state['injected']:
			break
		state['k'] += 1
	assert returned == []
	assert state['k'] > (100 if points == 'calls' else 10) and not multiCMD._live_procs


@pytest.mark.skipif(not sys.platform.startswith('linux'), reason='POSIX commands')
@pytest.mark.parametrize('points', _POINTS)
@pytest.mark.parametrize('batch', [False, True], ids=['run_command', 'serial batch'])
def test_the_only_ctrl_c_anywhere_while_a_missing_command_is_dealt_with_is_raised(monkeypatch, collecting, points, batch):
	# ( review of round 13, B1 ) _execute deals with a command that is not found in the handler that settles a Ctrl-C,
	# and once _settle() had returned, `settled = True` ended both its loops: a Ctrl-C landing next, on the inner loop's
	# way back ( a check point on 3.14, and on 3.6 - 3.9 ), was taken by the outer one into what was raised, and never
	# looked at. As the run deals with a failure, that is its only Ctrl-C: run_command() returned 127, and a serial
	# batch ran its next command. The only Ctrl-C of the run at each point of multiCMD's code in turn, from the moment
	# the command is found missing until the next one would start: it is raised, and the next command never starts
	mine = multiCMD.__file__
	def counted(frame, event):
		return frame.f_code.co_filename == mine or frame.f_back is not None and frame.f_back.f_code.co_filename == mine
	state = {'k': 0}
	spawns = []
	real_spawn = multiCMD._spawn
	def spawn(command, *args, **kwargs):
		# ( no Ctrl-C from the next command's start on )
		_unhook()
		spawns.append(command)
		try:
			return real_spawn(command, *args, **kwargs)
		except FileNotFoundError:
			_hook(_interrupt_at(state, counted, points, collecting))
			raise
	monkeypatch.setattr(multiCMD, '_spawn', spawn)
	lost = []
	while True:
		del spawns[:]
		try:
			if batch:
				outcome = run_commands([['mcmd-no-such-command-xyz'], ['true']], max_threads=1, quiet=True, return_code_only=True)
			else:
				outcome = multiCMD.run_command(['mcmd-no-such-command-xyz'], quiet=True, return_code_only=True)
		except KeyboardInterrupt:
			outcome = 'raised'
		finally:
			_unhook()
		if state['injected'] and (outcome != 'raised' or len(spawns) > 1):
			# before ( cf4a7c1 ): returned 127 ( [127, 0] ) after one on the way back of _execute's inner loop
			lost.append((state['where'], outcome, spawns[1:]))
		if not state['injected']:
			break
		state['k'] += 1
	assert lost == []
	assert state['k'] > (20 if points == 'calls' else 2) and not multiCMD._live_procs
