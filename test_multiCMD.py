import pytest
from multiCMD import _expand_ranges, run_command, run_commands, print_progress_bar, input_with_timeout_and_countdown
import time
import builtins
import select
import sys

# FILE: test_multiCMD.py


def test_expand_ranges():
	assert _expand_ranges("file[1-3].txt") == ["file1.txt", "file2.txt", "file3.txt"]
	assert _expand_ranges("file[a-c].txt") == ["filea.txt", "fileb.txt", "filec.txt"]
	assert _expand_ranges("file[1-2,a-b].txt") == ["file1.txt", "file2.txt", "filea.txt", "fileb.txt"]
	assert _expand_ranges("file[01-03].txt") == ["file01.txt", "file02.txt", "file03.txt"]
	assert _expand_ranges("file[1-3,5].txt") == ["file1.txt", "file2.txt", "file3.txt", "file5.txt"]

def test_run_command():
	result = run_command(["echo", "hello"], quiet=True)
	assert result == ["hello"]

def test_run_command_timeout():
	result = run_command(["sleep", "2"], timeout=1, quiet=True,with_stdErr=False)
	assert "Timeout!" not in result
	result = run_command(["sleep", "2"], timeout=1, quiet=True,with_stdErr=True)
	assert "Timeout!" in '\n'.join(result)

def test_run_command_dry_run():
	result = run_command(["echo", "hello"], dry_run=True, quiet=True)
	assert result == []

def test_run_command_return_code_only():
	result = run_command(["echo", "hello"], return_code_only=True, quiet=True)
	assert result == 0

def test_run_command_return_object():
	result = run_command(["echo", "hello"], return_object=True, quiet=True)
	assert result.returncode == 0
	assert result.stdout == ["hello"]
	assert result.stderr == []

def test_run_command_wait_for_return():
	result = run_command(["echo", "hello"], wait_for_return=True, quiet=True)
	assert result == ["hello"]
	result = run_command(["sleep", "2"], wait_for_return=False, timeout=1, quiet=True,return_object=True)
	assert result.returncode is None
	time.sleep(1.5)
	assert result.returncode is not None


def test_run_commands():
	commands = [["echo", "hello"], ["echo", "world"]]
	result = run_commands(commands, quiet=True)
	assert result == [["hello"], ["world"]]

def test_run_commands_timeout():
	commands = [["sleep", "2"], ["echo", "done"]]
	result = run_commands(commands, timeout=1, quiet=True,with_stdErr=True)
	assert "Timeout!" in '\n'.join(result[0])
	assert result[1] == ["done"]

def test_run_commands_dry_run():
	commands = [["echo", "hello"], ["echo", "world"]]
	result = run_commands(commands, dry_run=True, quiet=True)
	assert result == [[],[]]

def test_run_commands_return_code_only():
	commands = [["echo", "hello"], ["echo", "world"]]
	result = run_commands(commands, return_code_only=True, quiet=True)
	assert result == [0, 0]

def test_run_commands_return_object():
	commands = [["echo", "hello"], ["echo", "world"]]
	result = run_commands(commands, return_object=True, quiet=True)
	assert result[0].returncode == 0
	assert result[0].stdout == ["hello"]
	assert result[0].stderr == []
	assert result[1].returncode == 0
	assert result[1].stdout == ["world"]
	assert result[1].stderr == []

def test_run_commands_wait_for_return():
	commands = [["sleep", "1"], ["sleep", "2"]]
	result = run_commands(commands, wait_for_return=True, quiet=True)
	assert result == [[], []]
	result = run_commands(commands, wait_for_return=False, timeout=1.1, quiet=True,return_object=True)
	assert result[0].returncode is None
	assert result[1].returncode is None
	time.sleep(1.5)
	assert result[0].returncode == 0
	time.sleep(1)
	assert result[1].returncode is not None

def test_run_commands_concurrent():
	commands = [["sleep", "1"], ["sleep", "2"]]
	result = run_commands(commands, wait_for_return=False, timeout=1.1, quiet=True,return_object=True, max_threads=2)
	assert result[0].returncode is None
	assert result[1].returncode is None
	time.sleep(1.5)
	assert result[0].returncode == 0
	assert result[1].returncode is not None

def test_run_commands_max_threads():
	commands = [["sleep", "1"], ["sleep", "2"], ["sleep", "2"]]
	result = run_commands(commands, wait_for_return=False, timeout=1.1, quiet=True,return_object=True, max_threads=2)
	assert result[0].returncode is None
	assert result[1].returncode is None
	assert result[2].returncode is None
	time.sleep(1.5)
	assert result[0].returncode == 0
	assert result[1].returncode is not None
	assert result[2].returncode is None
	time.sleep(1)
	assert result[2].returncode is not None

def test_run_commands_parse():
	commands = [["echo", "[0-10]"]]
	result = run_commands(commands, quiet=True, parse=True)
	assert result == [[str(i)] for i in range(11)]

def test_print_progress_bar(capsys):
	print_progress_bar(5, 10, prefix='Progress:', suffix='Complete')
	captured = capsys.readouterr()
	assert "Progress:" in captured.out
	assert "Complete" in captured.out
	assert "50.0%" in captured.out


def test_input_with_timeout_and_countdown(monkeypatch):

	# Mock input to simulate user input
	def mock_input(prompt=''):
		return "user input"

	# Mock select to simulate no input
	def mock_select(rlist, wlist, xlist, timeout=None):
		return ([], [], [])

	# Test case when user provides input before timeout
	monkeypatch.setattr(builtins, 'input', mock_input)
	monkeypatch.setattr(select, 'select', lambda rlist, wlist, xlist, timeout=None: ([sys.stdin], [], []))
	result = input_with_timeout_and_countdown(5)
	assert result == "user input"

	# Test case when no input is provided before timeout
	monkeypatch.setattr(select, 'select', mock_select)
	result = input_with_timeout_and_countdown(1)
	assert result is None


