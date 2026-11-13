'''
`mcmd ... | head`: output that nobody reads any more ends the CLI quietly, as it ends `yes | head`
( by SIGPIPE, the shell's 141 ), after the commands it started were stopped. Python's own answer to a stdout it cannot
flush is a traceback, "Exception ignored" and exit status 120.
'''
import errno
import os
import select
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time

import pytest

from conftest import MULTICMD_PY, pid_gone
import multiCMD

posix_only = pytest.mark.skipif(os.name != 'posix', reason='SIGPIPE: POSIX only')
has_poll = pytest.mark.skipif(not hasattr(select, 'poll'), reason='needs select.poll')


def start_cli(args, driver=None, **fields):
	'''
	The CLI with its stdout a pipe this test reads ( reader ), and its stderr one that finish() collects; with a driver
	( Python code that ends with sys.exit(multiCMD.main()), formatted with {root}, the module's directory, and fields ) in its place
	'''
	read_end, write_end = os.pipe()
	if driver is None:
		argv = [sys.executable, str(MULTICMD_PY), *args]
	else:
		argv = [sys.executable, '-c', driver.format(root=str(MULTICMD_PY.parent), **fields), *args]
	try:
		proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=write_end, stderr=subprocess.PIPE)
	except BaseException:
		os.close(read_end)
		raise
	finally:
		os.close(write_end)
	proc.reader = os.fdopen(read_end, 'rb')
	return proc


def finish(proc):
	'''Wait for the CLI to end, as a pipeline's last reader having quit leaves it; its status and stderr'''
	try:
		_, err = proc.communicate(timeout=30)
	except subprocess.TimeoutExpired:
		proc.kill()
		proc.communicate()
		pytest.fail('the CLI did not end 30 s after its reader quit')
	return proc.returncode, (err or b'').decode(errors='replace')


def pids_in(tmp_path):
	pids = []
	for path in sorted(tmp_path.glob('pid*')):
		text = path.read_text().strip()
		if text:
			pids.append(int(text))
	return pids


def wait_for_pidfiles(tmp_path, count):
	deadline = time.monotonic() + 20
	while len(pids_in(tmp_path)) < count:
		assert time.monotonic() < deadline, 'the commands did not start'
		time.sleep(0.02)


def kill_leftovers(pids):
	for pid in pids:
		try:
			os.kill(pid, signal.SIGKILL)
		except OSError:
			pass


def streaming_command(tmp_path, index):
	# writes its pid, then prints for ever until it is stopped
	script = f'echo $$ > {tmp_path}/pid{index}; while :; do echo x{index}; sleep 0.05; done'
	return 'sh -c ' + shlex.quote(script)


@posix_only
@pytest.mark.parametrize('options, count, running', [
	pytest.param([], 1, 1, id='serial'),
	pytest.param(['-m', '2'], 4, 2, id='pool'),
])
def test_a_reader_that_quits_ends_the_cli_by_sigpipe_and_stops_its_commands(tmp_path, options, count, running):
	proc = start_cli(options + [streaming_command(tmp_path, i) for i in range(1, count + 1)])
	try:
		assert b'Running command' in proc.reader.readline()
		# only then does stdout have something to lose: the commands are running
		wait_for_pidfiles(tmp_path, running)
		proc.reader.close()
		status, err = finish(proc)
		pids = pids_in(tmp_path)
		assert status == -signal.SIGPIPE
		assert err == ''
		# the ones that never started, never do; the others are stopped, and nothing runs on after mcmd
		assert len(pids) == running
		assert all(pid_gone(pid) for pid in pids)
	finally:
		proc.reader.close()
		kill_leftovers(pids_in(tmp_path))


@posix_only
def test_a_command_that_ignores_sigpipe_is_stopped_too(tmp_path):
	# it keeps printing into a pipe nobody reads, and nothing but a stop ends it ( without one, mcmd waited for it for ever )
	script = f'trap "" PIPE; echo $$ > {tmp_path}/pid1; while :; do echo x 2>/dev/null; sleep 0.05; done'
	proc = start_cli(['sh -c ' + shlex.quote(script)])
	try:
		assert b'Running command' in proc.reader.readline()
		wait_for_pidfiles(tmp_path, 1)
		proc.reader.close()
		status, err = finish(proc)
		assert status == -signal.SIGPIPE
		assert err == ''
		assert all(pid_gone(pid) for pid in pids_in(tmp_path))
	finally:
		proc.reader.close()
		kill_leftovers(pids_in(tmp_path))


