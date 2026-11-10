'''pass_fds ( 1.59 ): a command inherits the descriptors it is given, at the same numbers, and no others.
Each test runs with Popen(process_group=) ( 3.11+ ) and with the exec wrapper ( the launcher fixture ).'''
import os
import sys

import pytest

import multiCMD
from multiCMD import AsyncExecutor, run_command, run_commands

pytestmark = pytest.mark.skipif(os.name != 'posix', reason='descriptors are inherited on POSIX only')


@pytest.fixture(autouse=True)
def _both_launchers(launcher):
	return launcher


def _pipe(text):
	'''The read end of a pipe holding text, its write end closed: one reader gets it whole, then EOF'''
	r, w = os.pipe()
	os.write(w, text.encode())
	os.close(w)
	return r


@pytest.fixture
def pipes():
	opened = []
	def make(text):
		r = _pipe(text)
		opened.append(r)
		return r
	yield make
	for fd in opened:
		try:
			os.close(fd)
		except OSError:
			pass


def _cat(fd):
	return ['cat', '/dev/fd/%d' % fd]


def _executor_run(command, **kwargs):
	ex = AsyncExecutor(quiet=True, **kwargs)
	ex.run_command(command)
	ex.join(print_error=False)
	return ex.get_results()[0]


@pytest.mark.parametrize('route', ['run_command', 'run_commands', 'AsyncExecutor', 'AsyncExecutor-call', 'pty', 'env', 'extra_env', 'shell'])
def test_a_command_reads_a_passed_descriptor(pipes, route):
	if route == 'run_commands':
		fds = [pipes('passed %d' % i) for i in range(4)]
		out = run_commands([_cat(fd) for fd in fds], max_threads=4, quiet=True, pass_fds=fds)
		assert out == [['passed %d' % i] for i in range(4)]
		return
	fd = pipes('passed')
	if route == 'run_command':
		out = run_command(_cat(fd), quiet=True, pass_fds=(fd,))
	elif route == 'AsyncExecutor':
		out = _executor_run(_cat(fd), pass_fds=fd)
	elif route == 'AsyncExecutor-call':
		ex = AsyncExecutor(quiet=True)
		ex.run_command(_cat(fd), pass_fds=[fd])
		ex.join(print_error=False)
		out = ex.get_results()[0]
	elif route == 'pty':
		out = run_command(_cat(fd), quiet=True, pty=True, pass_fds=(fd,))
	elif route == 'env':
		# before 3.11 the exec wrapper's branch for a command with an environment of its own
		out = run_command(_cat(fd), quiet=True, env={'PATH': os.environ.get('PATH', os.defpath)}, pass_fds=(fd,))
	elif route == 'extra_env':
		out = run_command(_cat(fd), quiet=True, extra_env={'MCMD_PASS_FDS': '1'}, pass_fds=(fd,))
	else:
		out = run_command('cat /dev/fd/%d | cat' % fd, quiet=True, shell=True, pass_fds=(fd,))
	assert out == ['passed']


def test_without_pass_fds_nothing_is_inherited(pipes):
	# the default closes every descriptor past 2 in the command, an inheritable one too
	fd = pipes('kept')
	os.set_inheritable(fd, True)
	task = run_command(_cat(fd), quiet=True, return_object=True)
	assert task.returncode != 0 and task.stdout == []


def test_only_the_named_descriptors_are_passed(pipes):
	passed, other = pipes('passed'), pipes('other')
	os.set_inheritable(other, True)
	assert run_command(_cat(passed), quiet=True, pass_fds=passed) == ['passed']
	task = run_command(_cat(other), quiet=True, return_object=True, pass_fds=passed)
	assert task.returncode != 0 and task.stdout == []


def test_file_objects_and_iterables_are_accepted(pipes, tmp_path):
	path = tmp_path / 'held'
	path.write_text('from a file object')
	with open(str(path), 'rb') as held:
		fd = pipes('from an int')
		assert multiCMD._check_pass_fds(fd) == (fd,)
		assert multiCMD._check_pass_fds(held) == (held.fileno(),)
		assert multiCMD._check_pass_fds([held, fd, fd]) == tuple(sorted({held.fileno(), fd}))
		assert multiCMD._check_pass_fds(None) == multiCMD._check_pass_fds(()) == ()
		out = run_commands([_cat(held.fileno()), _cat(fd)], quiet=True, max_threads=2, pass_fds=[held, fd])
	assert out == [['from a file object'], ['from an int']]


def test_a_closed_or_bad_descriptor_is_refused_before_anything_runs(tmp_path):
	marker = tmp_path / 'ran'
	r, w = os.pipe()
	os.close(r)
	os.close(w)
	closed_file = open(str(tmp_path / 'f'), 'wb')
	closed_file.close()
	for bad in (r, -1, True, [w], closed_file):
		with pytest.raises(ValueError):
			run_commands([['touch', str(marker)]], quiet=True, pass_fds=bad)
		with pytest.raises(ValueError):
			run_command(['touch', str(marker)], quiet=True, wait_for_return=False, pass_fds=bad)
		with pytest.raises(ValueError):
			AsyncExecutor(pass_fds=bad)
		ex = AsyncExecutor(quiet=True)
		with pytest.raises(ValueError):
			ex.run_command(['touch', str(marker)], pass_fds=bad)
		assert ex.tasks == []
	for wrong in ('3', b'\x03', 1.5, [None]):
		with pytest.raises(TypeError):
			run_command(['touch', str(marker)], quiet=True, pass_fds=wrong)
	multiCMD.join_threads(timeout=5)
	assert not marker.exists()


def test_pass_fds_off_posix_is_refused(monkeypatch, pipes):
	fd = pipes('x')
	monkeypatch.setattr(multiCMD, '_IS_POSIX', False)
	with pytest.raises(ValueError, match='pass_fds needs a POSIX system'):
		multiCMD._check_pass_fds(fd)
	with pytest.raises(ValueError, match='pass_fds needs a POSIX system'):
		AsyncExecutor(pass_fds=[fd])
	# nothing to pass is fine everywhere
	assert multiCMD._check_pass_fds(()) == ()
	assert AsyncExecutor().pass_fds == ()
