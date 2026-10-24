import errno
import io
import sys

import pytest

import multiCMD


def _reference_handle_stream(stream, target, pre='', post='', quiet=False):
	'''multiCMD 1.51's __handle_stream, verbatim: the behaviour the new reader must match'''
	def add_line(current_line,target, keepLastLine=True):
		if not keepLastLine:
			if not quiet:
				sys.stdout.write('\r')
			target.pop()
		else:
			if not quiet:
				sys.stdout.write('\n')
		current_line_str = current_line.decode('utf-8',errors='backslashreplace')
		target.append(current_line_str)
		if not quiet:
			sys.stdout.write(pre+current_line_str+post)
			sys.stdout.flush()
	current_line = bytearray()
	lastLineCommited = True
	for char in iter(lambda:stream.read(1), b''):
		if char == b'\n':
			if (not lastLineCommited) and current_line:
				add_line(current_line,target, keepLastLine=False)
			elif lastLineCommited:
				add_line(current_line,target, keepLastLine=True)
			current_line = bytearray()
			lastLineCommited = True
		elif char == b'\r':
			add_line(current_line,target, keepLastLine=lastLineCommited)
			current_line = bytearray()
			lastLineCommited = False
		else:
			current_line.extend(char)
	if current_line:
		add_line(current_line,target, keepLastLine=lastLineCommited)


class Chunked(io.RawIOBase):
	'''A raw stream that hands out data at most size bytes per read'''
	def __init__(self, data, size):
		self.data = data
		self.size = size

	def readable(self):
		return True

	def readinto(self, buffer):
		count = min(self.size, len(self.data), len(buffer))
		buffer[:count] = self.data[:count]
		self.data = self.data[count:]
		return count


CASES = [
	b'a\nb\n', b'x\r1\r2\r3\n', b'a\r\nb\r\n', b'\r\rp\n\n', b'no-newline', b'a\n\nb',
	b'p\rq', '✓ ok\n'.encode(), b'\n\n\n', b'p\r\n\nq\n', b'p\rq\nr\n',
	# review focus: invalid UTF-8, and one 200 KB line
	b'\xff\xfe bad utf8\n', b'x' * 200000 + b'\n',
]


def _run(reader, data, size, monkeypatch, quiet):
	shown = io.StringIO()
	monkeypatch.setattr(sys, 'stdout', shown)
	monkeypatch.setattr(multiCMD, '_display', multiCMD._Display())
	target = []
	stream = io.BufferedReader(Chunked(data, size))
	if reader is _reference_handle_stream:
		reader(stream, target, quiet=quiet)
	else:
		reader(stream, target, multiCMD.Task(['x']), quiet=quiet)
	return target, shown.getvalue()


@pytest.mark.parametrize('data', CASES, ids=range(len(CASES)))
@pytest.mark.parametrize('size', [1, 2, 3, 5, 65536])
def test_reader_matches_1_51(data, size, monkeypatch):
	assert _run(multiCMD._read_stream, data, size, monkeypatch, quiet=False) == \
		_run(_reference_handle_stream, data, size, monkeypatch, quiet=False)


def test_reader_quiet_shows_nothing(monkeypatch):
	target, shown = _run(multiCMD._read_stream, b'a\nb\n', 65536, monkeypatch, quiet=True)
	assert target == ['a', 'b']
	assert shown == ''


def test_reader_refreshes_activity_and_closes_stream():
	task = multiCMD.Task(['x'])
	assert task._last_output is None
	stream = io.BufferedReader(Chunked(b'abc', 2))
	multiCMD._read_stream(stream, task.stdout, task, quiet=True)
	assert isinstance(task._last_output, float)
	assert stream.closed
	assert task.stdout == ['abc']


def test_reader_records_nothing_once_finalized():
	task = multiCMD.Task(['x'])
	task._finalized = True
	multiCMD._read_stream(io.BufferedReader(Chunked(b'a\nb\n', 65536)), task.stdout, task, quiet=True)
	assert task.stdout == []


class EioAfterData(io.RawIOBase):
	'''A pty master: data, then EIO once the command side is closed'''
	def __init__(self):
		self.sent = False

	def readable(self):
		return True

	def readinto(self, buffer):
		if self.sent:
			raise OSError(errno.EIO, 'Input/output error')
		self.sent = True
		buffer[:6] = b'done\nx'
		return 6


def test_reader_treats_eio_as_end_of_output():
	task = multiCMD.Task(['x'])
	multiCMD._read_stream(io.BufferedReader(EioAfterData()), task.stdout, task, quiet=True)
	assert task.stdout == ['done', 'x']


class Scripted(io.RawIOBase):
	'''A raw stream that hands out the given chunks one read each, running a hook before a chunk is handed out'''
	def __init__(self, chunks, hooks):
		self.chunks = list(chunks)
		self.hooks = hooks

	def readable(self):
		return True

	def readinto(self, buffer):
		if not self.chunks:
			return 0
		hook = self.hooks.get(len(self.chunks))
		if hook is not None:
			hook()
		chunk = self.chunks.pop(0)
		buffer[:len(chunk)] = chunk
		return len(chunk)


def _read_with_writer(chunks, writes):
	'''Read chunks into a list; writes maps how many chunks are left to what another writer appends to the list first'''
	task = multiCMD.Task(['x'])
	target = []
	hooks = {left: (lambda lines=lines: target.extend(lines)) for left, lines in writes.items()}
	multiCMD._read_stream(io.BufferedReader(Scripted(chunks, hooks)), target, task, quiet=True)
	return target


def test_reader_keeps_a_line_another_writer_appended_to_its_list():
	# the timeout appends 'Timeout!' to task.stderr while the reader holds a '\r' line: it replaces its own line, not the last one
	assert _read_with_writer([b'12\r', b'34\r', b'56'], {2: ['Timeout!']}) == ['56', 'Timeout!']
	assert _read_with_writer([b'ab\r', b'cd\n', b'ef\n'], {2: ['Timeout!']}) == ['cd', 'Timeout!', 'ef']
	assert _read_with_writer([b'ab\r', b'\n', b'x\n'], {2: ['Timeout!']}) == ['ab', 'Timeout!', 'x']
	assert _read_with_writer([b'\r', b'12'], {1: ['Timeout!']}) == ['12', 'Timeout!']
	assert _read_with_writer([b'a\n', b'p\r', b'q\r', b'r'], {2: ['n1', 'n2'], 1: ['n3']}) == ['a', 'r', 'n1', 'n2', 'n3']


def test_reader_replaces_its_own_empty_line_not_another_writers():
	# an empty line is the one str object whoever made it: the reader replaces the line it put, not the other's equal one
	assert _read_with_writer([b'\r', b'12\r', b'34'], {2: ['', 'note']}) == ['34', '', 'note']