@posix_only
def test_a_reader_that_quit_before_the_summary_ends_the_cli_by_sigpipe():
	# -q: the summary is the first thing written, by the main thread, once the commands are done
	proc = start_cli(['-q', '-m', '2', 'sleep 0.3', 'false'])
	proc.reader.close()
	status, err = finish(proc)
	assert status == -signal.SIGPIPE
	assert err == ''


@posix_only
@pytest.mark.parametrize('options, count', [
	pytest.param(['-m', '2'], 3, id='pool-2'),
	pytest.param(['-m', '4'], 8, id='pool-4'),
])
def test_a_reader_that_quit_before_the_first_write_starts_no_command(tmp_path, options, count):
	# the first write, a worker's "Running command" header, finds stdout closed: in 1.60 it raised and the start never
	# happened. Quiet commands that end at once cannot be told from the stop, so the pipe is closed before mcmd starts, and
	# each command leaves a file the moment it runs.
	read_end, write_end = os.pipe()
	os.close(read_end)
	try:
		proc = subprocess.Popen([sys.executable, str(MULTICMD_PY), *options] +
			['touch %s/f%d' % (tmp_path, i) for i in range(1, count + 1)],
			stdin=subprocess.DEVNULL, stdout=write_end, stderr=subprocess.PIPE)
	finally:
		os.close(write_end)
	status, err = finish(proc)
	assert status == -signal.SIGPIPE
	assert err == ''
	assert sorted(path.name for path in tmp_path.iterdir()) == []


@posix_only
def test_a_reader_that_quit_before_a_dry_run_ends_the_cli_by_sigpipe():
	proc = start_cli(['-n', '-p', 'echo x[1-3000]'])
	proc.reader.close()
	status, err = finish(proc)
	assert status == -signal.SIGPIPE
	assert err == ''


@posix_only
@pytest.mark.parametrize('flag', ['-h', '-V'])
def test_a_reader_that_quit_before_help_or_version_ends_the_cli_by_sigpipe(flag):
	# argparse writes these, and Python's flush at exit used to fail on them
	proc = start_cli([flag])
	proc.reader.close()
	status, err = finish(proc)
	assert status == -signal.SIGPIPE
	assert err == ''


# a driver for the CLI that makes an interrupt raised by a SIGPIPE handler be dropped, where Python drops one: inside a
# __del__ ( a weakref callback is the same ). Where there is no such handler it leaves the CLI as it is.
DROPPED_RAISE_DRIVER = '''
import signal, sys
sys.path.insert(0, {root!r})
import multiCMD
install = multiCMD._install_cli_signal_handlers
def install_and_drop():
	install()
	handler = signal.getsignal(signal.SIGPIPE)
	if callable(handler):
		def dropped(signum, frame):
			class Drops:
				def __del__(self):
					handler(signum, frame)
			Drops()
		signal.signal(signal.SIGPIPE, dropped)
multiCMD._install_cli_signal_handlers = install_and_drop
sys.argv[0] = 'mcmd'
sys.exit(multiCMD.main())
'''


@posix_only
@pytest.mark.parametrize('options, count, running', [
	pytest.param([], 1, 1, id='serial'),
	pytest.param(['-m', '2'], 2, 2, id='pool'),
])
def test_the_stop_does_not_depend_on_one_interrupt_landing(tmp_path, options, count, running):
	# a signal handler's raise is dropped when it lands in a __del__ or a weakref callback ( Popen.__del__ runs on the main
	# thread all the time ): the CLI then ran its endless command until a Ctrl-C. The main thread's waits ask for the closed
	# stdout themselves, so nothing is lost.
	proc = start_cli(options + [streaming_command(tmp_path, i) for i in range(1, count + 1)], DROPPED_RAISE_DRIVER)
	try:
		assert b'Running command' in proc.reader.readline()
		wait_for_pidfiles(tmp_path, running)
		proc.reader.close()
		status, err = finish(proc)
		assert status == -signal.SIGPIPE
		assert all(pid_gone(pid) for pid in pids_in(tmp_path))
	finally:
		proc.reader.close()
		kill_leftovers(pids_in(tmp_path))


