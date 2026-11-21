'''A progress line fits its terminal (1.68): a terminal of no size counts as unknown, and a frame is cut to one column less
than the width, in terminal columns. A bar with a total keeps 10 cells, and its text gives way a field at a time; a bar past
its total ends its line; a control character in a frame's text is a '?' (1.69).'''
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

# hpcp's removal and listing lines ( hpcp 9.95: prefix=path[-50:], the suffix after it, its numbers from format_bytes(), which
# writes a blank between a number and its unit: '1.23 K', '1.15 Gi', and '45.00 ' with none )
PATH = '/srv/data/projects/2026/run-0042/outputs/checkpoints/step-001200/model.safetensors'


def fb(count):
	return multiCMD.format_bytes(count, use_1024_bytes=False, to_str=True)


REMOVAL = f'Removed: {fb(1230)} Folders: {fb(45)} | Overall: {fb(1230000)} Files'
LISTING = f'Files: {fb(1230)} Links: {fb(12)} Folders: {fb(45)} Size: {multiCMD.format_bytes(1.15 * 2 ** 30)}B | Overall: {fb(1230000)} Files'
WIDE_PATH = '/数据/项目/二〇二六/运行记录/输出结果/检查点/第一千二百步/模型权重文件.safetensors'
# hpcp 9.96's copy bar ( Adaptive_Progress_Bar.print_progress, read, not imported ), with this module's format_bytes: its count,
# size and jobs, then its rates, the last jobs' rates and the time left; with a rate limit set, the limits in the jobs' place
COPY_PREFIX = f'{fb(123456)}F ({multiCMD.format_bytes(45.6 * 2 ** 30)}B) Copied | 16 Scheduled {4.0:0>3.1f} F/Job '
COPY = (f'{multiCMD.format_bytes(1.23 * 2 ** 30)}B/s {fb(2345.6)}F/s | {2.5:.1f}s: {multiCMD.format_bytes(1.1 * 2 ** 30)}B/s '
	f'{fb(2100)}F/s | 1h2m3s')
COPY_LIMITED = (f'{multiCMD.format_bytes(1.23 * 2 ** 30)}B/s {fb(2345.6)}F/s | {multiCMD.format_bytes(2 ** 30)}B/s Limit | '
	f'{fb(5000)}F/s Limit | 1h2m3s')


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


def test_the_test_strings_are_what_format_bytes_writes():
	# REMOVAL and LISTING are the suffixes of hpcp 9.95's removal line ( _RemoveProgress.show ) and its listing line, with this
	# module's own format_bytes: hpcp's code is not read. A number and its unit are two words ( the first 1.68 build tested
	# '1.23K' and cut 'Size: 123.45 GiB' after its number )
	assert REMOVAL == 'Removed: 1.23 K Folders: 45.00  | Overall: 1.23 M Files'
	assert LISTING == 'Files: 1.23 K Links: 12.00  Folders: 45.00  Size: 1.15 GiB | Overall: 1.23 M Files'
	# and hpcp 9.96's copy bar ( the 1.69 tests )
	assert COPY_PREFIX == '123.46 KF (45.60 GiB) Copied | 16 Scheduled 4.0 F/Job '
	assert COPY == '1.23 GiB/s 2.35 KF/s | 2.5s: 1.10 GiB/s 2.10 KF/s | 1h2m3s'
	assert COPY_LIMITED == '1.23 GiB/s 2.35 KF/s | 1.00 GiB/s Limit | 5.00 KF/s Limit | 1h2m3s'


