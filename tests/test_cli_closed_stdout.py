'''
`mcmd ... | head`: output that nobody reads any more ends the CLI quietly, as it ends `yes | head`
( by SIGPIPE, the shell's 141 ), after the commands it started were stopped. Python's own answer to a stdout it cannot
flush is a traceback, "Exception ignored" and exit status 120.
'''
import os
import shlex
import signal
import subprocess
import sys
import time

import pytest

from conftest import MULTICMD_PY, pid_gone
import multiCMD

posix_only = pytest.mark.skipif(os.name != 'posix', reason='SIGPIPE: POSIX only')


def start_cli(args):
	'''The CLI with its stdout a pipe this test reads ( reader ), and its stderr one that finish() collects'''
	read_end, write_end = os.pipe()
	try:
		proc = subprocess.Popen([sys.executable, str(MULTICMD_PY), *args], stdin=subprocess.DEVNULL,
			stdout=write_end, stderr=subprocess.PIPE)
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
	return proc.returncode, err.decode(errors='replace')


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


@posix_only
def test_a_reader_that_stays_changes_nothing():
	# the shell's pipeline `mcmd ... | cat` : the handlers installed for a closed stdout leave a full read alone
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
	# Python ignores SIGPIPE; the CLI listens to it for one purpose only
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


# main() in this process: _end_by_closed_stdout is replaced first, it would end the test run itself


def test_main_ends_the_process_when_the_interrupt_was_lost(monkeypatch):
	# a _StdoutClosed raised where Python drops it ( a weakref callback ) is lost: the run goes on, and main() sees the display closed
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