# a driver that has a reader's write find stdout closed, from the thread that starts the command, right after Popen forked it
# ( the thread that sends SIGPIPE in 1.61's first build ). Python 3.11+ runs the command itself, with no wrapper that waits for
# it to be registered.
CLOSED_AS_IT_STARTS_DRIVER = '''
import os, signal, subprocess, sys
sys.path.insert(0, {root!r})
import multiCMD
execute_child = subprocess.Popen._execute_child
def forked_and_closed(self, *args, **kwargs):
	execute_child(self, *args, **kwargs)
	with open({pidfile!r}, 'w') as f:
		f.write(str(self.pid))
	multiCMD._display.closed = True
	os.kill(os.getpid(), signal.SIGPIPE)
subprocess.Popen._execute_child = forked_and_closed
sys.argv[0] = 'mcmd'
sys.exit(multiCMD.main())
'''


@posix_only
def test_a_closed_stdout_found_as_a_command_starts_does_not_leave_it_running(tmp_path):
	# a raise from a SIGPIPE handler landed inside Popen, once it had forked and before the command was registered: on Python
	# 3.11+ the command ran on in a process group of its own, which mcmd knew nothing of ( 12 s here, sleeping )
	proc = start_cli(['sleep 12'], CLOSED_AS_IT_STARTS_DRIVER, pidfile=str(tmp_path / 'pid1'))
	try:
		status, err = finish(proc)
		assert status == -signal.SIGPIPE
		assert len(pids_in(tmp_path)) == 1
		assert all(pid_gone(pid) for pid in pids_in(tmp_path))
	finally:
		proc.reader.close()
		kill_leftovers(pids_in(tmp_path))


@posix_only
def test_the_cli_leaves_sigpipe_ignored():
	# a handler for SIGPIPE that raises runs on the main thread wherever it is: inside Popen once it has forked ( on Python
	# 3.11+ the command then runs on, known to nobody ), or where Python drops the raise
	code = ('import signal, sys; sys.path.insert(0, %r); import multiCMD; multiCMD._install_cli_signal_handlers(); '
		'print(signal.getsignal(signal.SIGPIPE) == signal.SIG_IGN)' % str(MULTICMD_PY.parent))
	out = subprocess.run([sys.executable, '-c', code], stdout=subprocess.PIPE, universal_newlines=True, timeout=30).stdout
	assert out.strip() == 'True'


@posix_only
def test_a_reader_that_stays_changes_nothing():
	# the shell's pipeline `mcmd ... | cat`: a reader that stays leaves the run alone
	proc = start_cli(['-p', 'echo line[1-3]'])
	try:
		_, err = proc.communicate(timeout=30)
		out = proc.reader.read()
		assert proc.returncode == 0
		assert err == b''
		assert out.count(b'Process exited with return code 0') == 3
	finally:
		proc.reader.close()


@posix_only
def test_a_sigpipe_from_elsewhere_is_ignored():
	# Python ignores SIGPIPE, and the CLI leaves it so
	proc = start_cli(['sleep 0.5'])
	try:
		assert b'Running command' in proc.reader.readline()
		proc.send_signal(signal.SIGPIPE)
		_, err = proc.communicate(timeout=30)
		out = proc.reader.read()
		assert proc.returncode == 0
		assert err == b''
		assert b'Process exited with return code 0' in out
	finally:
		proc.reader.close()
		if proc.poll() is None:
			proc.kill()


# the waits of a run, in this process: a stdout found closed by another thread ( a reader's write ) ends them; the call
# raises BrokenPipeError once its commands are stopped


class Watched:
	'''A watch that reports nothing: with one set, the display is mcmd's, whose quiet runs end with its reader too'''
	def poll(self, timeout):
		return []


class CloseOnceStarted:
	'''
	Set the display's closed flag, as a reader whose write found no reader does, from another thread: once count commands
	have written their pid files, and settle seconds after
	'''
	def __init__(self, tmp_path, count, settle=0.2):
		self.cancelled = threading.Event()
		self.thread = threading.Thread(target=self.run, args=(tmp_path, count, settle), daemon=True)
		self.thread.start()

	def run(self, tmp_path, count, settle):
		deadline = time.monotonic() + 20
		while len(pids_in(tmp_path)) < count and time.monotonic() < deadline:
			if self.cancelled.wait(0.02):
				return
		if not self.cancelled.wait(settle):
			multiCMD._display.closed = True

	def stop(self):
		self.cancelled.set()
		self.thread.join()


