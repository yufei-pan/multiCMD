import os
import threading
import time

import pytest

import multiCMD
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
