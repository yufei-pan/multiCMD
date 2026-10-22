import os
import pathlib
import subprocess
import sys
import threading
from unittest import mock

import pytest

import multiCMD
from multiCMD import AsyncExecutor, run_command, run_commands

from conftest import PACKAGE_ROOT, run_cli

ECHO = ['sh', '-c', 'echo "[$MCMD_T]"']


@pytest.fixture(autouse=True)
def _clean(monkeypatch, launcher):
	# every test here runs with Popen(process_group=) ( 3.11+ ) and with the exec wrapper
	monkeypatch.delenv('MCMD_T', raising=False)


def _tool(tmp_path):
	'''An executable script in tmp_path; skips on a noexec filesystem'''
	tool = tmp_path / 'mcmd-env-tool'
	tool.write_text('#!/bin/sh\necho found\n')
	tool.chmod(0o755)
	try:
		subprocess.run([str(tool)], stdout=subprocess.PIPE, check=True)
	except PermissionError:
		pytest.skip(f'{tmp_path} does not allow executing files')
	return tool


def _clean_parent(code, launcher):
	'''Run code in a Python that has only PATH in its environment ( no locale, so PEP 538 coercion applies ) with multiCMD imported'''
	script = ('import sys; sys.path.insert(0, %r); import multiCMD; multiCMD._HAS_PROCESS_GROUP = %r\n' % (str(PACKAGE_ROOT), launcher == 'native')) + code
	proc = subprocess.run([sys.executable, '-c', script], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
		env={'PATH': os.environ['PATH']}, timeout=60)
	assert proc.returncode == 0, proc.stderr
	return proc.stdout


def test_extra_env_reaches_the_child():
	assert run_command(ECHO, quiet=True, extra_env={'MCMD_T': 'x'}) == ['[x]']


def test_extra_env_reaches_a_shell_command():
	assert run_command('echo "[$MCMD_T]" | cat', quiet=True, shell=True, extra_env={'MCMD_T': 'y'}) == ['[y]']


def test_env_reaches_the_child():
	out = run_command([sys.executable, '-c', 'import os; print(os.environ.get("MCMD_T"))'], quiet=True,
		env={'MCMD_T': 'z', 'PATH': os.environ.get('PATH', '')})
	assert out == ['z']


def test_default_inherits_the_parent_environment(monkeypatch):
	monkeypatch.setenv('MCMD_T', 'parent')
	assert run_command(ECHO, quiet=True) == ['[parent]']


def test_parent_environment_is_not_changed(monkeypatch):
	monkeypatch.setenv('MCMD_KEEP', 'k')
	before = dict(os.environ)
	run_command(ECHO, quiet=True, extra_env={'MCMD_T': 'x', 'MCMD_KEEP': 'other'})
	run_command(ECHO, quiet=True, env={'PATH': os.environ['PATH']})
	assert dict(os.environ) == before


def test_extra_env_overrides_and_keeps_the_rest(monkeypatch):
	monkeypatch.setenv('MCMD_T', 'parent')
	monkeypatch.setenv('MCMD_KEEP', 'k')
	out = run_command(['sh', '-c', 'echo "$MCMD_T $MCMD_KEEP"'], quiet=True, extra_env={'MCMD_T': 'child'})
	assert out == ['child k']


def test_extra_env_none_removes_a_variable(monkeypatch):
	monkeypatch.setenv('MCMD_T', 'parent')
	assert run_command(ECHO, quiet=True, extra_env={'MCMD_T': None}) == ['[]']


def test_env_replaces_the_inherited_environment(monkeypatch):
	monkeypatch.setenv('MCMD_KEEP', 'k')
	out = run_command(['sh', '-c', 'echo "[$MCMD_KEEP][$MCMD_T]"'], quiet=True, env={'MCMD_T': 'only', 'PATH': os.environ['PATH']})
	assert out == ['[][only]']


def test_extra_env_applies_on_top_of_env():
	out = run_command(['sh', '-c', 'echo "$A $B"'], quiet=True, env={'A': '1', 'B': '2', 'PATH': os.environ['PATH']}, extra_env={'B': '3'})
	assert out == ['1 3']


def test_path_lookup_follows_env(tmp_path):
	_tool(tmp_path)
	assert run_command(['mcmd-env-tool'], quiet=True, return_code_only=True) == 127
	assert run_command(['mcmd-env-tool'], quiet=True, extra_env={'PATH': str(tmp_path) + os.pathsep + os.environ['PATH']}) == ['found']


