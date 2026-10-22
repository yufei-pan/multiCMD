import _thread
import os
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


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
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


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_a_command_whose_thread_cannot_start_is_finalized_as_failed(monkeypatch, capsys):
	ex = AsyncExecutor(max_threads=1, quiet=True)
	real = ex._start_thread
	def broken(target, args, task=None):
		if task is None:
			return real(target, args)
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
def test_join_threads_after_an_interrupted_one_still_waits():
	task = multiCMD.run_command(['sleep', '1'], quiet=True, wait_for_return=False, return_object=True)
	with pytest.raises(KeyboardInterrupt), sigint_main_after(0.2):
		multiCMD.join_threads()
	assert task.is_alive() and task.returncode is None
	multiCMD.join_threads()
	assert task.returncode == 0 and not task.is_alive()
	assert task.thread not in _global_threads()


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
	while not thread._claim.locked() and time.monotonic() < deadline:
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
