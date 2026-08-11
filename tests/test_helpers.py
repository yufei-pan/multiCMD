import builtins
import select
import sys

import pytest

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
	assert "h1" in out
	assert "h2" not in out
	assert "a" in out


def test_parse_table():
	text = "col1  col2\nfoo   bar\n"
	table = parseTable(text)
	assert table[0] == ["col1", "col2"]
	assert table[1][0] == "foo"


def test_slugify_ascii_and_unicode():
	assert slugify("Hello World!") == "hello-world"
	assert slugify("café", allow_unicode=True) == "café"  # runtime: preserves unicode
	assert slugify("café", allow_unicode=False) == "cafe"


def test_format_bytes_roundtrip():
	assert format_bytes(1024, use_1024_bytes=True, to_str=True).lower().startswith("1")
	assert format_bytes("1 KiB", to_int=True) == 1024


def test_int_to_color_deterministic_and_in_range():
	c1 = int_to_color(0x123456)
	c2 = int_to_color(0x123456)
	assert c1 == c2
	assert all(0 <= ch <= 255 for ch in c1)
	# black / bright extremes should not raise
	assert len(int_to_color(0)) == 3
	assert len(int_to_color(0xFFFFFF)) == 3


def test_progress_bar_helpers(capsys):
	s = _genrate_progress_bar(5, 10, prefix="P", suffix="S", columns=80)
	assert "P" in s and "S" in s
	print_progress_bar(5, 10, prefix="Progress:", suffix="Complete")
	captured = capsys.readouterr()
	assert "Progress:" in captured.out
	assert "50.0%" in captured.out or "50%" in captured.out


def test_get_terminal_size_tuple():
	cols, rows = get_terminal_size()
	assert cols > 0 and rows > 0


def test_input_with_timeout_and_countdown(monkeypatch):
	monkeypatch.setattr(builtins, "input", lambda prompt="": "user input")
	monkeypatch.setattr(
		select, "select", lambda r, w, x, timeout=None: ([sys.stdin], [], [])
	)
	assert input_with_timeout_and_countdown(5) == "user input"
	monkeypatch.setattr(select, "select", lambda r, w, x, timeout=None: ([], [], []))
	assert input_with_timeout_and_countdown(1) is None
