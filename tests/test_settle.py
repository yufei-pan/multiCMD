'''
A Ctrl-C while multiCMD settles its bookkeeping: the step runs again to its end and the Ctrl-C is raised after it,
never lost; the failure messages a step shows appear once, after it
'''
import os
import signal
import sys
import threading
import time

import pytest

import multiCMD
from conftest import pid_gone, sigint_main_after
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
	def kill(commands, first_signal):
		commands = list(commands)
		with multiCMD._live_procs_lock:
			registered.extend(proc in multiCMD._live_procs for proc, _ in commands)
		real_kill(commands, first_signal)
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


def _left_running(before, wait=3.0):
	'''
	The children started since before that still run after up to wait seconds, or that are dead but neither
	reaped nor registered ( kill_all() and the exit hook reap what is registered )
	'''
	deadline = time.monotonic() + wait
	while True:
		with multiCMD._live_procs_lock:
			registered = {proc.pid for proc in multiCMD._live_procs}
		left = {pid for pid in _children() - before if not _zombie(pid) or pid not in registered}
		if not left or time.monotonic() > deadline:
			return left
		time.sleep(0.02)


@pytest.mark.skipif(not sys.platform.startswith('linux'), reason='checks processes through /proc')
@pytest.mark.parametrize('ctrl_cs', [1, 2])
def test_a_ctrl_c_while_the_command_starts_never_loses_it(launcher, monkeypatch, ctrl_cs):
	# ( review of round 9, blockers 1 and 2; probe rv9/orphan.py ) a real SIGINT from another thread lands
	# anywhere in the start: as _fork_exec / posix_spawn returned ( before Popen stored the pid ), Popen lost the
	# command, which ran on in its own process group ( 14 of 16 such Ctrl-Cs on 3.13 at 0e4d015 ). A second one
	# right after, while the first is dealt with, ended the wait without stopping the command, and when it was
	# pending as that began, left the command unregistered too
	import random
	main = threading.main_thread().ident
	inside = []
	if hasattr(multiCMD, '_Handoff'):
		real_take = multiCMD._Handoff.take
		def take(self):
			if not self._waited:
				# whether the Ctrl-C landed while the main thread waited for the start
				inside.append(not self.done.flag)
			return real_take(self)
		monkeypatch.setattr(multiCMD._Handoff, 'take', take)
	before = _children()
	lost = []
	for _ in range(100):
		first = random.uniform(0, 0.004)
		delays = [first] + ([first + random.uniform(0, 0.002)] if ctrl_cs == 2 else [])
		def send():
			start = time.monotonic()
			for delay in delays:
				time.sleep(max(0, start + delay - time.monotonic()))
				signal.pthread_kill(main, signal.SIGINT)
		timer = threading.Thread(target=send)
		timer.start()
		try:
			try:
				multiCMD.run_command(['sleep', '30'], quiet=True)
			finally:
				while True:
					try:
						timer.join()
						break
					except KeyboardInterrupt:
						pass
		except KeyboardInterrupt:
			pass
		else:
			pytest.fail('the command ran to its end')
		lost.extend(_left_running(before))
		# what a second Ctrl-C left registered is dead by now: reap it, as kill_all() would
		with multiCMD._live_procs_lock:
			left = list(multiCMD._live_procs)
		assert ctrl_cs == 2 or not left
		for proc in left:
			proc.wait(5)
			multiCMD._unregister(proc)
		if lost:
			break
	for pid in lost:
		os.killpg(pid, signal.SIGKILL)
	# before ( 0e4d015 ): a command started, unknown to kill_all(), left running
	assert lost == []
	if hasattr(multiCMD, '_Handoff'):
		assert any(inside), 'no Ctrl-C landed while the command started'


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
