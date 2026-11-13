'''
A program that imports multiCMD and shows its commands ( not quiet ), when the reader of its stdout quits ( `prog | head` ):
the calls that show commands stop them, skip the queued ones and raise BrokenPipeError, as mcmd stops its run. A call made
after that runs its commands without showing them; a quiet call never looks. 1.63's first builds lost the output and ran
on: a command that never ends ( `tail -f` ) kept the program running for ever.
'''
import errno
import os
import re
import subprocess
import sys
import textwrap
import threading
import time

import pytest

from conftest import PACKAGE_ROOT

posix_only = pytest.mark.skipif(os.name != 'posix', reason='POSIX only')

# a command that prints for ever, as `tail -f` on a log that keeps growing does; its pid appended to a file, one line per command
FOLLOW = "['sh', '-c', 'echo $$ >> %s/pids; while :; do echo tick; sleep 0.05; done']"

# what every program starts with: the followers, and alive(), which says how many processes of them are still running
PRELUDE = """
import os, subprocess, sys, threading, time
import multiCMD
FOLLOW = %s
PIDS = %r
MARK = %r

def alive(grace=1.0):
	'''
	How many processes of the followers still run: every pid they listed and everything in their process groups ( a zombie
	has exited ). Asked right after a call returns or raises, before the program ends, so what multiCMD's own atexit hook
	would kill is counted. A killed process is a zombie within milliseconds: grace gives it a second to be one, and what is
	still running after that was not stopped
	'''
	with open(PIDS) as f:
		pids = {int(word) for word in f.read().split()}
	deadline = time.monotonic() + grace
	while True:
		if os.path.isdir('/proc/self'):
			table = []
			for name in os.listdir('/proc'):
				if name.isdigit():
					try:
						with open('/proc/%%s/stat' %% name) as f:
							fields = f.read().rsplit(')', 1)[1].split()
					except OSError:
						continue
					table.append((int(name), int(fields[2]), fields[0]))
		else:
			out = subprocess.run(['ps', '-A', '-o', 'pid=,pgid=,stat='], stdout=subprocess.PIPE, universal_newlines=True).stdout
			table = [(int(pid), int(pgid), stat) for pid, pgid, stat in (line.split(None, 2) for line in out.splitlines())]
		running = sum(1 for pid, pgid, state in table if (pid in pids or pgid in pids) and not state.startswith('Z'))
		if running == 0 or time.monotonic() > deadline:
			return running
		time.sleep(0.01)
"""


