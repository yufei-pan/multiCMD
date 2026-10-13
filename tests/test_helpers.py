import builtins
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
