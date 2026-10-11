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
from conftest import PACKAGE_ROOT, pid_gone
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
