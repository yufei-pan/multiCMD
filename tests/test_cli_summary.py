import os
import shlex
import shutil
import subprocess
import sys

import pytest

from conftest import MULTICMD_PY, run_cli


def _rows(stdout):
	'''The summary table's data rows, as lists of cells'''
	rows = []
	for line in stdout.splitlines():
		cells = [cell.strip() for cell in line.split('|')]
		if len(cells) == 4 and cells[0].isdigit():
			rows.append(cells)
	return rows


def test_summary_lists_failed_commands_and_a_tally():
	proc = run_cli(['-q', 'true', 'false'])
	assert proc.returncode == 1
	assert [row[:3] for row in _rows(proc.stdout)] == [['2', 'false', '1']]
	assert proc.stdout.splitlines()[-1] == '2 commands: 1 succeeded, 1 failed'


def test_summary_when_everything_succeeded_is_one_line():
	proc = run_cli(['-q', 'true', 'true'])
	assert _rows(proc.stdout) == []
	assert proc.stdout.splitlines() == ['2 commands: all succeeded']


def test_summary_flag_lists_every_command():
	proc = run_cli(['-q', '--summary', 'true', 'false'])
	assert [row[:3] for row in _rows(proc.stdout)] == [['1', 'true', '0'], ['2', 'false', '1']]


def test_no_summary_flag():
	assert 'commands:' not in run_cli(['-q', '--no-summary', 'true', 'false']).stdout


def test_no_summary_for_one_command_or_a_dry_run():
	assert 'commands:' not in run_cli(['-q', 'false']).stdout
	assert 'commands:' not in run_cli(['-n', 'true', 'false']).stdout


@pytest.mark.slow
def test_summary_labels_return_codes():
	proc = run_cli(['-q', '-t', '1', 'sleep 5', 'no-such-command-xyz', "sh -c 'kill -TERM $$'", 'true'])
	assert proc.returncode == 3
	assert [row[:3] for row in _rows(proc.stdout)] == [
		['1', 'sleep 5', '124 (timeout)'],
		['2', 'no-such-command-xyz', '127 (not found)'],
		['3', "sh -c 'kill -TERM $$'", '-15 (SIGTERM)'],
	]
	assert proc.stdout.splitlines()[-1] == '4 commands: 1 succeeded, 3 failed'


def test_prefix_only_for_concurrent_runs():
	assert '[1] a' in run_cli(['-m', '2', 'echo a', 'echo b']).stdout
	assert '[1]' not in run_cli(['-m', '2', '--no-prefix', 'echo a', 'echo b']).stdout
	assert '[1]' not in run_cli(['-m', '1', 'echo a', 'echo b']).stdout
	assert '[1]' not in run_cli(['-m', '4', 'echo a']).stdout


@pytest.mark.skipif(os.name != 'posix', reason='pty mode is POSIX only')
def test_pty_flags():
	check = "sh -c '[ -t 1 ] && echo tty || echo pipe'"
	assert 'tty' in run_cli(['--pty', check]).stdout
	# run_cli's stdout is a pipe, so pty mode stays off by default
	assert 'pipe' in run_cli([check]).stdout
	assert 'pipe' in run_cli(['--no-pty', check]).stdout


@pytest.mark.skipif(shutil.which('script') is None or not sys.platform.startswith('linux'),
	reason='needs util-linux script for a terminal')
def test_colors_on_a_terminal_and_the_no_color_flag():
	env = {key: value for key, value in os.environ.items() if key != 'NO_COLOR'}
	def under_terminal(*args):
		command = ' '.join(shlex.quote(arg) for arg in [sys.executable, str(MULTICMD_PY), *args])
		return subprocess.run(['script', '-qec', command, '/dev/null'], stdin=subprocess.DEVNULL,
			stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=60, env=env).stdout
	assert '\x1b[38;2;' in under_terminal('-m', '2', 'echo a', 'echo b')
	assert '\x1b[38;2;' not in under_terminal('--no-color', '-m', '2', 'echo a', 'echo b')


def test_internal_errors_are_not_reported_as_sighup():
	import multiCMD
	task = multiCMD.Task(['x'])
	task.returncode = -1
	task._error = True
	assert multiCMD._format_returncode(task) == '-1 (error)'
	assert multiCMD._exit_status([task]) == 1
	# a real SIGHUP death still maps to 128+1
	assert run_cli(['-q', "sh -c 'kill -HUP $$'"]).returncode == 129


def test_empty_command_is_a_usage_error():
	proc = run_cli(['-q', ''])
	assert proc.returncode == 2
	assert 'empty command' in proc.stderr
