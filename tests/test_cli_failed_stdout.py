'''
A write to stdout that fails ( 1.63's rule ). A reader that quit ( a pipe's poll answer, EPIPE, ECONNRESET; Windows' EINVAL on
a pipe ) stops the run, as test_cli_closed_stdout.py checks. Any other failure ( EIO from a terminal that hung up, ENOSPC
from a full disk ) loses the output and nothing else: every command runs, running and queued, serial and pooled; one line
on stderr says so; the exit status is the commands' own. 1.62 raised it where it happened: tracebacks, queued commands not
started, exit status 120.
'''
import errno
import os
import select
import shlex
import signal
import socket
import subprocess
import sys
import time

import pytest

from conftest import MULTICMD_PY, pid_gone
from test_cli_closed_stdout import finish, kill_leftovers, pids_in, streaming_command, wait_for_pidfiles
import multiCMD

posix_only = pytest.mark.skipif(os.name != 'posix', reason='POSIX only')
has_dev_full = pytest.mark.skipif(not os.path.exists('/dev/full'), reason='needs /dev/full')

LOST = 'mcmd: cannot write to stdout'


def job(tmp_path, index, lines=10, status=0, wait=False):
	# writes its pid, with wait waits for the test's go file, prints for about lines * 50 ms, leaves done<index> and exits
	# with status
	pause = f'while [ ! -e {tmp_path}/go ]; do sleep 0.02; done; ' if wait else ''
	script = (f'echo $$ > {tmp_path}/pid{index}; {pause}i=0; while [ $i -lt {lines} ]; do echo x{index}; sleep 0.05; i=$((i+1)); done; '
		f'touch {tmp_path}/done{index}; exit {status}')
	return 'sh -c ' + shlex.quote(script)


def flood(tmp_path, index):
	# more output than any pipe or pty holds: it ends only if its reader keeps reading
	script = f'echo $$ > {tmp_path}/pid{index}; seq 1 200000; touch {tmp_path}/done{index}'
	return 'sh -c ' + shlex.quote(script)


def done(tmp_path):
	return sorted(int(path.name[4:]) for path in tmp_path.glob('done*'))


def lost_lines(err):
	'''The stderr lines, which must be the one line that says the output was lost'''
	return [line for line in err.splitlines() if line.strip()]


def read_until(fd, marker, timeout=20):
	data = b''
	deadline = time.monotonic() + timeout
	while marker not in data:
		left = deadline - time.monotonic()
		assert left > 0, 'no %r on the terminal' % marker
		if select.select([fd], [], [], left)[0]:
			try:
				chunk = os.read(fd, 65536)
			except OSError:
				break
			if not chunk:
				break
			data += chunk
	return data


def run_on_a_terminal_that_hangs_up(tmp_path, args, stderr_on_terminal=False):
	'''
	The CLI with a terminal for stdout ( and for stderr, if stderr_on_terminal: its stderr is then not read, and is '' ), in a
	session of its own ( `setsid mcmd ...`, or a job the hangup sends no SIGHUP ): the terminal hangs up ( its master closes )
	once the first command is running; its status and stderr
	The commands wait for the go file ( see job() ), which this creates once the terminal is gone, so that the first write
	after the hang-up is certain, however long the test took to close it.
	'''
	import pty
	master, slave = pty.openpty()
	try:
		proc = subprocess.Popen([sys.executable, str(MULTICMD_PY), *args], stdin=subprocess.DEVNULL, stdout=slave,
			stderr=slave if stderr_on_terminal else subprocess.PIPE, start_new_session=True)
		os.close(slave)
		slave = -1
		try:
			assert b'Running command' in read_until(master, b'Running command')
			wait_for_pidfiles(tmp_path, 1)
			os.close(master)
			master = -1
			(tmp_path / 'go').write_text('')
			return finish(proc)
		finally:
			if proc.poll() is None:
				proc.kill()
			kill_leftovers(pids_in(tmp_path))
	finally:
		for fd in (master, slave):
			if fd >= 0:
				os.close(fd)


