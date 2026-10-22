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
	assert runs == [1]
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
	multiCMD._settle(lambda: None)
	assert signal.getsignal(signal.SIGINT) is signal.default_int_handler


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
	# ( the interrupt that cut the step short is raised after it: round 8 took it, the caller being on its way out )
	with pytest.raises(KeyboardInterrupt):
		multiCMD._settle(step)
	assert runs == [1, 1]
	# shown once, outside the hold ( a write to a stalled stderr cannot keep a Ctrl-C waiting )
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
		# lands before the spawn has even returned
		_ctrl_c()
		return result
	monkeypatch.setattr(multiCMD, '_spawn', spawn)
	with pytest.raises(KeyboardInterrupt):
		multiCMD.run_command(['sh', '-c', f'echo $$ > {pidfile}; exec sleep 30'], quiet=True)
	# before: the Ctrl-C lost the command, which ran on in its own process group, unknown to kill_all()
	deadline = time.monotonic() + 5
	while not pidfile.exists() and time.monotonic() < deadline:
		time.sleep(0.01)
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


# ---- the pool: one failed start is enough, and failing every task is settled

def test_the_pool_stops_starting_workers_after_the_first_that_cannot_start(monkeypatch, capfd):
	calls = []
	real_start = multiCMD._Thread.start
	def start(self):
		if getattr(self, '_target', None) is not None and self._target.__name__ == 'worker':
			calls.append(self)
			if len(calls) == 2:
				raise RuntimeError("can't start new thread")
		real_start(self)
	monkeypatch.setattr(multiCMD._Thread, 'start', start)
	tasks = run_commands([['true']] * 6, max_threads=5, quiet=False, return_object=True)
	# before: every one of the 5 workers was tried
	assert len(calls) == 2
	assert [task.returncode for task in tasks] == [0] * 6
	assert "multiCMD: started 1 of 5 threads: can't start new thread" in capfd.readouterr().err


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


# ---- review round 8: a held Ctrl-C is always handed on, never absorbed

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


# ---- review round 8: a handler installed during a hold stays, and gets the held Ctrl-C

@pytest.mark.skipif(not hasattr(signal, 'pthread_sigmask'), reason='POSIX signals')
@pytest.mark.parametrize('held', [False, True])
def test_a_handler_installed_during_a_hold_stays_and_gets_the_held_ctrl_c(held, another_thread):
	# ( finding 6, review probe p_restore.py ) e.g. a SIGTERM handler running inside the hold that sets SIG_IGN
	calls = []
	def handler(signum, frame):
		calls.append(signum)
	def step():
		if held:
			_ctrl_c()
		signal.signal(signal.SIGINT, handler)
	try:
		multiCMD._settle(step)
		assert signal.getsignal(signal.SIGINT) is handler
		assert calls == ([signal.SIGINT] if held else [])
		# and SIG_IGN drops the held Ctrl-C
		def ignore():
			_ctrl_c()
			signal.signal(signal.SIGINT, signal.SIG_IGN)
		multiCMD._settle(ignore)
		assert signal.getsignal(signal.SIGINT) == signal.SIG_IGN
	finally:
		signal.signal(signal.SIGINT, signal.default_int_handler)


# ---- review round 8: a hold keeps no frame once it ends

@pytest.mark.skipif(not hasattr(signal, 'pthread_sigmask'), reason='POSIX signals')
def test_a_hold_hands_the_ctrl_c_on_from_its_holder_and_keeps_nothing(another_thread):
	# ( finding 10 ) round 8 kept the frame the Ctrl-C landed in, and kept it for good when it absorbed the Ctrl-C
	seen = []
	def handler(signum, frame):
		seen.append(frame.f_code.co_name)
	previous = signal.signal(signal.SIGINT, handler)
	hold = multiCMD._HoldCtrlC()
	def holder():
		with hold:
			_ctrl_c()
			assert seen == [] and hold.held == signal.SIGINT
			raise KeyboardInterrupt('the block\'s own')
	try:
		with pytest.raises(KeyboardInterrupt):
			holder()
		# handed on from the exit, as if it landed in the code that holds it
		assert seen == ['holder']
		assert hold.held is None and hold.owner is None and hold.early is None and not hold.active
		assert signal.getsignal(signal.SIGINT) is handler
	finally:
		signal.signal(signal.SIGINT, previous)
