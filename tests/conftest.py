import subprocess
import sys
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
