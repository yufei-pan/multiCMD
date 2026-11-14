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
	"5 kB extra", "5 i", "5 iB", "5 kbb", "5 G B", "5..", "-5", ".5", "", " ",
	# letters that only fold onto a unit's ( a dotless and a dotted Turkish i, the Kelvin sign )
	"5 k\u0131", "5 K\u0130B", "5 g\u0131b", "5 \u212a"])
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


@pytest.fixture
def terminal(monkeypatch):
	# a pseudo-terminal, in its line mode, as sys.stdin ( text; keyword arguments go to io.open ): terminal() returns its master
	# end, which the "user" types on. Both ends are closed after the test
	import pty
	opened = []
	def make(**kwargs):
		master, slave = pty.openpty()
		stdin = io.open(slave, "r", **kwargs)
		opened.extend([stdin, master])
		monkeypatch.setattr(sys, "stdin", stdin)
		monkeypatch.setitem(sys.modules, "msvcrt", None)  # the select() path, on any platform
		return master
	yield make
	for each in opened:
		try:
			each.close() if hasattr(each, "close") else os.close(each)
		except OSError:
			pass


def no_input(*args):
	# at a terminal the countdown reads the descriptor itself: input() or a read of sys.stdin waits for a whole line, in a test
	# for ever ( 1.65 did ), so a countdown that calls one there fails at once
	raise AssertionError("a read that waits for a whole line was called")


def no_line_reads(monkeypatch, stdin):
	monkeypatch.setattr(builtins, "input", no_input)
	for name in ("readline", "read"):
		monkeypatch.setattr(stdin, name, no_input)


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
	assert fake_clock.waited == [1, 1, 1, 0] and fake_clock.slept == []


def test_countdown_returns_at_its_end_when_stdin_ends_inside_a_second(pipe_stdin, fake_clock, monkeypatch, capsys):
	# the end of the input ( a Ctrl-D, a pipe that closes ) seen 0.6 s into a second: the wait goes on to that second's end
	# only, not for a whole second more ( the first 1.66 build returned up to a second late )
	os.close(pipe_stdin)
	def ends_inside_the_second(r, w, x, timeout=None):
		fake_clock.waited.append(timeout)
		assert len(fake_clock.waited) < 10000, "the countdown does not end"
		fake_clock.now += min(0.6, timeout)
		return (list(r), [], [])
	monkeypatch.setattr(select, "select", ends_inside_the_second)
	assert input_with_timeout_and_countdown(2, "Answer") is None
	assert fake_clock.now == pytest.approx(1002.0)
	assert capsys.readouterr().err == "Answer [2s]: \rAnswer [2s]: \rAnswer [1s]: "


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


# --- 1.66: at a terminal the countdown returns at its timeout whatever was typed ( 1.65 waited in input() for a line to end )

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
def test_countdown_at_a_terminal_returns_at_its_end_with_a_line_not_ended(terminal, fake_clock, monkeypatch):
	# "ye" typed, then Ctrl-D: the terminal hands over the text with no Enter, and the line is not ended, so it is no answer: the
	# default applies at the end ( 1.65 waited in input() for the line to end, past the timeout ), with no whole-line read
	master = terminal()
	os.write(master, b"ye\x04")
	real_select = select.select
	monkeypatch.setattr(select, "select", fake_select(fake_clock, lambda clock: bool(real_select([sys.stdin], [], [], 0)[0])))
	with monkeypatch.context() as guarded:
		no_line_reads(guarded, sys.stdin)
		assert input_with_timeout_and_countdown(3, "Answer") is None
	assert fake_clock.now == 1003.0 and fake_clock.slept == []
	# its text was read: what comes next is a line of its own
	os.write(master, b"s\n")
	assert sys.stdin.readline() == "s\n"


