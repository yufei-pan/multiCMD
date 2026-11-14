import builtins
import io
import os
import re
import select
import subprocess
import sys
import time
from pathlib import Path

import pytest

import multiCMD
from conftest import fake_select
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


# 1.66: text after the size, and an i with no prefix, are not a size either ( 1.65 read the start: '5Gb/s' was 5 G, '1e9' 1 E )
@pytest.mark.parametrize("size", ["5 foo", "5foo", "5 fooi", "5 KiBx", "1.5 gigs", "5 bytes", "1e9", "1,5", "1_000", "5Gb/s",
	"5 kB extra", "5 i", "5 iB", "5 kbb", "5 G B", "5..", "-5", ".5", "", " "])
@pytest.mark.parametrize("kwargs", [{"to_int": True}, {}, {"use_1024_bytes": True}, {"to_int": True, "use_1024_bytes": False}],
	ids=["to_int", "no flags", "1024", "to_int 1000"])
def test_format_bytes_a_unit_it_does_not_know_is_an_invalid_size(capsys, size, kwargs):
	# 1.64 returned None, silently, and int() of it raised TypeError; a size it cannot read at all already gave 0 and a line
	got = format_bytes(size, **kwargs)
	assert got == 0 and isinstance(got, int)
	out, err = capsys.readouterr()
	assert out == ""
	assert "Invalid size format" in err and "Got: " + size in err


@pytest.mark.parametrize("kwargs", [{"to_str": True}, {"to_str": True, "to_int": True}], ids=["to_str", "to_str to_int"])
@pytest.mark.parametrize("size", ["5 foo", "5Gb/s"])
def test_format_bytes_to_str_hands_back_a_size_it_does_not_know_the_unit_of(capsys, kwargs, size):
	# unchanged: the string asked to be shown comes back as it came, with nothing said
	assert format_bytes(size, **kwargs) == size
	assert capsys.readouterr() == ("", "")


def test_format_bytes_reads_a_long_run_of_blanks_in_one_pass():
	# ( a pattern with a \s* at each end retries every split of the run: 6.8 s for 40,000 blanks, quadratic, so minutes here )
	start = time.monotonic()
	assert format_bytes("5" + " " * 200000 + "x", to_int=True) == 0
	assert time.monotonic() - start < 5


@pytest.mark.parametrize("size, kwargs, expected", [
	("5", {}, 5), ("5 B", {"to_int": True}, 5), ("2.5 kb", {"to_int": True}, 2500), ("2.5 KiB", {}, 2560),
	("1 M", {"use_1024_bytes": True}, 1048576), ("3gib", {"to_int": True}, 3 * 1024 ** 3), ("1 T", {}, 10 ** 12),
	(7, {"to_int": True}, 7), (7.9, {"to_int": True}, 7),
	# a point at the end, and blanks around ( new in 1.66 before the number )
	("5.", {"to_int": True}, 5), ("5 KiB  ", {}, 5120), ("  5 KiB", {}, 5120), ("\t2.5 G\n", {"to_int": True}, 2500000000),
	("5KIB", {}, 5120), ("5 b", {}, 5)], ids=str)
def test_format_bytes_reads_every_unit_it_knows(capsys, size, kwargs, expected):
	assert format_bytes(size, **kwargs) == expected
	assert capsys.readouterr() == ("", "")


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


@pytest.fixture
def pipe_stdin(monkeypatch):
	# a real pipe as sys.stdin; returns its write end ( closed after the test, as is the read end )
	read_end, write_end = os.pipe()
	stdin = io.open(read_end, "r")
	monkeypatch.setattr(sys, "stdin", stdin)
	monkeypatch.setitem(sys.modules, "msvcrt", None)  # the select() path, on any platform
	yield write_end
	stdin.close()
	try:
		os.close(write_end)
	except OSError:
		pass


