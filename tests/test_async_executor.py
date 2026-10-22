import _thread
import os
import sys
import threading
import time

import pytest

import multiCMD
from conftest import sigint_main_after
from multiCMD import AsyncExecutor, run_commands


@pytest.mark.slow
def test_executor_run_wait_results_codes():
	ex = AsyncExecutor(max_threads=2, timeout=0, quiet=True)
	ex.run_command(["echo", "a"])
	ex.run_commands([["echo", "b"], ["false"]])
	assert len(ex) == 3
	assert bool(ex)
	ex.wait(timeout=2)
	assert ex.get_return_codes() == [0, 0, 1]
	assert ex.get_results() == [["a"], ["b"], []]


@pytest.mark.slow
def test_executor_join_prints_errors(capsys):
	ex = AsyncExecutor(max_threads=1, quiet=True)
	ex.run_command(["false"])
	ex.join(timeout=2, print_error=True)
	captured = capsys.readouterr()
	assert "failed with return code" in captured.out


@pytest.mark.slow
def test_executor_stop_and_cleanup():
	ex = AsyncExecutor(max_threads=1, quiet=True)
	ex.run_command(["sleep", "5"])
	time.sleep(0.1)
	ex.stop(timeout=3)
	# stop signals task.stop; process should end
	assert all(not t.is_alive() for t in ex.tasks)
	ex.cleanup(timeout=1)
	assert list(ex) == []


@pytest.mark.slow
def test_join_threads_module_helper():
	tasks = run_commands(
		[["sleep", "0.3"]],
		wait_for_return=False,
		return_object=True,
		quiet=True,
	)
	assert tasks[0].returncode is None
	multiCMD.join_threads(timeout=2)
	assert tasks[0].returncode == 0


def _global_threads():
	return getattr(multiCMD, '__running_threads')


def _stack_mappings(size):
	count = 0
	with open('/proc/self/maps') as maps:
		for line in maps:
			fields = line.split()
			if len(fields) == 5 and fields[1] == 'rw-p':
				low, high = (int(x, 16) for x in fields[0].split('-'))
				count += high - low == size
	return count


def test_executor_queues_commands_instead_of_parking_threads(monkeypatch):
	ran = []
	run_task = multiCMD._run_executor_task
	monkeypatch.setattr(multiCMD, '_run_executor_task', lambda task, *args: (ran.append(task), run_task(task, *args)))
	gate = threading.Semaphore(0)
	baseline = threading.active_count()
	ex = AsyncExecutor(semaphore=gate, quiet=True, dry_run=True)
	start = time.monotonic()
	tasks = [ex.run_command(['true']) for _ in range(5000)]
	assert time.monotonic() - start < 5
	# no permit is free, so only the dispatcher exists and it waits for one
	assert threading.active_count() <= baseline + 1
	assert all(task.thread is None for task in tasks)
	for _ in range(len(tasks)):
		gate.release()
	ex.wait(timeout=-1)
	assert ex.runningThreads == []
	assert sorted(map(id, ran)) == sorted(map(id, tasks))
	assert all(task.thread is None and not task.is_alive() for task in tasks)


@pytest.mark.slow
def test_executor_thread_count_stays_bounded():
	baseline = threading.active_count()
	ex = AsyncExecutor(semaphore=threading.Semaphore(3), quiet=True)
	ex.run_commands([['sleep', '0.05']] * 60)
	peak = 0
	while ex.runningThreads:
		peak = max(peak, threading.active_count())
		time.sleep(0.005)
	# the dispatcher, plus a runner and two stream readers per running command
	assert peak - baseline <= 1 + 3 * 3
	assert ex.get_return_codes() == [0] * 60


@pytest.mark.slow
@pytest.mark.skipif(not os.path.exists('/proc/self/maps'), reason='needs /proc/self/maps')
def test_executor_releases_finished_thread_stacks():
	# a stack size nothing else uses, so these threads' stacks can be counted in /proc/self/maps
	stack = 8 * 1024 * 1024 + 3 * 4096
	old_stack = threading.stack_size(stack)
	try:
		ex = AsyncExecutor(semaphore=threading.Semaphore(6), quiet=True)
		for _ in range(1200):
			ex.run_command(['sleep', '0.01'])
		ex.wait(timeout=-1)
		# the executor and its tasks are still referenced: every finished thread must already be joined
		assert ex.get_return_codes() == [0] * 1200
		# glibc keeps a few freed stacks cached for reuse
		assert _stack_mappings(stack) <= 8
	finally:
		threading.stack_size(old_stack)