def test_env_without_path_searches_the_default_path_not_the_parents(tmp_path, monkeypatch, capsys):
	_tool(tmp_path)
	monkeypatch.setenv('PATH', str(tmp_path) + os.pathsep + os.environ['PATH'])
	assert run_command(['mcmd-env-tool'], quiet=True) == ['found']
	# the parent's PATH is not the command's: the lookup fails by name, the same way on every Python
	for kwargs in ({'env': {'HOME': '/tmp'}}, {'extra_env': {'PATH': None}}):
		task = run_command(['mcmd-env-tool'], quiet=True, return_object=True, **kwargs)
		assert task.returncode == 127
		assert 'mcmd-env-tool' in capsys.readouterr().err
		assert 'mcmd-env-tool' in ' '.join(task.stderr)
	# and the default path finds the system tools
	assert run_command(['ls', '/'], quiet=True, return_code_only=True, env={'HOME': '/tmp'}) == 0
	monkeypatch.setenv('PATH', str(tmp_path))
	assert run_command(['ls', '/'], quiet=True, return_code_only=True, env={'HOME': '/tmp'}) == 0
	assert run_command(['ls', '/'], quiet=True, return_code_only=True, extra_env={'PATH': None}) == 0


def test_error_still_names_the_command(capsys):
	task = run_command(['mcmd-no-such-command', 'arg'], quiet=True, extra_env={'MCMD_T': 'x'}, return_object=True)
	assert task.returncode == 127
	assert 'mcmd-no-such-command' in capsys.readouterr().err
	assert 'mcmd-no-such-command' in ' '.join(task.stderr)


def test_return_codes_and_timeout_with_env():
	assert run_command(['sh', '-c', 'exit "${MCMD_T}"'], quiet=True, return_code_only=True, extra_env={'MCMD_T': '7'}) == 7
	task = run_command(['sh', '-c', 'sleep 5'], quiet=True, timeout=0.3, extra_env={'MCMD_T': 'x'}, return_object=True)
	assert task.returncode == 124


def test_dry_run_shows_the_environment(capsys):
	assert run_command(['xfs_info', '/data'], dry_run=True, extra_env={'LC_ALL': 'C'}) == []
	assert capsys.readouterr().out.strip() == 'env LC_ALL=C xfs_info /data'
	run_command(['ls'], dry_run=True, extra_env={'A B': 'x y', 'GONE': None})
	assert capsys.readouterr().out.strip() == "env -u GONE 'A B=x y' ls"
	run_command(['ls'], dry_run=True, env={'PATH': '/bin'}, extra_env={'A': '1'})
	assert capsys.readouterr().out.strip() == 'env -i PATH=/bin A=1 ls'
	run_command(['ls'], dry_run=True)
	assert capsys.readouterr().out.strip() == 'ls'
	run_command(['ls'], dry_run=True, extra_env={})
	assert capsys.readouterr().out.strip() == 'ls'


def test_concurrent_commands_each_see_their_own_value():
	values = [str(i) for i in range(16)]
	tasks = [run_command(['sh', '-c', 'sleep 0.1; echo "$MCMD_T"'], quiet=True, max_threads=1, extra_env={'MCMD_T': v}, return_object=True, wait_for_return=False) for v in values]
	multiCMD.join_threads()
	assert [t.stdout for t in tasks] == [[v] for v in values]


def test_run_commands_applies_to_every_command_in_a_pool():
	out = run_commands(['echo "$MCMD_T"'] * 6, quiet=True, max_threads=6, shell=True, extra_env={'MCMD_T': 'same'})
	assert out == [['same']] * 6


def test_calls_in_parallel_threads_do_not_leak():
	import threading
	results = {}
	def call(value):
		results[value] = run_command(['sh', '-c', 'sleep 0.1; echo "$MCMD_T"'], quiet=True, extra_env={'MCMD_T': value})
	threads = [threading.Thread(target=call, args=(str(i),)) for i in range(8)]
	for t in threads:
		t.start()
	for t in threads:
		t.join()
	assert results == {str(i): [str(i)] for i in range(8)}
	assert 'MCMD_T' not in os.environ


def test_caller_mutating_the_mapping_later_does_not_matter():
	extra = {'MCMD_T': 'before'}
	tasks = run_commands([ECHO], quiet=True, extra_env=extra, return_object=True, wait_for_return=False)
	extra['MCMD_T'] = 'after'
	multiCMD.join_threads()
	assert tasks[0].stdout == ['[before]']


def test_invalid_environment_is_rejected_before_running(tmp_path):
	marker = tmp_path / 'ran'
	with pytest.raises(TypeError):
		run_command(['touch', str(marker)], quiet=True, extra_env={'A': 1})
	with pytest.raises(TypeError):
		run_command(['touch', str(marker)], quiet=True, env={'A': None})
	with pytest.raises(ValueError):
		run_command(['touch', str(marker)], quiet=True, extra_env={'A=B': 'x'})
	with pytest.raises(TypeError):
		run_command(['touch', str(marker)], quiet=True, env=['A=1'])
	assert not marker.exists()


