'''
A Ctrl-C while multiCMD settles its bookkeeping: held on the main thread and raised once the step is done,
never cutting it short nor lost; the failure messages a step shows appear once, after it
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


# ---- _settle(): a Ctrl-C is held while the step runs and raised right after it

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
	# before: the Ctrl-C cut the step short, it ran again, and the Ctrl-C was lost
	assert runs == [1] and done == [1]
	assert signal.getsignal(signal.SIGINT) is signal.default_int_handler


@pytest.mark.skipif(not hasattr(signal, 'pthread_sigmask'), reason='POSIX signals')
def test_a_ctrl_c_inside_a_settled_step_is_taken_when_the_caller_handles_one_already(another_thread):
	runs = []
	def step():
		runs.append(1)
		if len(runs) == 1:
			_ctrl_c()
	try:
		raise KeyboardInterrupt
	except KeyboardInterrupt:
		# the caller is on its way out with the first Ctrl-C: the second only tells it to end its wait
		assert multiCMD._settle(step) is True
	assert runs == [1]
	assert multiCMD._settle(lambda: None) is False
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
def test_a_settled_step_on_another_thread_holds_nothing_and_needs_not(another_thread):
	# Python runs signal handlers on the main thread only: a Ctrl-C meanwhile is the main thread's
	seen = []
	def step():
		seen.append(signal.getsignal(signal.SIGINT))
	worker = threading.Thread(target=multiCMD._settle, args=(step,))
	worker.start()
	worker.join(5)
	assert seen == [signal.default_int_handler]


@pytest.mark.skipif(not hasattr(signal, 'pthread_sigmask'), reason='POSIX signals')
def test_a_ctrl_c_goes_to_a_handler_of_the_program_once_the_step_is_done(another_thread):
	calls = []
	def handler(signum, frame):
		calls.append(signum)
	previous = signal.signal(signal.SIGINT, handler)
	try:
		def step():
			_ctrl_c()
			calls.append('step')
		multiCMD._settle(step)
		assert calls == ['step', signal.SIGINT] and signal.getsignal(signal.SIGINT) is handler
	finally:
		signal.signal(signal.SIGINT, previous)


def test_a_hold_left_in_place_hands_the_ctrl_c_on():
	# a restore cut short leaves the hold installed but inactive: a Ctrl-C then still reaches the program's handler
	calls = []
	hold = multiCMD._HoldCtrlC()
	hold.previous = lambda signum, frame: calls.append(signum)
	previous = signal.signal(signal.SIGINT, hold)
	try:
		hold(signal.SIGINT, None)
		assert calls == [signal.SIGINT] and signal.getsignal(signal.SIGINT) is hold.previous
	finally:
		signal.signal(signal.SIGINT, previous)


@pytest.mark.skipif(not hasattr(signal, 'pthread_sigmask'), reason='POSIX signals')
def test_a_hold_whose_block_was_left_without_its_exit_hands_the_ctrl_c_on(another_thread):
	# the exit itself cut short ( a KeyboardInterrupt from another signal's handler as it begins ): the hold is
	# still active, but the code that holds it is gone
	def enter():
		hold = multiCMD._HoldCtrlC()
		hold.__enter__()
		return hold
	hold = enter()
	assert hold.active and signal.getsignal(signal.SIGINT) is hold
	with pytest.raises(KeyboardInterrupt):
		_ctrl_c()
	assert signal.getsignal(signal.SIGINT) is signal.default_int_handler and hold.held is None
	# and a new hold is not taken for one already held
	assert multiCMD._settle(lambda: None) is False


@pytest.mark.skipif(not sys.platform.startswith('linux'), reason='reads /proc/<pid>/status')
@pytest.mark.parametrize('where', ['the main thread', 'another thread'])
def test_commands_started_while_a_ctrl_c_is_held_do_not_inherit_a_blocked_or_ignored_sigint(where, tmp_path):
	# the hold changes SIGINT's Python handler, never the signal mask: a caught signal is reset by exec
	command = ['sh', '-c', 'grep -E "^Sig(Blk|Ign)" /proc/$$/status']
	if where == 'the main thread':
		# the spawn itself is held ( see _execute )
		out = multiCMD.run_command(command, quiet=True)
	else:
		result = []
		started = threading.Event()
		def step():
			if not started.is_set():
				started.set()
				worker = threading.Thread(target=lambda: result.append(multiCMD.run_command(command, quiet=True)))
				worker.start()
				worker.join(10)
		multiCMD._settle(step)
		out = result[0]
	bits = {line.split(':')[0]: bool(int(line.split(':')[1], 16) & (1 << (signal.SIGINT - 1))) for line in out}
	assert bits == {'SigBlk': False, 'SigIgn': False}


# ---- the messages a settled step shows: once, after it, with the program's handler back

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
	try:
		raise KeyboardInterrupt
	except KeyboardInterrupt:
		multiCMD._settle(step)
	assert runs == [1, 1]
	# shown once, outside the hold ( a write to a stalled stderr cannot keep a Ctrl-C waiting )
	assert written == [('first', signal.default_int_handler)]
	multiCMD._show_error('now')
	assert written[-1][0] == 'now'


# ---- a Ctrl-C the bookkeeping of a failure held is not lost

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