def test_executor_threads_stay_out_of_global_set():
	ex = AsyncExecutor(max_threads=2, quiet=True)
	tasks = ex.run_commands([['sleep', '0.3'], ['sleep', '0.3']])
	# finished tasks drop their thread, so take the threads while the commands run
	deadline = time.monotonic() + 5
	while not all(task.thread for task in tasks) and time.monotonic() < deadline:
		time.sleep(0.01)
	threads = {task.thread for task in tasks}
	assert None not in threads
	assert not threads & _global_threads()
	ex.wait(timeout=-1)


def test_finished_fire_and_forget_threads_leave_global_set():
	tasks = run_commands([['true']] * 20, wait_for_return=False, return_object=True, quiet=True)
	for task in tasks:
		task.thread.join()
	run_commands([['true']], wait_for_return=False, quiet=True)
	assert not {task.thread for task in tasks} & _global_threads()


def test_finished_executor_tasks_release_their_threads():
	ex = AsyncExecutor(max_threads=4, quiet=True)
	tasks = ex.run_commands([['true']] * 20)
	ex.wait(timeout=-1)
	assert [task.returncode for task in tasks] == [0] * 20
	# a finished Thread object costs ~2 KB, kept for as long as the executor keeps the task
	assert all(task.thread is None and not task.is_alive() for task in tasks)


def test_stop_skips_queued_commands():
	ex = AsyncExecutor(semaphore=threading.Semaphore(0), quiet=True)
	tasks = ex.run_commands([['sleep', '5']] * 3)
	start = time.monotonic()
	ex.stop(timeout=2)
	assert time.monotonic() - start < 2
	assert ex.runningThreads == []
	assert all(task.thread is None and task.returncode is None for task in tasks)


def test_failed_thread_start_keeps_executor_usable(monkeypatch):
	ex = AsyncExecutor(max_threads=2, quiet=True)
	real_start = threading.Thread.start

	def fail_start(self):
		if getattr(self, '_target', None) is multiCMD._run_executor_task:
			raise RuntimeError("can't start new thread")
		real_start(self)
	monkeypatch.setattr(threading.Thread, 'start', fail_start)
	failed = ex.run_command(['true'])
	ex.wait(timeout=-1)
	assert ex.runningThreads == []
	assert failed.thread is None
	monkeypatch.setattr(threading.Thread, 'start', real_start)
	# the permit taken for the failed start was given back, and a new dispatcher picks up new work
	tasks = ex.run_commands([['true'], ['true']])
	ex.wait(timeout=-1)
	assert [task.returncode for task in tasks] == [0, 0]


def test_a_command_whose_thread_cannot_start_is_finalized_as_failed(monkeypatch, capsys):
	ex = AsyncExecutor(max_threads=1, quiet=True)
	real = ex._start_thread
	def broken(thread):
		if thread.task is None:
			return real(thread)
		raise RuntimeError("can't start new thread")
	monkeypatch.setattr(ex, '_start_thread', broken)
	task = ex.run_command(['true'])
	assert ex.wait(timeout=5) == []
	assert task.returncode == -1 and task._error and task._finalized and task.thread is None
	assert "cannot start its thread: can't start new thread" in task.stderr[0]
	ex.join()
	out = capsys.readouterr().out
	assert "Command: ['true'] failed with return code: -1" in out
	assert 'still running' not in out


@pytest.mark.slow
def test_join_waits_by_default(capsys):
	ex = AsyncExecutor(max_threads=2)
	ex.run_commands([['sleep', '0.5'], ['true']])
	start = time.monotonic()
	tasks = ex.join()
	assert time.monotonic() - start >= 0.4
	assert [task.returncode for task in tasks] == [0, 0]
	assert 'failed' not in capsys.readouterr().out


@pytest.mark.slow
def test_wait_timeout_is_one_overall_limit():
	ex = AsyncExecutor(max_threads=3)
	ex.run_commands([['sleep', '3']] * 3)
	start = time.monotonic()
	still_running = ex.wait(timeout=0.5)
	assert time.monotonic() - start < 1.2
	assert still_running
	ex.stop()


