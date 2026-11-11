'''Through the exec wrapper ( Python < 3.11 ) a command runs only once the caller has it registered ( 1.59 ): the wrapper
waits for one byte on a pipe that _popen writes once the command is in started, and a caller interrupted inside Popen once
it has forked closes the pipe instead, so the wrapper exits 125 without running anything.'''
import os
import random
import signal
import subprocess
import sys
import time

import pytest

import multiCMD
from conftest import PACKAGE_ROOT, pid_gone

pytestmark = pytest.mark.skipif(os.name != 'posix', reason='the exec wrapper runs on POSIX only')


_INSIDE_POPEN = r'''
import os, signal, subprocess, sys, threading, time
sys.path.insert(0, sys.argv[1])
import multiCMD
pids, signame, where, marker = sys.argv[2:6]
multiCMD._HAS_PROCESS_GROUP = False
signum = getattr(signal, signame)
if signum != signal.SIGINT:
	def handler(signum, frame):
		raise SystemExit(128 + signum)
	signal.signal(signum, handler)
# another thread, as a program has ( a pool's queue feeder thread, a reader ): it takes the signal as well as the main
# thread does, and Python raises it on the main thread all the same
threading.Thread(target=time.sleep, args=(60,), daemon=True).start()
real = subprocess.Popen._execute_child
def execute_child(self, *args, **kwargs):
	if where == 'before':
		os.kill(os.getpid(), signum)
	real(self, *args, **kwargs)
	with open(pids, 'a') as f:
		f.write('%d\n' % self.pid)
	if where == 'after':
		# the signal reaches this process once Popen has forked ( process-directed, as from kill or a terminal ), and Python
		# takes it at one of these instructions, inside Popen
		os.kill(os.getpid(), signum)
		for _ in range(1000):
			pass
subprocess.Popen._execute_child = execute_child
try:
	multiCMD.run_command(['sh', '-c', 'touch "$1"; exec sleep 30', 'sh', marker], quiet=True)
except KeyboardInterrupt:
	sys.exit(130)
sys.exit(0)
'''


@pytest.mark.parametrize('signame', ['SIGINT', 'SIGTERM', 'SIGHUP'])
@pytest.mark.parametrize('where', ['after', 'before'])
def test_a_stop_inside_popen_runs_nothing(tmp_path, signame, where):
	# 1.58: a stop Python took inside Popen once it had forked was raised there; the command was never registered and ran on,
	# its process group out of reach ( an early Ctrl-C of hpcp on Python 3.6 lost a cp in about one run in four )
	pids, marker = tmp_path / 'pids', tmp_path / 'ran'
	proc = subprocess.run([sys.executable, '-c', _INSIDE_POPEN, str(PACKAGE_ROOT), str(pids), signame, where, str(marker)],
		stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=60)
	started = [int(pid) for pid in pids.read_text().split()] if pids.exists() else []
	left = [pid for pid in started if not pid_gone(pid, wait=3)]
	for pid in left:
		try:
			os.killpg(pid, signal.SIGKILL)
		except OSError:
			pass
	assert proc.returncode == (130 if signame == 'SIGINT' else 128 + getattr(signal, signame)), proc.stderr
	assert left == [] and not marker.exists(), (started, left)


def _wrapper(tmp_path, handover):
	marker = tmp_path / 'ran'
	return marker, subprocess.Popen([sys.executable, '-I', '-S', '-c', multiCMD._PGROUP_EXEC, '-', str(handover), 'touch', str(marker)],
		pass_fds=(handover,), stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)


def test_the_wrapper_waits_for_the_hand_over_and_runs_nothing_without_it(tmp_path):
	r, w = os.pipe()
	marker, proc = _wrapper(tmp_path, r)
	os.close(r)
	try:
		time.sleep(0.5)
		assert proc.poll() is None and not marker.exists()
	finally:
		os.close(w)
	out, err = proc.communicate(timeout=30)
	assert proc.returncode == 125 and (out, err) == ('', '')
	assert not marker.exists()


def test_the_wrapper_runs_the_command_once_handed_over(tmp_path):
	r, w = os.pipe()
	marker, proc = _wrapper(tmp_path, r)
	os.close(r)
	os.write(w, b'1')
	os.close(w)
	assert proc.wait(timeout=30) == 0 and marker.exists()


@pytest.mark.slow
@pytest.mark.skipif(not hasattr(signal, 'pthread_sigmask'), reason='POSIX signals')
def test_a_stop_as_the_command_starts_ends_it_by_sigint_alone(launcher):
	# ( 1.61; seen as 1.60's 3.6 CI job slowing to a timeout ) a stop sends SIGINT first. One that reached the exec
	# wrapper's Python as it initialized aborted it with a core dump on 3.6 and 3.7 ( 80 and 63 of 400 stops aimed into
	# a start, Docker ), and ended it with 'Fatal Python error' and 1 on 3.8 - 3.10 ( 41 to 54 of 400 ); later in its
	# start it printed a KeyboardInterrupt traceback. On a 2-vCPU GitHub runner each core dump started an apport, and
	# test_stops_in_serial_commands_never_jam_thread_starts drove the load to 250. The wrapper now starts with SIGINT
	# blocked and unblocks it once handed over, so a stop ends it by SIGINT, as it ends the command, without a word.
	# 200 stops: at 1 in 10 ( the rarest above ) a regression goes unseen with odds of 7e-10
	times = []
	for _ in range(10):
		start = time.perf_counter()
		proc, _ = multiCMD._spawn(['true'], subprocess.PIPE, subprocess.PIPE)
		proc.communicate(timeout=30)
		times.append(time.perf_counter() - start)
	start_time = sorted(times)[len(times) // 2]
	# ( a regression's core dumps go nowhere )
	import resource
	core = resource.getrlimit(resource.RLIMIT_CORE)
	resource.setrlimit(resource.RLIMIT_CORE, (0, core[1]))
	ends = []
	try:
		for _ in range(200):
			proc, _ = multiCMD._spawn(['true'], subprocess.PIPE, subprocess.PIPE)
			time.sleep(random.uniform(0, start_time))
			try:
				os.kill(proc.pid, signal.SIGINT)
			except ProcessLookupError:
				pass
			_, err = proc.communicate(timeout=30)
			ends.append((proc.returncode, err.decode(errors='replace')[:200]))
	finally:
		resource.setrlimit(resource.RLIMIT_CORE, core)
	assert [(code, err) for code, err in ends if code not in (0, -signal.SIGINT) or err] == []


@pytest.mark.skipif(not os.path.isdir('/proc/self/fd'), reason='needs /proc/self/fd')
def test_the_hand_over_pipe_reaches_neither_the_command_nor_the_next_start(launcher):
	before = set(os.listdir('/proc/self/fd'))
	for _ in range(20):
		out = multiCMD.run_command([sys.executable, '-c', 'import os; print(sorted(map(int, os.listdir("/proc/self/fd"))))'], quiet=True)
		# 0, 1, 2 and listdir's own
		assert out == ['[0, 1, 2, 3]']
	assert set(os.listdir('/proc/self/fd')) == before