@pytest.mark.parametrize('kwargs, count', [
	pytest.param({}, 1, id='serial'),
	pytest.param({'max_threads': 2}, 2, id='pool'),
])
def test_a_closed_stdout_ends_the_wait_for_the_commands(tmp_path, kwargs, count):
	# ( the commands would sleep 12 s: only the raise, and the stop it starts, ends them sooner )
	commands = [f'sh -c {shlex.quote(f"echo $$ > {tmp_path}/pid{i}; exec sleep 12")}' for i in range(1, count + 1)]
	closer = CloseOnceStarted(tmp_path, count)
	try:
		multiCMD._display.cli = True
		multiCMD._display.watch = Watched()
		with pytest.raises(BrokenPipeError):
			multiCMD.run_commands(commands, quiet=True, **kwargs)
		assert len(pids_in(tmp_path)) == count
		assert all(pid_gone(pid) for pid in pids_in(tmp_path))
	finally:
		closer.stop()
		kill_leftovers(pids_in(tmp_path))


def test_a_closed_stdout_ends_the_wait_for_output_that_a_command_left_open(tmp_path):
	# the command has exited; what it started holds its output open, and the run reads it to its end
	closer = CloseOnceStarted(tmp_path, 1)
	try:
		multiCMD._display.cli = True
		multiCMD._display.watch = Watched()
		with pytest.raises(BrokenPipeError):
			multiCMD.run_command('sh -c ' + shlex.quote(f'sleep 12 & echo $! > {tmp_path}/pid1'), quiet=True)
		assert all(pid_gone(pid) for pid in pids_in(tmp_path))
	finally:
		closer.stop()
		kill_leftovers(pids_in(tmp_path))


def test_the_stop_that_a_closed_stdout_starts_is_not_cut_short(tmp_path):
	# the stop is not cut short: the waits of the stop ( up to 2 s here: the command ignores SIGINT and SIGTERM, and only
	# SIGKILL ends it ) go on although the reader is gone, so the command is dead before the call raises
	script = f'trap "" INT TERM; echo $$ > {tmp_path}/pid1; i=0; while [ $i -lt 240 ]; do sleep 0.05; i=$((i+1)); done'
	closer = CloseOnceStarted(tmp_path, 1)
	try:
		multiCMD._display.cli = True
		multiCMD._display.watch = Watched()
		with pytest.raises(BrokenPipeError):
			multiCMD.run_command('sh -c ' + shlex.quote(script), quiet=True)
		assert multiCMD._live_procs == {}
		assert all(pid_gone(pid, wait=0) for pid in pids_in(tmp_path))
	finally:
		closer.stop()
		kill_leftovers(pids_in(tmp_path))


# a command not started yet is not started once a reader has found stdout closed ( 1.60 never started it: its "Running command"
# write failed ). Found after the main thread's last look, or while the workers go on taking commands: each of these ends one
# command, and then has the display closed, as such a reader does


def count_executions_closing_after_each(monkeypatch):
	'''The list of the commands whose run begins; every run that ends leaves the display closed'''
	begun = []
	real = multiCMD._execute
	def execute(task, *args, **kwargs):
		begun.append(task.command)
		try:
			return real(task, *args, **kwargs)
		finally:
			multiCMD._display.closed = True
	monkeypatch.setattr(multiCMD, '_execute', execute)
	return begun


@pytest.mark.parametrize('kwargs, running', [
	pytest.param({}, 1, id='serial'),
	pytest.param({'max_threads': 2}, 2, id='pool'),
])
def test_no_command_is_started_after_stdout_was_found_closed(monkeypatch, kwargs, running):
	# ( the commands in flight when it was found go on to their stop; the others never begin )
	begun = count_executions_closing_after_each(monkeypatch)
	commands = ['sleep 0.3'] * 6
	multiCMD._display.cli = True
	multiCMD._display.watch = Watched()
	with pytest.raises(BrokenPipeError):
		multiCMD.run_commands(commands, quiet=True, **kwargs)
	assert len(begun) == running


# main() in this process: _end_by_closed_stdout is replaced first, it would end the test run itself


