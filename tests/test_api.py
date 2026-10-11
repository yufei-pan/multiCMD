import pytest

import multiCMD
from multiCMD import CommandError, run_command, run_commands


def test_dry_run_prints_each_command_quoted_in_order(capsys):
	assert run_commands([['echo', 'a b'], 'echo [1-2]'], parse=True, dry_run=True) == [[], [], []]
	assert capsys.readouterr().out == "echo 'a b'\necho 1\necho 2\n"


def test_quiet_dry_run_prints_nothing(capsys):
	run_commands([['echo', 'a']], dry_run=True, quiet=True)
	assert capsys.readouterr().out == ''


def test_dry_run_skips_sudo_validation(monkeypatch, capsys):
	monkeypatch.setattr(multiCMD, 'SUDO_PATH', '/usr/bin/sudo')
	def fail():
		raise AssertionError('sudo -v must not run in a dry run')
	monkeypatch.setattr(multiCMD, '_validate_sudo', fail)
	run_commands([['id']], dry_run=True, use_sudo=True)
	assert capsys.readouterr().out == '/usr/bin/sudo id\n'


def test_dry_run_tasks_are_not_failures():
	assert run_commands([['false']], dry_run=True, quiet=True, raise_error=True) == [[]]


def test_command_error_lists_failed_commands():
	with pytest.raises(CommandError) as info:
		run_commands([['true'], ['false'], ['sh', '-c', 'echo oops >&2; exit 3']], quiet=True, raise_error=True)
	error = info.value
	assert isinstance(error, Exception)
	assert [task.returncode for task in error.tasks] == [1, 3]
	assert str(error).splitlines() == [
		'Commands failed (2 of 3):',
		'  [rc 1] false',
		"  [rc 3] sh -c 'echo oops >&2; exit 3': oops",
	]


def test_command_error_truncates_long_lines():
	with pytest.raises(CommandError) as info:
		run_command(['sh', '-c', 'printf "%0300d\\n" 0 >&2; exit 1'], quiet=True, raise_error=True)
	last = str(info.value).splitlines()[-1]
	assert last.endswith('...')
	assert len(last.split(': ', 1)[1]) == 200


def test_fire_and_forget_does_not_raise_for_unfinished_commands():
	run_commands([['sleep', '0.2']], wait_for_return=False, quiet=True, raise_error=True)
	multiCMD.join_threads(timeout=5)