@posix_only
@pytest.mark.parametrize('stderr', ['pipe', 'terminal'])
@pytest.mark.parametrize('options, kinds, status', [
	pytest.param([], ['job', 'job3'], 1, id='serial'),
	pytest.param(['-m', '2'], ['job', 'job', 'flood', 'job'], 0, id='pool'),
	pytest.param([], ['job3'], 3, id='one'),
	pytest.param(['--no-pty'], ['job', 'flood', 'job'], 0, id='no-pty'),
	pytest.param(['-m', '2', '--no-pty', '--summary'], ['job', 'job3', 'job'], 1, id='pool-no-pty'),
])
def test_a_terminal_that_hung_up_loses_the_output_and_every_command_runs(tmp_path, options, kinds, status, stderr):
	# stderr on the terminal too ( `mcmd ... > log 2>&1` on one ): the line that says the output was lost fails as well, and
	# must not end the run with 120 at exit
	commands = []
	for index, kind in enumerate(kinds, 1):
		if kind == 'flood':
			commands.append(flood(tmp_path, index))
		else:
			commands.append(job(tmp_path, index, status=3 if kind == 'job3' else 0, wait=True))
	got, err = run_on_a_terminal_that_hangs_up(tmp_path, options + commands, stderr_on_terminal=stderr == 'terminal')
	assert done(tmp_path) == list(range(1, len(kinds) + 1))
	assert got == status
	if stderr == 'pipe':
		assert len(lost_lines(err)) == 1 and LOST in err and '[Errno %d]' % errno.EIO in err


@posix_only
def test_a_quiet_run_on_a_terminal_that_hung_up_writes_nothing_and_says_nothing(tmp_path):
	# -q with one command writes nothing to stdout ( no header, no summary ): it meets no failed write
	import pty
	master, slave = pty.openpty()
	try:
		proc = subprocess.Popen([sys.executable, str(MULTICMD_PY), '-q', job(tmp_path, 1, status=4)], stdin=subprocess.DEVNULL,
			stdout=slave, stderr=subprocess.PIPE, start_new_session=True)
		os.close(slave)
		slave = -1
		try:
			wait_for_pidfiles(tmp_path, 1)
			os.close(master)
			master = -1
			got, err = finish(proc)
		finally:
			if proc.poll() is None:
				proc.kill()
			kill_leftovers(pids_in(tmp_path))
	finally:
		for fd in (master, slave):
			if fd >= 0:
				os.close(fd)
	assert (got, err) == (4, '')
	assert done(tmp_path) == [1]


def run_with_stdout(path, args, timeout=60, stderr_path=None):
	'''The CLI with stdout on path, and stderr on stderr_path or a pipe; its status and stderr ( '' for a stderr_path )'''
	with open(path, 'wb') as out, open(stderr_path or os.devnull, 'wb') as err:
		proc = subprocess.run([sys.executable, str(MULTICMD_PY), *args], stdin=subprocess.DEVNULL, stdout=out,
			stderr=err if stderr_path else subprocess.PIPE, universal_newlines=True, timeout=timeout)
	return proc.returncode, proc.stderr or ''


@has_dev_full
@posix_only
@pytest.mark.parametrize('options, count, failing, status', [
	pytest.param([], 3, 0, 0, id='serial'),
	pytest.param(['-m', '2'], 5, 2, 1, id='pool'),
	pytest.param(['--no-pty'], 1, 1, 3, id='one'),
])
def test_a_full_disk_under_stdout_loses_the_output_and_every_command_runs(tmp_path, options, count, failing, status):
	# ( /dev/full: every write fails with ENOSPC, as on a full disk; failing is the number of the command that exits 3 )
	commands = [job(tmp_path, i, lines=2, status=3 if i == failing else 0) for i in range(1, count + 1)]
	got, err = run_with_stdout('/dev/full', options + commands)
	assert done(tmp_path) == list(range(1, count + 1))
	assert got == status
	assert len(lost_lines(err)) == 1 and LOST in err and '[Errno %d]' % errno.ENOSPC in err


@has_dev_full
@posix_only
@pytest.mark.parametrize('options, count, failing, status', [
	pytest.param([], 3, 0, 0, id='serial'),
	pytest.param(['-m', '2'], 5, 2, 1, id='pool'),
	pytest.param(['-q', '-m', '2'], 4, 2, 1, id='quiet-pool'),
	pytest.param(['--no-pty'], 1, 1, 3, id='one'),
])
def test_a_full_disk_under_stdout_and_stderr_ends_with_the_commands_status(tmp_path, options, count, failing, status):
	# `mcmd ... > log 2>&1` on a full disk, which includes the README's `nohup mcmd -q ... > log 2>&1 &`: the line that says the
	# output was lost stays in stderr's buffer, and Python's flush at exit failed with it: status 120
	commands = [job(tmp_path, i, lines=2, status=3 if i == failing else 0) for i in range(1, count + 1)]
	got, _ = run_with_stdout('/dev/full', options + commands, stderr_path='/dev/full')
	assert done(tmp_path) == list(range(1, count + 1))
	assert got == status


