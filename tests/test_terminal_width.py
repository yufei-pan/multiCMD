'''A progress line fits its terminal (1.68): a terminal of no size counts as unknown, and a frame is cut to one column less
than the width, in terminal columns.'''
import os
import re
import select
import subprocess
import sys
import time
import unicodedata
from pathlib import Path

import pytest

import multiCMD
from multiCMD import _genrate_progress_bar, get_terminal_size, print_progress_bar

ROOT = str(Path(__file__).resolve().parents[1])
posix_only = pytest.mark.skipif(os.name != 'posix', reason='needs a pseudo-terminal')

# hpcp's removal and listing lines ( hpcp 9.95: prefix=path[-50:], the suffix after it )
PATH = '/srv/data/projects/2026/run-0042/outputs/checkpoints/step-001200/model.safetensors'
REMOVAL = 'Removed: 1.23K Folders: 45 | Overall: 1.23M Files'
LISTING = 'Files: 1.23K Links: 12 Folders: 45 Size: 1.23GB | Overall: 1.23M Files'
WIDE_PATH = '/数据/项目/二〇二六/运行记录/输出结果/检查点/第一千二百步/模型权重文件.safetensors'


def cells(text):
	'''The terminal columns text takes, by the test's own count: a wide character two, a combining mark none.'''
	return sum(0 if unicodedata.combining(ch) else 2 if unicodedata.east_asian_width(ch) in ('W', 'F') else 1 for ch in text)


def frames(text):
	'''The frames in what print_progress_bar wrote: each starts with a carriage return.'''
	return [frame for frame in re.split(r'[\r\n]', text) if frame]


def env_without_a_size():
	env = dict(os.environ)
	env.pop('COLUMNS', None)
	env.pop('LINES', None)
	return env


def at_a_pty(code, rows, columns):
	'''What a child Python running code writes to a pty of rows x columns (its stdin and stdout), up to the "END" it writes last,
	with the pty's \\r\\n made \\n.'''
	import fcntl
	import pty
	import struct
	import termios
	master, slave = pty.openpty()
	try:
		fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', rows, columns, 0, 0))
		child = subprocess.Popen([sys.executable, '-c', code + '\nimport sys; sys.stdout.write("END"); sys.stdout.flush()'],
			stdin=slave, stdout=slave, stderr=subprocess.PIPE, cwd=ROOT, env=env_without_a_size())
	finally:
		os.close(slave)
	output = b''
	deadline = time.monotonic() + 60
	try:
		while not output.endswith(b'END') and time.monotonic() < deadline:
			if not select.select([master], [], [], 1)[0]:
				continue
			try:
				chunk = os.read(master, 65536)
			except OSError:  # Linux: EIO, the child has closed its side
				break
			if not chunk:
				break
			output += chunk
		_, err = child.communicate(timeout=60)
	finally:
		os.close(master)
	assert child.returncode == 0 and err == b'', err
	assert output.endswith(b'END'), output
	return output[:-3].decode('utf-8', 'replace').replace('\r\n', '\n')


@pytest.mark.parametrize('reported, environ, expected', [
	((0, 0), {}, (240, 50)),
	((0, 0), {'COLUMNS': '100', 'LINES': '40'}, (100, 40)),
	((0, 40), {}, (240, 40)),
	((200, 0), {'LINES': '30'}, (200, 30)),
	((0, 0), {'COLUMNS': '0', 'LINES': '-3'}, (240, 50)),
	((0, 0), {'COLUMNS': 'wide', 'LINES': ''}, (240, 50)),
	# a size the terminal knows wins, as before
	((200, 40), {'COLUMNS': '100', 'LINES': '10'}, (200, 40)),
])
def test_get_terminal_size_takes_a_reported_zero_as_unknown(monkeypatch, reported, environ, expected):
	# a pty nobody set a size on (script without a terminal, some CI runners) reports 0x0; 1.67 returned it
	monkeypatch.setattr(os, 'get_terminal_size', lambda *args: os.terminal_size(reported))
	monkeypatch.delenv('COLUMNS', raising=False)
	monkeypatch.delenv('LINES', raising=False)
	for name, value in environ.items():
		monkeypatch.setenv(name, value)
	assert tuple(get_terminal_size()) == expected


@posix_only
@pytest.mark.parametrize('stdout_is_the_pty', [True, False], ids=['stdout', 'stdin'])
def test_get_terminal_size_of_a_pty_without_a_size(stdout_is_the_pty):
	# stdout the pty: os.get_terminal_size(); stdout a pipe (cmd | less): the TIOCGWINSZ of stdin
	import pty
	master, slave = pty.openpty()
	code = 'import sys, multiCMD; sys.stderr.write(" ".join(map(str, multiCMD.get_terminal_size()[:2])))'
	try:
		result = subprocess.run([sys.executable, '-c', code], stdin=slave, stdout=slave if stdout_is_the_pty else subprocess.PIPE,
			stderr=subprocess.PIPE, cwd=ROOT, env=env_without_a_size(), universal_newlines=True, timeout=60)
	finally:
		os.close(slave)
		os.close(master)
	assert result.stderr.split() == ['240', '50']


@posix_only
def test_a_bar_of_a_known_total_stays_on_one_line_at_a_terminal_of_no_size():
	# 1.67 took the 0 columns for a width, and each frame came out over six lines
	shown = at_a_pty('import multiCMD\nfor i in range(11): multiCMD.print_progress_bar(i, 10, "p", "s")', 0, 0)
	assert shown.count('\n') == 1 and shown.endswith('\n'), shown
	assert len(frames(shown)) == 11 and all(cells(frame) <= 239 for frame in frames(shown)), shown
	assert frames(shown)[5].startswith('p |') and frames(shown)[5].endswith('|50.0% | s'), shown


