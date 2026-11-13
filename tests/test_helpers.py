import builtins
import io
import os
import select
import subprocess
import sys
from pathlib import Path

import pytest

import multiCMD
from multiCMD import (
	_genrate_progress_bar,
	format_bytes,
	get_terminal_size,
	input_with_timeout_and_countdown,
	int_to_color,
	parseTable,
	pretty_format_table,
	print_progress_bar,
	slugify,
)


def test_pretty_format_table_basic_and_remove_empty():
	data = [["a", ""], ["b", ""]]
	header = ["h1", "h2"]
	out = pretty_format_table(data, header=header, remove_empty_columns=True)
	assert out == "h1\n--\na \nb \n"


def test_pretty_format_table_pads_wide_characters_by_display_width():
	# East Asian wide characters take two terminal columns, a combining mark none.
	out = pretty_format_table([["name", "v"], ["中文", "1"], ["abcd", "2"], ["e\u0301", "3"]], full=True)
	assert out == "name | v\n-----+--\n中文 | 1\nabcd | 2\ne\u0301    | 3\n"


def test_pretty_format_table_truncates_wide_characters_to_the_terminal(monkeypatch):
	monkeypatch.setattr(multiCMD, "get_terminal_size", lambda: (12, 24))
	out = pretty_format_table([["k", "v"], ["a", "中文中文中文"]])
	assert out == "k|v         \n-+----------\na|中文中文..\n"


def test_pretty_format_table_narrows_a_wide_header_by_display_width(monkeypatch):
	monkeypatch.setattr(multiCMD, "get_terminal_size", lambda: (10, 24))
	out = pretty_format_table([["名前", "v"], ["aaaaaaaaaa", "1"]])
	assert out == "名前    |v\n--------+-\naaaaaa..|1\n"


def test_parse_table():
	text = "col1  col2\nfoo   bar\n"
	table = parseTable(text)
	assert table == [["col1", "col2"], ["foo", "bar"]]


def test_slugify_ascii_and_unicode():
	assert slugify("Hello World!") == "hello-world"
	assert slugify("café", allow_unicode=True) == "café"  # runtime: preserves unicode
	assert slugify("café", allow_unicode=False) == "cafe"


def test_format_bytes_roundtrip():
	assert format_bytes(1024, use_1024_bytes=True, to_str=True) == "1.00 Ki"
	assert format_bytes("1 KiB", to_int=True) == 1024


def test_int_to_color_deterministic_and_in_range():
	assert int_to_color(0x123456) == (36, 103, 171)
	assert int_to_color(0) == (160, 160, 160)
	assert int_to_color(0xFFFFFF) == (220, 220, 220)


def test_progress_bar_helpers(capsys, monkeypatch):
	expected_bar = (
		"P██████████████████████████████████▁__________________________________|50.0% S"
	)
	assert _genrate_progress_bar(5, 10, prefix="P", suffix="S", columns=80) == expected_bar

	monkeypatch.setattr(
		"multiCMD.get_terminal_size", lambda: (80, 24)
	)
	print_progress_bar(5, 10, prefix="Progress:", suffix="Complete")
	captured = capsys.readouterr()
	assert captured.out == (
		"\rProgress: |████████████████████████▄_________________________|50.0% | Complete"
	)


def ascii_stdout(monkeypatch, encoding='ascii', errors='surrogateescape'):
	# Python 3.6 in a C locale ( hpcp's library calls ): an ASCII stdout that takes undecodable file names
	out = io.TextIOWrapper(io.BytesIO(), encoding=encoding, errors=errors)
	monkeypatch.setattr(sys, 'stdout', out)
	monkeypatch.setattr("multiCMD.get_terminal_size", lambda: (80, 24))
	return out


def test_progress_bar_on_a_stdout_that_cannot_write_its_blocks(monkeypatch):
	# 1.61 raised UnicodeEncodeError out of the call, from its fallback, which printed the same bar again
	out = ascii_stdout(monkeypatch)
	print_progress_bar(5, 10, prefix="Progress:", suffix="Complete")
	print_progress_bar(10, 10, prefix="Progress:", suffix="Complete")
	out.flush()
	assert out.buffer.getvalue().decode('ascii') == (
		"\rProgress: |" + "#" * 24 + "=" + "_" * 25 + "|50.0% | Complete"
		"\rProgress: |" + "#" * 48 + "|100.0% | Complete\n"
	)


def test_progress_bar_with_a_name_its_stdout_cannot_write(monkeypatch):
	# a file name with a byte that is not UTF-8 ( decoded with surrogateescape ) on a UTF-8 stdout, which is strict
	out = ascii_stdout(monkeypatch, 'utf-8', 'strict')
	print_progress_bar(3, 0, prefix='dir/caf\udce9', suffix='s')
	out.flush()
	assert out.buffer.getvalue().decode('utf-8') == '\r' + 'dir/caf? | iteration:3 | s'.ljust(80)


def test_progress_bar_in_a_c_locale_python():
	# the real stdout of a Python whose locale has no encoding for the bar ( on 3.7+ only PYTHONIOENCODING makes it ASCII )
	code = ('import sys; sys.path.insert(0, %r); import multiCMD; multiCMD.print_progress_bar(5, 10, "p", "s")'
		% str(Path(multiCMD.__file__).parent))
	env = dict(os.environ, LC_ALL='C', LANG='C', PYTHONIOENCODING='ascii')
	proc = subprocess.run([sys.executable, '-c', code], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=30)
	assert proc.returncode == 0, proc.stderr
	assert proc.stderr == b''
	assert b'#' in proc.stdout and b'|50.0% | s' in proc.stdout