def test_main_ends_the_process_when_no_wait_saw_the_closed_stdout(monkeypatch):
	# a reader found stdout closed as the run ended, and no wait asked: main() sees the display closed
	ended = []
	monkeypatch.setattr(multiCMD, '_end_by_closed_stdout', lambda: ended.append(True))
	def run():
		multiCMD._display.closed = True
		return 3
	monkeypatch.setattr(multiCMD, '_run_cli', run)
	assert multiCMD.main() == 3
	assert ended == [True]


def test_main_ends_the_process_when_the_final_flush_finds_no_reader(monkeypatch):
	class NoReader:
		def write(self, text):
			pass

		def flush(self):
			raise BrokenPipeError(32, 'Broken pipe')
	ended = []
	monkeypatch.setattr(multiCMD, '_end_by_closed_stdout', lambda: ended.append(True))
	monkeypatch.setattr(multiCMD, '_run_cli', lambda: 0)
	monkeypatch.setattr(sys, 'stdout', NoReader())
	assert multiCMD.main() == 0
	assert ended == [True]


def test_main_leaves_a_working_stdout_alone(monkeypatch):
	ended = []
	monkeypatch.setattr(multiCMD, '_end_by_closed_stdout', lambda: ended.append(True))
	monkeypatch.setattr(multiCMD, '_run_cli', lambda: 5)
	assert multiCMD.main() == 5
	assert ended == []


# stdout polled: a reader that quits ends the run though nothing is written ( 1.61 found out only from a write, and the
# commands ran to their end first )


@posix_only
@has_poll
@pytest.mark.parametrize('options, count', [
	pytest.param([], 1, id='serial'),
	pytest.param(['-q'], 1, id='quiet'),
	pytest.param(['-t', '30'], 1, id='timeout'),
	pytest.param(['-q', '-m', '2'], 2, id='pool'),
])
def test_a_reader_that_quits_while_the_commands_print_nothing_ends_the_cli_at_once(tmp_path, options, count):
	# ( `mcmd 'sleep 4' | head -2`: 1.61 waited the 4 s, 12 here )
	commands = [f'sh -c {shlex.quote(f"echo $$ > {tmp_path}/pid{i}; exec sleep 12")}' for i in range(1, count + 1)]
	proc = start_cli(options + commands)
	try:
		wait_for_pidfiles(tmp_path, count)
		closed = time.monotonic()
		proc.reader.close()
		status, err = finish(proc)
		took = time.monotonic() - closed
		assert status == -signal.SIGPIPE
		assert err == ''
		# ( the stop itself takes about 10 ms: SIGINT ends sleep )
		assert took < 6
		assert all(pid_gone(pid) for pid in pids_in(tmp_path))
	finally:
		proc.reader.close()
		kill_leftovers(pids_in(tmp_path))


def run_cli_quiet(stdout, command):
	'''The CLI, quiet, with the given stdout ( a file ), run to its end'''
	return subprocess.run([sys.executable, str(MULTICMD_PY), '-q', command], stdin=subprocess.DEVNULL, stdout=stdout,
		stderr=subprocess.PIPE, universal_newlines=True, timeout=30)


@posix_only
@pytest.mark.parametrize('target', ['devnull', 'file'])
def test_a_stdout_that_is_not_a_pipe_is_not_taken_for_one_whose_reader_quit(tmp_path, target):
	path = os.devnull if target == 'devnull' else str(tmp_path / 'out')
	with open(path, 'wb') as out:
		proc = run_cli_quiet(out, 'sleep 0.3')
	assert proc.returncode == 0
	assert proc.stderr == ''


@posix_only
@has_poll
def test_a_terminal_that_hung_up_is_not_taken_for_a_reader_that_quit(tmp_path):
	# `mcmd 'long job' &` then `exit` or `disown`, or `setsid mcmd 'long job'`: the terminal goes, poll() answers POLLHUP for its slave, and
	# the shell and the kernel send the job no signal. 1.62 first stopped it ( 141 ); a job left running on purpose runs on
	import pty
	master, slave = pty.openpty()
	command = 'sh -c ' + shlex.quote(f'echo $$ > {tmp_path}/pid1; sleep 0.6; touch {tmp_path}/done')
	try:
		proc = subprocess.Popen([sys.executable, str(MULTICMD_PY), '-q', command], stdin=subprocess.DEVNULL, stdout=slave,
			stderr=subprocess.PIPE, start_new_session=True)
		os.close(slave)
		slave = -1
		try:
			wait_for_pidfiles(tmp_path, 1)
			os.close(master)
			master = -1
			status, err = finish(proc)
		finally:
			proc.kill()
			kill_leftovers(pids_in(tmp_path))
	finally:
		for fd in (master, slave):
			if fd >= 0:
				os.close(fd)
	assert (status, err) == (0, '')
	assert (tmp_path / 'done').exists()


