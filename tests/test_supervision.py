import _thread
import subprocess
import sys
import threading
import time

import pytest

import multiCMD
from conftest import pid_gone
from multiCMD import AsyncExecutor, run_command

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