def test_get_terminal_size_tuple():
	cols, rows = get_terminal_size()
	assert cols > 0 and rows > 0


@pytest.mark.skipif(os.name != "posix", reason="needs a pseudo-terminal")
def test_get_terminal_size_reads_columns_from_stdin_when_stdout_is_piped():
	# `cmd | less` on a 200x40 terminal: os.get_terminal_size() fails on the pipe, so the
	# TIOCGWINSZ fallback reads stdin, whose winsize struct holds the rows before the columns.
	import fcntl
	import pty
	import struct
	import termios
	master, slave = pty.openpty()
	try:
		fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 200, 0, 0))
		out = subprocess.run(
			[sys.executable, "-c", "import multiCMD; print(*multiCMD.get_terminal_size()[:2])"],
			stdin=slave, stdout=subprocess.PIPE, cwd=str(Path(__file__).resolve().parents[1]),
			universal_newlines=True, check=True,
		).stdout
	finally:
		os.close(slave)
		os.close(master)
	assert out.split() == ["200", "40"]


def test_input_with_timeout_and_countdown(monkeypatch):
	monkeypatch.setattr(builtins, "input", lambda prompt="": "user input")
	monkeypatch.setattr(
		select, "select", lambda r, w, x, timeout=None: ([sys.stdin], [], [])
	)
	assert input_with_timeout_and_countdown(5) == "user input"
	monkeypatch.setattr(select, "select", lambda r, w, x, timeout=None: ([], [], []))
	assert input_with_timeout_and_countdown(1) is None


def test_input_with_timeout_and_countdown_returns_none_at_the_end_of_stdin(monkeypatch):
	# select() calls a stdin at its end readable, and input() raises EOFError there ( 1.64 let it out )
	def at_its_end(prompt=""):
		raise EOFError
	monkeypatch.setattr(builtins, "input", at_its_end)
	monkeypatch.setattr(select, "select", lambda r, w, x, timeout=None: ([sys.stdin], [], []))
	assert input_with_timeout_and_countdown(5) is None


posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX only")

ASK = (
	"import os, sys, time, multiCMD\n"
	"%s"
	"t = time.time()\n"
	"got = multiCMD.input_with_timeout_and_countdown(5, 'Answer')\n"
	"sys.stdout.write('GOT %%r %%s\\n' %% (got, 'at once' if time.time() - t < 3 else 'late'))\n"
	"sys.stdout.flush()\n"
	"os._exit(0)\n")


def ask(tmp_path, before="", stdin=subprocess.DEVNULL, shell_prefix=None, feed=None):
	# runs the countdown in a program of its own, with real descriptors; returns what the program said on stdout and stderr
	program = tmp_path / "ask.py"
	program.write_text(ASK % before)
	argv = [sys.executable, str(program)]
	if shell_prefix:
		argv = ["sh", "-c", 'exec "$0" "$@" ' + shell_prefix] + argv
	proc = subprocess.Popen(argv, stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
		env=dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1])))
	try:
		if feed is not None:
			feed()
		out, err = proc.communicate(timeout=30)
	finally:
		if proc.poll() is None:
			proc.kill()
			proc.wait()
	return proc.returncode, out.decode(), err.decode()


@posix_only
def test_countdown_at_the_end_of_stdin_returns_none_at_once_for_dev_null(tmp_path):
	assert ask(tmp_path)[:2] == (0, "GOT None at once\n")


@posix_only
def test_countdown_at_the_end_of_stdin_returns_none_at_once_for_a_pipe_that_ended(tmp_path):
	proc_stdin = os.pipe()
	os.close(proc_stdin[1])
	try:
		assert ask(tmp_path, stdin=proc_stdin[0])[:2] == (0, "GOT None at once\n")
	finally:
		os.close(proc_stdin[0])


@posix_only
def test_countdown_with_a_closed_stdin_returns_none_at_once(tmp_path):
	# `prog <&-`: Python has no sys.stdin at all
	assert ask(tmp_path, shell_prefix="<&-")[:2] == (0, "GOT None at once\n")


@posix_only
@pytest.mark.parametrize("before", ["sys.stdin.close()\n", "os.close(0)\n"], ids=["sys.stdin closed", "descriptor 0 closed"])
def test_countdown_whose_stdin_the_program_closed_returns_none_at_once(tmp_path, before):
	# a pipe that stays open, so that nothing but the close ends it
	read_end, write_end = os.pipe()
	try:
		assert ask(tmp_path, before=before, stdin=read_end)[:2] == (0, "GOT None at once\n")
	finally:
		os.close(read_end)
		os.close(write_end)


@posix_only
def test_countdown_returns_none_at_once_for_ctrl_d_at_a_terminal(tmp_path):
	import pty
	master, slave = pty.openpty()
	try:
		# Ctrl-D ( the terminal's EOF character ) typed at the prompt
		got = ask(tmp_path, stdin=slave, feed=lambda: os.write(master, b"\x04"))
	finally:
		os.close(slave)
		os.close(master)
	assert got[:2] == (0, "GOT None at once\n")
	assert "Answer [5s]: " in got[2]


@posix_only
def test_countdown_still_returns_what_was_typed_and_still_times_out(tmp_path):
	read_end, write_end = os.pipe()
	try:
		os.write(write_end, b" yes \n")
		assert ask(tmp_path, stdin=read_end)[:2] == (0, "GOT 'yes' at once\n")
	finally:
		os.close(read_end)
		os.close(write_end)