def run_program(tmp_path, body, lines=5, timeout=10):
	'''
	Run a program that imports multiCMD with its stdout on a pipe whose reader quits after reading lines lines, as
	`python prog.py | head -5` does, and then creates the file reader-closed in tmp_path ( PRELUDE's MARK )
	@returns:
		( exit status, stderr, seconds from the reader quitting to the program's end )
	'''
	program = tmp_path / 'prog.py'
	program.write_text(PRELUDE % (FOLLOW % tmp_path, str(tmp_path / 'pids'), str(tmp_path / 'reader-closed')) + textwrap.dedent(body))
	env = dict(os.environ, PYTHONPATH=str(PACKAGE_ROOT))
	proc = subprocess.Popen([sys.executable, str(program)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
	# a program that prints fewer than lines lines ends here, not at CI's hour limit: the 5 lines come within a second, and
	# 20 s is slow enough for the oldest Python on a loaded runner
	deadline = threading.Timer(20, proc.kill)
	deadline.start()
	try:
		for _ in range(lines):
			assert proc.stdout.readline()
		proc.stdout.close()
		quit_at = time.monotonic()
		(tmp_path / 'reader-closed').touch()
		# ( its stderr is a few lines: it fits the pipe, and is read once the program has ended )
		status = proc.wait(timeout)
		took = time.monotonic() - quit_at
		return status, proc.stderr.read().decode(), took
	finally:
		if proc.poll() is None:
			proc.kill()
			proc.wait()
		deadline.cancel()
		# cleanup only: the tests ask the program, which counted what still ran before it ended
		pidfile = tmp_path / 'pids'
		for word in pidfile.read_text().split() if pidfile.exists() else []:
			try:
				os.killpg(int(word), 9)
			except OSError:
				pass


def assert_followers_stopped(tmp_path, err):
	# the program counted the processes of its followers, and their process groups, right after its call returned or raised
	assert 'ALIVE 0\n' in err
	assert (tmp_path / 'pids').read_text().split()


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
		sys.stderr.write('ALIVE %d\\n' % alive())
		''')
	assert 'RAISED %d' % errno.EPIPE in err
	assert 'cannot write to stdout' not in err
	assert took < 5
	assert_followers_stopped(tmp_path, err)
	assert not (tmp_path / 'queued-ran').exists()


@posix_only
def test_a_call_made_after_the_reader_quit_runs_its_commands_without_showing_them(tmp_path):
	# a cleanup in a finally ( an unmount ): 1.62 did not run it, and raised BrokenPipeError again
	status, err, took = run_program(tmp_path, f'''
		try:
			multiCMD.run_commands([FOLLOW])
		except BrokenPipeError:
			sys.stderr.write('RAISED\\n')
			raise
		finally:
			codes = multiCMD.run_commands([['touch', {str(tmp_path / 'cleaned')!r}], ['false']], return_code_only=True)
			sys.stderr.write('CLEANUP %s\\n' % codes)
		''')
	assert 'CLEANUP [0, 1]' in err
	assert (tmp_path / 'cleaned').exists()
	# ( Python's own "Exception ignored ... BrokenPipeError" at exit says nothing: the marker comes from the except clause )
	assert 'RAISED\n' in err


@posix_only
def test_a_quiet_call_never_looks_at_stdout(tmp_path):
	# on another thread while the shown call loses its reader, and after it. The quiet command is running when the reader
	# quits ( the program starts the shown call only once the quiet command has said it started ) and while the shown call is
	# stopped: it ends only once the program has caught BrokenPipeError
	go = tmp_path / 'go'
	started = tmp_path / 'started'
	# the quiet command lists its pid with the followers' ( run_program's cleanup kills it after a failed run ) and says it started
	quiet_command = f'echo $$ >> {tmp_path}/pids; touch {started}; i=0; while [ ! -e {go} ] && [ $i -lt 400 ]; do sleep 0.05; i=$((i+1)); done; echo done'
	status, err, took = run_program(tmp_path, f'''
		got = []
		def quiet():
			# ( at most 20 s: a program that never gets there ends at run_program's deadline )
			got.append(multiCMD.run_command(['sh', '-c', {quiet_command!r}], quiet=True))
		thread = threading.Thread(target=quiet)
		thread.start()
		# ( 5 s: the command starts within milliseconds, in 10 ms through the exec wrapper of Python 3.6 - 3.10; a loaded
		# runner is slower, but not 500 times )
		deadline = time.monotonic() + 5
		while not os.path.exists({str(started)!r}) and time.monotonic() < deadline:
			time.sleep(0.01)
		sys.stderr.write('STARTED %s\\n' % os.path.exists({str(started)!r}))
		try:
			multiCMD.run_commands([FOLLOW])
		except BrokenPipeError:
			sys.stderr.write('RAISED\\n')
		open({str(go)!r}, 'w').close()
		thread.join()
		got.append(multiCMD.run_command(['echo', 'later'], quiet=True))
		sys.stderr.write('QUIET %s\\n' % got)
		''')
	# the quiet command was running before the shown call began, so before its reader quit
	assert 'STARTED True\n' in err
	assert 'RAISED\n' in err
	assert "QUIET [['done'], ['later']]" in err


@posix_only
def test_a_call_on_a_thread_of_the_program_stops_and_raises_there(tmp_path):
	status, err, took = run_program(tmp_path, '''
		def run():
			try:
				multiCMD.run_commands([FOLLOW])
			except BrokenPipeError:
				sys.stderr.write('RAISED on %s\\n' % (threading.current_thread() is not threading.main_thread()))
			sys.stderr.write('ALIVE %d\\n' % alive())
		thread = threading.Thread(target=run)
		thread.start()
		thread.join(10)
		sys.stderr.flush()  # ( Python 3.6 - 3.8 block-buffer a redirected stderr )
		os._exit(0)
		''')
	assert 'RAISED on True' in err
	assert took < 5
	assert_followers_stopped(tmp_path, err)


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
			sys.stderr.write('ALIVE %d\\n' % alive())
			sys.stderr.flush()  # ( Python 3.6 - 3.8 block-buffer a redirected stderr )
			os._exit(0)
			'''
	else:
		body = f'''
			tasks = multiCMD.run_commands([FOLLOW, ['touch', {str(tmp_path / 'queued-ran')!r}]], wait_for_return=False, return_object=True)
			multiCMD.join_threads(timeout=10)
			sys.stderr.write('LEFT %d CODES %s\\n' % (sum(task.is_alive() for task in tasks), [task.returncode for task in tasks]))
			sys.stderr.write('ALIVE %d\\n' % alive())
			sys.stderr.flush()  # ( Python 3.6 - 3.8 block-buffer a redirected stderr )
			os._exit(0)
			'''
	status, err, took = run_program(tmp_path, body)
	assert 'LEFT 0' in err
	assert took < 5
	assert_followers_stopped(tmp_path, err)
	assert not (tmp_path / 'queued-ran').exists()
	# the running command was stopped ( a negative return code: killed by a signal ), the queued one never started
	assert re.search(r'CODES \[-\d+, None\]', err)


@posix_only
def test_the_bar_raises_nothing_when_the_reader_quit(tmp_path):
	# it has no command to stop: its frames are dropped, and the program's own next write meets the closed pipe
	status, err, took = run_program(tmp_path, '''
		print('first', flush=True)
		# ( the frames are drawn once the parent has closed the reader )
		while not os.path.exists(MARK):
			time.sleep(0.01)
		for i in range(1, 11):
			multiCMD.print_progress_bar(i, 10)
		sys.stderr.write('CLOSED %s\\n' % multiCMD._display.closed)
		sys.stderr.write('BAR DONE\\n')
		''', lines=1)
	assert 'BAR DONE' in err
	# a frame did meet the closed pipe ( and nothing was raised )
	assert 'CLOSED True\n' in err


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
