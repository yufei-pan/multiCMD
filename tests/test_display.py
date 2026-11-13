import errno
import io
import sys

import pytest

import multiCMD


@pytest.fixture
def out(capsys, monkeypatch):
	'''What the display wrote ( pytest re-installs its own sys.stdout after fixtures, so read it through capsys )'''
	monkeypatch.setattr(multiCMD, '_stdout_isatty', lambda: False)
	class Captured:
		def getvalue(self):
			return capsys.readouterr().out
	return Captured()


def test_lines_are_written_newline_first(out):
	display = multiCMD._Display()
	display.lines(['a', 'b'])
	assert out.getvalue() == '\na\nb'


def test_message_starts_on_its_own_line(out):
	display = multiCMD._Display()
	display.message('header')
	display.lines(['a'])
	display.message('footer')
	assert out.getvalue() == 'header\n\na\nfooter\n'


def test_redraw_overwrites_the_last_cr_line(out):
	display = multiCMD._Display()
	display.lines(['10%'], ends_with_cr=True)
	display.lines(['20%'], replace_first=True, ends_with_cr=True)
	assert out.getvalue() == '\n10%\r20%'


def test_redraw_after_a_normal_line_starts_a_new_line(out):
	display = multiCMD._Display()
	display.lines(['10%'], ('[1] ', '', ''), ends_with_cr=True)
	display.lines(['other'], ('[2] ', '', ''))
	display.lines(['20%'], ('[1] ', '', ''), replace_first=True, ends_with_cr=True)
	assert out.getvalue() == '\n[1] 10%\n[2] other\n[1] 20%'


def test_two_progress_bars_share_the_last_line(out):
	display = multiCMD._Display()
	display.lines(['10%'], ('[1] ', '', ''), ends_with_cr=True)
	display.lines(['5%'], ('[2] ', '', ''), ends_with_cr=True)
	display.lines(['11%'], ('[1] ', '', ''), replace_first=True, ends_with_cr=True)
	assert out.getvalue() == '\n[1] 10%\n[2] 5%\r[1] 11%'


def test_a_finished_progress_line_stays(out):
	display = multiCMD._Display()
	display.lines(['50%'], ends_with_cr=True)
	display.lines(['100%'], replace_first=True)
	display.lines(['next'], replace_first=True)
	assert out.getvalue() == '\n50%\r100%\nnext'


def test_settle_protects_a_crlf_line(out):
	display = multiCMD._Display()
	display.lines(['a'], ends_with_cr=True)
	display.settle()
	display.lines(['b'], replace_first=True)
	assert out.getvalue() == '\na\nb'


def test_redraw_clears_the_rest_of_the_line_on_a_terminal(out, monkeypatch):
	monkeypatch.setattr(multiCMD, '_stdout_isatty', lambda: True)
	display = multiCMD._Display()
	display.lines(['100%'], ends_with_cr=True)
	display.lines(['5%'], replace_first=True)
	assert out.getvalue() == '\n100%\r5%\x1b[K'


def test_decor_wraps_each_line(out):
	display = multiCMD._Display()
	display.lines(['a'], ('[1] ', '<', '>'))
	display.message('m', ('[1] ', '<', '>'))
	assert out.getvalue() == '\n[1] <a>\n[1] <m>\n'


def test_decor_prefix_is_right_aligned():
	assert multiCMD._decor(3, 2, True, False) == ('[ 3] ', '', '')
	assert multiCMD._decor(3, 0, True, False) == ('[3] ', '', '')
	assert multiCMD._decor(3, 2, False, False) == ('', '', '')


def test_decor_colors_are_deterministic_and_spread():
	colors = [multiCMD._decor(i, 0, False, True)[1] for i in range(1, 11)]
	assert all(color.startswith('\x1b[38;2;') for color in colors)
	assert colors == [multiCMD._decor(i, 0, False, True)[1] for i in range(1, 11)]
	assert len(set(colors)) == 10
	assert multiCMD._decor(1, 0, False, True)[2] == '\x1b[0m'


def test_resolve_color(monkeypatch):
	monkeypatch.delenv('NO_COLOR', raising=False)
	monkeypatch.setattr(multiCMD, '_stdout_isatty', lambda: True)
	assert multiCMD._resolve_color(None, True) is True
	assert multiCMD._resolve_color(None, False) is False
	assert multiCMD._resolve_color(False, True) is False
	assert multiCMD._resolve_color(True, False) is True
	monkeypatch.setenv('NO_COLOR', '1')
	assert multiCMD._resolve_color(None, True) is False
	monkeypatch.setenv('NO_COLOR', '')
	assert multiCMD._resolve_color(None, True) is True
	monkeypatch.setattr(multiCMD, '_stdout_isatty', lambda: False)
	assert multiCMD._resolve_color(None, True) is False