@pytest.mark.parametrize('reported, environ, expected', [
	# a terminal that does not say: 80x24, what other programs take it for, so a line that fits never wraps on its screen
	((0, 0), {}, (80, 24)),
	((0, 0), {'COLUMNS': '100', 'LINES': '40'}, (100, 40)),
	((0, 40), {}, (80, 40)),
	((200, 0), {}, (200, 24)),
	((200, 0), {'LINES': '30'}, (200, 30)),
	((0, 0), {'COLUMNS': '0', 'LINES': '-3'}, (80, 24)),
	((0, 0), {'COLUMNS': 'wide', 'LINES': ''}, (80, 24)),
	# a size the terminal knows wins, as before
	((200, 40), {'COLUMNS': '100', 'LINES': '10'}, (200, 40)),
])
def test_get_terminal_size_takes_a_reported_zero_as_unknown(monkeypatch, reported, environ, expected):
	# a pty nobody set a size on (script without a terminal, a serial console, some CI runners) reports 0x0; 1.67 returned it
	monkeypatch.setattr(os, 'get_terminal_size', lambda *args: os.terminal_size(reported))
	monkeypatch.delenv('COLUMNS', raising=False)
	monkeypatch.delenv('LINES', raising=False)
	for name, value in environ.items():
		monkeypatch.setenv(name, value)
	size = get_terminal_size()
	assert isinstance(size, os.terminal_size) and tuple(size) == expected


@pytest.mark.parametrize('environ, expected', [
	({}, ['240', '50']),
	({'COLUMNS': '100', 'LINES': '30'}, ['100', '30']),
])
def test_get_terminal_size_without_a_terminal_is_240x50_or_what_the_environment_says(environ, expected):
	# no terminal at all is a pipe or a log: nothing wraps, so the wide fallback stays ( a terminal that answered 0 is 80x24 )
	code = 'import sys, multiCMD; sys.stderr.write(" ".join(map(str, multiCMD.get_terminal_size()[:2])))'
	result = subprocess.run([sys.executable, '-c', code], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
		cwd=ROOT, env=dict(env_without_a_size(), **environ), universal_newlines=True, timeout=60)
	assert result.stderr.split() == expected


@posix_only
@pytest.mark.parametrize('stdout_is_the_pty, size, expected', [
	(True, (0, 0), ['80', '24']),
	# stdout a pipe ( cmd | less ): stdin's terminal answers, with zeros too, so it is a terminal of no size and not no terminal ( 240x50 )
	(False, (0, 0), ['80', '24']),
	(False, (30, 100), ['100', '30']),
], ids=['stdout', 'stdin', 'stdin-sized'])
def test_get_terminal_size_of_a_pty(stdout_is_the_pty, size, expected):
	import fcntl
	import pty
	import struct
	import termios
	master, slave = pty.openpty()
	code = 'import sys, multiCMD; sys.stderr.write(" ".join(map(str, multiCMD.get_terminal_size()[:2])))'
	try:
		fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', size[0], size[1], 0, 0))
		result = subprocess.run([sys.executable, '-c', code], stdin=slave, stdout=slave if stdout_is_the_pty else subprocess.PIPE,
			stderr=subprocess.PIPE, cwd=ROOT, env=env_without_a_size(), universal_newlines=True, timeout=60)
	finally:
		os.close(slave)
		os.close(master)
	assert result.stderr.split() == expected


@posix_only
def test_a_pty_command_gets_a_window_of_80x24_less_the_prefix_at_a_terminal_of_no_size():
	# 1.67 gave the command a window of 20 columns by 0 rows ( the terminal's zeros ), a pager or a table in it no room
	code = ('import fcntl, struct, termios, multiCMD\n'
		'master, slave = multiCMD._open_pty(4)\n'
		'rows, columns = struct.unpack("HHHH", fcntl.ioctl(slave, termios.TIOCGWINSZ, b"\\0" * 8))[:2]\n'
		'print(rows, columns)')
	assert at_a_pty(code, 0, 0).split() == ['24', '76']


@posix_only
def test_a_table_gets_80_columns_at_a_terminal_of_no_size():
	# 1.67 cut every column to its heading's width ( 'a   |xxx..', 10 columns a row ): the zero was the width
	code = ('import sys, multiCMD\n'
		'sys.stdout.write(multiCMD.pretty_format_table([["name", "value"], ["a", "x" * 100], ["b", "y" * 30]]))')
	lines = at_a_pty(code, 0, 0).split('\n')
	assert [len(line) for line in lines[:4]] == [80] * 4 and lines[2].startswith('a   |xxx') and lines[2].endswith('xxx..'), lines
	assert lines[3] == 'b   |' + 'y' * 30 + ' ' * 45, lines