@pytest.mark.slow
def test_join_reports_failed_skipped_and_still_running(capsys):
	ex = AsyncExecutor(max_threads=1)
	ex.run_commands([['false']])
	ex.wait()
	running, queued = ex.run_commands([['sleep', '5'], ['true']])
	queued.stop = True
	ex.join(timeout=0.3)
	out = capsys.readouterr().out
	assert "Command: ['false'] failed with return code: 1" in out
	assert "Command: ['sleep', '5'] still running" in out
	assert "Command: ['true'] skipped: stopped before it started" in out
	ex.stop()


@pytest.mark.slow
def test_ctrl_c_during_wait_stops_the_executors_commands():
	ex = AsyncExecutor(max_threads=1)
	task = ex.run_command(['sleep', '30'])
	timer = threading.Timer(0.5, _thread.interrupt_main)
	timer.start()
	start = time.monotonic()
	with pytest.raises(KeyboardInterrupt):
		ex.wait()
	assert time.monotonic() - start < 4
	assert task.returncode is not None


def test_executor_shell_prefix_and_color(capsys):
	ex = AsyncExecutor(max_threads=2, quiet=False, shell=True, prefix=True, color=True)
	ex.run_commands(['echo abc | tr a x', 'echo two'])
	ex.wait()
	out = capsys.readouterr().out
	assert '[1] ' in out and '[2] ' in out and 'xbc' in out
	assert '\x1b[38;2;' in out


def test_executor_dry_run_prints_the_quoted_command(capsys):
	ex = AsyncExecutor(quiet=False, dry_run=True)
	ex.run_command(['echo', 'a b'])
	ex.wait()
	assert capsys.readouterr().out == "echo 'a b'\n"


def _interrupt_join(thread):
	'''Make the next thread.join() raise KeyboardInterrupt, as a Ctrl-C landing in it would'''
	real_join = thread.join
	def interrupted_join(*args, **kwargs):
		thread.join = real_join
		raise KeyboardInterrupt
	thread.join = interrupted_join


def _wait_in_thread(ex, timeout):
	done = threading.Event()
	def waiter():
		ex.wait(timeout=timeout)
		done.set()
	threading.Thread(target=waiter, daemon=True).start()
	return done


def test_interrupted_reap_leaves_no_dead_thread_behind():
	ex = AsyncExecutor(max_threads=2, quiet=True)
	task = ex.run_command(['true'])
	# the task's own thread, not the dispatcher's ( which finishes first ): the join to interrupt is the one that reaps it
	deadline = time.monotonic() + 10
	while time.monotonic() < deadline:
		thread = task.thread
		if thread is not None and any(entry[0] is thread for entry in list(ex._finished)):
			break
		time.sleep(0.01)
	else:
		raise AssertionError('the task thread never reported itself finished')
	_interrupt_join(thread)
	with pytest.raises(KeyboardInterrupt):
		ex.runningThreads
	assert thread not in ex.runningThreads
	assert task.thread is None
	done = _wait_in_thread(ex, 1)
	assert done.wait(2)


def test_wait_deadline_returns_despite_stale_bookkeeping():
	ex = AsyncExecutor(max_threads=2, quiet=True)
	dead = threading.Thread(target=lambda: None)
	dead.start()
	dead.join()
	ex._threads[dead] = None
	done = _wait_in_thread(ex, 1)
	assert done.wait(2)
	ex = AsyncExecutor(max_threads=2, quiet=True)
	ex._threads[dead] = None
	assert not ex.wait()


def test_interrupted_module_reap_forgets_the_thread():
	reap = getattr(multiCMD, '__reap_finished_threads')
	finished = getattr(multiCMD, '__finished_threads')
	thread = threading.Thread(target=lambda: None)
	thread.start()
	thread.join()
	_global_threads().add(thread)
	finished.append(thread)
	_interrupt_join(thread)
	try:
		with pytest.raises(KeyboardInterrupt):
			reap()
		assert thread not in _global_threads()
		assert thread not in finished
	finally:
		_global_threads().discard(thread)
		if thread in finished:
			finished.remove(thread)


# ---- an interrupted join ( Ctrl-C ) never makes a running thread look finished: CPython bpo-45274, 3.9.8 - 3.12