def test_input_with_timeout_and_countdown(pipe_stdin, fake_clock, monkeypatch):
	os.write(pipe_stdin, b" user input \n")
	monkeypatch.setattr(select, "select", fake_select(fake_clock, lambda clock: True))
	assert input_with_timeout_and_countdown(5) == "user input"
	assert fake_clock.now == 1000.0
	monkeypatch.setattr(select, "select", fake_select(fake_clock, lambda clock: False))
	assert input_with_timeout_and_countdown(1) is None
	assert fake_clock.now == 1001.0


def test_countdown_at_the_end_of_stdin_asks_again_each_second_and_runs_out(pipe_stdin, fake_clock, monkeypatch, capsys):
	# select() calls a stdin at its end readable for ever ( 1.64 let input()'s EOFError out ). Ctrl-D counts as no answer: the
	# countdown goes on to its end, so the default applies then and a Ctrl-C can still abort, and stdin is asked again once a
	# second and once more at the end, as a terminal can still answer after a Ctrl-D ( the second 1.65 build stopped asking )
	os.close(pipe_stdin)
	monkeypatch.setattr(select, "select", fake_select(fake_clock, lambda clock: True))
	assert input_with_timeout_and_countdown(3, "Answer") is None
	assert fake_clock.waited == [1, 1, 1, 0]
	assert fake_clock.slept == [1, 1, 1]
	assert fake_clock.now == 1003.0
	assert capsys.readouterr().err == "Answer [3s]: \rAnswer [3s]: \rAnswer [2s]: \rAnswer [1s]: "


def test_countdown_at_the_end_of_stdin_lets_a_ctrl_c_abort_it(pipe_stdin, fake_clock, monkeypatch):
	os.close(pipe_stdin)
	def interrupted(seconds):
		raise KeyboardInterrupt
	monkeypatch.setattr(select, "select", fake_select(fake_clock, lambda clock: True))
	monkeypatch.setattr(fake_clock, "sleep", interrupted)
	with pytest.raises(KeyboardInterrupt):
		input_with_timeout_and_countdown(60)


def test_countdown_lets_a_ctrl_c_abort_its_wait(pipe_stdin, fake_clock, monkeypatch):
	def interrupted(r, w, x, timeout=None):
		raise KeyboardInterrupt
	monkeypatch.setattr(select, "select", interrupted)
	with pytest.raises(KeyboardInterrupt):
		input_with_timeout_and_countdown(60)


def test_countdown_takes_an_answer_typed_in_its_last_second(pipe_stdin, fake_clock, monkeypatch):
	# stdin is asked once more when the countdown ends: a line that is there by then is the answer, all of it ( 1.64 and the
	# first three 1.65 builds returned None )
	os.write(pipe_stdin, b" n \n")
	monkeypatch.setattr(select, "select", fake_select(fake_clock, lambda clock: clock.now >= 1003.0))
	assert input_with_timeout_and_countdown(3, "Answer") == "n"
	assert fake_clock.waited == [1, 1, 1] + [0] * 4 and fake_clock.slept == []


def test_countdown_returns_at_its_end_with_a_line_not_ended(pipe_stdin, fake_clock, monkeypatch):
	# text with no Enter ( typed, then Ctrl-D; a pipe that sent part of a line and stays open ) is no answer: the default
	# applies at the end ( 1.65 waited in input() for the line to end, past the timeout, and returned the text )
	os.write(pipe_stdin, b"ye")
	real_select = select.select
	monkeypatch.setattr(select, "select", fake_select(fake_clock, lambda clock: bool(real_select([sys.stdin], [], [], 0)[0])))
	assert input_with_timeout_and_countdown(3, "Answer") is None
	assert fake_clock.now == 1003.0 and fake_clock.slept == []
	# its text was read: what comes next is a line of its own
	os.write(pipe_stdin, b"s\n")
	assert sys.stdin.readline() == "s\n"