@posix_only
def test_a_bar_of_a_known_total_stays_on_one_line_at_a_terminal_of_no_size():
	# 1.67 took the 0 columns for a width, and each frame came out over six lines
	shown = at_a_pty('import multiCMD\nfor i in range(11): multiCMD.print_progress_bar(i, 10, "p", "s")', 0, 0)
	assert shown.count('\n') == 1 and shown.endswith('\n'), shown
	assert len(frames(shown)) == 11 and all(cells(frame) <= 79 for frame in frames(shown)), shown
	assert frames(shown)[5].startswith('p |') and frames(shown)[5].endswith('|50.0% | s'), shown


@posix_only
def test_a_short_line_stays_on_one_row_at_a_terminal_of_no_size():
	# a serial console or `qm terminal` gives 0x0 until someone runs resize ( known from the platform, not tested ), with a real
	# 80-column screen behind it: the first 1.68 build padded every frame to 239 columns, three rows that scrolled twice a
	# second ( 1.67 wrote 58 and did not pad )
	shown = at_a_pty('import multiCMD\nfor i in range(3): multiCMD.print_progress_bar(i, 0, "/scratch/old-runs/job-1234", "Removed: 1.23 K")', 0, 0)
	assert '\n' not in shown and len(frames(shown)) == 3, shown
	assert [len(frame) for frame in frames(shown)] == [79] * 3 and frames(shown)[2].rstrip() == (
		'/scratch/old-runs/job-1234 | iteration:2 | Removed: 1.23 K'), shown


@posix_only
def test_a_long_prefix_does_not_wrap_at_80_columns():
	# hpcp's removal and listing lines pass 80 columns with a path of 50 ( this test's: 122 and 149 ): 1.67 wrapped them, and
	# each frame scrolled a row. %a, not %r: the child's code stays ASCII, so it starts under a C locale ( Python 3.6 ) too
	code = ('import multiCMD\n'
		'for i in range(3):\n'
		'	multiCMD.print_progress_bar(i, 0, %a, %a)\n'
		'	multiCMD.print_progress_bar(i, 0, %a, %a)\n'
		'	multiCMD.print_progress_bar(i, 0, %a, %a)\n'
		% (PATH[-50:], REMOVAL, PATH[-50:], LISTING, WIDE_PATH[-50:], REMOVAL))
	shown = at_a_pty(code, 24, 80)
	assert '\n' not in shown, shown
	assert len(frames(shown)) == 9 and all(cells(frame) <= 79 for frame in frames(shown)), shown
	removal, listing, wide = frames(shown)[-3:]
	# the prefix gives way first, from its start: the end of the path stays, and the counts stay whole
	assert removal.rstrip() == 'tensors | iteration:2 | ' + REMOVAL, removal
	assert wide.rstrip() == 'tensors | iteration:2 | ' + REMOVAL, wide
	# wider than the terminal without the path: the end gives way a whole field at a time ( 1.15 GiB stays whole, then the
	# label of a number that does not fit goes with it )
	assert listing.rstrip() == ' iteration:2 | Files: 1.23 K Links: 12.00  Folders: 45.00  Size: 1.15 GiB |', listing


def test_a_frame_is_measured_as_an_ascii_stdout_writes_it():
	# a combining mark counts none, and an ASCII stdout ( PYTHONIOENCODING, or Python 3.6 in a C locale ) writes it as one '?': a
	# decomposed name ( macOS writes them ), measured before it is written, came out 109 columns at 80, and wrapped. Only a
	# combining mark shows it: a wide character is two columns and written as one '?', so the frame comes out narrower
	prefix = 'e\u0301' * 30
	code = ('import multiCMD\n'
		'for i in range(3): multiCMD.print_progress_bar(i, 0, %a, "| Removed: 1.23 K")\n' % prefix)
	result = subprocess.run([sys.executable, '-c', code], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
		cwd=ROOT, env=dict(env_without_a_size(), PYTHONIOENCODING='ascii', COLUMNS='80'), timeout=60)
	assert result.returncode == 0 and result.stderr == b'', result.stderr
	shown = frames(result.stdout.decode('ascii'))
	# the prefix as the stream writes it is 62 columns, and what is left of the 79 after the rest of the line is its end
	written, rests = 'e?' * 30 + ' |', [' iteration:%d | Removed: 1.23 K' % i for i in range(3)]
	assert shown == [written[len(rest) - 79:] + rest for rest in rests], shown


