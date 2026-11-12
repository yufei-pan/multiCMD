import errno
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


class ClosedPipe:
	'''A stdout nobody reads: every write fails as a pipe whose reader quit does'''
	def write(self, text):
		raise BrokenPipeError(errno.EPIPE, 'Broken pipe')

	def flush(self):
		pass


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


@pytest.mark.parametrize('error', [BrokenPipeError(errno.EPIPE, 'Broken pipe'), OSError(errno.EIO, 'Input/output error')],
	ids=['EPIPE', 'EIO'])
def test_a_failed_write_loses_the_output_for_the_library(monkeypatch, capfd, error):
	# on_failed is the CLI's: a program that imports multiCMD has no run of multiCMD's to end, whatever the error. 1.62 raised
	# it in the thread that wrote: a command whose header failed did not run, a reader died with a traceback
	stdout = FailingStdout(error)
	monkeypatch.setattr(sys, 'stdout', stdout)
	display = multiCMD._Display()
	display.message('header')
	display.lines(['a'])
	display.flush()
	assert display.lost and not display.closed
	assert stdout.writes == 1
	err = capfd.readouterr().err
	assert err.count('multiCMD: cannot write to stdout') == 1 and len(err.splitlines()) == 1


@pytest.mark.parametrize('flag', ['closed', 'lost'])
def test_on_failed_gets_the_error_once_and_every_later_write_is_dropped(monkeypatch, flag):
	# the CLI's hook sets closed ( a reader that quit ) or lost ( anything else ): either drops the writes that follow
	stdout = FailingStdout(OSError(errno.EIO, 'Input/output error'))
	monkeypatch.setattr(sys, 'stdout', stdout)
	display = multiCMD._Display()
	calls = []
	def failed(error):
		calls.append(error.errno)
		setattr(display, flag, True)
	display.on_failed = failed
	display.message('header')
	display.lines(['a'])
	display.message('footer')
	display.flush()
	assert calls == [errno.EIO]
	assert stdout.writes == 1


def test_flush_meets_the_same_hook(monkeypatch):
	# what argparse left in the buffer: main()'s final flush goes through the display
	class FlushFails:
		def write(self, text):
			pass

		def flush(self):
			raise OSError(errno.ENOSPC, 'No space left on device')
	monkeypatch.setattr(sys, 'stdout', FlushFails())
	display = multiCMD._Display()
	calls = []
	display.on_failed = lambda error: calls.append(error.errno)
	display.flush()
	assert calls == [errno.ENOSPC]
	assert display.lock.acquire(False)


def test_on_failed_may_raise_in_the_writing_thread(monkeypatch):
	# the main thread's answer to a reader that quit ( _stdout_failed ): the write is the one that raises, with the lock let go
	monkeypatch.setattr(sys, 'stdout', ClosedPipe())
	display = multiCMD._Display()
	def stop(error):
		display.closed = True
		raise multiCMD._StdoutClosed()
	display.on_failed = stop
	with pytest.raises(multiCMD._StdoutClosed):
		display.message('header')
	assert display.closed
	assert display.lock.acquire(False)