@has_dev_full
@pytest.mark.parametrize('args', [['-h'], ['-V']], ids=['help', 'version'])
def test_a_full_disk_under_stdout_and_stderr_ends_0_for_help_and_version(args):
	assert run_with_stdout('/dev/full', args, stderr_path='/dev/full')[0] == 0


@posix_only
@pytest.mark.parametrize('dead', ['full', 'closed-pipe'])
@pytest.mark.parametrize('args, status', [
	pytest.param(['no-such-command-xyz'], 127, id='not-found'),
	pytest.param(['-f', os.devnull], 2, id='no-commands'),
])
def test_a_stderr_that_cannot_be_written_changes_no_exit_status(tmp_path, dead, args, status):
	# the message that cannot be written is dropped ( 1.62 and 1.63: 120, from Python's flush at exit; or a traceback and 1 for
	# the usage error )
	if dead == 'full':
		if not os.path.exists('/dev/full'):
			pytest.skip('needs /dev/full')
		with open('/dev/full', 'wb') as full:
			proc = subprocess.run([sys.executable, str(MULTICMD_PY), *args], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
				stderr=full, timeout=60)
	else:
		read_end, write_end = os.pipe()
		os.close(read_end)
		try:
			proc = subprocess.run([sys.executable, str(MULTICMD_PY), *args], stdin=subprocess.DEVNULL,
				stdout=subprocess.DEVNULL, stderr=write_end, timeout=60)
		finally:
			os.close(write_end)
	assert proc.returncode == status


@posix_only
@pytest.mark.parametrize('options, count, failing, status', [
	pytest.param([], 2, 0, 0, id='serial'),
	pytest.param(['-m', '2'], 3, 2, 1, id='pool'),
	pytest.param(['-q', '-m', '2'], 3, 2, 1, id='quiet-pool'),
])
def test_a_stdout_closed_at_start_drops_the_output_and_every_command_runs(tmp_path, options, count, failing, status):
	# `mcmd ... >&-`: sys.stdout is None. 1.62 and 1.63: "Error running command" and a traceback, the commands not run ( a
	# serial run ), or a traceback at the summary
	commands = [job(tmp_path, i, lines=2, status=3 if i == failing else 0) for i in range(1, count + 1)]
	proc = subprocess.run(['sh', '-c', 'exec "$@" >&-', 'sh', sys.executable, str(MULTICMD_PY), *options, *commands],
		stdin=subprocess.DEVNULL, stderr=subprocess.PIPE, universal_newlines=True, timeout=60)
	assert done(tmp_path) == list(range(1, count + 1))
	assert (proc.returncode, proc.stderr) == (status, '')


@has_dev_full
@pytest.mark.parametrize('args', [['-h'], ['-V'], ['-n', '-p', 'echo x[1-3]']], ids=['help', 'version', 'dry-run'])
def test_a_full_disk_under_what_argparse_or_a_dry_run_writes_says_so_once(args):
	# argparse's own writes stay in Python's buffer until main()'s flush, which meets the same rule ( 1.62: 120 and
	# "Exception ignored" from Python's flush at exit )
	got, err = run_with_stdout('/dev/full', args)
	assert got == 0
	assert len(lost_lines(err)) == 1 and LOST in err


@posix_only
@pytest.mark.parametrize('kind', ['devnull', 'file'])
def test_a_stdout_that_works_changes_nothing(tmp_path, kind):
	path = os.devnull if kind == 'devnull' else str(tmp_path / 'out')
	got, err = run_with_stdout(path, ['-m', '2', job(tmp_path, 1, lines=1), job(tmp_path, 2, lines=1, status=5)])
	assert (got, err) == (1, '')
	if kind == 'file':
		assert (tmp_path / 'out').read_text().count('Running command') == 2


