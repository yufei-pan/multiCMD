import os
import subprocess
import sys

import pytest

import multiCMD
from multiCMD import AsyncExecutor, run_command, run_commands

ECHO = ['sh', '-c', 'echo "[$MCMD_T]"']


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
	monkeypatch.delenv('MCMD_T', raising=False)


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
	tool = tmp_path / 'mcmd-env-tool'
	tool.write_text('#!/bin/sh\necho found\n')
	tool.chmod(0o755)
	assert run_command(['mcmd-env-tool'], quiet=True, return_code_only=True) == 127
	assert run_command(['mcmd-env-tool'], quiet=True, extra_env={'PATH': str(tmp_path) + os.pathsep + os.environ['PATH']}) == ['found']


def test_error_still_names_the_command(capsys):
	task = run_command(['mcmd-no-such-command', 'arg'], quiet=True, extra_env={'MCMD_T': 'x'}, return_object=True)
	assert task.returncode == 127
	assert 'mcmd-no-such-command' in capsys.readouterr().err
	assert 'mcmd-no-such-command' in ' '.join(task.stderr)


def test_return_codes_and_timeout_with_env():
	assert run_command(['sh', '-c', 'exit "${MCMD_T}"'], quiet=True, return_code_only=True, extra_env={'MCMD_T': '7'}) == 7
	task = run_command(['sh', '-c', 'sleep 5'], quiet=True, timeout=0.3, extra_env={'MCMD_T': 'x'}, return_object=True)
	assert task.returncode == 124


def test_dry_run_shows_the_command_only(capsys):
	assert run_command(ECHO, dry_run=True, extra_env={'MCMD_T': 'secret'}) == []
	assert 'secret' not in capsys.readouterr().out


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
	proc = subprocess.run([sys.executable, 'multiCMD.py', '-q', '--no-pty', '-e', 'MCMD_T=cli', '-e', 'MCMD_GONE', 'echo hi'],
		stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, env=dict(os.environ, MCMD_GONE='1'))
	assert proc.returncode == 0
	proc = subprocess.run([sys.executable, 'multiCMD.py', '--no-pty', '--shell', '-e', 'MCMD_T=cli', '-e', 'MCMD_GONE', 'echo "[$MCMD_T][$MCMD_GONE]"'],
		stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, env=dict(os.environ, MCMD_GONE='1'))
	assert '[cli][]' in proc.stdout