@posix_only
@has_poll
def test_a_socket_whose_peer_half_closed_is_not_taken_for_a_reader_that_quit():
	# `socat - TCP:host:port` or an HTTP/1.0 client sends its request, calls shutdown(SHUT_WR) and reads the reply: macOS
	# answers POLLHUP for the server side of it though the client is reading ( Linux sets POLLHUP on a connected socket only
	# once both directions are shut, so there this checks only that the reply gets through )
	client, served = socket.socketpair()
	try:
		client.shutdown(socket.SHUT_WR)
		client.settimeout(30)
		proc = subprocess.Popen([sys.executable, str(MULTICMD_PY), "sh -c 'sleep 0.5; echo RESULT'"], stdin=subprocess.DEVNULL,
			stdout=served.fileno(), stderr=subprocess.PIPE, universal_newlines=True)
		served.close()
		reply = b''
		try:
			while True:
				data = client.recv(65536)
				if not data:
					break
				reply += data
		finally:
			err = proc.communicate(timeout=30)[1]
	finally:
		client.close()
		served.close()
	assert proc.returncode == 0
	assert err == ''
	assert b'RESULT' in reply


@posix_only
@pytest.mark.skipif(not sys.platform.startswith('linux'), reason='macOS does not tell a named FIFO that its reader quit')
def test_a_named_fifo_whose_reader_quits_ends_the_cli_at_once(tmp_path):
	# `mcmd -q ... > fifo`: a FIFO is a pipe to poll, named or not
	fifo = str(tmp_path / 'fifo')
	os.mkfifo(fifo)
	command = 'sh -c ' + shlex.quote(f'echo $$ > {tmp_path}/pid1; exec sleep 12')
	reader = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
	try:
		writer = os.open(fifo, os.O_WRONLY)
		try:
			proc = subprocess.Popen([sys.executable, str(MULTICMD_PY), '-q', command], stdin=subprocess.DEVNULL, stdout=writer,
				stderr=subprocess.PIPE)
		finally:
			os.close(writer)
		wait_for_pidfiles(tmp_path, 1)
		closed = time.monotonic()
		os.close(reader)
		reader = -1
		status, err = finish(proc)
		assert (status, err) == (-signal.SIGPIPE, '')
		assert time.monotonic() - closed < 6
		assert all(pid_gone(pid) for pid in pids_in(tmp_path))
	finally:
		if reader >= 0:
			os.close(reader)
		kill_leftovers(pids_in(tmp_path))


class StdoutOn:
	'''A stdout that is a descriptor'''
	def __init__(self, fd):
		self.fd = fd

	def fileno(self):
		return self.fd


@posix_only
@has_poll
def test_only_a_pipe_is_watched_for_a_reader_that_quit(tmp_path, monkeypatch):
	# what poll() says about a terminal ( POLLHUP once it hung up ), a socket ( macOS: POLLHUP once the peer half-closed ) or a
	# device ( macOS: POLLNVAL ) is not a reader that quit; _watch_stdout asks a pipe only
	import pty
	pipe = os.pipe()
	master, slave = pty.openpty()
	sock, peer = socket.socketpair()
	named_fifo = str(tmp_path / 'fifo')
	os.mkfifo(named_fifo)
	opened = [pipe[0], pipe[1], master, slave, os.open(os.devnull, os.O_WRONLY), os.open(str(tmp_path / 'f'), os.O_WRONLY | os.O_CREAT),
		os.open(named_fifo, os.O_RDWR)]
	_, _, _, _, devnull, file, fifo = opened
	try:
		wrong = []
		for name, fd in [('pipe', pipe[1]), ('named FIFO', fifo), ('file', file), ('/dev/null', devnull), ('terminal', slave),
				('socket', sock.fileno())]:
			monkeypatch.setattr(sys, 'stdout', StdoutOn(fd))
			if (multiCMD._watch_stdout() is not None) != (name in ('pipe', 'named FIFO')):
				wrong.append(name)
		assert wrong == []
	finally:
		for fd in opened:
			os.close(fd)
		sock.close()
		peer.close()