def _started_thread(task):
	deadline = time.monotonic() + 5
	while task.thread is None and time.monotonic() < deadline:
		time.sleep(0.01)
	return task.thread


def test_sigint_helper_fails_a_call_that_returns_first_and_sends_nothing_later():
	start = time.monotonic()
	with pytest.raises(pytest.fail.Exception, match='returned before the SIGINT'):
		with sigint_main_after(0.3):
			pass
	# the timer is gone: no SIGINT lands after the block ( this sleep would take it )
	time.sleep(0.5)
	assert time.monotonic() - start < 0.3 + 0.5 + 0.2


def test_sigint_helper_fails_a_call_that_ends_before_every_sigint():
	with pytest.raises(pytest.fail.Exception, match='after 1 of the 2 SIGINTs'):
		with sigint_main_after(0.1, 0.5):
			time.sleep(5)
	time.sleep(0.6)


def test_sigint_helper_delivers_each_sigint():
	caught = []
	with pytest.raises(KeyboardInterrupt), sigint_main_after(0.1, 0.3):
		try:
			time.sleep(5)
		except KeyboardInterrupt:
			caught.append(time.monotonic())
		time.sleep(5)
	assert len(caught) == 1


def test_an_interrupted_join_does_not_make_a_running_thread_look_finished():
	release = threading.Event()
	thread = multiCMD._Thread(release.wait, (10,))
	thread.start()
	try:
		with pytest.raises(KeyboardInterrupt), sigint_main_after(0.2):
			thread.join()
		assert thread.is_alive()
		assert not multiCMD._join(thread, 0.2)
	finally:
		release.set()
	assert multiCMD._join(thread, 5)
	assert not thread.is_alive()


@pytest.mark.slow
def test_wait_and_stop_after_an_interrupted_join_still_wait_for_the_command():
	ex = AsyncExecutor(max_threads=1, quiet=True)
	task = ex.run_command(['sleep', '1'])
	thread = _started_thread(task)
	with pytest.raises(KeyboardInterrupt), sigint_main_after(0.2):
		thread.join()
	assert thread in ex.wait(timeout=0.1)
	assert task.is_alive() and task.returncode is None
	assert ex.wait() == []
	assert task.returncode == 0 and not task.is_alive()
	task = ex.run_command(['sleep', '30'])
	thread = _started_thread(task)
	with pytest.raises(KeyboardInterrupt), sigint_main_after(0.2):
		thread.join()
	ex.stop()
	assert task.returncode is not None and not task.is_alive()
	ex.cleanup()
	assert not thread.is_alive()


@pytest.mark.slow
def test_ctrl_c_in_join_threads_stops_the_commands_and_waits_for_them():
	# the command dies only to SIGKILL, a second after the stop: on 3.9.8 - 3.12 a wait that trusted Thread.join() would not wait
	task = multiCMD.run_command(['sh', '-c', 'trap "" INT TERM; sleep 30'], quiet=True, wait_for_return=False, return_object=True)
	with pytest.raises(KeyboardInterrupt), sigint_main_after(0.3):
		multiCMD.join_threads()
	assert task.stop and task.returncode is not None and not task.is_alive()
	assert not multiCMD._live_procs
	multiCMD.join_threads()
	assert task.thread not in _global_threads()


def _slow_kill(monkeypatch):
	'''Make every kill of a stopped command wait until the returned event is set ( at most 10 s )'''
	release = threading.Event()
	real = multiCMD._kill_commands
	def slow_kill(commands, first_signal):
		release.wait(10)
		real(commands, first_signal)
	monkeypatch.setattr(multiCMD, '_kill_commands', slow_kill)
	return release