@pytest.mark.skipif(not os.path.exists("/dev/zero"), reason="needs /dev/zero")
def test_countdown_stops_reading_an_endless_stdin_at_its_end(fake_clock, monkeypatch):
	# a stdin that never ends a line ( /dev/zero ): read up to the end, and past it only as much as a terminal line holds
	# ( 1.65 read it in input() until memory ran out )
	monkeypatch.setitem(sys.modules, "msvcrt", None)
	def a_byte_each_quarter_second(clock):
		clock.now += 0.25
		return True
	monkeypatch.setattr(select, "select", fake_select(fake_clock, a_byte_each_quarter_second))
	with open("/dev/zero") as zero:
		monkeypatch.setattr(sys, "stdin", zero)
		assert input_with_timeout_and_countdown(1, "Answer") is None
	assert fake_clock.waited[:4] == [1.0, 0.75, 0.5, 0.25]
	assert fake_clock.waited[4:] == [0] * 4096


@pytest.mark.parametrize("timeout, frames", [(2.5, ["3", "3", "2", "1"]), (0.5, ["1", "1"]), (3.0, ["3", "3", "2", "1"])],
	ids=["2.5", "0.5", "3.0"])
def test_countdown_takes_a_float_timeout(pipe_stdin, fake_clock, monkeypatch, capsys, timeout, frames):
	# by the clock: it returns when the timeout has run out, and shows the seconds left rounded up ( 1.65: TypeError )
	monkeypatch.setattr(select, "select", fake_select(fake_clock, lambda clock: False))
	assert input_with_timeout_and_countdown(timeout, "Answer") is None
	assert fake_clock.now == 1000.0 + timeout
	err = capsys.readouterr().err
	assert err == "\r".join("Answer [%ss]: " % seconds for seconds in frames)


def test_countdown_says_so_when_select_cannot_watch_stdin(monkeypatch):
	# a stdin with no descriptor ( a StringIO ) was not asked: not "no answer", and its line is not dropped in silence ( as 1.64 )
	monkeypatch.setitem(sys.modules, "msvcrt", None)
	monkeypatch.setattr(sys, "stdin", io.StringIO("yes\n"))
	monkeypatch.setattr(multiCMD.time, "sleep", lambda seconds: None)
	with pytest.raises(io.UnsupportedOperation):
		input_with_timeout_and_countdown(3)


posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX only")

# the countdown ( of 1 s unless said ), in a program of its own: what it returned, and how long it took
ASK = (
	"import os, sys, time, multiCMD\n"
	"%s"
	"t = time.monotonic()\n"
	"got = multiCMD.input_with_timeout_and_countdown(%r, 'Answer')\n"
	"sys.stdout.write('GOT %%r %%.2f\\n' %% (got, time.monotonic() - t))\n"
	"sys.stdout.flush()\n"
	"os._exit(0)\n")


def ask(tmp_path, before="", stdin=subprocess.DEVNULL, shell_prefix=None, feed=None, timeout=1):
	# runs the countdown in a program of its own, with real descriptors; returns its exit status, what it said on stdout (the
	# answer and the seconds it took) and what it wrote on stderr
	program = tmp_path / "ask.py"
	program.write_text(ASK % (before, timeout))
	argv = [sys.executable, str(program)]
	if shell_prefix:
		argv = ["sh", "-c", 'exec "$0" "$@" ' + shell_prefix] + argv
	proc = subprocess.Popen(argv, stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
		env=dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1])))
	try:
		if feed is not None:
			feed(proc)
		out, err = proc.communicate(timeout=30)
	finally:
		if proc.poll() is None:
			proc.kill()
			proc.wait()
	return proc.returncode, out.decode(), err.decode()


def answered_none_after_the_timeout(got, timeout=1):
	# None, and not sooner than the countdown of 1 s ( never at once ); the upper bound only catches a wait that is not the countdown
	status, out, err = got
	words = out.split()
	assert status == 0 and words[:2] == ["GOT", "None"], got
	assert timeout <= float(words[2]) < 6.0, got
	assert "Answer [1s]: " in err, got
	return True