@posix_only
def test_a_long_prefix_does_not_wrap_at_80_columns():
	# hpcp's removal and listing lines are 88 to 123 columns: 1.67 wrapped them, and each frame scrolled a row
	code = ('import multiCMD\n'
		'for i in range(3):\n'
		'	multiCMD.print_progress_bar(i, 0, %r, %r)\n'
		'	multiCMD.print_progress_bar(i, 0, %r, %r)\n'
		'	multiCMD.print_progress_bar(i, 0, %r, %r)\n'
		% (PATH[-50:], REMOVAL, PATH[-50:], LISTING, WIDE_PATH[-50:], REMOVAL))
	shown = at_a_pty(code, 24, 80)
	assert '\n' not in shown, shown
	assert len(frames(shown)) == 9 and all(cells(frame) <= 79 for frame in frames(shown)), shown
	removal, listing, wide = frames(shown)[-3:]
	# the prefix gives way first, from its start: the end of the path stays, and the counts stay whole
	assert removal.rstrip() == 'l.safetensors | iteration:2 | ' + REMOVAL, removal
	assert wide.rstrip().endswith('.safetensors | iteration:2 | ' + REMOVAL), wide
	# wider than the terminal without the path: the end gives way at a blank, never inside a number
	rest = ' iteration:2 | ' + LISTING
	assert rest.startswith(listing.rstrip()) and rest[len(listing.rstrip())] == ' ', listing


def test_a_pipe_gets_the_same_bar_of_a_known_total():
	# no terminal: COLUMNS, else 240 columns, as in 1.67
	code = ('import multiCMD\n'
		'multiCMD.print_progress_bar(5, 10, "p", "s")\n'
		'multiCMD.print_progress_bar(10, 10, "p", "s")\n'
		'multiCMD.print_progress_bar(3, 0, "p", "s")\n')
	result = subprocess.run([sys.executable, '-c', code], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
		cwd=ROOT, env=dict(env_without_a_size(), PYTHONIOENCODING='utf-8'), timeout=60)
	assert result.returncode == 0 and result.stderr == b'', result.stderr
	full = '\N{FULL BLOCK}'
	assert result.stdout.decode('utf-8') == (
		'\rp |' + full * 112 + '\N{LOWER ONE EIGHTH BLOCK}' + '_' * 112 + '|50.0% | s'
		'\rp |' + full * 223 + '|100.0% | s\n'
		# padded to clear the last frame, to 239 columns ( 1.67: 240 )
		'\r' + 'p | iteration:3 | s'.ljust(239))


@pytest.mark.parametrize('columns, shown', [
	(240, '/outputs/checkpoints/step-001200/model.safetensors | iteration:7 | ' + REMOVAL),
	(80, 'l.safetensors | iteration:7 | ' + REMOVAL),
	(68, 's | iteration:7 | ' + REMOVAL),
	(67, ' | iteration:7 | ' + REMOVAL),
	(60, ' iteration:7 | Removed: 1.23K Folders: 45 | Overall: 1.23M'),
	(30, ' iteration:7 | Removed: 1.23K'),
	(29, ' iteration:7 | Removed:'),
	(8, ' iterat'),
	(1, ''),
])
def test_a_frame_of_no_total_gives_way_from_the_prefix_then_the_end(columns, shown):
	frame = _genrate_progress_bar(7, 0, PATH[-50:] + ' |', '| ' + REMOVAL, columns)
	assert frame == shown.ljust(columns - 1)


def test_a_frame_counts_wide_characters_as_two_columns():
	frame = _genrate_progress_bar(7, 0, WIDE_PATH[-50:] + ' |', '| ' + REMOVAL, 80)
	# ( padded with a blank where a wide character did not fit in the last column )
	assert cells(frame) == 79 and frame.rstrip().endswith('.safetensors | iteration:7 | ' + REMOVAL), frame
	# with a known total, the bar is what gives way
	frame = _genrate_progress_bar(5, 10, '中文前缀 |', '| 后缀', 40)
	assert cells(frame) == 38 and frame.startswith('中文前缀 |') and frame.endswith('|50.0% | 后缀'), frame
	# a combining mark takes none
	frame = _genrate_progress_bar(7, 0, 'cafe\N{COMBINING ACUTE ACCENT} |', '| s', 40)
	assert cells(frame) == 39 and len(frame) == 40, frame


def test_a_bar_of_a_known_total_at_a_terminal_too_narrow_for_it():
	# 1.67 wrote the frame over six lines at 3 columns or fewer
	assert [_genrate_progress_bar(5, 10, 'p |', '| s', columns) for columns in (1, 2, 3)] == ['', '', '']
	assert _genrate_progress_bar(5, 10, 'p |', '| s', 5) == '\N{FULL BLOCK}\N{LOWER ONE EIGHTH BLOCK}_'


def test_a_bar_past_its_total_is_full_not_longer():
	# a total that was an estimate: 1.67 drew a bar one and a half times its length, over the terminal's width
	frame = _genrate_progress_bar(15, 10, 'p |', '| s', 40)
	assert frame == 'p |' + '\N{FULL BLOCK}' * 23 + '|150.0% | s' and cells(frame) <= 39


def test_print_progress_bar_draws_at_one_column_less_than_the_terminal(monkeypatch, capsys):
	monkeypatch.setattr(multiCMD, 'get_terminal_size', lambda: os.terminal_size((80, 24)))
	print_progress_bar(7, 0, PATH[-50:], REMOVAL)
	assert capsys.readouterr().out == '\r' + ('l.safetensors | iteration:7 | ' + REMOVAL).ljust(79)
