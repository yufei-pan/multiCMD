import contextlib
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import multiCMD

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
MULTICMD_PY = PACKAGE_ROOT / "multiCMD.py"


@pytest.fixture(autouse=True)
def restore_module_globals():
	saved_use_sudo = multiCMD.USE_SUDO
	saved_sudo_path = multiCMD.SUDO_PATH
	yield
	multiCMD.USE_SUDO = saved_use_sudo
	multiCMD.SUDO_PATH = saved_sudo_path
	# ( a test that runs the exit hook leaves it set )
	multiCMD._exiting = False
	multiCMD.join_threads(timeout=2)


def run_cli(args, timeout=30, check=False, env=None):
	return subprocess.run(
		[sys.executable, str(MULTICMD_PY), *args],
		stdout=subprocess.PIPE,
		stderr=subprocess.PIPE,
		universal_newlines=True,
		timeout=timeout,
		check=check,
		env=env,
	)


def has_sudo_binary():
	return bool(multiCMD.SUDO_PATH)


def loopback_ping_works():
	try:
		r = subprocess.run(
			["ping", "-c", "1", "-W", "1", "127.0.0.1"],
			stdout=subprocess.PIPE,
			stderr=subprocess.PIPE,
			timeout=5,
		)
		return r.returncode == 0
	except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
		return False


@pytest.fixture(params=['native', 'wrapper'])
def launcher(request, monkeypatch):
	'''Run a test once with Popen(process_group=0) ( Python 3.11+ ) and once with the exec wrapper'''
	if request.param == 'native':
		if sys.version_info < (3, 11):
			pytest.skip('Popen(process_group=) needs Python 3.11+')
		monkeypatch.setattr(multiCMD, '_HAS_PROCESS_GROUP', True)
	else:
		monkeypatch.setattr(multiCMD, '_HAS_PROCESS_GROUP', False)
	return request.param


@contextlib.contextmanager
def sigint_main_after(*delays):
	'''
	Send SIGINT to the main thread after each delay, in seconds from now: a real Ctrl-C, which interrupts a join()
	blocked in C ( _thread.interrupt_main() does not ). The body must end with the KeyboardInterrupt, raised on:
	on the way out no further SIGINT is sent ( one sent meanwhile is taken here, not by a later test or pytest ),
	and the test fails if the body returned first or ended before every SIGINT was sent.
	'''
	main = threading.main_thread().ident
	lock = threading.Lock()
	state = {'cancelled': False, 'sent': 0}
	cancelled = threading.Event()

	def send():
		start = time.monotonic()
		for delay in delays:
			if cancelled.wait(max(0, start + delay - time.monotonic())):
				return
			with lock:
				if state['cancelled']:
					return
				state['sent'] += 1
				signal.pthread_kill(main, signal.SIGINT)

	timer = threading.Thread(target=send, daemon=True)
	timer.start()
	outcome = 'returned'
	try:
		yield
	except KeyboardInterrupt:
		outcome = 'interrupted'
		raise
	finally:
		sent_by_then, stray = state['sent'], 0
		while True:
			try:
				with lock:
					state['cancelled'] = True
				cancelled.set()
				timer.join()
				break
			except KeyboardInterrupt:
				# sent while the body was ending: it is taken here
				stray += 1
		if outcome == 'returned':
			pytest.fail(f'the call returned before the SIGINT at {delays[sent_by_then] if sent_by_then < len(delays) else delays[-1]}s')
		if sent_by_then < len(delays) or stray:
			pytest.fail(f'the call ended after {sent_by_then} of the {len(delays)} SIGINTs ( at {delays}s )')


def pid_gone(pid, wait=3.0):
	'''Whether a process has exited within wait seconds ( a zombie counts as exited )'''
	deadline = time.monotonic() + wait
	while True:
		try:
			os.kill(pid, 0)
		except ProcessLookupError:
			return True
		try:
			with open(f'/proc/{pid}/stat') as f:
				if f.read().rsplit(')', 1)[1].split()[0] == 'Z':
					return True
		except OSError:
			return True
		if time.monotonic() > deadline:
			return False
		time.sleep(0.05)


@pytest.fixture(autouse=True)
def fresh_display(monkeypatch):
	'''Every test starts with a display that has written nothing yet'''
	monkeypatch.setattr(multiCMD, '_display', multiCMD._Display())