@posix_only
def test_countdown_at_the_end_of_stdin_runs_out_for_dev_null(tmp_path):
	assert answered_none_after_the_timeout(ask(tmp_path))


@posix_only
def test_countdown_at_the_end_of_stdin_runs_out_for_a_pipe_that_ended(tmp_path):
	read_end, write_end = os.pipe()
	os.close(write_end)
	try:
		assert answered_none_after_the_timeout(ask(tmp_path, stdin=read_end))
	finally:
		os.close(read_end)


@posix_only
def test_countdown_with_a_closed_stdin_runs_out(tmp_path):
	# `prog <&-`: Python has no sys.stdin at all
	assert answered_none_after_the_timeout(ask(tmp_path, shell_prefix="<&-"))


@posix_only
@pytest.mark.parametrize("before", ["sys.stdin.close()\n", "os.close(0)\n"], ids=["sys.stdin closed", "descriptor 0 closed"])
def test_countdown_whose_stdin_the_program_closed_runs_out(tmp_path, before):
	# a pipe that stays open, so that nothing but the close ends it
	read_end, write_end = os.pipe()
	try:
		assert answered_none_after_the_timeout(ask(tmp_path, before=before, stdin=read_end))
	finally:
		os.close(read_end)
		os.close(write_end)


@posix_only
def test_countdown_runs_out_after_ctrl_d_at_a_terminal(tmp_path):
	import pty
	master, slave = pty.openpty()
	try:
		# Ctrl-D ( the terminal's EOF character ) typed at the prompt
		got = ask(tmp_path, stdin=slave, feed=lambda proc: os.write(master, b"\x04"))
	finally:
		os.close(slave)
		os.close(master)
	assert answered_none_after_the_timeout(got)


@posix_only
def test_countdown_takes_an_answer_typed_after_ctrl_d_at_a_terminal(tmp_path):
	# a terminal's Ctrl-D ends nothing: the countdown is still asking, and "n" typed after it is the answer, at the next second,
	# not the default at the end ( the second 1.65 build stopped reading: None after the 3 s, and the "n" left for the shell )
	import pty
	master, slave = pty.openpty()
	try:
		status, out, err = ask(tmp_path, stdin=slave, timeout=3, feed=lambda proc: os.write(master, b"\x04n\n"))
	finally:
		os.close(slave)
		os.close(master)
	assert status == 0 and out.split()[:2] == ["GOT", "'n'"], (status, out, err)
	assert 1.0 <= float(out.split()[2]) < 3.0, out


def read_until(fd, marker, seconds=20):
	# reads fd until marker has come ( the countdown has printed its last second ), or until it stays quiet once marker is None
	seen, deadline = b"", time.monotonic() + seconds
	while time.monotonic() < deadline and (marker is None or marker not in seen):
		if select.select([fd], [], [], 0.2)[0]:
			seen += os.read(fd, 4096)
		elif marker is None:
			break
	assert marker is None or marker in seen, seen
	return seen


@posix_only
def test_countdown_takes_a_line_sent_in_its_last_second(tmp_path):
	# a pipe that stays open: "n" sent once the last second has begun is the answer, at the end ( 1.64 and 981bbc2: None )
	read_end, write_end = os.pipe()
	def in_the_last_second(proc):
		read_until(proc.stderr.fileno(), b"Answer [1s]: ")
		os.write(write_end, b"n\n")
	try:
		status, out, err = ask(tmp_path, stdin=read_end, timeout=2, feed=in_the_last_second)
	finally:
		os.close(read_end)
		os.close(write_end)
	assert status == 0 and out.split()[:2] == ["GOT", "'n'"], (status, out, err)
	assert 1.0 <= float(out.split()[2]) < 6.0, out