def test_a_pipe_draws_its_frames_at_240_columns():
	# no terminal, and no COLUMNS ( the environment has none ): 240 columns, as in 1.67
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
		'\rp |' + full * 224 + '|100.0% | s\n'
		# padded to clear the last frame, to 239 columns ( 1.67: 240 )
		'\r' + 'p | iteration:3 | s'.ljust(239))


@pytest.mark.parametrize('columns, shown', [
	(240, '/outputs/checkpoints/step-001200/model.safetensors | iteration:7 | ' + REMOVAL),
	(80, 'tensors | iteration:7 | ' + REMOVAL),
	(73, ' | iteration:7 | ' + REMOVAL),
	(71, ' iteration:7 | ' + REMOVAL),
	# the end gives way a word at a time: ' Files', the word after a number's unit, goes on its own; the number keeps its unit
	(70, ' iteration:7 | Removed: 1.23 K Folders: 45.00  | Overall: 1.23 M'),
	# a label goes with its number ( 'Overall: 1.23 M' did not fit; 'Overall:' alone is not left )
	(64, ' iteration:7 | Removed: 1.23 K Folders: 45.00  |'),
	# 'Folders:' fits in the 39 columns, its number does not
	(40, ' iteration:7 | Removed: 1.23 K'),
	# '1.23' fits in the 29 columns, its unit does not
	(30, ' iteration:7 |'),
	# a field that fits to the last column stays ( the blank after it is the first thing that does not fit )
	(31, ' iteration:7 | Removed: 1.23 K'),
	(8, ' iterat'),
	(1, ''),
])
def test_a_frame_of_no_total_gives_way_from_the_prefix_then_the_end(columns, shown):
	frame = _genrate_progress_bar(7, 0, PATH[-50:] + ' |', '| ' + REMOVAL, columns)
	assert frame == shown.ljust(columns - 1)


def test_a_number_is_not_cut_from_its_unit_at_80_columns():
	# hpcp's listing at 80 columns, five minutes in: 1.68's first build cut it at the blank before the unit, 'Size: 123.45' of 123.45 GiB
	suffix = '| Files: 123.45 K Links: 1.20 K Folders: 12.34 K Size: 123.45 GiB '
	frame = _genrate_progress_bar(300, 0, PATH[-50:] + ' |', suffix, 80)
	assert frame == ' iteration:300 | Files: 123.45 K Links: 1.20 K Folders: 12.34 K'.ljust(79)


def a_broken_end(kept, whole):
	'''Why a frame of the line whole ends badly, at kept ( what is left of whole from ' iteration:' on ), else None: inside a
	word, on a label without its number, or on a number without its unit. A plain count ( '45.00' ) is complete: the word after
	it is a label or a bar, not a unit.'''
	if not whole.startswith(kept):
		return 'is not the start of the line'
	if kept != whole.rstrip() and whole[len(kept)] != ' ':
		return 'ends inside a word'
	if kept.endswith(':'):
		return 'ends on a label without its number'
	after = whole[len(kept):].split()
	if re.fullmatch(r'\d+(\.\d+)?', kept.split(' ')[-1]) and after and after[0][:1].isalpha() and not after[0].endswith(':'):
		return 'ends on a number without its unit'
	return None


@pytest.mark.parametrize('prefix', [PATH[-50:], WIDE_PATH[-50:]], ids=['path', 'wide-path'])
@pytest.mark.parametrize('suffix', [REMOVAL, LISTING, 'Files: 123.45 K Links: 1.20 K Folders: 12.34 K Size: 123.45 GiB '],
	ids=['removal', 'listing', 'large-counts'])
