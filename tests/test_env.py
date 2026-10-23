import copy
import os
import pathlib
import pickle
import subprocess
import sys
import threading
import time
import types
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
		assert 'mcmd-env-tool' in ' '.join(task.stderr)
	# and the default path finds the system tools
	assert run_command(['ls', '/'], quiet=True, return_code_only=True, env={'HOME': '/tmp'}) == 0
	monkeypatch.setenv('PATH', str(tmp_path))
	assert run_command(['ls', '/'], quiet=True, return_code_only=True, env={'HOME': '/tmp'}) == 0
	assert run_command(['ls', '/'], quiet=True, return_code_only=True, extra_env={'PATH': None}) == 0


def test_error_still_names_the_command(capfd):
	task = run_command(['mcmd-no-such-command', 'arg'], extra_env={'MCMD_T': 'x'}, return_object=True)
	assert task.returncode == 127
	assert 'Command path not found: mcmd-no-such-command' in capfd.readouterr().err
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
	# main() runs in this process: nothing past the argument check may happen ( no signal handlers, no command )
	def forbidden(*args, **kwargs):
		raise AssertionError('main() went past the argument check')
	monkeypatch.setattr(multiCMD, '_install_cli_signal_handlers', forbidden)
	monkeypatch.setattr(multiCMD, 'run_commands', forbidden)
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


def test_wrapper_starts_in_the_parents_environment_and_gets_the_target_one_in_reserved_variables(launcher, monkeypatch):
	if launcher == 'native':
		pytest.skip('only the exec wrapper starts a Python first')
	real_popen = subprocess.Popen
	seen = []
	def popen(argv, **kwargs):
		seen.append((argv, kwargs.get('env')))
		return real_popen(argv, **kwargs)
	monkeypatch.setattr(subprocess, 'Popen', popen)
	monkeypatch.setenv('LD_LIBRARY_PATH', '/mcmd/lib')
	# the interpreter running the wrapper must not be starved of what the parent's environment gives it ( LD_LIBRARY_PATH, ... )
	assert run_command(['sh', '-c', 'echo "[$A][$LD_LIBRARY_PATH]"'], quiet=True, env={'A': 'x', 'PATH': os.environ['PATH']}) == ['[x][]']
	assert run_command(ECHO, quiet=True, extra_env={'MCMD_T': 'y', 'LD_LIBRARY_PATH': None}) == ['[y]']
	for argv, env in seen:
		assert env[b'LD_LIBRARY_PATH'] == b'/mcmd/lib'
		assert argv[5] == str(len([k for k in env if k.startswith(b'_MULTICMD_ENV_')]))
	assert sorted(v for k, v in seen[0][1].items() if k.startswith(b'_MULTICMD_ENV_')) == sorted([b'A=x', b'PATH=' + os.fsencode(os.environ['PATH'])])
	# the command itself never sees the reserved names
	out = run_command(['env'], quiet=True, extra_env={'MCMD_T': 'y'})
	assert 'MCMD_T=y' in out and not [line for line in out if line.startswith('_MULTICMD_ENV_')]


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


def test_an_environment_is_checked_and_copied_once_per_call():
	env = multiCMD._check_env({'PATH': os.environ['PATH']}, 'env')
	tasks, _ = multiCMD._build_tasks([['true'], ['true']], env=env, extra_env={'A': None})
	assert tasks[0]._env is tasks[1]._env
	assert tasks[0]._extra_env is tasks[1]._extra_env
	assert tasks[0]._extra_env['A'] is None
	# nothing to add: the command gets a copy of the validated snapshot
	tasks[0]._extra_env = None
	child = multiCMD._child_env(tasks[0])
	assert child == dict(env) and type(child) is dict
	# a snapshot is read-only, and one with None values is not an env
	with pytest.raises(TypeError):
		env['X'] = 'y'
	with pytest.raises(TypeError):
		multiCMD._check_env(tasks[1]._extra_env, 'env')
	assert multiCMD._check_env(tasks[1]._extra_env, 'extra_env', allow_none=True) == {'A': None}
	# a read-only view of something the caller can still change is checked again, not trusted
	backing = {'A': '1'}
	view = types.MappingProxyType(backing)
	snap = multiCMD._check_env(view, 'env')
	backing['A'] = 2
	assert snap == {'A': '1'}
	with pytest.raises(TypeError):
		multiCMD._check_env(view, 'env')


