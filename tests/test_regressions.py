import time

import pytest

import multiCMD
from multiCMD import int_to_color, run_command


def test_regression_sudo_none_path_no_typeerror():
	"""use_sudo with SUDO_PATH=None must not prepend None into Popen argv."""
	multiCMD.SUDO_PATH = None
	out = run_command(["echo", "ok"], quiet=True, use_sudo=True)
	assert out == ["ok"]


@pytest.mark.slow
def test_regression_stdin_eof_no_hang():
	"""Commands that read stdin must see EOF (DEVNULL), not hang until timeout."""
	t0 = time.time()
	task = run_command(["cat"], quiet=True, return_object=True, timeout=2)
	elapsed = time.time() - t0
	assert elapsed < 1.5
	assert task.returncode == 0
	assert task.stdout == []


def test_regression_int_to_color_no_recursion():
	# Previously could RecursionError / non-deterministic str hashing
	colors = [int_to_color(i) for i in range(0, 5000, 97)]
	assert len(colors) == len(set(range(0, 5000, 97)))
	assert int_to_color(42) == int_to_color(42)
	for rgb in colors:
		assert all(0 <= c <= 255 for c in rgb)


@pytest.mark.slow
def test_regression_concurrent_stdout_stderr_no_race():
	code = (
		"import sys\n"
		"for i in range(200):\n"
		"    print('O', i, flush=True)\n"
		"    print('E', i, file=sys.stderr, flush=True)\n"
	)
	task = run_command(["python3", "-c", code], quiet=True, return_object=True)
	assert task.returncode == 0
	assert len(task.stdout) == 200
	assert len(task.stderr) == 200


@pytest.mark.slow
def test_regression_inactivity_timeout_not_total_runtime():
	"""A chatty process lasting longer than timeout must not be killed for wall time."""
	code = (
		"import time\n"
		"for _ in range(6):\n"
		"    print('tick', flush=True)\n"
		"    time.sleep(0.25)\n"
	)
	task = run_command(
		["python3", "-c", code],
		timeout=0.5,
		quiet=True,
		return_object=True,
	)
	assert task.returncode == 0
	assert not any(line.strip().startswith("Timeout!") for line in task.stderr)
	assert len(task.stdout) == 6
