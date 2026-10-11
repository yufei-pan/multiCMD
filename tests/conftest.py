import os
import subprocess
import sys
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
	saved_vars = dict(multiCMD.__variables)
	yield
	multiCMD.USE_SUDO = saved_use_sudo
	multiCMD.SUDO_PATH = saved_sudo_path
	multiCMD.__variables.clear()
	multiCMD.__variables.update(saved_vars)
	multiCMD.join_threads(timeout=2)


def run_cli(args, timeout=30, check=False):
	return subprocess.run(
		[sys.executable, str(MULTICMD_PY), *args],
		capture_output=True,
		text=True,
		timeout=timeout,
		check=check,
	)


def has_sudo_binary():
	return bool(multiCMD.SUDO_PATH)


def loopback_ping_works():
	try:
		r = subprocess.run(
			["ping", "-c", "1", "-W", "1", "127.0.0.1"],
			capture_output=True,
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