def test_executor_per_call_and_default_env():
	ex = AsyncExecutor(max_threads=3, quiet=True, extra_env={'MCMD_T': 'default'})
	ex.run_command(ECHO)
	ex.run_command(ECHO, extra_env={'MCMD_T': 'call'})
	ex.run_command(ECHO, extra_env=None)
	ex.wait(timeout=5)
	assert ex.get_results() == [['[default]'], ['[call]'], ['[]']]


def test_pty_gets_the_env_and_keeps_the_pager_default(monkeypatch):
	monkeypatch.setenv('PAGER', 'less')
	cmd = ['sh', '-c', 'echo "$MCMD_T $PAGER $GIT_PAGER"']
	assert run_command(cmd, quiet=True, pty=True, extra_env={'MCMD_T': 'p'}) == ['p cat cat']
	assert run_command(cmd, quiet=True, pty=True, extra_env={'MCMD_T': 'p', 'PAGER': 'more'}) == ['p more cat']
	assert run_command(cmd, quiet=True, pty=True, env={'PATH': os.environ['PATH']}) == [' cat cat']


def test_cli_env_option():
	parent = dict(os.environ, MCMD_GONE='1')
	proc = run_cli(['-q', '--no-pty', '-e', 'MCMD_T=cli', '-e', 'MCMD_GONE', 'echo hi'], env=parent)
	assert proc.returncode == 0
	proc = run_cli(['--no-pty', '--shell', '-e', 'MCMD_T=cli', '-e', 'MCMD_GONE', 'echo "[$MCMD_T][$MCMD_GONE]"'], env=parent)
	assert '[cli][]' in proc.stdout
	proc = run_cli(['-n', '-e', 'LC_ALL=C', '-e', 'GONE', 'xfs_info /data'])
	assert proc.stdout.strip() == 'env -u GONE LC_ALL=C xfs_info /data'


def test_cli_rejects_a_bad_assignment_before_the_sudo_check(monkeypatch, capsys):
	monkeypatch.setattr(multiCMD, 'SUDO_PATH', None)
	monkeypatch.setattr(os, 'geteuid', lambda: 1000)
	monkeypatch.setattr(sys, 'argv', ['mcmd', '--sudo', '-e', '=X', 'ls'])
	with pytest.raises(SystemExit) as raised:
		multiCMD.main()
	assert raised.value.code == 2
	err = capsys.readouterr().err
	assert 'invalid assignment' in err
	assert 'ignoring it' not in err


def test_exec_is_exactly_the_requested_environment(launcher):
	# a Python that starts without a locale ( 3.7-3.10 ) sets LC_CTYPE for itself; the command must not get it
	out = _clean_parent("""
for kwargs in ({'env': {'A': '1', 'PATH': '/usr/bin:/bin'}}, {'env': {}}, {'env': {'PATH': '/usr/bin:/bin'}, 'extra_env': {'LC_CTYPE': None}}):
	print(sorted(multiCMD.run_command(['/usr/bin/env'], quiet=True, **kwargs)))
print(sorted(multiCMD.run_command(['/usr/bin/env'], quiet=True, extra_env={'LC_CTYPE': None, 'A': '1'})))
""", launcher).splitlines()
	assert out[0] == "['A=1', 'PATH=/usr/bin:/bin']"
	assert out[1] == '[]'
	assert out[2] == "['PATH=/usr/bin:/bin']"
	# removing a variable the parent was given, by its own coercion or not, works
	assert 'A=1' in out[3] and 'LC_CTYPE' not in out[3]


def test_wrapper_starts_in_the_parents_environment_and_gets_the_target_one_separately(launcher, monkeypatch):
	if launcher == 'native':
		pytest.skip('only the exec wrapper starts a Python first')
	real_popen = subprocess.Popen
	seen = []
	def popen(argv, **kwargs):
		seen.append(kwargs.get('env', 'unset'))
		return real_popen(argv, **kwargs)
	monkeypatch.setattr(subprocess, 'Popen', popen)
	# the interpreter running the wrapper must not be starved of what the parent's environment gives it ( LD_LIBRARY_PATH, ... )
	assert run_command(['sh', '-c', 'echo "[$A]"'], quiet=True, env={'A': 'x', 'PATH': os.environ['PATH']}) == ['[x]']
	assert run_command(ECHO, quiet=True, extra_env={'MCMD_T': 'y', 'LD_LIBRARY_PATH': None}) == ['[y]']
	assert seen == [None, None]


def test_child_env_survives_other_threads_changing_os_environ():
	names = ['MCMD_RACE_%d' % i for i in range(40)]
	stop = threading.Event()
	def churn():
		while not stop.is_set():
			for name in names:
				os.environ[name] = '1'
			for name in names:
				os.environ.pop(name, None)
	task = multiCMD.Task(['true'])
	task._extra_env = multiCMD._check_env({'A': '1'}, 'extra_env', allow_none=True)
	thread = threading.Thread(target=churn)
	thread.start()
	try:
		for _ in range(20000):
			env = multiCMD._child_env(task)
			assert env['A'] == '1'
	finally:
		stop.set()
		thread.join()
		for name in names:
			os.environ.pop(name, None)