def test_a_frame_never_ends_inside_a_field_at_any_width(prefix, suffix):
	# hpcp's lines at 20 to 200 columns: the end gives way a field at a time, after what is left of the prefix. 61cedd0 cut at
	# any blank, and ended on a label without its number ( 'Removed:' ) and on a number without its unit
	suffix = '| ' + suffix
	whole = ' iteration:7 ' + suffix
	for columns in range(20, 201):
		frame = _genrate_progress_bar(7, 0, prefix + ' |', suffix, columns)
		shown = frame.rstrip()
		kept = shown[shown.index(' iteration:'):]
		assert cells(frame) == columns - 1 and a_broken_end(kept, whole) is None, (columns, a_broken_end(kept, whole), shown)


@pytest.mark.parametrize('text, expected', [
	('', 0), ('abc', 3), ('caf\u00e9', 4), ('\u00ff\u0100\u02ff', 3),
	# U+0300 is where the count by length stops: a combining mark takes none, a format character ( zero width space ) none, a Hangul
	# vowel jamo none, a wide character ( Chinese, full-width ) two, an emoji two
	('\u0300', 0), ('e\u0301', 1), ('a\u200bb', 2), ('\u1160', 0), ('\u4e2d\u6587', 4), ('ab\u4e2d', 4), ('\uff21', 2), ('\U0001f600x', 3),
	('x' * 300 + '\u4e2d', 302),
])
def test_cells_counts_a_character_by_its_unicode_width(text, expected):
	# the module's own count, by Unicode's width of each character: no terminal measures it ( a tab or a newline counts one, and a
	# frame draws each as a '?' before it counts: 1.69 ). ASCII and Latin-1, the usual text, is counted by length: a fast path that
	# must give what the count by character gives
	assert multiCMD._cells(text) == expected


def test_a_frame_counts_a_wide_character_as_two_columns_and_a_combining_mark_as_none():
	frame = _genrate_progress_bar(7, 0, WIDE_PATH[-50:] + ' |', '| ' + REMOVAL, 109)
	# ( padded with a blank where a wide character did not fit in the last column )
	assert cells(frame) == 108 and frame.startswith('一千二百步/') and frame.endswith(REMOVAL + ' '), frame
	assert _genrate_progress_bar(7, 0, WIDE_PATH[-50:] + ' |', '| ' + REMOVAL, 110).startswith('第一千二百步/'), frame
	# the suffix's wide characters count too: what is left of the prefix is cut to fit them
	rest = ' iteration:7 | 已删除: 1.23 K 文件夹: 45.00 '
	frame = _genrate_progress_bar(7, 0, PATH[-50:] + ' |', '| 已删除: 1.23 K 文件夹: 45.00 ', 80)
	assert cells(frame) == 79 and frame.rstrip() == (PATH[-50:] + ' |')[-(79 - cells(rest)):] + rest.rstrip(), frame
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


def test_a_bar_of_a_known_total_keeps_10_cells_before_its_text():
	# 15 columns: the bar keeps its 10 cells, the percent goes, then the suffix, and the prefix's first field stays ( 1.68: a bar
	# of 7 cells and both ). 12 columns: the bar alone, of 10; fewer: what there is
	half = '\N{FULL BLOCK}' * 4 + '\N{LOWER HALF BLOCK}' + '_' * 5
	assert _genrate_progress_bar(5, 10, 'p |', '| s', 15) == 'p |' + half
	assert _genrate_progress_bar(5, 10, 'p |', '| s', 12) == half
	assert _genrate_progress_bar(5, 10, 'p |', '| s', 8) == '\N{FULL BLOCK}' * 2 + '\N{LOWER HALF BLOCK}' + '_' * 3


def test_a_bar_of_a_known_total_drops_a_long_prefix_and_its_percent():
	# 20 columns: the prefix, which the bar of a known total drops whole ( the no-total frame cuts it from its start ), and the percent
	assert _genrate_progress_bar(5, 10, 'x' * 30 + ' |', '| s', 20) == '\N{FULL BLOCK}' * 7 + '\N{LOWER ONE EIGHTH BLOCK}' + '_' * 7 + '| s'


