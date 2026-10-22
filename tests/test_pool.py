import _thread
import sys
import threading
import time

import pytest

import multiCMD
from conftest import pid_gone, sigint_main_after
from multiCMD import run_commands


@pytest.mark.slow
def test_pool_keeps_threads_bounded():
	baseline = threading.active_count()
	peak = [0]
	done = threading.Event()
	def watch():
		while not done.is_set():
			peak[0] = max(peak[0], threading.active_count())
			time.sleep(0.001)
	watcher = threading.Thread(target=watch, daemon=True)
	watcher.start()
	try:
		codes = run_commands([['true']] * 1000, max_threads=4, quiet=True, return_code_only=True)
	finally:
		done.set()
		watcher.join()
	assert codes == [0] * 1000
	# 4 workers, 2 output readers per running command, and the watcher itself
	assert peak[0] <= baseline + 4 + 2 * 4 + 1


def test_empty_command_list():
	assert run_commands([], max_threads=4, quiet=True) == []
	assert run_commands([], max_threads=0, quiet=True) == []
	assert run_commands([], quiet=True) == []


def test_prefix_only_when_commands_run_at_once(capsys):
	run_commands([['echo', 'a'], ['echo', 'b']], max_threads=2, prefix=True)
	out = capsys.readouterr().out
	assert '[1] a' in out and '[2] b' in out
	assert '[1] Running command: echo a' in out
	run_commands([['echo', 'a'], ['echo', 'b']], max_threads=1, prefix=True)
	assert '[1]' not in capsys.readouterr().out
	run_commands([['echo', 'a']], max_threads=4, prefix=True)
	assert '[1]' not in capsys.readouterr().out


def test_colors_follow_the_color_argument(capsys):
	# 1.51 never colored this path: its color argument landed in with_stdErr
	run_commands([['echo', 'a'], ['echo', 'b']], max_threads=2, color=True)
	assert '\x1b[38;2;' in capsys.readouterr().out
	run_commands([['echo', 'a'], ['echo', 'b']], max_threads=2)
	assert '\x1b[' not in capsys.readouterr().out


@pytest.mark.slow
@pytest.mark.skipif(not sys.platform.startswith('linux'), reason='checks processes through /proc')
def test_ctrl_c_stops_every_running_command_in_the_pool(tmp_path):
	pidfiles = [tmp_path / f'pid{i}' for i in range(2)]
	commands = [['sh', '-c', f'sleep 30 & echo $! > {path}; wait'] for path in pidfiles] + [['sleep', '30']] * 4
	timer = threading.Timer(0.5, _thread.interrupt_main)
	timer.start()
	start = time.monotonic()
	with pytest.raises(KeyboardInterrupt):
		run_commands(commands, max_threads=2, quiet=True)
	assert time.monotonic() - start < 5
	for path in pidfiles:
		assert pid_gone(int(path.read_text()))


@pytest.mark.slow
def test_ctrl_c_in_a_pool_waits_for_a_command_slow_to_die(monkeypatch):
	# a real SIGINT lands in the wait for the first worker; on 3.9.8 - 3.12 Thread.join() then takes it for finished
	tasks = []
	real = multiCMD._build_tasks
	def build(*args, **kwargs):
		built = real(*args, **kwargs)
		tasks.extend(built[0])
		return built
	monkeypatch.setattr(multiCMD, '_build_tasks', build)
	start = time.monotonic()
	with pytest.raises(KeyboardInterrupt), sigint_main_after(0.7):
		run_commands([['sh', '-c', 'trap "" INT TERM; sleep 30'], ['sleep', '30']], max_threads=2, quiet=True)
	# the first one dies only to SIGKILL, a second after the stop
	assert time.monotonic() - start >= 1.5
	assert all(task.returncode is not None for task in tasks), [task.returncode for task in tasks]
	assert not multiCMD._live_procs


def test_a_finished_pool_task_does_not_report_its_worker(monkeypatch):
	# one worker runs the first and the third command: while it runs the third, the first is not alive
	real = getattr(multiCMD, '__run_command')
	seen = []
	def run(task, *args, **kwargs):
		if task.command == ['echo', 'third']:
			seen.append((first[0].is_alive(), first[0].thread))
		return real(task, *args, **kwargs)
	first = []
	real_build = multiCMD._build_tasks
	def build(*args, **kwargs):
		built = real_build(*args, **kwargs)
		first.append(built[0][0])
		return built
	monkeypatch.setattr(multiCMD, '__run_command', run)
	monkeypatch.setattr(multiCMD, '_build_tasks', build)
	tasks = run_commands([['true'], ['sleep', '0.5'], ['echo', 'third']], max_threads=2, quiet=True, return_object=True)
	assert seen == [(False, None)]
	assert all(task.thread is None and not task.is_alive() for task in tasks)


def _maps():
	with open('/proc/self/maps') as f:
		return len(f.readlines())


@pytest.mark.skipif(not sys.platform.startswith('linux') or sys.version_info < (3, 13), reason='from 3.13 an unjoined finished thread keeps its stack; checked through /proc')
@pytest.mark.parametrize('how', ['pool', 'executor', 'fire-and-forget'])
def test_finished_threads_release_their_stacks(how):
	kept = []
	before = _maps()
	for _ in range(30):
		if how == 'pool':
			kept += run_commands([['true']] * 4, max_threads=4, quiet=True, return_object=True)
		elif how == 'executor':
			ex = multiCMD.AsyncExecutor(max_threads=4, quiet=True)
			ex.run_commands([['true']] * 4)
			ex.wait()
			kept.append(ex)
		else:
			kept += run_commands([['true']] * 4, max_threads=4, quiet=True, return_object=True, wait_for_return=False)
			multiCMD.join_threads()
	# each unjoined thread stack is a mapping and its guard page another: 30 x 4 commands would add hundreds
	assert _maps() - before < 40, (before, _maps())