@posix_only
@has_poll
def test_a_stdout_that_cannot_be_asked_is_not_watched(monkeypatch):
	read_end, write_end = os.pipe()
	os.close(read_end)
	os.close(write_end)
	monkeypatch.setattr(sys, 'stdout', StdoutOn(write_end))
	assert multiCMD._watch_stdout() is None
	monkeypatch.setattr(sys, 'stdout', object())
	assert multiCMD._watch_stdout() is None


@has_poll
def test_a_stdout_descriptor_that_is_not_open_is_not_taken_for_a_reader_that_quit():
	# poll answers POLLNVAL for it whatever was asked: only POLLERR ( Linux ) and POLLHUP ( macOS ) say the reader quit
	read_end, write_end = os.pipe()
	watch = select.poll()
	watch.register(write_end, select.POLLERR | select.POLLHUP)
	os.close(read_end)
	os.close(write_end)
	multiCMD._display.cli = True
	multiCMD._display.watch = watch
	assert not multiCMD._reader_quit()
	assert not multiCMD._display.closed


@has_poll
def test_the_library_does_not_poll_stdout(monkeypatch):
	# only the CLI is a filter that ends when its reader quits: a program that imports multiCMD, with a pipe for stdout
	# whose reader quit, gets no BrokenPipeError from a quiet run, which writes nothing and asks nothing
	read_end, write_end = os.pipe()
	os.close(read_end)
	pipe = os.fdopen(write_end, 'w')
	monkeypatch.setattr(sys, 'stdout', pipe)
	try:
		status = multiCMD.run_command([sys.executable, '-c', 'import time; time.sleep(0.2)'], quiet=True, return_code_only=True)
		assert status == 0
		assert multiCMD._display.watch is None
	finally:
		monkeypatch.undo()
		try:
			pipe.close()
		except OSError:
			pass


# Windows reports a write to a pipe whose reader quit as OSError EINVAL, not BrokenPipeError ( seen on windows-latest with
# Python 3.9, 3.12 and 3.14, 2026-11-11 ). Its C runtime gives EINVAL for other write errors too: on another stdout it is
# not taken for a reader that quit.


class EinvalStdout:
	'''A stdout on descriptor fd whose writes fail as Windows reports them'''
	def __init__(self, fd):
		self.fd = fd

	def fileno(self):
		return self.fd

	def write(self, text):
		raise OSError(errno.EINVAL, 'Invalid argument')

	def flush(self):
		raise OSError(errno.EINVAL, 'Invalid argument')


@pytest.fixture(params=['pipe', 'file'])
def einval_stdout(request, tmp_path, monkeypatch):
	'''Its kind, and what makes sys.stdout an EinvalStdout on it, a pipe or a file ( the test calls it: pytest puts its own
	sys.stdout back after the fixtures )'''
	if request.param == 'pipe':
		read_end, fd = os.pipe()
		os.close(read_end)
	else:
		fd = os.open(str(tmp_path / 'out'), os.O_WRONLY | os.O_CREAT)
	yield request.param, lambda: monkeypatch.setattr(sys, 'stdout', EinvalStdout(fd))
	monkeypatch.undo()
	os.close(fd)


@pytest.mark.parametrize('posix', [False, True], ids=['windows', 'posix'])
def test_einval_from_a_stdout_write_is_a_reader_that_quit_on_a_windows_pipe_only(monkeypatch, capfd, einval_stdout, posix):
	kind, install = einval_stdout
	monkeypatch.setattr(multiCMD, '_IS_POSIX', posix)
	multiCMD._display.cli = True
	install()
	if not posix and kind == 'pipe':
		# the write raises nothing, in whatever thread: it leaves the flag, which every wait and every start asks
		multiCMD._display.message('x')
		assert multiCMD._display.closed and not multiCMD._display.lost
		assert capfd.readouterr().err == ''
	else:
		multiCMD._display.message('x')
		multiCMD._display.message('y')
		assert multiCMD._display.lost and not multiCMD._display.closed
		assert capfd.readouterr().err.count('mcmd: cannot write to stdout') == 1