def test_empty_extra_env_is_no_extra_env():
	assert multiCMD._check_env({}, 'extra_env', allow_none=True) is None
	assert multiCMD._check_env({}, 'env') == {}
	tasks, _ = multiCMD._build_tasks([['true']], extra_env={})
	assert tasks[0]._extra_env is None
	assert multiCMD._child_env(tasks[0]) is None


def test_the_inherited_environment_is_os_environ_at_spawn_time(monkeypatch):
	monkeypatch.setenv('MCMD_T', 'first')
	assert run_command(ECHO, quiet=True, extra_env={'A': '1'}) == ['[first]']
	monkeypatch.setenv('MCMD_T', 'second')
	assert run_command(ECHO, quiet=True, extra_env={'A': '1'}) == ['[second]']


def test_names_and_values_are_what_subprocess_accepts(tmp_path):
	env = multiCMD._check_env({b'A': b'1', 'B': pathlib.Path('/tmp'), os.fsencode('C'): 'x'}, 'env')
	assert env == {'A': '1', 'B': '/tmp', 'C': 'x'}
	assert multiCMD._check_env(os.environb, 'env') == dict(os.environ)
	assert run_command(['sh', '-c', 'echo "$H"'], quiet=True, extra_env={'H': pathlib.Path('/tmp')}) == ['/tmp']
	assert run_command(['sh', '-c', 'echo "$H"'], quiet=True, env={b'H': b'bytes', b'PATH': os.fsencode(os.environ['PATH'])}) == ['bytes']
	for bad in (['AB'], [], set(), [('A', 'B')], 'AB', 5):
		with pytest.raises(TypeError):
			multiCMD._check_env(bad, 'extra_env', allow_none=True)
		with pytest.raises(TypeError):
			multiCMD._check_env(bad, 'env')
	with pytest.raises(TypeError):
		multiCMD._check_env({1: 'x'}, 'env')
	with pytest.raises(TypeError):
		multiCMD._check_env({'A': 1.5}, 'env')
	with pytest.raises(ValueError):
		multiCMD._check_env({'': 'x'}, 'env')
	with pytest.raises(ValueError):
		multiCMD._check_env({'A': 'x\0y'}, 'env')
	with pytest.raises(ValueError):
		multiCMD._check_env({b'A=B': 'x'}, 'env')


def test_windows_names_are_case_insensitive_and_may_start_with_equals():
	with mock.patch.object(os, 'name', 'nt'):
		task = multiCMD.Task(['x'])
		task._env = multiCMD._check_env({'PATH': 'x', 'HOME': 'h', '=C:': 'C:\\dir'}, 'env')
		task._extra_env = multiCMD._check_env({'Path': 'y'}, 'extra_env', allow_none=True)
		replaced = multiCMD._child_env(task)
		task._extra_env = multiCMD._check_env({'path': None}, 'extra_env', allow_none=True)
		removed = multiCMD._child_env(task)
		with pytest.raises(ValueError):
			multiCMD._check_env({'A=B': 'x'}, 'env')
		with pytest.raises(TypeError):
			multiCMD._check_env({b'A': 'x'}, 'env')
	assert replaced == {'Path': 'y', 'HOME': 'h', '=C:': 'C:\\dir'}
	assert removed == {'HOME': 'h', '=C:': 'C:\\dir'}
	# POSIX names may not start with '='
	with pytest.raises(ValueError):
		multiCMD._check_env({'=C:': 'x'}, 'env')


def test_an_environment_is_checked_and_copied_once():
	ex = AsyncExecutor(quiet=True, env={'PATH': os.environ['PATH']}, extra_env={'MCMD_T': 'd'})
	assert multiCMD._check_env(ex.env, 'env') is ex.env
	tasks, _ = multiCMD._build_tasks([['true'], ['true']], env=ex.env, extra_env=ex.extra_env)
	assert tasks[0]._env is ex.env and tasks[1]._env is ex.env
	assert tasks[0]._extra_env is ex.extra_env
	# nothing to add: the command gets the validated snapshot itself
	tasks[0]._extra_env = None
	assert multiCMD._child_env(tasks[0]) is ex.env


def test_tasks_do_not_keep_their_environment_once_started():
	ex = AsyncExecutor(quiet=True)
	task = ex.run_command(ECHO, env={'PATH': os.environ['PATH']}, extra_env={'MCMD_T': 'k'})
	ex.wait(timeout=5)
	assert task.stdout == ['[k]']
	assert task._env is None and task._extra_env is None