def test_quote_command():
	assert multiCMD._quote_command(['echo', 'a b', "it's"]) == "echo 'a b' 'it'\"'\"'s'"
	assert multiCMD._quote_command('echo hi') == 'echo hi'


class FailingStdout:
	'''A stdout every write and flush of which fails with error'''
	def __init__(self, error):
		self.error = error
		self.writes = 0

	def write(self, text):
		self.writes += 1
		raise self.error

	def flush(self):
		raise self.error


@pytest.mark.parametrize('error, flag', [
	(BrokenPipeError(errno.EPIPE, 'Broken pipe'), 'closed'),
	(ConnectionResetError(errno.ECONNRESET, 'Connection reset by peer'), 'closed'),
	(OSError(errno.EIO, 'Input/output error'), 'lost'),
	(OSError(errno.ENOSPC, 'No space left on device'), 'lost'),
], ids=['EPIPE', 'ECONNRESET', 'EIO', 'ENOSPC'])
def test_a_failed_write_is_classified_once_where_it_is_written(monkeypatch, capfd, error, flag):
	# for mcmd and for a program that imports multiCMD alike: a reader that quit closes the display ( the calls that show
	# commands stop them ), anything else loses the output, said once on stderr; neither raises in the thread that wrote,
	# and every later write is dropped. 1.62 raised it there: a command whose header failed did not run, a reader died
	stdout = FailingStdout(error)
	monkeypatch.setattr(sys, 'stdout', stdout)
	display = multiCMD._Display()
	display.message('header')
	display.lines(['a'])
	display.message('footer')
	display.flush()
	assert (display.closed, display.lost) == (flag == 'closed', flag == 'lost')
	assert stdout.writes == 1
	assert display.lock.acquire(False)
	err = capfd.readouterr().err
	if flag == 'closed':
		assert err == ''
	else:
		assert err.count('multiCMD: cannot write to stdout') == 1 and len(err.splitlines()) == 1


def test_flush_meets_the_same_rule(monkeypatch):
	# what argparse left in the buffer: main()'s final flush goes through the display
	class FlushFails:
		def write(self, text):
			pass

		def flush(self):
			raise OSError(errno.ENOSPC, 'No space left on device')
	monkeypatch.setattr(sys, 'stdout', FlushFails())
	display = multiCMD._Display()
	display.flush()
	assert display.lost and not display.closed
	assert display.lock.acquire(False)


def test_text_the_stdout_encoding_cannot_take_is_written_with_backslash_escapes(monkeypatch):
	# a command's output with an accent, on an ASCII stdout ( Python 3.6 in a C locale ): UnicodeEncodeError killed its reader
	raw = io.BytesIO()
	stdout = io.TextIOWrapper(raw, encoding='ascii', errors='strict', write_through=True)
	monkeypatch.setattr(sys, 'stdout', stdout)
	display = multiCMD._Display()
	display.message('header')
	display.lines(['caf\u00e9', 'plain'])
	assert raw.getvalue() == b'header\n\ncaf\\xe9\nplain'
	assert not display.lost


def test_a_stdout_that_is_none_drops_the_output_silently(monkeypatch, capfd):
	# `mcmd ... >&-` ( and pythonw ): print() ignores it, and so does the display, so every command runs
	monkeypatch.setattr(sys, 'stdout', None)
	display = multiCMD._Display()
	display.message('header')
	display.lines(['a'])
	display.flush()
	assert not display.lost and not display.closed
	assert capfd.readouterr() == ('', '')


def test_losing_the_output_twice_says_so_once(capfd):
	# two writers that both met the failure ( a bar frame racing a live line, in a library program ): the second finds the
	# output already lost, and says nothing
	display = multiCMD._Display()
	display.lose(OSError(errno.EIO, 'Input/output error'))
	display.lose(OSError(errno.ENOSPC, 'No space left on device'))
	err = capfd.readouterr().err
	assert display.lost
	assert err.count('multiCMD: cannot write to stdout') == 1 and '[Errno %d]' % errno.EIO in err and len(err.splitlines()) == 1