def test_windows_einval_from_a_pipe_reaches_the_library_caller_as_broken_pipe_error(monkeypatch, capfd, einval_stdout):
	# one meaning, one error: a program that imports multiCMD gets BrokenPipeError ( errno EPIPE ) for a reader that quit,
	# whichever form the platform gave the failed write. 1.62 documented "OSError EINVAL on Windows". On a file the same
	# EINVAL is only a lost output: the command runs and nothing is raised
	kind, install = einval_stdout
	monkeypatch.setattr(multiCMD, '_IS_POSIX', False)
	install()
	command = [sys.executable, '-c', 'import sys; sys.exit(3)']
	if kind == 'pipe':
		with pytest.raises(BrokenPipeError) as raised:
			multiCMD.run_command([sys.executable, '-u', '-c', 'import time; print("a line"); time.sleep(1.5); print("another"); time.sleep(30)'])
		assert raised.value.errno == errno.EPIPE
		assert multiCMD._display.closed and not multiCMD._display.lost
		# a command that prints nothing runs once the reader was found gone, and nothing is raised
		assert multiCMD.run_command(command, return_code_only=True) == 3
		assert capfd.readouterr().err == ''
	else:
		assert multiCMD.run_command(command, return_code_only=True) == 3
		assert multiCMD._display.lost and not multiCMD._display.closed


class Ended(BaseException):
	'''_end_by_closed_stdout, which never returns, was called'''


def end_by_closed_stdout():
	raise Ended()


@pytest.mark.parametrize('where', ['flush', 'run'])
def test_main_on_windows_ends_the_process_for_einval_from_a_stdout_pipe_only(monkeypatch, einval_stdout, where):
	# the final flush, or argparse writing its help or usage to the closed pipe; on a file it is not a reader that quit
	monkeypatch.setattr(multiCMD, '_IS_POSIX', False)
	monkeypatch.setattr(multiCMD, '_end_by_closed_stdout', end_by_closed_stdout)
	monkeypatch.setattr(multiCMD, '_watch_stdout', lambda: None)
	if where == 'flush':
		monkeypatch.setattr(multiCMD, '_run_cli', lambda: 0)
	else:
		def run():
			raise OSError(errno.EINVAL, 'Invalid argument')
		monkeypatch.setattr(multiCMD, '_run_cli', run)
	kind, install = einval_stdout
	install()
	if kind == 'pipe':
		with pytest.raises(Ended):
			multiCMD.main()
	elif where == 'flush':
		assert multiCMD.main() == 0
		assert multiCMD._display.lost
	else:
		# ( what _run_cli raises is not a display write: an error other than a reader quitting is raised as before )
		with pytest.raises(OSError):
			multiCMD.main()


def test_main_called_in_a_program_leaves_no_watch_behind(monkeypatch):
	# 1.63 left the watch and the name set: a later quiet call of the program asked the pipe
	monkeypatch.setattr(multiCMD, '_end_by_closed_stdout', end_by_closed_stdout)
	monkeypatch.setattr(multiCMD, '_watch_stdout', Watched)
	# ( main() installs the CLI's signal handlers, which would stay in the test process )
	monkeypatch.setattr(multiCMD, '_install_cli_signal_handlers', lambda: None)
	monkeypatch.setattr(sys, 'argv', ['mcmd', '-n', 'true'])
	assert multiCMD.main() == 0
	assert multiCMD._display.watch is None and not multiCMD._display.cli


def test_main_called_in_a_program_that_closed_sys_stderr_returns_and_leaves_no_watch_behind(tmp_path, monkeypatch):
	# _flush_stderr let the ValueError of a closed sys.stderr out of main(), which then left the CLI's state set
	closed = open(tmp_path / 'err', 'w')
	closed.close()
	monkeypatch.setattr(multiCMD, '_end_by_closed_stdout', end_by_closed_stdout)
	monkeypatch.setattr(multiCMD, '_watch_stdout', Watched)
	monkeypatch.setattr(multiCMD, '_install_cli_signal_handlers', lambda: None)
	monkeypatch.setattr(sys, 'argv', ['mcmd', '-n', 'true'])
	monkeypatch.setattr(sys, 'stderr', closed)
	assert multiCMD.main() == 0
	assert multiCMD._display.watch is None and not multiCMD._display.cli
