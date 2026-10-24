import time

import pytest

from multiCMD import run_command


def test_echo_stdout_list():
	assert run_command(["echo", "hello"], quiet=True) == ["hello"]


def test_string_command_form():
	assert run_command("echo hello", quiet=True) == ["hello"]


def test_dry_run_empty():
	assert run_command(["echo", "hello"], dry_run=True, quiet=True) == []


def test_return_code_only_success_and_failure():
	assert run_command(["echo", "hello"], return_code_only=True, quiet=True) == 0
	assert run_command(["false"], return_code_only=True, quiet=True) == 1


def test_return_object():
	task = run_command(["echo", "hello"], return_object=True, quiet=True)
	assert task.returncode == 0
	assert task.stdout == ["hello"]
	assert task.stderr == []


def test_with_stderr_merged():
	# python writes to stderr
	out = run_command(
		["python3", "-c", "import sys; sys.stderr.write('e\\n'); print('o')"],
		quiet=True,
		with_stdErr=True,
	)
	assert "o" in out
	assert "e" in out


@pytest.mark.slow
def test_timeout_silent_command():
	result = run_command(["sleep", "2"], timeout=0.3, quiet=True, with_stdErr=False)
	assert "Timeout!" not in result
	result = run_command(["sleep", "2"], timeout=0.3, quiet=True, with_stdErr=True)
	assert "Timeout!" in "\n".join(result)
	task = run_command(
		["sleep", "2"], timeout=0.3, quiet=True, return_object=True
	)
	assert task.returncode == 124
	assert task.stderr and task.stderr[-1].strip().startswith("Timeout!")


@pytest.mark.slow
def test_timeout_returncode_preserved_after_sigint_handler_stderr():
	task = run_command(
		[
			"python3",
			"-c",
			"import signal, sys, time\n"
			"def handler(sig, frame):\n"
			"    sys.stderr.write('handled\\n')\n"
			"    sys.stderr.flush()\n"
			"    sys.exit(0)\n"
			"signal.signal(signal.SIGINT, handler)\n"
			"time.sleep(10)\n",
		],
		timeout=0.3,
		quiet=True,
		return_object=True,
	)
	assert task.returncode == 124
	assert "Timeout!" in task.stderr
	assert "handled" in task.stderr
	assert task.stderr[-1].strip() == "handled"


@pytest.mark.slow
@pytest.mark.parametrize("progress, expected", [
	# a progress line the command was redrawing when it stalled: the reader's last line replaces its own, not 'Timeout!'
	("\\r12345 bytes copied", ["12345 bytes copied", "Timeout!"]),
	("\\r1 bytes\\r12 bytes\\r123 bytes", ["123 bytes", "Timeout!"]),
	("line\\r12345 bytes copied", ["12345 bytes copied", "Timeout!"]),
	("\\r12345 bytes copied\\r", ["12345 bytes copied", "Timeout!"]),
])
def test_timeout_marker_kept_with_progress_line_on_stderr(progress, expected):
	task = run_command(
		["sh", "-c", "printf '%s' >&2; exec sleep 10" % progress],
		timeout=0.5,
		quiet=True,
		return_object=True,
	)
	assert task.returncode == 124
	assert "Timeout!" in task.stderr
	assert task.stderr == expected


def test_progress_lines_on_stderr_recorded_as_before():
	task = run_command(
		["python3", "-c", "import sys\n"
			"sys.stderr.write('\\r1 bytes\\r12 bytes\\r123 bytes\\ndone\\n')\n"
			"sys.stderr.flush()\n"],
		quiet=True,
		return_object=True,
	)
	assert task.returncode == 0
	assert task.stderr == ["123 bytes", "done"]


@pytest.mark.slow
def test_wait_for_return_false():
	task = run_command(
		["sleep", "0.4"],
		wait_for_return=False,
		quiet=True,
		return_object=True,
	)
	assert task.returncode is None
	deadline = time.time() + 2
	while task.returncode is None and time.time() < deadline:
		time.sleep(0.05)
	assert task.returncode == 0


def test_raise_error_on_failure():
	with pytest.raises(Exception, match="Commands failed"):
		run_command(["false"], quiet=True, raise_error=True)
