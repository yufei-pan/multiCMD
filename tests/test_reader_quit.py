'''
A program that imports multiCMD and shows its commands ( not quiet ), when the reader of its stdout quits ( `prog | head` ):
the calls that show commands stop them, skip the queued ones and raise BrokenPipeError, as mcmd stops its run. A call made
after that runs its commands without showing them; a quiet call never looks. 1.63's first builds lost the output and ran
on: a command that never ends ( `tail -f` ) kept the program running for ever.
'''
import errno
import os
import subprocess
import sys
import textwrap
import time

import pytest

from conftest import PACKAGE_ROOT, pid_gone

posix_only = pytest.mark.skipif(os.name != 'posix', reason='POSIX only')

# a command that prints for ever, as `tail -f` on a log that keeps growing does; its pid in a file
FOLLOW = "['sh', '-c', 'echo $$ > %s/pid; while :; do echo tick; sleep 0.05; done']"


def run_program(tmp_path, body, lines=5, timeout=10):
	'''
	Run a program that imports multiCMD with its stdout on a pipe whose reader quits after reading lines lines, as
	`python prog.py | head -5` does
	@returns:
		( exit status, stderr, seconds from the reader quitting to the program's end )
	'''
	program = tmp_path / 'prog.py'
	program.write_text('import os, sys, threading, time\nimport multiCMD\nFOLLOW = %s\n' % (FOLLOW % tmp_path) + textwrap.dedent(body))
	env = dict(os.environ, PYTHONPATH=str(PACKAGE_ROOT))
	proc = subprocess.Popen([sys.executable, str(program)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
	try:
		for _ in range(lines):
			assert proc.stdout.readline()
		proc.stdout.close()
		quit_at = time.monotonic()
		# ( its stderr is a few lines: it fits the pipe, and is read once the program has ended )
		status = proc.wait(timeout)
		took = time.monotonic() - quit_at
		return status, proc.stderr.read().decode(), took
	finally:
		if proc.poll() is None:
			proc.kill()
			proc.wait()
		pidfile = tmp_path / 'pid'
		if pidfile.exists() and pidfile.read_text().strip():
			try:
				os.killpg(int(pidfile.read_text()), 9)
			except OSError:
				pass


def follower_gone(tmp_path):
	return pid_gone(int((tmp_path / 'pid').read_text()))


@posix_only
@pytest.mark.parametrize('call', [
	'multiCMD.run_commands([FOLLOW])',
	'multiCMD.run_command(FOLLOW)',
	"multiCMD.run_commands([FOLLOW, ['touch', 'queued-ran']])",
	"multiCMD.run_commands([FOLLOW, FOLLOW, ['touch', 'queued-ran']], max_threads=2)",
], ids=['serial', 'run_command', 'queued', 'pool'])
def test_a_program_whose_reader_quit_stops_its_command_and_gets_broken_pipe_error(tmp_path, call):
	# `python prog.py | head -5` with a command that never ends: 1.63's first builds ran for ever ( one line on stderr )
	status, err, took = run_program(tmp_path, f'''
		os.chdir({str(tmp_path)!r})
		try:
			{call}
		except BrokenPipeError as e:
			sys.stderr.write('RAISED %d\\n' % e.errno)
		''')
	assert 'RAISED %d' % errno.EPIPE in err
	assert 'cannot write to stdout' not in err
	assert took < 5
	assert follower_gone(tmp_path)
	assert not (tmp_path / 'queued-ran').exists()


@posix_only
def test_a_call_made_after_the_reader_quit_runs_its_commands_without_showing_them(tmp_path):
	# a cleanup in a finally ( an unmount ): 1.62 did not run it, and raised BrokenPipeError again
	status, err, took = run_program(tmp_path, f'''
		try:
			multiCMD.run_commands([FOLLOW])
		finally:
			codes = multiCMD.run_commands([['touch', {str(tmp_path / 'cleaned')!r}], ['false']], return_code_only=True)
			sys.stderr.write('CLEANUP %s\\n' % codes)
		''')
	assert 'CLEANUP [0, 1]' in err
	assert (tmp_path / 'cleaned').exists()
	assert 'BrokenPipeError' in err


@posix_only
def test_a_quiet_call_never_looks_at_stdout(tmp_path):
	# on another thread while the shown call loses its reader, and after it
	status, err, took = run_program(tmp_path, '''
		got = []
		def quiet():
			got.append(multiCMD.run_command(['sh', '-c', 'sleep 1; echo done'], quiet=True))
		thread = threading.Thread(target=quiet)
		thread.start()
		try:
			multiCMD.run_commands([FOLLOW])
		except BrokenPipeError:
			pass
		thread.join()
		got.append(multiCMD.run_command(['echo', 'later'], quiet=True))
		sys.stderr.write('QUIET %s\\n' % got)
		''')
	assert "QUIET [['done'], ['later']]" in err


@posix_only
def test_a_call_on_a_thread_of_the_program_stops_and_raises_there(tmp_path):
	status, err, took = run_program(tmp_path, '''
		def run():
			try:
				multiCMD.run_commands([FOLLOW])
			except BrokenPipeError:
				sys.stderr.write('RAISED on %s\\n' % (threading.current_thread() is not threading.main_thread()))
		thread = threading.Thread(target=run)
		thread.start()
		thread.join(10)
		sys.stderr.flush()  # ( Python 3.6 - 3.8 block-buffer a redirected stderr )
		os._exit(0)
		''')
	assert 'RAISED on True' in err
	assert took < 5 and follower_gone(tmp_path)


@posix_only
@pytest.mark.parametrize('how', ['executor', 'fire-and-forget'])
def test_commands_nobody_waits_for_are_stopped_and_their_tasks_say_so(tmp_path, how):
	# no call is waiting that could raise: the running command is stopped, the queued one never starts, wait() returns
	if how == 'executor':
		body = f'''
			executor = multiCMD.AsyncExecutor(max_threads=1, quiet=False)
			tasks = executor.run_commands([FOLLOW, ['touch', {str(tmp_path / 'queued-ran')!r}]])
			left = executor.wait(10)
			sys.stderr.write('LEFT %d CODES %s\\n' % (len(left), [task.returncode for task in tasks]))
			sys.stderr.flush()  # ( Python 3.6 - 3.8 block-buffer a redirected stderr )
			os._exit(0)
			'''
	else:
		body = '''
			tasks = multiCMD.run_commands([FOLLOW], wait_for_return=False, return_object=True)
			multiCMD.join_threads(timeout=10)
			sys.stderr.write('LEFT %d CODES %s\\n' % (sum(task.is_alive() for task in tasks), [task.returncode for task in tasks]))
			sys.stderr.flush()  # ( Python 3.6 - 3.8 block-buffer a redirected stderr )
			os._exit(0)
			'''
	status, err, took = run_program(tmp_path, body)
	assert 'LEFT 0' in err
	assert took < 5 and follower_gone(tmp_path)
	assert not (tmp_path / 'queued-ran').exists()
	if how == 'executor':
		assert ', None]' in err


@posix_only
def test_the_bar_raises_nothing_when_the_reader_quit(tmp_path):
	# it has no command to stop: its frames are dropped, and the program's own next write meets the closed pipe
	status, err, took = run_program(tmp_path, '''
		print('first', flush=True)
		time.sleep(0.5)
		for i in range(1, 11):
			multiCMD.print_progress_bar(i, 10)
		sys.stderr.write('BAR DONE\\n')
		''', lines=1)
	assert 'BAR DONE' in err


@posix_only
def test_any_other_failed_write_still_loses_only_the_output(tmp_path):
	# the other half of the rule, for a program that imports multiCMD: every command runs, one line on stderr
	if not os.path.exists('/dev/full'):
		pytest.skip('needs /dev/full')
	program = tmp_path / 'prog.py'
	program.write_text(textwrap.dedent(f'''
		import sys, multiCMD
		codes = multiCMD.run_commands([['touch', {str(tmp_path / 'a')!r}], ['touch', {str(tmp_path / 'b')!r}]], return_code_only=True)
		sys.stderr.write('CODES %s\\n' % codes)
		'''))
	with open('/dev/full', 'w') as full:
		done = subprocess.run([sys.executable, str(program)], stdout=full, stderr=subprocess.PIPE, env=dict(os.environ, PYTHONPATH=str(PACKAGE_ROOT)), timeout=20)
	err = done.stderr.decode()
	assert 'CODES [0, 0]' in err and err.count('multiCMD: cannot write to stdout') == 1
	assert (tmp_path / 'a').exists() and (tmp_path / 'b').exists()