@posix_only
def test_a_socket_its_peer_reset_ends_the_cli_by_sigpipe_and_stops_its_commands(tmp_path):
	# a TCP client that goes away with a reset ( SO_LINGER 0 ): the next write gets ECONNRESET, a reader that quit. 1.62 raised it
	# in the thread that wrote: a traceback on stderr
	server = socket.socket()
	server.bind(('127.0.0.1', 0))
	server.listen(1)
	client = socket.create_connection(server.getsockname())
	served, _ = server.accept()
	server.close()
	try:
		proc = subprocess.Popen([sys.executable, str(MULTICMD_PY), '-m', '2'] +
			[streaming_command(tmp_path, i) for i in range(1, 5)], stdin=subprocess.DEVNULL, stdout=served.fileno(),
			stderr=subprocess.PIPE)
		served.close()
		try:
			client.settimeout(20)
			data = b''
			while b'Running command' not in data:
				chunk = client.recv(65536)
				assert chunk, 'mcmd wrote nothing'
				data += chunk
			wait_for_pidfiles(tmp_path, 2)
			client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b'\x01\x00\x00\x00\x00\x00\x00\x00')
			client.close()
			status, err = finish(proc)
		finally:
			if proc.poll() is None:
				proc.kill()
			kill_leftovers(pids_in(tmp_path))
	finally:
		client.close()
		served.close()
	assert (status, err) == (-signal.SIGPIPE, '')
	assert len(pids_in(tmp_path)) == 2
	assert all(pid_gone(pid) for pid in pids_in(tmp_path))


@posix_only
def test_a_stdout_that_cannot_take_what_a_command_prints_loses_only_the_characters(tmp_path):
	# an ASCII stdout ( Python 3.6 in a C locale, and later ones with the coercion and the UTF-8 mode off ) gets the text with
	# backslash escapes: the reader thread used to die of the UnicodeEncodeError, and with it the command's pipe
	env = dict(os.environ, LC_ALL='C', PYTHONCOERCECLOCALE='0', PYTHONUTF8='0')
	env.pop('PYTHONIOENCODING', None)
	argv = [sys.executable, '-c', 'import sys; sys.stdout.buffer.write("caf\\u00e9\\n".encode()); sys.stdout.flush()']
	with open(tmp_path / 'out', 'wb') as out:
		proc = subprocess.run([sys.executable, str(MULTICMD_PY), ' '.join(shlex.quote(a) for a in argv)], stdin=subprocess.DEVNULL,
			stdout=out, stderr=subprocess.PIPE, env=env, timeout=60)
	assert (proc.returncode, proc.stderr) == (0, b'')
	assert b'caf\\xe9' in (tmp_path / 'out').read_bytes()


def test_the_exit_does_not_wait_for_a_display_lock_a_ctrl_c_left_held():
	# the approved limitation ( docs/limitations.md: a Ctrl-C as a lock is taken can leave it held ) promises the exit does not
	# hang: 1.63's first build flushed under that lock, and a run that returned 130 waited for ever
	code = ('import sys; sys.path.insert(0, %r); import multiCMD; multiCMD._display.lock.acquire(); '
		'multiCMD._run_cli = lambda: 130; sys.exit(multiCMD.main())' % str(MULTICMD_PY.parent))
	try:
		proc = subprocess.run([sys.executable, '-c', code], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
			stderr=subprocess.PIPE, timeout=30)
	except subprocess.TimeoutExpired:
		pytest.fail('main() waited for a display lock nobody will release')
	assert (proc.returncode, proc.stderr) == (130, b'')


# in this process


class FailingStdout:
	'''A stdout on descriptor fd whose writes fail with error'''
	def __init__(self, fd, error):
		self.fd = fd
		self.error = error

	def fileno(self):
		return self.fd

	def write(self, text):
		raise self.error

	def flush(self):
		raise self.error


@pytest.mark.parametrize('error, reader_quit', [
	pytest.param(BrokenPipeError(errno.EPIPE, 'Broken pipe'), True, id='EPIPE'),
	pytest.param(ConnectionResetError(errno.ECONNRESET, 'Connection reset by peer'), True, id='ECONNRESET'),
	pytest.param(OSError(errno.EIO, 'Input/output error'), False, id='EIO'),
	pytest.param(OSError(errno.ENOSPC, 'No space left on device'), False, id='ENOSPC'),
	pytest.param(OSError(errno.EINVAL, 'Invalid argument'), False, id='EINVAL-posix'),
])
def test_what_a_reader_that_quit_is(monkeypatch, error, reader_quit):
	# ( POSIX: on Windows EINVAL is a reader that quit for a pipe, as the next test checks )
	monkeypatch.setattr(multiCMD, '_IS_POSIX', True)
	assert multiCMD._no_reader(error) is reader_quit


