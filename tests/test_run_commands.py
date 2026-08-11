import threading
import time

import pytest

from multiCMD import run_commands


def test_batch_stdout():
	assert run_commands(
		[["echo", "hello"], ["echo", "world"]], quiet=True
	) == [["hello"], ["world"]]


def test_dry_run_and_return_codes():
	cmds = [["echo", "hello"], ["echo", "world"]]
	assert run_commands(cmds, dry_run=True, quiet=True) == [[], []]
	assert run_commands(cmds, return_code_only=True, quiet=True) == [0, 0]


def test_return_objects_preserve_order():
	tasks = run_commands(
		[["echo", "hello"], ["false"], ["echo", "world"]],
		return_object=True,
		quiet=True,
	)
	assert [t.returncode for t in tasks] == [0, 1, 0]
	assert tasks[0].stdout == ["hello"]
	assert tasks[2].stdout == ["world"]


def test_parse_expands_commands():
	result = run_commands([["echo", "[0-10]"]], quiet=True, parse=True)
	assert result == [[str(i)] for i in range(11)]


@pytest.mark.slow
def test_timeout_mixed_batch():
	result = run_commands(
		[["sleep", "2"], ["echo", "done"]],
		timeout=0.3,
		quiet=True,
		with_stdErr=True,
		max_threads=2,
	)
	assert "Timeout!" in "\n".join(result[0])
	assert result[1] == ["done"]


@pytest.mark.slow
def test_max_threads_scheduling():
	# Port of legacy test_run_commands_max_threads with slightly tighter sleeps
	cmds = [["sleep", "0.6"], ["sleep", "0.6"], ["sleep", "0.6"]]
	tasks = run_commands(
		cmds,
		wait_for_return=False,
		timeout=0,
		quiet=True,
		return_object=True,
		max_threads=2,
	)
	time.sleep(0.2)
	alive = sum(1 for t in tasks if t.returncode is None)
	assert alive == 3
	deadline = time.time() + 3
	while any(t.returncode is None for t in tasks) and time.time() < deadline:
		time.sleep(0.05)
	assert [t.returncode for t in tasks] == [0, 0, 0]


@pytest.mark.slow
def test_shared_semaphore_bounds_concurrency():
	sem = threading.Semaphore(1)
	# With sem=1, two 0.3s sleeps cannot overlap even if max_threads=2
	t0 = time.time()
	run_commands(
		[["sleep", "0.3"], ["sleep", "0.3"]],
		quiet=True,
		max_threads=2,
		sem=sem,
	)
	elapsed = time.time() - t0
	assert elapsed >= 0.55


def test_raise_error_batch():
	with pytest.raises(Exception, match="Commands failed"):
		run_commands([["true"], ["false"]], quiet=True, raise_error=True)