def test_changing_the_executors_defaults_later_never_reaches_queued_commands():
	ex = AsyncExecutor(max_threads=1, quiet=True, env={'PATH': os.environ['PATH']}, extra_env={'MCMD_T': 'first'})
	blocker = ex.run_command(['sh', '-c', 'sleep 0.5'])
	queued = [ex.run_command(ECHO) for _ in range(3)]
	ex.env['BAD'] = 5
	ex.extra_env['MCMD_T'] = 'second'
	ex.extra_env['MORE'] = None
	ex.wait(timeout=10)
	assert [t.stdout for t in queued] == [['[first]']] * 3
	assert all(t.returncode == 0 for t in queued) and blocker.returncode == 0
	# the new value is checked and used by the next call
	with pytest.raises(TypeError):
		ex.run_command(ECHO)
	ex.env['BAD'] = '5'
	assert ex.run_command(ECHO, quiet=True) is not None
	ex.wait(timeout=10)
	assert ex.tasks[-1].stdout == ['[second]']
	# an extra_env snapshot is no env: None values only mean something to extra_env
	with pytest.raises(TypeError):
		AsyncExecutor(env=multiCMD._check_env({'A': None}, 'extra_env', allow_none=True))


def test_tasks_do_not_keep_their_environment_once_started():
	ex = AsyncExecutor(quiet=True)
	task = ex.run_command(ECHO, env={'PATH': os.environ['PATH']}, extra_env={'MCMD_T': 'k'})
	ex.wait(timeout=5)
	assert task.stdout == ['[k]']
	assert task._env is None and task._extra_env is None


# ---- the environment reaches the command whole, whatever the parent's descriptors, signals and locale are

def _in_fresh_python(code, launcher, env=None, closed=(), timeout=60, prefix=()):
	'''Run code in a new Python ( multiCMD imported, the launcher chosen ) whose descriptors in closed are shut first; returns the CompletedProcess'''
	script = ('import os, sys; sys.path.insert(0, %r); import multiCMD; multiCMD._HAS_PROCESS_GROUP = %r\n' % (str(PACKAGE_ROOT), launcher == 'native')
		+ ''.join('os.close(%d)\n' % fd for fd in closed) + code)
	return subprocess.run([*prefix, sys.executable, '-c', script], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
		env=env or {'PATH': os.environ['PATH']}, timeout=timeout)


def test_a_closed_standard_descriptor_in_the_parent_does_not_hurt(launcher, tmp_path):
	for fd in (0, 1, 2):
		out = tmp_path / ('out%d' % fd)
		proc = _in_fresh_python("""
task = multiCMD.run_command(['/usr/bin/env'], quiet=True, extra_env={'A': 'x'}, return_object=True)
open(%r, 'w').write(repr((task.returncode, sorted(task.stdout))))
""" % str(out), launcher, closed=(fd,))
		assert eval(out.read_text())[0] == 0, (fd, proc.stderr, out.read_text())
		assert 'A=x' in eval(out.read_text())[1] and 'PATH=' + os.environ['PATH'] in eval(out.read_text())[1], fd


def test_a_missing_part_of_the_environment_stops_the_wrapper_before_it_runs_anything(tmp_path):
	marker = tmp_path / 'ran'
	for count, entries in (('3', {b'_MULTICMD_ENV_0': b'A=1', b'_MULTICMD_ENV_1': b'B=2'}), ('1', {b'_MULTICMD_ENV_0': b'NOEQUALS'}), ('x', {})):
		proc = subprocess.run([sys.executable, '-I', '-S', '-c', multiCMD._PGROUP_EXEC, count, 'touch', str(marker)],
			env=dict({k.decode(): v.decode() for k, v in entries.items()}, PATH=os.environ['PATH']),
			stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=30)
		assert proc.returncode == 126, (count, proc.stderr)
		assert 'incomplete environment' in proc.stderr
		assert not marker.exists()


def test_an_interrupt_between_the_spawn_and_the_registration_leaves_no_half_started_command(launcher, monkeypatch, tmp_path):
	# the environment travels with the exec itself: whenever the spawn is cut short, the command either never ran or has all of it
	marker = tmp_path / 'env'
	real_register = multiCMD._register
	procs = []
	def interrupt(proc, pgid):
		real_register(proc, pgid)
		procs.append(proc)
		raise KeyboardInterrupt
	monkeypatch.setattr(multiCMD, '_register', interrupt)
	try:
		with pytest.raises(KeyboardInterrupt):
			run_command(['sh', '-c', 'sleep 0.3; echo "[$A][$B]" > ' + str(marker)], quiet=True, extra_env={'A': 'a', 'B': 'b'}, timeout=5)
		multiCMD.kill_all()
		multiCMD.join_threads(timeout=5)
	finally:
		# the interrupt came before the command was reaped: reap this test's own and unregister it, nothing else
		for proc in procs:
			multiCMD._live_procs.pop(proc, None)
			proc.kill()
			proc.wait(timeout=10)
			proc.stdout.close()
			proc.stderr.close()
	assert not marker.exists() or marker.read_text() == '[a][b]\n'