@pytest.mark.parametrize('how', ['executor', 'join_threads', 'pool'])
def test_a_second_ctrl_c_during_the_stop_gets_out_at_once(monkeypatch, how):
	release = _slow_kill(monkeypatch)
	ex = AsyncExecutor(max_threads=2, quiet=True)
	try:
		start = time.monotonic()
		with pytest.raises(KeyboardInterrupt) as info, sigint_main_after(0.3, 0.8):
			if how == 'executor':
				ex.run_commands([['sleep', '30']] * 2)
				ex.wait()
			elif how == 'join_threads':
				multiCMD.run_commands([['sleep', '30']] * 2, max_threads=2, quiet=True, wait_for_return=False)
				multiCMD.join_threads()
			else:
				multiCMD.run_commands([['sleep', '30']] * 2, max_threads=2, quiet=True)
		# not after the 5 s the stop may take ( nor 5 s more per press )
		assert time.monotonic() - start < 3
		assert isinstance(info.value.__context__, KeyboardInterrupt)
	finally:
		release.set()
		ex.stop(timeout=10)
		multiCMD.join_threads(timeout=10)
	# the pool's workers, which nothing here can join, finish the stop once released
	deadline = time.monotonic() + 10
	while multiCMD._live_procs and time.monotonic() < deadline:
		time.sleep(0.02)
	assert not multiCMD._live_procs


# ---- a _Thread counts as running from its creation until it is done or abandoned, and its join() joins it

def test_a_thread_whose_start_fails_is_abandoned(monkeypatch):
	ran = []
	thread = multiCMD._Thread(ran.append, (1,))
	assert thread.is_alive()
	def fail(self):
		raise RuntimeError("can't start new thread")
	monkeypatch.setattr(threading.Thread, 'start', fail)
	with pytest.raises(RuntimeError):
		thread.start()
	assert thread.done.is_set() and not thread.is_alive() and thread._abandon()
	thread.join()
	assert multiCMD._join(thread, 1) and ran == []


def test_an_abandoned_thread_never_runs_its_target_and_a_running_one_is_not_abandoned():
	ran = []
	thread = multiCMD._Thread(ran.append, (1,))
	assert thread._abandon()
	thread.start()
	thread.join()
	assert ran == [] and not thread.is_alive()
	release = threading.Event()
	thread = multiCMD._Thread(release.wait, (10,))
	thread.start()
	deadline = time.monotonic() + 5
	while not thread._runs() and time.monotonic() < deadline:
		time.sleep(0.01)
	assert not thread._abandon() and thread.is_alive()
	release.set()
	assert multiCMD._join(thread, 5)


def test_join_threads_raises_for_a_thread_never_started():
	with pytest.raises(RuntimeError):
		multiCMD.join_threads(threads=[threading.Thread(target=print)])


def test_join_threads_waits_for_a_registered_thread_that_has_not_started_yet(monkeypatch):
	# a join_threads() on another thread between run_commands() registering the thread and starting it
	main = threading.current_thread()
	registered, gate = threading.Event(), threading.Event()
	real_start = multiCMD._Thread.start
	def slow_start(self):
		if getattr(self, '_target', None) is getattr(multiCMD, '__run_tracked_command') and threading.current_thread() is main:
			registered.set()
			gate.wait(5)
		real_start(self)
	monkeypatch.setattr(multiCMD._Thread, 'start', slow_start)
	joined = threading.Event()
	def joiner():
		registered.wait(5)
		multiCMD.join_threads()
		joined.set()
	threading.Thread(target=joiner, daemon=True).start()
	def release():
		# the command has not even started: join_threads() must still be waiting
		registered.wait(5)
		if not joined.wait(0.3):
			gate.set()
	threading.Thread(target=release, daemon=True).start()
	task = run_commands([['true']], quiet=True, wait_for_return=False, return_object=True)[0]
	assert gate.is_set(), 'join_threads() returned before the command started'
	assert joined.wait(5)
	assert task.returncode == 0


# ---- a thread that cannot start, or whose start a Ctrl-C interrupts, leaves nothing behind

def _fail_nth_start(monkeypatch, target, n, exc=RuntimeError("can't start new thread"), after=False):
	'''Make the nth start() of a thread running target raise exc: before the thread starts, or after it got to its target'''
	real_start = multiCMD._Thread.start
	count = [0]
	def start(self):
		if getattr(self, '_target', None) == target:
			count[0] += 1
			if count[0] == n:
				if after:
					real_start(self)
					deadline = time.monotonic() + 5
					while not self._runs() and time.monotonic() < deadline:
						time.sleep(0.001)
				raise exc
		real_start(self)
	monkeypatch.setattr(multiCMD._Thread, 'start', start)