@pytest.mark.parametrize('kind', ['pipe', 'file'])
def test_windows_einval_is_a_reader_that_quit_on_a_pipe_only(tmp_path, monkeypatch, kind):
	monkeypatch.setattr(multiCMD, '_IS_POSIX', False)
	if kind == 'pipe':
		read_end, fd = os.pipe()
		os.close(read_end)
	else:
		fd = os.open(str(tmp_path / 'out'), os.O_WRONLY | os.O_CREAT)
	try:
		monkeypatch.setattr(sys, 'stdout', FailingStdout(fd, OSError(errno.EINVAL, 'Invalid argument')))
		assert multiCMD._no_reader(sys.stdout.error) is (kind == 'pipe')
	finally:
		monkeypatch.undo()
		os.close(fd)


def test_a_lost_stdout_points_at_devnull_and_says_so_once(tmp_path, monkeypatch, capfd):
	# so that what the failed write left in Python's buffer goes nowhere at exit, instead of failing again ( status 120 )
	path = tmp_path / 'out'
	fd = os.open(str(path), os.O_WRONLY | os.O_CREAT)
	try:
		monkeypatch.setattr(sys, 'stdout', FailingStdout(fd, OSError(errno.EIO, 'Input/output error')))
		multiCMD._display.on_failed = multiCMD._stdout_failed
		multiCMD._display.message('header')
		multiCMD._display.lines(['a', 'b'])
		multiCMD._display.flush()
		assert multiCMD._display.lost and not multiCMD._display.closed
		os.write(fd, b'after')
	finally:
		monkeypatch.undo()
		os.close(fd)
	assert path.read_bytes() == b''
	err = capfd.readouterr().err
	assert err.count(LOST) == 1 and len(lost_lines(err)) == 1


@pytest.mark.parametrize('kwargs', [{}, {'max_threads': 2}], ids=['serial', 'pool'])
def test_the_library_runs_every_command_when_stdout_fails(tmp_path, monkeypatch, capfd, kwargs):
	# a program that imports multiCMD, not quiet, on a stdout that fails ( hpcp's mkfs runs so ): 1.62 counted each command whose
	# header could not be written as failed ( -1 ) without running it, and a reader's failed line left a traceback
	fd = os.open(str(tmp_path / 'out'), os.O_WRONLY | os.O_CREAT)
	try:
		monkeypatch.setattr(sys, 'stdout', FailingStdout(fd, OSError(errno.EIO, 'Input/output error')))
		tasks = multiCMD.run_commands([[sys.executable, '-c', 'print(%d)' % i] for i in range(4)], return_object=True, **kwargs)
	finally:
		monkeypatch.undo()
		os.close(fd)
	assert [(task.returncode, task.stdout) for task in tasks] == [(0, [str(i)]) for i in range(4)]
	err = capfd.readouterr().err
	assert err.count('multiCMD: cannot write to stdout') == 1 and len(lost_lines(err)) == 1


@pytest.mark.parametrize('call', ['bar', 'bar-fallback', 'join'])
def test_the_bar_and_the_failure_report_lose_the_output_when_stdout_fails(tmp_path, monkeypatch, capfd, call):
	# they wrote with a bare print() or sys.stdout.write(): a terminal that hung up raised OSError in the caller ( hpcp's bar )
	fd = os.open(str(tmp_path / 'out'), os.O_WRONLY | os.O_CREAT)
	try:
		monkeypatch.setattr(sys, 'stdout', FailingStdout(fd, OSError(errno.EIO, 'Input/output error')))
		if call == 'bar':
			for i in range(1, 4):
				multiCMD.print_progress_bar(i, 3)
		elif call == 'bar-fallback':
			# the plain line the bar falls back to when it cannot draw ( every fifth call )
			monkeypatch.setattr(multiCMD, 'get_terminal_size', lambda: 1 / 0)
			multiCMD.print_progress_bar(5, 10)
			multiCMD.print_progress_bar(10, 10)
		else:
			executor = multiCMD.AsyncExecutor(quiet=True)
			executor.run_command(['false'])
			executor.join()
			assert executor.get_return_codes() == [1]
	finally:
		monkeypatch.undo()
		os.close(fd)
	err = capfd.readouterr().err
	assert err.count('multiCMD: cannot write to stdout') == 1 and len(lost_lines(err)) == 1
