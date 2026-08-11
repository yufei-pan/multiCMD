import time

import pytest

import multiCMD
from multiCMD import AsyncExecutor, run_commands


@pytest.mark.slow
def test_executor_run_wait_results_codes():
	ex = AsyncExecutor(max_threads=2, timeout=0, quiet=True)
	ex.run_command(["echo", "a"])
	ex.run_commands([["echo", "b"], ["false"]])
	assert len(ex) == 3
	assert bool(ex)
	ex.wait(timeout=2)
	assert ex.get_return_codes() == [0, 0, 1]
	assert ex.get_results() == [["a"], ["b"], []]


@pytest.mark.slow
def test_executor_join_prints_errors(capsys):
	ex = AsyncExecutor(max_threads=1, quiet=True)
	ex.run_command(["false"])
	ex.join(timeout=2, print_error=True)
	captured = capsys.readouterr()
	assert "failed with return code" in captured.out


@pytest.mark.slow
def test_executor_stop_and_cleanup():
	ex = AsyncExecutor(max_threads=1, quiet=True)
	ex.run_command(["sleep", "5"])
	time.sleep(0.1)
	ex.stop(timeout=3)
	# stop signals task.stop; process should end
	assert all(not t.is_alive() for t in ex.tasks)
	ex.cleanup(timeout=1)
	assert list(ex) == []


@pytest.mark.slow
def test_join_threads_module_helper():
	tasks = run_commands(
		[["sleep", "0.3"]],
		wait_for_return=False,
		return_object=True,
		quiet=True,
	)
	assert tasks[0].returncode is None
	multiCMD.join_threads(timeout=2)
	assert tasks[0].returncode == 0