def test_a_command_thread_that_cannot_start_fails_and_the_rest_still_run(monkeypatch, capfd):
	ex = AsyncExecutor(max_threads=1, quiet=True)
	_fail_nth_start(monkeypatch, multiCMD._run_executor_task, 1)
	tasks = ex.run_commands([['true'], ['true'], ['true']])
	assert ex.wait(timeout=10) == []
	assert [task.returncode for task in tasks] == [-1, 0, 0]
	assert tasks[0]._error and tasks[0].end_time is not None
	ex.join()
	out, err = capfd.readouterr()
	assert 'still running' not in out
	# quiet: no message, and no thread died with a traceback
	assert err == ''


def test_a_fire_and_forget_thread_that_cannot_start_fails_and_the_rest_still_run(monkeypatch, capfd):
	_fail_nth_start(monkeypatch, getattr(multiCMD, '__run_tracked_command'), 2)
	tasks = run_commands([['true']] * 3, quiet=True, wait_for_return=False, return_object=True)
	multiCMD.join_threads()
	assert [task.returncode for task in tasks] == [0, -1, 0]
	assert "cannot start its thread: can't start new thread" in tasks[1].stderr[0]
	assert not _global_threads()
	assert capfd.readouterr().err == ''


@pytest.mark.parametrize('after', [False, True], ids=['before-it-runs', 'once-it-runs'])
def test_ctrl_c_while_fire_and_forget_threads_start(monkeypatch, after):
	tasks = []
	real_build = multiCMD._build_tasks
	def build(*args, **kwargs):
		built = real_build(*args, **kwargs)
		tasks.extend(built[0])
		return built
	monkeypatch.setattr(multiCMD, '_build_tasks', build)
	_fail_nth_start(monkeypatch, getattr(multiCMD, '__run_tracked_command'), 2, KeyboardInterrupt(), after)
	start = time.monotonic()
	with pytest.raises(KeyboardInterrupt):
		run_commands([['sleep', '30']] * 4, max_threads=0, quiet=True, wait_for_return=False)
	assert time.monotonic() - start < 4
	ran = 2 if after else 1
	# the ones that run are stopped ( killed, or skipped if they had not spawned yet ) before the call re-raises, the others never start
	assert all(task.stop and task.thread is not None and not task.is_alive() for task in tasks[:ran])
	assert all(task.stop and task.thread is None and task.returncode is None and task._extra_env is None for task in tasks[ran:])
	assert not _global_threads() or all(not thread.is_alive() for thread in _global_threads())
	assert not multiCMD._live_procs


def test_abandon_gives_the_same_answer_after_a_ctrl_c_cut_it_short():
	thread = multiCMD._Thread(print)
	real_set = thread.done.set
	armed = [True]
	def set_once():
		if armed[0]:
			armed[0] = False
			raise KeyboardInterrupt
		real_set()
	thread.done.set = set_once
	with pytest.raises(KeyboardInterrupt):
		thread._abandon()
	# asked again, it is still abandoned, and this time it is done
	assert thread._abandon() and thread.done.is_set() and not thread.is_alive()
	thread.start()
	thread.join(1)
	assert not thread._runs()


@pytest.mark.skipif(sys.version_info >= (3, 9, 8), reason='the held tstate lock of bpo-45274 needs Python < 3.9.8')
def test_a_join_after_an_interrupted_one_left_the_tstate_lock_held_does_not_block():
	thread = multiCMD._Thread(time.sleep, (0.1,))
	thread.start()
	thread.done.wait(5)
	time.sleep(0.1)
	# the state a Thread.join() leaves when a Ctrl-C lands right after it took the lock
	thread._tstate_lock.acquire()
	try:
		joined = threading.Event()
		def joiner():
			multiCMD._join(thread, 0.5)
			thread.join()
			joined.set()
		threading.Thread(target=joiner, daemon=True).start()
		assert joined.wait(5)
		assert not thread.is_alive()
	finally:
		thread._tstate_lock.release()




def test_ctrl_c_while_the_dispatcher_starts_keeps_it_once_it_runs(monkeypatch):
	ex = AsyncExecutor(max_threads=1, quiet=True)
	_fail_nth_start(monkeypatch, ex._dispatch, 1, KeyboardInterrupt(), after=True)
	with pytest.raises(KeyboardInterrupt):
		ex.run_commands([['sleep', '0.2']] * 3)
	assert ex._dispatching
	ex.run_commands([['true']])
	assert len([thread for thread in ex.runningThreads if thread.task is None]) <= 1
	assert ex.wait(timeout=10) == []
	assert [task.returncode for task in ex.tasks] == [0, 0, 0, 0]