def test_a_parent_that_restored_the_default_sigpipe_is_not_killed(launcher):
	proc = _in_fresh_python("""
import signal
signal.signal(signal.SIGPIPE, signal.SIG_DFL)
# the wrapper dies without reading anything: its command cannot be started
task = multiCMD.run_command(['/no/such/dir/cmd'], quiet=True, extra_env={'A': 'x'}, return_object=True)
print(task.returncode)
""", launcher)
	assert proc.returncode == 0, (proc.returncode, proc.stderr)
	assert proc.stdout.strip() == '127'


def test_an_inherited_variable_with_an_empty_name_is_left_out_of_an_explicit_environment(launcher):
	proc = _in_fresh_python("""
task = multiCMD.run_command(['/usr/bin/env'], quiet=True, extra_env={'A': 'x'}, return_object=True)
print(task.returncode, sorted(task.stdout))
task = multiCMD.run_command(['env'], quiet=True, extra_env={'A': 'x'}, return_object=True)
print(task.returncode, '=weird' in task.stdout)
task = multiCMD.run_command(['/no/such/cmd'], quiet=True, extra_env={'A': 'x'}, return_object=True)
print(task.returncode)
task = multiCMD.run_command(['/usr/bin/env'], quiet=True, extra_env={'A': None}, return_object=True)
print(task.returncode, '=weird' in task.stdout)
# the common idiom copies the inherited variable with the empty name into env
copied = dict(os.environ, A='x')
assert '' in copied
task = multiCMD.run_command(['/usr/bin/env'], quiet=True, env=copied, return_object=True)
print(task.returncode, '=weird' in task.stdout, 'A=x' in task.stdout)
ex = multiCMD.AsyncExecutor(quiet=True, env=copied)
ex.run_command(['/usr/bin/env'])
ex.wait(timeout=30)
print(ex.tasks[0].returncode, '=weird' in ex.tasks[0].stdout, 'A=x' in ex.tasks[0].stdout)
""", launcher, prefix=('env', '=weird'))
	assert proc.returncode == 0, proc.stderr
	first, second, third, fourth, fifth, sixth = proc.stdout.splitlines()
	assert fifth == '0 False True' and sixth == '0 False True'
	assert first.startswith('0 ') and "'=weird'" not in first and "'A=x'" in first
	assert second == '0 False'
	assert third == '127'
	assert fourth == '0 False'
	# an empty name in extra_env is an error, as are names holding '=' or NUL in both
	for kwargs in ({'extra_env': {'': 'x'}}, {'env': {'a=b': 'x'}}, {'env': {'a\0b': 'x'}}, {'extra_env': {'a=b': 'x'}}):
		with pytest.raises(ValueError):
			run_command(['true'], quiet=True, **kwargs)
	# env drops the empty name ( only an inherited variable can have one )
	assert multiCMD._check_env({'': 'x', 'A': '1'}, 'env', drop_empty=True) == {'A': '1'}
	assert run_command(['true'], quiet=True, env={'': 'x', 'PATH': os.environ['PATH']}, return_object=True).returncode == 0


def test_bytes_that_are_not_text_reach_the_command_unchanged(launcher):
	value = '\udcff\udc80ok'
	code = 'import os,sys; sys.stdout.write(repr(os.environb[b"A"]))'
	assert run_command([sys.executable, '-c', code], quiet=True, extra_env={'A': value}) == [repr(b'\xff\x80ok')]
	assert run_command([sys.executable, '-c', code], quiet=True, extra_env={'A': b'\xff\x80ok'}) == [repr(b'\xff\x80ok')]


def test_a_value_that_cannot_be_encoded_is_refused_before_anything_starts(launcher, monkeypatch, tmp_path):
	marker = tmp_path / 'ran'
	started = []
	real_popen = subprocess.Popen
	def popen(*args, **kwargs):
		started.append(args)
		return real_popen(*args, **kwargs)
	monkeypatch.setattr(subprocess, 'Popen', popen)
	for bad in ({'A': '\ud800'}, {'\ud800': 'x'}):
		with pytest.raises(ValueError):
			run_command(['touch', str(marker)], quiet=True, extra_env=bad)
		with pytest.raises(ValueError):
			run_command(['touch', str(marker)], quiet=True, env=dict(bad, PATH=os.environ['PATH']))
	assert not started and not marker.exists()