@posix_only
def test_countdown_at_a_terminal_takes_an_answer_typed_after_ctrl_d_in_its_last_second(tmp_path):
	# stdin, stdout and stderr all on the terminal, as at a shell, where input() reads through C stdio: Ctrl-D, then "n" typed
	# once the last second has begun, is the answer
	import pty
	master, slave = pty.openpty()
	program = tmp_path / "ask.py"
	program.write_text(ASK % ("", 2))
	proc = subprocess.Popen([sys.executable, str(program)], stdin=slave, stdout=slave, stderr=slave,
		env=dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1])))
	try:
		os.write(master, b"\x04")
		read_until(master, b"Answer [1s]: ")
		os.write(master, b"n\n")
		proc.wait(30)
		# its answer line is on the terminal: the program has ended, so what it wrote is all there to read
		got = re.search(rb"GOT (\S+) ([0-9.]+)", read_until(master, None))
	finally:
		if proc.poll() is None:
			proc.kill()
			proc.wait()
		os.close(slave)
		os.close(master)
	assert got and got.group(1) == b"'n'", got
	assert 1.0 <= float(got.group(2)) < 6.0, got


@posix_only
def test_countdown_still_returns_what_was_typed_at_once(tmp_path):
	read_end, write_end = os.pipe()
	try:
		os.write(write_end, b" yes \n")
		status, out, err = ask(tmp_path, stdin=read_end)
	finally:
		os.close(read_end)
		os.close(write_end)
	assert status == 0 and out.split()[:2] == ["GOT", "'yes'"] and float(out.split()[2]) < 1.0


# --- 1.66: the countdown returns at its timeout whatever stdin holds ( 1.65 waited in input() for a line to end )

def answer_after(proc, write, seconds=8):
	# the program's answer is due at its timeout ( 1 s ): give it 8 s, past the 6 s that answered_none_after_the_timeout allows,
	# then end the line, so a countdown that waits for one ( 1.65 ) returns with it instead of hanging the test
	try:
		proc.wait(seconds)
	except subprocess.TimeoutExpired:
		pass
	write()


@posix_only
def test_countdown_at_a_terminal_returns_at_its_end_after_text_and_ctrl_d(tmp_path):
	# "ye" typed, then Ctrl-D: the terminal hands over the text with no Enter; the line is not ended, so it is no answer
	import pty
	master, slave = pty.openpty()
	def feed(proc):
		os.write(master, b"ye\x04")
		answer_after(proc, lambda: os.write(master, b"s\n"))
	try:
		got = ask(tmp_path, stdin=slave, feed=feed)
	finally:
		os.close(slave)
		os.close(master)
	assert answered_none_after_the_timeout(got)


@posix_only
def test_countdown_at_a_terminal_with_readline_loaded_returns_at_its_end_after_ctrl_d(tmp_path):
	# all three streams on the terminal, as at a shell, in a program that loaded readline: 1.65's input() went through readline,
	# which took no Ctrl-D on an empty line for an end, and waited for the "n" typed after the timeout
	pytest.importorskip("readline")
	import pty
	master, slave = pty.openpty()
	program = tmp_path / "ask.py"
	program.write_text(ASK % ("import readline\n", 1))
	proc = subprocess.Popen([sys.executable, str(program)], stdin=slave, stdout=slave, stderr=slave,
		env=dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1])))
	try:
		read_until(master, b"Answer [1s]: ")
		os.write(master, b"\x04")
		answer_after(proc, lambda: os.write(master, b"n\n"))
		proc.wait(30)
		got = re.search(rb"GOT (\S+) ([0-9.]+)", read_until(master, None))
	finally:
		if proc.poll() is None:
			proc.kill()
			proc.wait()
		os.close(slave)
		os.close(master)
	assert got and got.group(1) == b"None", got
	assert 1.0 <= float(got.group(2)) < 6.0, got


@posix_only
def test_countdown_returns_at_its_end_when_a_pipe_sent_part_of_a_line(tmp_path):
	read_end, write_end = os.pipe()
	def feed(proc):
		os.write(write_end, b"ye")
		answer_after(proc, lambda: os.write(write_end, b"s\n"))
	try:
		got = ask(tmp_path, stdin=read_end, feed=feed)
	finally:
		os.close(read_end)
		os.close(write_end)
	assert answered_none_after_the_timeout(got)