@posix_only
@pytest.mark.parametrize("errors, expected", [("surrogateescape", "\udcffy"), ("strict", UnicodeDecodeError)])
def test_countdown_at_a_terminal_decodes_bytes_that_are_not_text_as_stdin_does(terminal, errors, expected):
	# under the C locale stdin keeps such a byte ( surrogateescape ), so a file name typed with it still opens; a strict stdin
	# raises, as in 1.65 and as for a pipe ( a 1.66 build replaced it, and a typed path became another path )
	master = terminal(encoding="utf-8", errors=errors)
	os.write(master, b"\xffy\n")
	if expected is UnicodeDecodeError:
		with pytest.raises(UnicodeDecodeError):
			input_with_timeout_and_countdown(5)
	else:
		assert input_with_timeout_and_countdown(5) == expected


@posix_only
@pytest.mark.parametrize("errors, expected", [("surrogateescape", "\udcffy"), ("strict", UnicodeDecodeError)])
def test_countdown_takes_bytes_that_are_not_text_of_a_pipe_as_input_does(monkeypatch, errors, expected):
	# a stdin that is not a terminal is read through input(), as in 1.65: a strict stdin raises
	read_end, write_end = os.pipe()
	stdin = io.open(read_end, "r", encoding="utf-8", errors=errors)
	monkeypatch.setattr(sys, "stdin", stdin)
	monkeypatch.setitem(sys.modules, "msvcrt", None)
	try:
		os.write(write_end, b"\xffy\n")
		if expected is UnicodeDecodeError:
			with pytest.raises(UnicodeDecodeError):
				input_with_timeout_and_countdown(5)
		else:
			assert input_with_timeout_and_countdown(5) == expected
	finally:
		stdin.close()
		os.close(write_end)


@posix_only
def test_countdown_takes_each_line_of_a_file_behind_a_codecs_reader(tmp_path):
	# a program whose stdin is a codecs reader, asking twice over "n", "y": the reader keeps what it read ahead, and nothing
	# reads or moves the descriptor beside it ( a second 1.66 build put the descriptor back after the first line: None )
	answers = tmp_path / "answers"
	answers.write_bytes(b"n\ny\n")
	program = tmp_path / "twice.py"
	program.write_text("import codecs, sys, multiCMD\nsys.stdin = codecs.getreader('utf-8')(sys.stdin.detach())\n"
		"print([multiCMD.input_with_timeout_and_countdown(1, 'Answer') for _ in range(2)])\n")
	with open(str(answers), "rb") as stdin:
		out = subprocess.run([sys.executable, str(program)], stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
			env=dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))).stdout
	assert out.decode().strip() == "['n', 'y']", out


@posix_only
def test_countdown_takes_a_line_of_a_stdin_that_has_a_descriptor_and_readline_only(tmp_path):
	# a stdin of the program's own that answers fileno() and readline() and nothing else ( no seek, tell, encoding or errors )
	answers = tmp_path / "answers"
	answers.write_bytes(b"first\nsecond\n")
	program = tmp_path / "own.py"
	program.write_text("import sys, multiCMD\n"
		"class Own:\n    def __init__(self, f): self.f = f\n    def fileno(self): return self.f.fileno()\n"
		"    def readline(self, *a): return self.f.readline(*a)\n"
		"sys.stdin = Own(sys.stdin)\n"
		"print(repr(multiCMD.input_with_timeout_and_countdown(1, 'Answer')))\n")
	with open(str(answers), "rb") as stdin:
		proc = subprocess.run([sys.executable, str(program)], stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
			env=dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1])))
	assert proc.stdout.decode().strip() == "'first'", proc