def test_a_bar_past_its_total_is_full_not_longer():
	# a total that was an estimate: 1.67 drew a bar one and a half times its length, over the terminal's width
	frame = _genrate_progress_bar(15, 10, 'p |', '| s', 40)
	assert frame == 'p |' + '\N{FULL BLOCK}' * 24 + '|150.0% | s' and cells(frame) == 38


@pytest.mark.parametrize('columns', [40, 80, 240])
def test_the_last_frame_of_a_bar_covers_the_one_before_it(columns):
	# a frame is drawn over the one before it from the line's start, and what it does not cover stays: 1.67's full bar was one
	# column shorter than the others, and a bar of 10 ended '| 00:00:001' for '| 00:00:00' ( hpcp's copy bar too )
	drawn = [_genrate_progress_bar(i, 10, '10 files |', '| 3.2 F/s | 00:00:0%d' % (10 - i), columns) for i in range(1, 11)]
	assert [len(frame) for frame in drawn] == [columns - 2] * 10
	line = []
	for frame in drawn:
		line[:len(frame)] = list(frame)
	# ( at 40 columns '3.2 F/s' gives way, to keep a bar of 10 cells or more, and the bar takes the room: 1.68 drew '3.2 F/s' beside
	# a bar of 8 cells there )
	tail = '|100.0% | 3.2 F/s | 00:00:00' if columns > 40 else '| 00:00:00'
	assert ''.join(line) == drawn[-1] == '10 files |' + '\N{FULL BLOCK}' * (columns - 2 - 10 - len(tail)) + tail


def test_print_progress_bar_draws_at_one_column_less_than_the_terminal(monkeypatch, capsys):
	monkeypatch.setattr(multiCMD, 'get_terminal_size', lambda: os.terminal_size((80, 24)))
	print_progress_bar(7, 0, PATH[-50:], REMOVAL)
	assert capsys.readouterr().out == '\r' + ('tensors | iteration:7 | ' + REMOVAL).ljust(79)


# ---- 1.69: a bar with a total keeps 10 cells; a bar past its total ends its line; a control character is a '?'

BAR = re.compile('[' + multiCMD._BAR_BLOCKS + '_]+')


def parts(frame):
	'''A frame of a known total as ( what is before its bar, the bar's cells, what is after it ).'''
	found = BAR.search(frame)
	return frame[:found.start()], found.end() - found.start(), frame[found.end():]


def a_broken_start(kept, whole):
	'''Why a frame's text that keeps the end of whole, from kept on, starts badly, else None: inside a word, on a unit without
	its number, on a number without its label, or on a word whose number went ( '| Files' of '1.23 M Files' ): it starts with
	a number, a label or a '|' ( the mirror of a_broken_end ).'''
	if not whole.endswith(kept):
		return 'is not the end of the line'
	before = whole[:len(whole) - len(kept)]
	if before and before[-1] != ' ':
		return 'starts inside a word'
	before = before.split()
	if before and re.fullmatch(r'\d+(\.\d+)?', before[-1]):
		return 'starts on a unit without its number'
	if before and before[-1].endswith(':'):
		return 'starts on a number without its label'
	first = kept.split()[0]
	if before and not (first[0].isdigit() or first[0] == '|' or first.endswith(':')):
		return 'starts on a word whose number went'
	return None


