import pytest

import multiCMD
from multiCMD import run_command, run_commands


def test_string_commands_follow_shell_quoting():
	assert run_command("printf '%s|' 'a b'", quiet=True) == ['a b|']
	assert run_command('printf "%s|" "it\'s"', quiet=True) == ["it's|"]


def test_simple_strings_split_as_before():
	tasks, _ = multiCMD._build_tasks(['lsblk -b -n -p -o NAME,SIZE -P', 'ping -c 1 host1'])
	assert [task.command for task in tasks] == [
		['lsblk', '-b', '-n', '-p', '-o', 'NAME,SIZE', '-P'],
		['ping', '-c', '1', 'host1'],
	]


def test_unbalanced_quote_fails_before_anything_runs(tmp_path):
	marker = tmp_path / 'ran'
	with pytest.raises(ValueError, match='cannot parse command'):
		run_commands([['touch', str(marker)], "echo 'oops"], quiet=True)
	assert not marker.exists()


def test_shell_mode_runs_pipelines():
	assert run_command('echo abc | tr a x', shell=True, quiet=True) == ['xbc']


def test_shell_mode_quotes_list_commands():
	assert run_command(['echo', 'a  b'], shell=True, quiet=True) == ['a  b']


def test_shell_mode_with_sudo_wraps_the_shell(monkeypatch):
	monkeypatch.setattr(multiCMD, 'SUDO_PATH', '/usr/bin/sudo')
	tasks, used = multiCMD._build_tasks(['echo hi'], use_sudo=True, shell=True)
	assert used is True
	assert tasks[0].command == ['/usr/bin/sudo', '/bin/sh', '-c', 'echo hi']


def test_shell_mode_expands_ranges_first():
	assert run_commands(['echo x[1-2] | tr x y'], parse=True, shell=True, quiet=True) == [['y1'], ['y2']]


def test_shell_mode_refuses_argument_lists_for_cmd_exe(monkeypatch):
	# POSIX quoting means nothing to cmd.exe, so a list cannot be joined safely there
	monkeypatch.setattr(multiCMD, '_IS_POSIX', False)
	with pytest.raises(ValueError, match='POSIX shell'):
		multiCMD._shell_argv(['echo', 'a&calc'])
	assert multiCMD._shell_argv('echo hi')[-2:] == ['/c', 'echo hi']


def test_shell_template_keeps_the_placeholder_out_of_the_shell_code():
	assert multiCMD._shell_template('echo {}') == 'echo "$1"'
	assert multiCMD._shell_template('echo "x{}y"') == 'echo "x$1y"'
	assert multiCMD._shell_template("echo '{}'") == "echo ''\"$1\"''"
	assert multiCMD._shell_template('echo \\"{}') == 'echo \\""$1"'
	assert multiCMD._shell_template('echo') == 'echo "$1"'