@posix_only
def test_countdown_takes_the_line_after_the_one_the_program_read_of_a_pipe_fed_ahead(tmp_path):
	# the program read "a" itself, and Python read the "y" after it ahead into its buffer; "z" comes once the countdown is
	# asking: the answer is the "y", as Python reads it and as 1.65 gave it ( the first 1.66 builds read the descriptor beside
	# Python's reader and gave "z" )
	read_end, write_end = os.pipe()
	def feed(proc):
		read_until(proc.stderr.fileno(), b"Answer [3s]: ")
		os.write(write_end, b"z\n")
	try:
		os.write(write_end, b"a\ny\n")
		status, out, err = ask(tmp_path, before="sys.stdin.readline()\n", stdin=read_end, timeout=3, feed=feed)
	finally:
		os.close(read_end)
		os.close(write_end)
	assert status == 0 and out.split()[:2] == ["GOT", "'y'"], (status, out, err)


@posix_only
def test_countdown_takes_a_float_timeout_on_real_descriptors(tmp_path):
	assert answered_none_after_the_timeout(ask(tmp_path, timeout=1.5), timeout=1.5)


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
def test_countdown_takes_the_next_line_of_a_file_the_program_read_part_of(tmp_path):
	# Python read 8 KiB of the file ahead for the program's own readline(): the answer is the line after that one, as Python
	# reads it and as 1.65 did, not the text that follows the read-ahead ( the first 1.66 build returned 'ne00819' )
	answers = tmp_path / "answers"
	answers.write_bytes(b"".join(b"line%05d\n" % number for number in range(2000)))
	with open(str(answers), "rb") as stdin:
		status, out, err = ask(tmp_path, before="sys.stdin.readline()\n", stdin=stdin)
	assert status == 0 and out.split()[:2] == ["GOT", "'line00001'"], (status, out, err)


@posix_only
@pytest.mark.parametrize("before", ["next(sys.stdin)\n", "sys.stdin.buffer.readline()\n"], ids=["next", "buffer"])
def test_countdown_takes_the_next_line_of_a_file_the_program_iterated_or_read_in_bytes(tmp_path, before):
	# Python's own reader gives the next line wherever the program's reading stands, after next() and after sys.stdin.buffer too
	# ( 200 KiB: more than the 128 KiB a 3.14 buffer reads ahead; the first 1.66 builds gave the middle of a line )
	answers = tmp_path / "answers"
	answers.write_bytes(b"".join(b"line%05d\n" % number for number in range(20000)))
	with open(str(answers), "rb") as stdin:
		status, out, err = ask(tmp_path, before=before, stdin=stdin)
	assert status == 0 and out.split()[:2] == ["GOT", "'line00001'"], (status, out, err)


@posix_only
def test_countdown_raises_for_a_file_line_a_strict_stdin_cannot_decode(tmp_path):
	# a line that a strict stdin cannot decode raises, as in 1.65: Python's reader has dropped it
	answers = tmp_path / "answers"
	answers.write_bytes(b"\xffy\n" + b"".join(b"line%05d\n" % number for number in range(2000)))
	program = tmp_path / "strict.py"
	program.write_text("import io, sys, multiCMD\nsys.stdin = io.TextIOWrapper(sys.stdin.buffer, encoding='utf-8', errors='strict')\n"
		"try:\n    multiCMD.input_with_timeout_and_countdown(1, 'Answer')\nexcept UnicodeDecodeError:\n    print('RAISED')\n")
	with open(str(answers), "rb") as stdin:
		out = subprocess.run([sys.executable, str(program)], stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
			env=dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))).stdout
	assert out.split() == [b"RAISED"], out


@posix_only
def test_countdown_at_a_terminal_out_of_its_line_mode_leaves_the_next_line(tmp_path):
	# cbreak ( no ICANON ): one read gives everything typed so far, so such a terminal is read a byte at a time, and the line
	# after the answer is still there for the next reader ( the first 1.66 build dropped it )
	import pty
	master, slave = pty.openpty()
	program = tmp_path / "ask.py"
	program.write_text("import os, sys, tty, multiCMD\ntty.setcbreak(0)\nsys.stderr.write('READY')\nsys.stderr.flush()\n"
		"got = multiCMD.input_with_timeout_and_countdown(5, 'Answer')\n"
		"sys.stdout.write('GOT %r %r\\n' % (got, sys.stdin.readline()))\nsys.stdout.flush()\nos._exit(0)\n")
	proc = subprocess.Popen([sys.executable, str(program)], stdin=slave, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
		env=dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1])))
	try:
		read_until(proc.stderr.fileno(), b"READY")
		os.write(master, b"a\nb\n")
		out, err = proc.communicate(timeout=30)
	finally:
		if proc.poll() is None:
			proc.kill()
			proc.wait()
		os.close(slave)
		os.close(master)
	assert out.decode().split() == ["GOT", "'a'", "'b\\n'"], (out, err)