@posix_only
def test_countdown_takes_a_float_timeout_on_real_descriptors(tmp_path):
	assert answered_none_after_the_timeout(ask(tmp_path, timeout=1.5), timeout=1.5)


@posix_only
@pytest.mark.skipif(not os.path.exists("/dev/zero"), reason="needs /dev/zero")
def test_countdown_returns_at_its_end_from_an_endless_stdin(tmp_path):
	# /dev/zero never ends a line. The memory limit makes a countdown that reads it whole ( 1.65 ) fail fast, not fill memory
	limit = ("import resource\ntry:\n\tresource.setrlimit(resource.RLIMIT_AS, (1 << 31, 1 << 31))\n"
		"except (ValueError, OSError):\n\tpass\n")
	with open("/dev/zero", "rb") as zero:
		assert answered_none_after_the_timeout(ask(tmp_path, before=limit, stdin=zero))


@posix_only
def test_countdown_leaves_what_follows_the_line_in_stdin(tmp_path):
	# it reads stdin up to the end of the answer and no further: the next line is for whoever reads stdin next ( a shell's
	# `while read`; 1.65's input() read ahead into the program's own buffer )
	read_end, write_end = os.pipe()
	try:
		os.write(write_end, b" yes \nnext\n")
		os.close(write_end)
		status, out, err = ask(tmp_path, stdin=read_end)
		left = os.read(read_end, 100)
	finally:
		os.close(read_end)
	assert status == 0 and out.split()[:2] == ["GOT", "'yes'"], (status, out, err)
	assert left == b"next\n"


@posix_only
def test_countdown_takes_what_the_program_read_ahead_of_an_ended_pipe(tmp_path):
	# the program read a line of stdin itself, and Python read the rest of the pipe ahead: its next line is the answer, as in 1.65
	read_end, write_end = os.pipe()
	try:
		os.write(write_end, b"a\ny\nz\n")
		os.close(write_end)
		status, out, err = ask(tmp_path, before="sys.stdin.readline()\n", stdin=read_end)
	finally:
		os.close(read_end)
	assert status == 0 and out.split()[:2] == ["GOT", "'y'"], (status, out, err)


@posix_only
def test_countdown_takes_text_that_the_end_of_input_ends(tmp_path):
	# a last line with no newline, at the end of a pipe, is a line, as input() read it
	read_end, write_end = os.pipe()
	try:
		os.write(write_end, b"y")
		os.close(write_end)
		status, out, err = ask(tmp_path, stdin=read_end)
	finally:
		os.close(read_end)
	assert status == 0 and out.split()[:2] == ["GOT", "'y'"], (status, out, err)


@posix_only
def test_countdown_at_a_terminal_takes_text_ended_by_ctrl_d_twice(tmp_path):
	# at a terminal, a Ctrl-D after text hands it over, and a second one is the end of the input: that ends the line
	import pty
	master, slave = pty.openpty()
	try:
		status, out, err = ask(tmp_path, stdin=slave, timeout=3, feed=lambda proc: os.write(master, b"ye\x04\x04"))
	finally:
		os.close(slave)
		os.close(master)
	assert status == 0 and out.split()[:2] == ["GOT", "'ye'"], (status, out, err)
	assert float(out.split()[2]) < 3.0, out


@posix_only
def test_countdown_on_real_descriptors_lets_a_ctrl_c_abort_it(tmp_path):
	import signal
	read_end, write_end = os.pipe()
	def interrupt(proc):
		read_until(proc.stderr.fileno(), b"Answer [5s]: ")
		proc.send_signal(signal.SIGINT)
	try:
		status, out, err = ask(tmp_path, stdin=read_end, timeout=5, feed=interrupt)
	finally:
		os.close(read_end)
		os.close(write_end)
	assert status != 0 and "GOT" not in out and "KeyboardInterrupt" in err, (status, out, err)