@pytest.mark.parametrize('columns, suffix, shown', [
	# 1.68 dropped the percent and the whole prefix: ( '', 18, '| ' + COPY ), with no count, size or percent
	(80, COPY, ('123.46 KF |', 18, '| 2.35 KF/s | 2.5s: 1.10 GiB/s 2.10 KF/s | 1h2m3s')),
	(80, COPY_LIMITED, ('123.46 KF |', 10, '| 2.35 KF/s | 1.00 GiB/s Limit | 5.00 KF/s Limit | 1h2m3s')),
	(100, COPY, ('123.46 KF (45.60 GiB) |', 15, '| ' + COPY)),
	(100, COPY_LIMITED, ('123.46 KF |', 19, '| ' + COPY_LIMITED)),
	# 1.68: the whole prefix and a bar of 2 cells
	(120, COPY, ('123.46 KF (45.60 GiB) Copied | 16 Scheduled |', 13, '| ' + COPY)),
	(160, COPY, (COPY_PREFIX + ' |', 35, '|50.0% | ' + COPY)),
], ids=['80', '80-limited', '100', '100-limited', '120', '160'])
def test_hpcp_copy_bar_keeps_its_count_and_its_time_left(columns, suffix, shown):
	# the bar keeps 10 cells; beside it the percent gives way, then the prefix from its end down to its count, then the suffix
	# from its start: the line keeps what it starts and ends with ( the count, the time left )
	frame = _genrate_progress_bar(5, 10, COPY_PREFIX + ' |', '| ' + suffix, columns)
	assert parts(frame) == shown and cells(frame) == columns - 2, frame


@pytest.mark.parametrize('suffix', [COPY, COPY_LIMITED, REMOVAL], ids=['copy', 'copy-limited', 'removal'])
@pytest.mark.parametrize('prefix', [COPY_PREFIX, PATH[-50:], WIDE_PATH[-50:]], ids=['copy', 'path', 'wide-path'])
def test_a_frame_of_a_known_total_keeps_whole_fields_beside_10_cells_at_any_width(prefix, suffix):
	# at 12 to 200 columns: a bar of 10 cells or more, the start of the prefix and the end of the suffix, each cut only between
	# fields ( a number keeps its unit, a label its number ), and the prefix's first field and the suffix's last as long as
	# there is room for them
	# ( a path has no blank: it is its one field )
	first = '123.46 KF |' if prefix == COPY_PREFIX else prefix + ' |'
	last = '| Overall: 1.23 M Files' if suffix == REMOVAL else '| 1h2m3s'
	prefix, suffix = prefix + ' |', '| ' + suffix
	for columns in range(12, 201):
		frame = _genrate_progress_bar(5, 10, prefix, suffix, columns)
		head, bar, tail = parts(frame)
		if tail.startswith('|50.0% '):
			tail = tail[len('|50.0% '):]
		assert cells(frame) == columns - 2 and bar >= 10, (columns, frame)
		assert head == '' or head == prefix or head.endswith(' |') and a_broken_end(head[:-2], prefix) is None, (columns, frame)
		assert tail == '' or tail == suffix or tail.startswith('| ') and a_broken_start(tail[2:], suffix) is None, (columns, frame)
		if cells(first) + 10 <= columns - 2:
			assert head.startswith(first[:-2]), (columns, frame)
		if cells(head) + cells(last) + 10 <= columns - 2:
			assert tail.endswith(last), (columns, frame)


@pytest.mark.parametrize('suffix', [COPY, COPY_LIMITED, REMOVAL], ids=['copy', 'copy-limited', 'removal'])
@pytest.mark.parametrize('prefix', [COPY_PREFIX, PATH[-50:], WIDE_PATH[-50:]], ids=['copy', 'path', 'wide-path'])
def test_the_first_frame_of_a_known_total_has_the_place_of_its_bar_and_no_double_joint(prefix, suffix):
	# iteration 0 ( hpcp's first copy bar frame ) is cut as the frames after it, and its text stands where theirs does: the bar's
	# place holds ' Calculating... ' when it fits, else blanks ( 1.69 first cut it for a bar it never drew and joined prefix and
	# suffix with nothing between: '0.00 F || 0.00 F/s | ∞' at 40 columns ). 1 of 1000 has a percent as long as 0's
	prefix, suffix = prefix + ' |', '| ' + suffix
	for columns in range(12, 201):
		first = _genrate_progress_bar(0, 1000, prefix, suffix, columns)
		head, bar, tail = parts(_genrate_progress_bar(1, 1000, prefix, suffix, columns))
		if tail.startswith('|0.1% '):
			tail = tail[len('|0.1% '):]
		place = ' Calculating... ' if bar >= 16 else ' ' * bar
		assert first == head + place + tail, (columns, first)
		assert '||' not in first and cells(first) <= columns - 2, (columns, first)