@posix_only
@pytest.mark.parametrize("mode", ["line mode", "cbreak"])
def test_countdown_at_a_terminal_returns_at_its_end_while_text_keeps_coming(terminal, fake_clock, monkeypatch, mode):
	# text with no Enter that keeps coming ( Ctrl-D after each piece in line mode; keys out of it ) is read only to the end of
	# the countdown, which returns then, not when the text stops ( a 1.66 build read on: None at 5.97 s for a timeout of 1 )
	master = terminal()
	if mode == "cbreak":
		import tty
		tty.setcbreak(sys.stdin.fileno())
		os.write(master, b"x" * 40)
	else:
		os.write(master, b"x\x04" * 40)
	real_select = select.select
	def a_piece_each_quarter_second(clock):
		clock.now += 0.25
		return bool(real_select([sys.stdin], [], [], 0)[0])
	monkeypatch.setattr(select, "select", fake_select(fake_clock, a_piece_each_quarter_second))
	with monkeypatch.context() as guarded:
		no_line_reads(guarded, sys.stdin)
		assert input_with_timeout_and_countdown(1, "Answer") is None
	assert fake_clock.now == 1001.25


def test_countdown_raises_for_a_stdin_the_program_opened_for_writing(tmp_path, monkeypatch):
	# not readable: it was not asked, so it is not "no answer" ( as 1.65 )
	monkeypatch.setitem(sys.modules, "msvcrt", None)
	with open(str(tmp_path / "out"), "w") as written:
		monkeypatch.setattr(sys, "stdin", written)
		with pytest.raises(io.UnsupportedOperation):
			input_with_timeout_and_countdown(1)


@posix_only
def test_countdown_raises_when_select_cannot_watch_stdin_s_descriptor(monkeypatch):
	# select() takes descriptors below FD_SETSIZE ( 1024 ) only: one above it was not asked, so it is not "no answer" ( as 1.65 )
	import resource
	soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
	if soft <= 1100:
		if hard != resource.RLIM_INFINITY and hard <= 1100:
			pytest.skip("needs more than 1100 open files")
		resource.setrlimit(resource.RLIMIT_NOFILE, (2048, hard))
	read_end, write_end = os.pipe()
	os.dup2(read_end, 1100)
	os.close(read_end)
	stdin = io.open(1100, "r")
	monkeypatch.setattr(sys, "stdin", stdin)
	monkeypatch.setitem(sys.modules, "msvcrt", None)
	try:
		os.write(write_end, b"yes\n")
		with pytest.raises(ValueError):
			input_with_timeout_and_countdown(1)
	finally:
		stdin.close()
		os.close(write_end)
		resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))


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
		# the countdown's own first frame ( "\r" ): the prompt alone comes before the function looks for msvcrt, and a Ctrl-C
		# inside that failed import can land in a weakref callback of the import system, where Python drops it
		read_until(proc.stderr.fileno(), b"\rAnswer [5s]: ")
		proc.send_signal(signal.SIGINT)
	try:
		status, out, err = ask(tmp_path, stdin=read_end, timeout=5, feed=interrupt)
	finally:
		os.close(read_end)
		os.close(write_end)
	assert status != 0 and "GOT" not in out and "KeyboardInterrupt" in err, (status, out, err)