def test_ctrl_c_before_the_dispatcher_runs_leaves_the_queue_to_the_next_call(monkeypatch):
	ex = AsyncExecutor(max_threads=1, quiet=True)
	_fail_nth_start(monkeypatch, ex._dispatch, 1, KeyboardInterrupt())
	with pytest.raises(KeyboardInterrupt):
		ex.run_commands([['true']] * 2)
	assert not ex._dispatching and ex.runningThreads == []
	ex.run_commands([['true']])
	assert ex.wait(timeout=10) == []
	assert [task.returncode for task in ex.tasks] == [0, 0, 0]


@pytest.mark.parametrize('quiet', [True, False])
def test_an_internal_error_fails_the_command_and_shows_only_when_not_quiet(monkeypatch, capfd, quiet):
	def broken(*args, **kwargs):
		raise ValueError('boom')
	monkeypatch.setattr(multiCMD, '_execute', broken)
	task = multiCMD.run_command(['true'], quiet=quiet, return_object=True)
	assert task.returncode == -1 and task._error and task._finalized and task.end_time is not None
	assert task.stderr[0] == 'boom' and any('ValueError: boom' in line for line in task.stderr)
	out, err = capfd.readouterr()
	if quiet:
		assert out == '' and err == ''
	else:
		assert "Error running command: ['true']\nboom\n" in err and 'boom' not in out


@pytest.mark.parametrize('quiet', [True, False])
def test_every_failure_message_shows_only_when_not_quiet(tmp_path, capfd, quiet):
	script = tmp_path / 'not-executable'
	script.write_text('#!/bin/sh\necho hi\n')
	tasks = run_commands([['mcmd-no-such-command-xyz'], [str(script)]], quiet=quiet, return_object=True)
	assert [task.returncode for task in tasks] == [127, 126]
	# the failure is on the task either way
	assert 'mcmd-no-such-command-xyz' in tasks[0].stderr[0] and 'Permission denied' in tasks[1].stderr[0]
	err = capfd.readouterr().err
	if quiet:
		assert err == ''
	else:
		assert 'Command path not found: mcmd-no-such-command-xyz' in err and f'Command cannot be executed: {script}' in err


class _BrokenStderr:
	def write(self, text):
		raise BrokenPipeError(32, 'Broken pipe')

	def flush(self):
		raise BrokenPipeError(32, 'Broken pipe')


def test_a_failure_that_cannot_be_shown_is_still_recorded(monkeypatch):
	# a fire-and-forget thread that cannot start, its message on a stderr that cannot be written
	_fail_nth_start(monkeypatch, getattr(multiCMD, '__run_tracked_command'), 1)
	monkeypatch.setattr(sys, 'stderr', _BrokenStderr())
	tasks = run_commands([['true']] * 3, wait_for_return=False, return_object=True)
	multiCMD.join_threads()
	# before the fix the write error stopped the whole batch and escaped from run_commands()
	assert [task.returncode for task in tasks] == [-1, 0, 0]
	assert tasks[0]._finalized and "can't start new thread" in tasks[0].stderr[0]
	# the internal error path, and a command that is not found
	def broken(*args, **kwargs):
		raise ValueError('boom')
	monkeypatch.setattr(multiCMD, '_execute', broken)
	task = multiCMD.run_command(['true'], return_object=True)
	assert task.returncode == -1 and task._finalized and task.stderr[0] == 'boom'
	monkeypatch.undo()
	monkeypatch.setattr(sys, 'stderr', _BrokenStderr())
	assert multiCMD.run_command(['mcmd-no-such-command-xyz'], return_code_only=True) == 127


def test_fail_task_run_again_records_the_failure_once():
	task = multiCMD.Task(['true'])
	try:
		raise RuntimeError("can't start new thread")
	except RuntimeError as e:
		error = e
	multiCMD._fail_task(task, 'cannot start its thread', error, quiet=True)
	first = list(task.stderr)
	multiCMD._fail_task(task, 'cannot start its thread', error, quiet=True)
	assert task.stderr == first and task.returncode == -1 and task._finalized