def test_a_bar_past_its_total_ends_its_line(monkeypatch, capsys):
	# 1.68 ended the line only at a frame equal to the total: a count that stepped over it left the line open, and the program's
	# next output joined the bar ( '...|110.0% | snext' ). The first frame at or past the total ends it now; the frame at the
	# total still ends it each time; a later frame past it is drawn on the next line and left open, so it does not scroll
	monkeypatch.setattr(multiCMD, 'get_terminal_size', lambda: os.terminal_size((40, 24)))
	monkeypatch.setattr(multiCMD, '_bar_done', [None])
	print_progress_bar(9, 10, 'p', 's')
	print_progress_bar(11, 10, 'p', 's')
	print('next')
	shown = capsys.readouterr().out.split('\n')
	assert len(shown) == 3 and shown[0].endswith('|110.0% | s') and shown[1:] == ['next', ''], shown
	for frames, lines in [
		# a count that steps over the total, then goes on: one line end, after the first frame past it
		([9, 12, 13, 15], 1),
		# a frame at the total, each time ( as 1.68 )
		([5, 10, 10], 2),
		# at the total, then past it: as 1.68
		([9, 10, 11], 1),
		# a new bar of the same total: a frame below it starts it again
		([12, 3, 12], 2),
	]:
		for iteration in frames:
			print_progress_bar(iteration, 10, 'p', 's')
		assert capsys.readouterr().out.count('\n') == lines, frames
		print_progress_bar(0, 10, 'p', 's')
		capsys.readouterr()


@posix_only
def test_the_output_after_a_bar_past_its_total_starts_a_line_of_its_own():
	# at a terminal of no size ( 80x24 ): the bar's line ends with the first frame past the total, and 'next' starts the line after it
	code = ('import multiCMD\n'
		'for i in (5, 11): multiCMD.print_progress_bar(i, 10, "p", "s")\n'
		'print("next")')
	shown = at_a_pty(code, 0, 0)
	assert shown.endswith('|110.0% | s\nnext\n') and shown.count('\n') == 2, shown


CONTROL = re.compile('[\x00-\x1f\x7f-\x9f]')


@pytest.mark.parametrize('name', ['run\tlog', 'run\nlog', 'run\rlog', 'run\x1b[2Jlog', 'run\x7flog', 'run\x9b2Jlog', 'run\x00log'],
	ids=['tab', 'newline', 'carriage-return', 'escape', 'delete', 'c1-csi', 'nul'])
def test_a_control_character_in_a_frame_is_a_question_mark(name):
	# a file name may hold any of them ( hpcp's removal and listing lines show the end of a path ): 1.68 wrote a newline as a
	# new row, so each redraw scrolled, and a tab to the next tab stop, so the frame could reach the last column and wrap
	shown = CONTROL.sub('?', name)
	assert shown.startswith('run?') and shown.endswith('log')
	frame = _genrate_progress_bar(7, 0, '/data/' + name + ' |', '| Removed: 1.23 K', 80)
	assert frame == ('/data/' + shown + ' | iteration:7 | Removed: 1.23 K').ljust(79), frame
	frame = _genrate_progress_bar(5, 10, name + ' |', '| ' + name, 80)
	assert not CONTROL.search(frame) and cells(frame) == 78 and frame.startswith(shown + ' |') and frame.endswith('| ' + shown)


@posix_only
def test_a_name_with_a_tab_and_a_newline_stays_on_one_row():
	# at an 80-column terminal: three frames, no row of their own and no tab ( 1.68: a row more per frame, each scrolling )
	code = ('import multiCMD\n'
		'for i in range(3): multiCMD.print_progress_bar(i, 0, "/data/run\\tlog\\nold", "Removed: 1.23 K")\n')
	shown = at_a_pty(code, 24, 80)
	assert '\n' not in shown and '\t' not in shown and len(frames(shown)) == 3, shown
	assert [frame.rstrip() for frame in frames(shown)] == ['/data/run?log?old | iteration:%d | Removed: 1.23 K' % i for i in range(3)]
	assert all(cells(frame) == 79 for frame in frames(shown)), shown
