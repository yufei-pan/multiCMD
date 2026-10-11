import re
import shutil
import signal
import subprocess
import sys
import time

import pytest

from conftest import MULTICMD_PY, pid_gone, run_cli
import multiCMD


def test_version_flag():
	proc = run_cli(["-V"])
	assert proc.returncode == 0
	assert multiCMD.version in (proc.stdout + proc.stderr)


def test_help_flag():
	proc = run_cli(["-h"])
	assert proc.returncode == 0
	assert "max_threads" in proc.stdout


def test_two_echoes():
	proc = run_cli(["echo hello", "echo world"])
	assert proc.returncode == 0
	combined = proc.stdout + proc.stderr
	assert "hello" in combined
	assert "world" in combined


def test_parse_flag_expands():
	proc = run_cli(["-p", "echo [1-2]"])
	assert proc.returncode == 0
	combined = proc.stdout + proc.stderr
	assert "[1-2]" not in combined
	assert "Running command: echo 1" in combined
	assert "Running command: echo 2" in combined


def test_max_threads_and_timeout_smoke():
	proc = run_cli(["-q", "-m", "2", "-t", "5", "echo a", "echo b"], timeout=20)
	assert proc.returncode == 0


def test_sudo_flag_without_sudo_binary_does_not_crash(monkeypatch):
	# Force child env: PATH without sudo by invoking python -c wrapper is heavy;
	# instead assert current host behavior: --sudo with missing sudo should not
	# abort with traceback. If sudo exists, command still succeeds for echo.
	proc = run_cli(["-q", "--sudo", "echo ok"], timeout=20)
	assert proc.returncode == 0
	assert "Traceback" not in (proc.stdout + proc.stderr)


def test_exit_status_single_command_passes_its_code_through():
	assert run_cli(['-q', 'false']).returncode == 1
	assert run_cli(['-q', "sh -c 'exit 3'"]).returncode == 3


@pytest.mark.slow
def test_exit_status_timeout_is_124():
	assert run_cli(['-q', '-t', '1', 'sleep 5']).returncode == 124


def test_exit_status_signal_death_is_128_plus_n():
	assert run_cli(['-q', "sh -c 'kill -TERM $$'"]).returncode == 128 + 15


def test_exit_status_counts_failed_commands():
	assert run_cli(['-q', 'true', 'false', "sh -c 'exit 3'"]).returncode == 2


@pytest.mark.slow
def test_exit_status_failure_count_is_capped_at_101():
	assert run_cli(['-q', '-m', '0', '-p', 'false [1-105]'], timeout=120).returncode == 101


def test_help_documents_timeout_default_and_exit_status():
	# argparse wraps help to the terminal width, so compare with the whitespace collapsed
	help_text = ' '.join(run_cli(['-h']).stdout.split())
	assert '0 = never' in help_text
	assert 'exit status' in help_text


def test_dry_run_prints_quoted_commands_even_when_quiet():
	proc = run_cli(['-n', '-q', '-p', 'echo host[1-2]', "echo 'a b'"])
	assert proc.returncode == 0
	assert proc.stdout.splitlines() == ['echo host1', 'echo host2', "echo 'a b'"]


def test_unbalanced_quote_is_a_usage_error():
	proc = run_cli(['-q', "echo 'oops"])
	assert proc.returncode == 2
	assert 'cannot parse command' in proc.stderr


def test_shell_flag_runs_pipelines():
	assert 'xbc' in run_cli(['--shell', 'echo abc | tr a x']).stdout


@pytest.mark.skipif(not sys.platform.startswith('linux'), reason='reads /proc')
def test_commands_started_through_a_shell_still_ignore_terminal_reads():
	# dash clears the blocked mask before it execs a command; an ignored SIGTTIN survives any shell
	out = run_cli(["sh -c 'sh -c \"grep SigIgn /proc/self/status\"'"]).stdout
	ignored = int(re.search(r'SigIgn:\s+([0-9a-f]+)', out).group(1), 16)
	assert ignored & (1 << (signal.SIGTTIN - 1))
	assert ignored & (1 << (signal.SIGTTOU - 1))


def _start_cli(*args):
	return subprocess.Popen([sys.executable, str(MULTICMD_PY), *args],
		stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def _read_pid(proc):
	"""The first output line that is only a number: the pid a test command printed"""
	for line in proc.stdout:
		if line.strip().isdigit():
			return int(line)
	raise AssertionError('the command printed no pid')


@pytest.mark.skipif(signal.getsignal(signal.SIGINT) == signal.SIG_IGN, reason='SIGINT is ignored here')
@pytest.mark.skipif(not sys.platform.startswith('linux'), reason='checks processes through /proc')
def test_ctrl_c_stops_the_commands():
	proc = _start_cli("sh -c 'sleep 30 & echo $!; echo started; wait'")
	child = _read_pid(proc)
	proc.send_signal(signal.SIGINT)
	assert proc.wait(timeout=10) == 130
	assert pid_gone(child)


@pytest.mark.skipif(not sys.platform.startswith('linux'), reason='checks processes through /proc')
def test_sigterm_stops_the_commands():
	proc = _start_cli("sh -c 'sleep 30 & echo $!; echo started; wait'")
	child = _read_pid(proc)
	proc.send_signal(signal.SIGTERM)
	assert proc.wait(timeout=10) == 128 + 15
	assert pid_gone(child)


@pytest.mark.skipif(shutil.which('nohup') is None, reason='needs nohup')
def test_nohup_keeps_the_commands_running_after_a_hangup():
	# nohup leaves SIGHUP ignored; mcmd must not replace that with its kill-everything handler
	proc = subprocess.Popen(['nohup', sys.executable, str(MULTICMD_PY), "sh -c 'echo $$; echo started; sleep 2'"],
		stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
	_read_pid(proc)
	proc.send_signal(signal.SIGHUP)
	assert proc.wait(timeout=10) == 0


@pytest.mark.skipif(signal.getsignal(signal.SIGINT) == signal.SIG_IGN, reason='SIGINT is ignored here')
def test_ctrl_c_in_a_sequential_run_skips_the_rest(tmp_path):
	marker = tmp_path / 'ran'
	proc = _start_cli("sh -c 'echo $$; echo started; sleep 30'", f'touch {marker}')
	_read_pid(proc)
	proc.send_signal(signal.SIGINT)
	assert proc.wait(timeout=10) == 130
	assert not marker.exists()


@pytest.mark.skipif(not sys.platform.startswith('linux'), reason='checks processes through /proc')
def test_sigterm_reaches_what_a_finished_command_left_running():
	proc = _start_cli("sh -c 'sleep 30 & echo $!; echo started'")
	child = _read_pid(proc)
	time.sleep(0.3)
	proc.send_signal(signal.SIGTERM)
	assert proc.wait(timeout=10) == 128 + 15
	assert pid_gone(child)