def _arg_max():
	return os.sysconf('SC_ARG_MAX')


def test_an_environment_too_large_for_exec_fails_the_same_way_on_both_launchers(launcher, capsys):
	# ~1.5x the system limit in 64 KiB values: no launcher can pass that on
	extra = {'MCMD_BIG_%d' % i: 'x' * 65536 for i in range(_arg_max() * 3 // 2 // 65536 + 1)}
	task = run_command(['true'], quiet=True, extra_env=extra, return_object=True)
	assert task.returncode == 126
	assert 'Argument list too long' in ' '.join(task.stderr)
	assert not task._error
	assert task._env is None and task._extra_env is None


def test_a_large_environment_still_works(launcher, monkeypatch):
	# an environment taking a quarter of the limit ( the wrapper needs it twice ) in values below the per-string limit
	extra = {'MCMD_BIG_%d' % i: 'y' * 65536 for i in range(_arg_max() // 4 // 65536)}
	for name in list(os.environ):
		if len(name) + len(os.environ[name]) > 16384:
			monkeypatch.delenv(name)
	out = run_command(['sh', '-c', 'echo "$MCMD_BIG_0" | wc -c; echo "${MCMD_BIG_%d}" | wc -c' % (len(extra) - 1)], quiet=True, extra_env=extra)
	assert [int(x) for x in out] == [65537, 65537]


def test_the_pty_pager_defaults_alone_do_not_use_the_environment_hand_over(launcher, monkeypatch):
	if launcher == 'native':
		pytest.skip('only the exec wrapper has a hand-over')
	real_popen = subprocess.Popen
	seen = []
	def popen(argv, **kwargs):
		seen.append((argv, kwargs.get('env')))
		return real_popen(argv, **kwargs)
	monkeypatch.setattr(subprocess, 'Popen', popen)
	monkeypatch.setenv('MCMD_T', 'parent')
	assert run_command(ECHO, quiet=True, pty=True) == ['[parent]']
	argv, env = seen[0]
	# as before extra_env existed: the wrapper's own environment is the command's
	assert argv[5] == '-'
	assert env['PAGER'] == 'cat' and env['GIT_PAGER'] == 'cat' and env['MCMD_T'] == 'parent'
	assert not [k for k in env if k.startswith('_MULTICMD_ENV_')]


def test_an_empty_path_is_the_current_directory_for_every_call(launcher, monkeypatch, tmp_path):
	# as os.get_exec_path() gives it ( and os.execvp, 1.54's wrapper and Popen use it ): [''], the current directory
	_tool(tmp_path)
	monkeypatch.chdir(tmp_path)
	monkeypatch.setenv('PATH', '')
	assert os.get_exec_path() == ['']
	for pty in (False, True):
		for kwargs in ({}, {'extra_env': {'A': '1'}}, {'env': {'PATH': ''}}):
			task = run_command(['mcmd-env-tool'], quiet=True, pty=pty, return_object=True, **kwargs)
			assert (task.returncode, task.stdout) == (0, ['found']), (pty, kwargs, task.stderr)
	(tmp_path / 'mcmd-env-tool').unlink()
	assert run_command(['mcmd-env-tool'], quiet=True, return_code_only=True) == 127


def test_the_wrapper_pre_check_stops_at_the_first_file(launcher, monkeypatch, tmp_path):
	if launcher == 'native':
		pytest.skip("the pre-check is the exec wrapper's")
	dirs = [tmp_path / f'd{i}' for i in range(30)]
	for d in dirs:
		d.mkdir()
		_tool(d)
	monkeypatch.setenv('PATH', os.pathsep.join(map(str, dirs)))
	real_isfile = os.path.isfile
	looked = []
	def isfile(path):
		if os.path.basename(path) == 'mcmd-env-tool':
			looked.append(path)
		return real_isfile(path)
	monkeypatch.setattr(os.path, 'isfile', isfile)
	assert run_command(['mcmd-env-tool'], quiet=True) == ['found']
	assert looked == [str(dirs[0] / 'mcmd-env-tool')]


@pytest.mark.skipif(sys.platform != 'linux', reason='MAX_ARG_STRLEN is Linux')
def test_a_bare_name_too_big_to_exec_is_not_tried_again_per_path_entry(launcher, monkeypatch, tmp_path):
	# E2BIG does not depend on which file runs: every other one in PATH would fail the same way
	dirs = [tmp_path / f'd{i}' for i in range(5)]
	for d in dirs:
		d.mkdir()
		_tool(d)
	monkeypatch.setenv('PATH', os.pathsep.join(map(str, dirs)))
	real = multiCMD._spawn_process
	calls = []
	def spawn(*args, **kwargs):
		calls.append(kwargs.get('executable'))
		return real(*args, **kwargs)
	monkeypatch.setattr(multiCMD, '_spawn_process', spawn)
	# one argument longer than MAX_ARG_STRLEN ( 128 KiB )
	task = run_command(['mcmd-env-tool', 'x' * 200000], quiet=True, return_object=True)
	assert task.returncode == 126 and not task._error and 'Argument list too long' in task.stderr[0]
	# ( once: through the wrapper, or Popen given the first file's path, see _search_dirs )
	assert calls == ([None] if launcher == 'wrapper' else [str(dirs[0] / 'mcmd-env-tool')])


def test_snapshots_are_dropped_whatever_happens_to_the_task(launcher, tmp_path, monkeypatch):
	ex = AsyncExecutor(quiet=True, dry_run=True)
	dry = ex.run_command(ECHO, extra_env={'A': '1'})
	ex.wait(timeout=5)
	assert dry._env is None and dry._extra_env is None
	failing = run_command(['/no/such/cmd'], quiet=True, extra_env={'A': '1'}, return_object=True)
	assert failing._extra_env is None and failing._env is None
	tasks, _ = multiCMD._build_tasks([['true']], env={'PATH': '/bin'}, extra_env={'A': '1'})
	tasks[0].stop = True
	multiCMD.__dict__['__run_command'](tasks[0], threading.Semaphore(1), quiet=True)
	assert tasks[0]._env is None and tasks[0]._extra_env is None
	# an exec the system refuses ( a file without a shebang ) is 126 on both launchers
	script = tmp_path / 'noshebang'
	script.write_text('echo hi\n')
	script.chmod(0o755)
	task = run_command([str(script)], quiet=True, extra_env={'A': '1'}, return_object=True)
	assert task.returncode == 126 and not task._error


# ---- a command the system refuses to execute is 126 with the reason, whichever way it was started

def _noshebang(tmp_path):
	script = tmp_path / 'nsb'
	script.write_text('echo hi\n')
	script.chmod(0o755)
	try:
		subprocess.run([str(script)], stdout=subprocess.PIPE)
	except PermissionError:
		pytest.skip(f'{tmp_path} does not allow executing files')
	except OSError:
		pass
	return script


@pytest.mark.parametrize('kwargs', [{}, {'extra_env': {'A': '1'}}, {'env': {'PATH': '/bin'}}], ids=['plain', 'extra_env', 'env'])
def test_enoexec_is_126_with_the_reason(launcher, tmp_path, kwargs):
	script = _noshebang(tmp_path)
	task = run_command([str(script)], quiet=True, return_object=True, **kwargs)
	assert task.returncode == 126 and not task._error
	assert task.stderr and 'Exec format error' in task.stderr[0]
	if kwargs:
		# the same words whichever way an environment was handed over: Python 3.11+'s own, the error with the command as given
		assert task.stderr[0] == "[Errno 8] Exec format error: %r" % str(script)
	elif launcher == 'wrapper':
		# a command without an environment of its own keeps the wrapper's text from before
		assert task.stderr[0] == '[Errno 8] Exec format error'


@pytest.mark.parametrize('kwargs', [{}, {'extra_env': {'A': '1'}}], ids=['plain', 'extra_env'])
def test_e2big_is_126(launcher, kwargs):
	# one argument longer than the system takes in a string
	task = run_command(['true', 'x' * (256 * 1024)], quiet=True, return_object=True, **kwargs)
	assert task.returncode == 126 and not task._error
	assert 'Argument list too long' in ' '.join(task.stderr)


def test_an_internal_oserror_is_still_an_error_with_a_traceback(launcher, monkeypatch):
	started = []
	def broken(proc, pgid):
		started.append(proc)
		raise OSError(5, 'Input/output error', 'somefile')
	monkeypatch.setattr(multiCMD, '_register', broken)
	try:
		task = run_command(['true'], quiet=True, return_object=True)
		assert task.returncode == -1 and task._error
		assert any('Traceback' in line for line in task.stderr)
	finally:
		# the process was never registered: nothing else will reap it or close its pipes
		for proc in started:
			proc.kill()
			proc.wait()
			proc.stdout.close()
			proc.stderr.close()


def test_the_wrapper_does_not_unset_each_variable_it_reads():
	# unsetting one is a scan of the whole environment: N variables would cost N**2
	assert 'SIGXFZ' not in multiCMD._PGROUP_EXEC and '.pop(' not in multiCMD._PGROUP_EXEC
	# ( Python before 3.9 calls os.environb.unsetenv, 3.9+ the module's os.unsetenv )
	code = ("import os, sys\nos.environb.unsetenv = os.unsetenv = lambda *a: sys.exit(99)\n"
		"def execve(f, a, e):\n\tprint(sorted(e.items()))\n\tsys.exit(0)\n"
		"os.execve = execve\nexec(%r)" % multiCMD._PGROUP_EXEC)
	env = {'PATH': os.environ['PATH'], '_MULTICMD_ENV_0': 'A=1', '_MULTICMD_ENV_1': 'B=x=y'}
	proc = subprocess.run([sys.executable, '-I', '-S', '-c', code, '2', 'true'], env=env,
		stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=30)
	assert proc.returncode == 0, proc.stderr
	assert proc.stdout.strip() == "[(b'A', b'1'), (b'B', b'x=y')]"


def test_tasks_that_never_started_do_not_keep_their_environment():
	ex = AsyncExecutor(max_threads=1, quiet=True)
	blocker = ex.run_command(['sh', '-c', 'sleep 0.5'])
	skipped = ex.run_command(ECHO, env={'PATH': os.environ['PATH']}, extra_env={'MCMD_T': 'k'})
	skipped.stop = True
	ex.wait(timeout=10)
	assert skipped.returncode is None
	assert skipped._env is None and skipped._extra_env is None
	assert blocker.returncode == 0
	tasks = run_commands([ECHO], quiet=True, dry_run=True, env={'PATH': '/bin'}, extra_env={'MCMD_T': 'k'}, return_object=True)
	assert tasks[0]._env is None and tasks[0]._extra_env is None


def test_an_executors_empty_extra_env_can_be_added_to():
	ex = AsyncExecutor(quiet=True, extra_env={}, env={})
	ex.extra_env['MCMD_T'] = '1'
	ex.env['PATH'] = os.environ['PATH']
	assert ex.run_command(ECHO) is not None
	ex.wait(timeout=10)
	assert ex.tasks[-1].stdout == ['[1]']
	assert AsyncExecutor().extra_env is None


# ---- a bare command name is not found unless a PATH entry holds a file by that name that cannot be run, on both launchers

def _path_cases(tmp_path):
	'''PATH values with no command in them, by the errno a lookup in them ends with'''
	afile = tmp_path / 'afile'
	afile.write_text('x')
	loop = tmp_path / 'loop'
	loop.symlink_to('loop')
	return {
		'ENOTDIR': ('/usr/bin:' + str(afile), 'no_such_cmd'),
		'ELOOP': (str(loop), 'no_such_cmd'),
		'ENAMETOOLONG': ('/usr/bin', 'x' * 300),
	}


@pytest.mark.parametrize('case', ['ENOTDIR', 'ELOOP', 'ENAMETOOLONG'])
@pytest.mark.parametrize('how', ['plain', 'extra_env', 'env'])
def test_a_lookup_that_ends_in_an_errno_other_than_enoent_is_still_not_found(launcher, tmp_path, monkeypatch, case, how):
	path, command = _path_cases(tmp_path)[case]
	kwargs = {}
	if how == 'plain':
		monkeypatch.setenv('PATH', path)
	elif how == 'extra_env':
		kwargs['extra_env'] = {'PATH': path}
	else:
		kwargs['env'] = {'PATH': path}
	task = run_command([command], quiet=True, return_object=True, **kwargs)
	assert task.returncode == 127 and not task._error, task.stderr
	assert 'No such file or directory' in ' '.join(task.stderr)


@pytest.mark.parametrize('how', ['plain', 'extra_env', 'env'])
def test_a_file_in_path_that_cannot_be_run_is_126(launcher, tmp_path, monkeypatch, how):
	_noshebang(tmp_path)
	plain = tmp_path / 'plain'
	plain.write_text('#!/bin/sh\necho hi\n')
	plain.chmod(0o644)
	for name, reason in (('plain', 'Permission denied'), ('nsb', 'Exec format error')):
		path = '/usr/bin:' + str(tmp_path)
		kwargs = {}
		if how == 'plain':
			monkeypatch.setenv('PATH', path)
		elif how == 'extra_env':
			kwargs['extra_env'] = {'PATH': path}
		else:
			kwargs['env'] = {'PATH': path}
		task = run_command([name], quiet=True, return_object=True, **kwargs)
		assert task.returncode == 126 and not task._error, task.stderr
		assert reason in ' '.join(task.stderr)


# ---- a bare name is looked up as sh does: only regular files count, and the first real failure to run one decides

def _run_in_path(monkeypatch, how, path, command):
	kwargs = {}
	if how == 'plain':
		monkeypatch.setenv('PATH', path)
	elif how == 'extra_env':
		kwargs['extra_env'] = {'PATH': path}
	else:
		kwargs['env'] = {'PATH': path}
	return run_command([command], quiet=True, return_object=True, **kwargs)


def _sh_status(path, command):
	return subprocess.run(['/bin/sh', '-c', command], env={'PATH': path}, stdout=subprocess.PIPE, stderr=subprocess.PIPE).returncode


@pytest.mark.parametrize('kind', ['directory', 'fifo', 'unsearchable'])
@pytest.mark.parametrize('how', ['plain', 'extra_env', 'env'])
def test_a_path_entry_without_a_regular_file_by_that_name_is_not_found(launcher, tmp_path, monkeypatch, kind, how):
	command = 'mcmd_' + kind
	where = tmp_path
	if kind == 'directory':
		(tmp_path / command).mkdir()
	elif kind == 'fifo':
		os.mkfifo(str(tmp_path / command))
	else:
		if os.geteuid() == 0:
			pytest.skip('root may search any directory')
		where = tmp_path / 'noperm'
		where.mkdir()
		where.chmod(0)
	try:
		path = str(where) + ':/usr/bin:/bin'
		task = _run_in_path(monkeypatch, how, path, command)
		assert task.returncode == 127 and not task._error, task.stderr
		assert "No such file or directory: '%s'" % command in ' '.join(task.stderr)
		assert _sh_status(path, command) == 127
	finally:
		where.chmod(0o755)


@pytest.mark.parametrize('rest', ['enoent', 'enotdir'])
@pytest.mark.parametrize('how', ['plain', 'extra_env', 'env'])
def test_a_script_whose_interpreter_is_missing_is_not_found_whatever_path_follows(launcher, tmp_path, monkeypatch, rest, how):
	_noshebang(tmp_path)
	script = tmp_path / 'badinterp'
	script.write_text('#!/nonexistent/interp\n')
	script.chmod(0o755)
	afile = tmp_path / 'afile'
	afile.write_text('x')
	path = str(tmp_path) + ':' + (str(tmp_path / 'missing') if rest == 'enoent' else str(afile))
	task = _run_in_path(monkeypatch, how, path, 'badinterp')
	assert task.returncode == 127 and not task._error, task.stderr
	assert "No such file or directory: 'badinterp'" in ' '.join(task.stderr)
	assert _sh_status(path, 'badinterp') == 127


@pytest.mark.parametrize('how', ['plain', 'extra_env', 'env'])
def test_the_first_file_that_cannot_run_decides_and_one_that_can_runs(launcher, tmp_path, monkeypatch, how):
	_noshebang(tmp_path)
	dirs = {}
	for name, (mode, body) in {'denied': (0o644, '#!/bin/sh\necho denied\n'), 'enoexec': (0o755, 'echo enoexec\n'),
			'runs': (0o755, '#!/bin/sh\necho ran\n')}.items():
		dirs[name] = tmp_path / name
		dirs[name].mkdir()
		tool = dirs[name] / 'mcmd-tool'
		tool.write_text(body)
		tool.chmod(mode)
	for order, code, reason in ((('denied', 'enoexec'), 126, 'Permission denied'), (('enoexec', 'denied'), 126, 'Exec format error'),
			(('denied', 'enoexec', 'runs'), 0, None)):
		task = _run_in_path(monkeypatch, how, ':'.join(str(dirs[name]) for name in order), 'mcmd-tool')
		assert task.returncode == code and not task._error, (order, task.stderr)
		if reason:
			assert reason in ' '.join(task.stderr)
		else:
			assert task.stdout == ['ran']


# ---- a launcher that cannot start is an internal error, not the command's 126

@pytest.mark.parametrize('kind', ['ENOEXEC', 'EACCES', 'ENOENT', 'ELOOP'])
def test_an_interpreter_that_cannot_run_is_an_internal_error(launcher, tmp_path, monkeypatch, kind):
	if launcher == 'native':
		pytest.skip('only the exec wrapper starts the interpreter')
	if kind == 'ENOEXEC':
		fake = _noshebang(tmp_path)
	elif kind == 'EACCES':
		fake = tmp_path / 'noexec'
		fake.write_text('x')
		fake.chmod(0o644)
	elif kind == 'ENOENT':
		fake = tmp_path / 'missing'
	else:
		fake = tmp_path / 'loop'
		fake.symlink_to('loop')
	monkeypatch.setattr(sys, 'executable', str(fake))
	for kwargs in ({}, {'extra_env': {'A': '1'}}):
		task = run_command(['true'], quiet=True, return_object=True, **kwargs)
		assert task.returncode == -1 and task._error, (task.returncode, task.stderr)
		assert any('Traceback' in line for line in task.stderr)
		assert not any('Command cannot be executed' in line for line in task.stderr)


# ---- a snapshot can be copied and pickled

def test_a_task_still_holding_its_snapshot_can_be_copied_and_pickled():
	tasks, _ = multiCMD._build_tasks([['true']], env={'PATH': '/bin'}, extra_env={'A': None, 'B': '1'})
	for clone in (copy.deepcopy(tasks[0]), pickle.loads(pickle.dumps(tasks[0])), copy.copy(tasks[0])):
		assert clone._env == {'PATH': '/bin'} and clone._extra_env == {'A': None, 'B': '1'}
		with pytest.raises(TypeError):
			clone._env['X'] = 'y'
		assert type(multiCMD._child_env(clone)) is dict
	clone = pickle.loads(pickle.dumps(tasks[0]))
	with pytest.raises(TypeError):
		multiCMD._check_env(clone._extra_env, 'env')


# ---- an executor that is cleaned up keeps no environments of what never started

def test_cleanup_drops_the_snapshots_of_what_is_still_queued():
	ex = AsyncExecutor(max_threads=1, quiet=True)
	tasks = ex.run_commands([['sleep', '1']] * 4, extra_env={'MCMD_SECRET': 'x'}, env={'PATH': os.environ['PATH']})
	ex.cleanup(timeout=0)
	deadline = time.monotonic() + 10
	while time.monotonic() < deadline and any(t._extra_env is not None for t in tasks):
		time.sleep(0.02)
	assert all(t._env is None and t._extra_env is None for t in tasks)
	multiCMD.kill_all()


def test_a_task_whose_thread_cannot_start_drops_its_snapshot(monkeypatch):
	ex = AsyncExecutor(max_threads=1, quiet=True)
	real = ex._track
	def broken(thread):
		if thread.task is None:
			return real(thread)
		raise RuntimeError("can't start new thread")
	monkeypatch.setattr(ex, '_track', broken)
	task = ex.run_commands([['true']], extra_env={'MCMD_SECRET': 'x'})[0]
	deadline = time.monotonic() + 10
	while time.monotonic() < deadline and ex._dispatching:
		time.sleep(0.02)
	assert task._extra_env is None and task._env is None


def test_a_bare_name_is_looked_up_without_os_get_exec_path(launcher, monkeypatch):
	# os.get_exec_path() takes warnings.catch_warnings(), whose lock 3.14 takes with `with`: a Ctrl-C right after the
	# acquire, as Popen looked a serial run's command up on the main thread, left it held, and every later Popen of a
	# bare name and every warning hung ( seen with the start stress test on 3.14t )
	calls = []
	real = os.get_exec_path
	monkeypatch.setattr(os, 'get_exec_path', lambda *args: (calls.append(args), real(*args))[1])
	import warnings
	real_catch = warnings.catch_warnings
	class Catch(real_catch):
		def __enter__(self):
			calls.append('catch_warnings')
			return super().__enter__()
	monkeypatch.setattr(warnings, 'catch_warnings', Catch)
	assert run_command(['echo', 'a'], quiet=True) == ['a']
	assert run_command(['sh', '-c', 'echo $A'], quiet=True, extra_env={'A': 'b'}) == ['b']
	assert run_command(['sh', '-c', 'echo $A'], quiet=True, env={'A': 'c', 'PATH': os.environ['PATH']}) == ['c']
	assert run_command(['echo', 'd'], quiet=True, pty=True) == ['d']
	assert run_command(['mcmd-no-such-command-xyz'], quiet=True, return_code_only=True) == 127
	# before ( fdc45b9 ): Popen ( 3.11+ ) and an environment of the command's own looked the name up with it
	assert calls == []
