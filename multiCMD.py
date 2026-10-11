#!/usr/bin/env python3
# /// script
# requires-python = ">=3.6"
# dependencies = [
#     "argparse",
# ]
# ///
#%% imports
import argparse
import atexit
import collections
import errno
import io
import itertools
import math
import os
import re
import select
import shlex
import shutil
import signal
import string
import subprocess
import sys
import threading
import time
from pprint import pformat

#%% global vars
version = '1.51'
__version__ = version
COMMIT_DATE = '2026-10-10'
# fire-and-forget threads (run_commands with wait_for_return=False) that join_threads() waits on
__running_threads = set()
__running_threads_lock = threading.Lock()
# fire-and-forget threads append themselves here when done, so they get joined and dropped from __running_threads
__finished_threads = collections.deque()
__variables = {}

# immutable helpers compiled once at import time
_BRACKET_RX   = re.compile(r'\[([^\]]+)\]')
_ALPHANUM     = string.digits + string.ascii_letters
_ALPHA_IDX    = {c: i for i, c in enumerate(_ALPHANUM)}

SUDO_PATH = shutil.which("sudo")
USE_SUDO = False
_IS_POSIX = os.name == 'posix'
# Popen(process_group=) exists from Python 3.11; older Pythons start commands through _PGROUP_EXEC
_HAS_PROCESS_GROUP = sys.version_info >= (3, 11)
# exec wrapper for Python < 3.11: joins a new process group, then becomes the command.
# preexec_fn is not safe with threads, and a new session ( setsid ) would hide the terminal from sudo
_PGROUP_EXEC = (
	"import os,sys\n"
	"os.setpgid(0,0)\n"
	"try:\n"
	"\tos.execvp(sys.argv[1],sys.argv[1:])\n"
	"except FileNotFoundError as e:\n"
	"\tsys.stderr.write(str(e)+'\\n');sys.exit(127)\n"
	"except OSError as e:\n"
	"\tsys.stderr.write(str(e)+'\\n');sys.exit(126)\n"
)
# blocked while a command starts and inherited by it: reading the terminal from a background
# process group then fails with EIO instead of the kernel stopping the command
_TTY_SIGNALS = {signal.SIGTTIN, signal.SIGTTOU} if _IS_POSIX else set()
# commands still running: Popen -> process group id ( None when the command has no group of its own )
_live_procs = {}
_live_procs_lock = threading.Lock()
# set once the "cannot use a pty" warning has been printed
_PTY_WARNED = False

#%% objects
class Task:
	def __init__(self, command):
		self.command = command
		self.returncode = None
		self.stdout = []
		self.stderr = []
		self.thread = None
		self.stop = False
		# wall-clock times the command started and finished ( None until then )
		self.start_time = None
		self.end_time = None
		# monotonic time of the latest output; drives the inactivity timeout
		self._last_output = None
		# set once the command is done with; readers still draining after a kill stop recording
		self._finalized = False
		# whether stdout and stderr were merged into one pty
		self._pty = False

	def __iter__(self):
		yield from (
			('command', self.command),
			('returncode', self.returncode),
			('stdout', self.stdout),
			('stderr', self.stderr)
		)

	def __repr__(self):
		return f"""Task(command={pformat(self.command)},
returncode={self.returncode!r},
stdout=
{pformat(self.stdout)},
stderr=
{pformat(self.stderr)},
stop={self.stop!r})"""

	def __str__(self):
		return pformat(dict(self))

	def is_alive(self):
		return self.thread is not None and self.thread.is_alive()

	@property
	def duration(self):
		'''
		How long the command ran, in seconds ( None if it has not both started and finished )
		'''
		if self.start_time is None or self.end_time is None:
			return None
		return self.end_time - self.start_time

class _HeldPermit:
	'''
	Semaphore stand-in for a command whose permit the dispatcher already holds ( internal )
	Entering is free, exiting releases the permit.
	'''
	def __init__(self, semaphore):
		self.semaphore = semaphore

	def __enter__(self):
		return self

	def __exit__(self, *exc_info):
		self.semaphore.release()
		return False

class _Display:
	'''
	Writes multiCMD's live output to sys.stdout, one write per call under one lock ( internal )

	Command output lines are written newline-first, so the cursor stays at the end of the last
	line. redrawable is True when that last line ended with '\r': the next line that replaces a
	'\r' line, from any command, then overwrites it in place, and anything else starts a new line.
	'''
	def __init__(self):
		self.lock = threading.Lock()
		self.redrawable = False
		self.at_line_start = True

	def lines(self, texts, decor=('', '', ''), replace_first=False, ends_with_cr=False):
		'''
		Show consecutive output lines of one command
		@params:
			texts: The lines ( list[str] )
			decor: ( prefix, color start, color end ) for this command
			replace_first: Whether the first line replaces this command's previous '\r' line
			ends_with_cr: Whether the last line ended with '\r', so the next line may replace it
		'''
		prefix, start, end = decor
		with self.lock:
			parts = []
			for i, text in enumerate(texts):
				if i == 0 and replace_first and self.redrawable:
					parts.append('\r' + prefix + start + text + end + ('\x1b[K' if _stdout_isatty() else ''))
				else:
					parts.append('\n' + prefix + start + text + end)
			self.redrawable = ends_with_cr
			self.at_line_start = False
			sys.stdout.write(''.join(parts))
			sys.stdout.flush()

	def message(self, text, decor=('', '', '')):
		'''
		Show a status line ( or several, split on '\n' ) on lines of its own
		@params:
			text: The text
			decor: ( prefix, color start, color end ) for this command
		'''
		prefix, start, end = decor
		with self.lock:
			lead = '' if self.at_line_start else '\n'
			body = '\n'.join(prefix + start + line + end for line in text.split('\n'))
			sys.stdout.write(lead + body + '\n')
			sys.stdout.flush()
			self.redrawable = False
			self.at_line_start = True

	def settle(self):
		'''
		Mark the last line as finished, so no redraw overwrites it ( a '\r\n' line ending )
		'''
		with self.lock:
			self.redrawable = False

_display = _Display()

class AsyncExecutor:
	def __init__(self, max_threads=1,semaphore=...,timeout=0,quiet=True,dry_run=False,parse=False):
		'''
		AsyncExecutor class to run commands in parallel asynchronously

		Commands are queued and a dispatcher thread starts each one only after taking a permit
		from the semaphore, so the executor holds at most one thread per permit it uses plus the
		dispatcher, however many commands are queued. A command's task.thread is None until it
		starts, and goes back to None once it has finished and the executor has joined its thread,
		so finished tasks do not keep their Thread objects.
		@params:
			max_threads: The maximum number of threads to use ( int ) ( Note: if passing semaphore, this likely will be ignored )
			semaphore: The semaphore to use for threading ( threading.Semaphore )
			timeout: The timeout for each command ( int )
			quiet: Whether to suppress output ( bool )
			dry_run: Whether to simulate running the commands ( bool )
			parse: Whether to parse ranged input ( bool )
		'''
		self.max_threads = max_threads
		if semaphore is ...:
			semaphore = threading.Semaphore(max_threads)
		self.semaphore = semaphore
		self.tasks = []
		self.timeout = timeout
		self.quiet = quiet
		self.dry_run = dry_run
		self.parse = parse
		self._lock = threading.Lock()
		# (task, sem, timeout, quiet, dry_run) waiting for a permit
		self._queue = collections.deque()
		self._dispatching = False
		# started threads not yet joined, in start order ( dict used as an ordered set )
		self._threads = {}
		# threads append themselves here when done, so _reap() joins them without scanning
		self._finished = collections.deque()

	def __iter__(self):
		return iter(self.tasks)
	
	def __repr__(self):
		return f'AsyncExecutor(max_threads={self.max_threads}, semaphore={self.semaphore}, runningThreads={self.runningThreads}, tasks={self.tasks}, timeout={self.timeout}, quiet={self.quiet}, dry_run={self.dry_run}, parse={self.parse})'
	
	def __str__(self):
		return str(self.tasks)
	
	def __len__(self):
		return len(self.tasks)
	
	def __bool__(self):
		return bool(self.tasks)

	@property
	def runningThreads(self):
		'''
		The threads that have not finished yet: the dispatcher while commands are queued, and the started commands
		@returns:
			list[threading.Thread]: The unfinished threads
		'''
		self._reap()
		with self._lock:
			return list(self._threads)

	def _reap(self):
		'''
		Join the threads that finished and forget them ( internal )
		Joining releases the thread's stack: a finished thread that is still referenced but never
		joined keeps its stack mapped.
		'''
		while True:
			try:
				thread, task = self._finished.popleft()
			except IndexError:
				return
			thread.join()
			with self._lock:
				self._threads.pop(thread, None)
				# a finished Thread object costs ~2 KB, and the task outlives it in self.tasks
				if task is not None and task.thread is thread:
					task.thread = None

	def _start_thread(self, target, args, task=None):
		thread = threading.Thread(target=target, args=args, daemon=True)
		# registered before it starts, so it is tracked before it can report itself finished
		with self._lock:
			self._threads[thread] = None
			if task is not None:
				# set before it starts, so _reap() never sees the task without its thread
				task.thread = thread
			try:
				thread.start()
			except BaseException:
				# never started: it would never report itself finished
				del self._threads[thread]
				if task is not None:
					task.thread = None
				raise
		return thread

	def _dispatch(self):
		'''
		Start queued commands one permit at a time ( internal )
		'''
		try:
			while True:
				with self._lock:
					if not self._queue:
						self._dispatching = False
						break
					task, sem, timeout, quiet, dry_run = self._queue.popleft()
				# wait for a permit, but give up on a command that gets stopped meanwhile
				acquired = False
				while not task.stop and not acquired:
					acquired = sem.acquire(timeout=0.1)
				if task.stop:
					if acquired:
						sem.release()
					continue
				self._reap()
				try:
					self._start_thread(_run_executor_task, (task, _HeldPermit(sem), timeout, quiet, dry_run, self._finished), task)
				except BaseException:
					sem.release()
					raise
		except BaseException:
			# let the next run_commands() start a new dispatcher for what is still queued
			with self._lock:
				self._dispatching = False
			raise
		finally:
			self._finished.append((threading.current_thread(), None))

	def run_commands(self, commands, timeout=...,max_threads=...,quiet=...,dry_run=...,parse = ...,sem = ...):
		'''
		Run multiple commands in parallel asynchronously. Returns right away, the commands are queued
		@params:
			commands: A list of commands to run ( list[str] | list[list[str]] )
			timeout: The timeout for each command to override the object default
			max_threads: The maximum number of threads to use to override the object default
			quiet: Whether to suppress output to override the object default
			dry_run: Whether to simulate running the commands to override the object default
			parse: Whether to parse ranged input to override the object default
			sem: The semaphore to use for threading to override the object default
		@returns:
			list: The Task Object of the commands ( list[Task] )
		'''
		if timeout is ...:
			timeout = self.timeout
		if max_threads is ...:
			max_threads = self.max_threads
		if quiet is ...:
			quiet = self.quiet
		if dry_run is ...:
			dry_run = self.dry_run
		if parse is ...:
			parse = self.parse
		if sem is ...:
			sem = self.semaphore
		taskObjects, use_sudo = _build_tasks(commands, parse=parse)
		if not sem:
			sem = threading.Semaphore(max_threads if max_threads >= 1 else max(len(taskObjects), 1))
		if use_sudo:
			_validate_sudo()
		self._reap()
		with self._lock:
			self.tasks.extend(taskObjects)
			self._queue.extend((task, sem, timeout, quiet, dry_run) for task in taskObjects)
			start_dispatcher = not self._dispatching and bool(self._queue)
			if start_dispatcher:
				# only the dispatcher clears this, under the lock, once it sees the queue empty
				self._dispatching = True
		if start_dispatcher:
			try:
				self._start_thread(self._dispatch, ())
			except BaseException:
				with self._lock:
					self._dispatching = False
				raise
		return taskObjects
	
	def run_command(self, command, timeout=...,max_threads=...,quiet=...,dry_run=...,parse = ...,sem = ...):
		'''
		Run a command in parallel asynchronously
		@params:
			command: The command to run ( str | list[str] )
			timeout: The timeout for each command to override the object default
			max_threads: The maximum number of threads to use to override the object default
			quiet: Whether to suppress output to override the object default
			dry_run: Whether to simulate running the commands to override the object default
			parse: Whether to parse ranged input to override the object default
			sem: The semaphore to use for threading to override the object default
		@returns:
			Task: The Task Object of the command
		'''
		return self.run_commands([command],timeout=timeout,max_threads=max_threads,quiet=quiet,dry_run=dry_run,parse=parse,sem=sem)[0]
	
	def wait(self, timeout=..., threads = ...):
		'''
		Wait for the threads to finish
		@params:
			timeout: The timeout for each thread to override the object default ( negative waits forever )
			threads: The threads to join, default to all running threads managed by this object,
				including the ones the dispatcher starts while waiting
		@returns:
			list[threading.Thread]: The list of running threads that are still running
		'''
		if timeout is ...:
			timeout = self.timeout
		while True:
			pending = self.runningThreads if threads is ... else list(threads)
			for thread in pending:
				if timeout >= 0:
					thread.join(timeout=timeout)
				else:
					thread.join()
			# once a pass finishes every thread, look again for commands the dispatcher started meanwhile
			if threads is not ... or not pending or any(thread.is_alive() for thread in pending):
				break
		return self.runningThreads
	
	def stop(self,timeout=...):
		'''
		Stop all running threads. This signals all threads to stop and joins them.
		Commands still queued are skipped: they keep task.thread None and returncode None
		@params:
			None
		@returns:
			list[Task]: The list of tasks that are managed by this object
		'''
		for task in self.tasks:
			task.stop = True
		self.wait(timeout)
		return self.tasks
	
	def cleanup(self,timeout=...):
		'''
		Cleanup the tasks and threads. This calls stop() and clears the tasks and threads
		@params:
			None
		@returns:
			list[Task]: The list of tasks that are managed by this object
		'''
		self.stop(timeout)
		with self._lock:
			self.tasks = []
			self._queue.clear()
			self._threads.clear()
		return self.tasks
	
	def join(self, timeout=..., threads = ..., print_error=True):
		'''
		Wait for the threads to finish and print error if there is any
		@params:
			timeout: The timeout for each command to override the object default
			threads: The threads to wait for, default to all running threads managed by this object
		@returns:
			list[Task]: The list of tasks that are managed by this object
		'''
		self.wait(timeout=timeout, threads=threads)
		for task in self.tasks:
			if task.returncode != 0 and print_error:
				print(f'Command: {task.command} failed with return code: {task.returncode}')
				print('Stdout:')
				print('\n  '.join(task.stdout))
				print('Stderr:')
				print('\n  '.join(task.stderr))
		return self.tasks
	
	def get_results(self, with_stdErr=False):
		'''
		Get the results of the tasks
		@params:
			with_stdErr: Whether to append the standard error output to the standard output	
		@returns:
			list[list[str]]: The output of the tasks ( list[list[str]] )
		'''
		if with_stdErr:
			return [task.stdout + task.stderr for task in self.tasks]
		else:
			return [task.stdout for task in self.tasks]
		
	def get_return_codes(self):
		'''
		Get the return codes of the tasks
		@params:
			None
		@returns:
			list[int]: The return codes of the tasks ( list[int] )
		'''
		return [task.returncode for task in self.tasks]

#%% helper functions
def __evaluate_value(expr, vars_,dereference=True):
	"""Evaluate an expression with variables."""
	if expr.startswith('{') and expr.endswith('}'):
		try:
			return eval(expr[1:-1], {}, vars_)
		except Exception:
			return expr
	elif dereference:
		return vars_.get(expr, expr)
	else:
		return expr

def _expand_piece(piece, vars_):
	"""Turn one comma-separated element from inside [...] into a list of strings."""
	piece = piece.strip()

	# variable assignment  foo:BAR
	if ':' in piece:
		var, _, value = piece.partition(':')
		vars_[var] = __evaluate_value(value.strip(), vars_, dereference=False)
		return

	# explicit range  start-end
	if '-' in piece:
		start, _, end = (p.strip() for p in piece.partition('-'))

		start = str(__evaluate_value(start, vars_))
		end   = str(__evaluate_value(end,   vars_))

		if start.isdigit() and end.isdigit():
			pad = min(len(start), len(end))
			return [f"{i:0{pad}d}" for i in range(int(start), int(end) + 1)]

		if all(c in string.hexdigits for c in start + end):
			return [format(i, 'x') for i in range(int(start, 16),int(end,16) + 1)]

		# alphanumeric range (0-9a-zA-Z)
		try:
			return [_ALPHANUM[i] for i in range(_ALPHA_IDX[start], _ALPHA_IDX[end] + 1)]
		except KeyError:
			pass

	# plain token or ${var}
	value = __evaluate_value(piece, vars_)
	if isinstance(value, str):
		return [value]
	else:
		try:
			return [str(v) for v in value]
		except Exception:
			return [str(value)] 

def _expand_ranges_fast(inStr):
	global __variables
	segments = []
	pos = 0
	# split the template into literal pieces + expandable pieces
	for match in _BRACKET_RX.finditer(inStr):
		if match.start() > pos:
			segments.append([inStr[pos:match.start()]])
		choices = []
		for sub in match.group(1).split(','):
			expandedPieces = _expand_piece(sub, __variables)
			if expandedPieces:
				choices.extend(expandedPieces)
		if choices:
			segments.append(choices)
		pos = match.end()
	segments.append([inStr[pos:]])

	# cartesian product of all segments
	return [''.join(parts) for parts in itertools.product(*segments)]

# backwards-compatible alias for the pre-rename public name
_expand_ranges = _expand_ranges_fast

def _read_stream(stream, target, task, decor=('', '', ''), quiet=False):
	'''
	Read one output stream of a command into target, showing complete lines live ( internal )

	A '\n' ends a line. A '\r' ends a line that the next line replaces, like a terminal does, both
	in target and on screen. Chunks without '\r' are split in one pass; chunks with '\r' are
	handled separator by separator. Every chunk refreshes task._last_output, which drives the
	inactivity timeout. Once the task is finalized the reader keeps draining but records nothing.
	The reader closes its stream when done.

	@params:
		stream: The binary stream to read ( a pipe, or a pty master )
		target: The list the lines go into ( task.stdout or task.stderr )
		task: The Task the stream belongs to
		decor: ( prefix, color start, color end ) for displayed lines
		quiet: Whether to skip showing the lines
	'''
	def emit(lines, replace_first, ends_with_cr):
		if task._finalized:
			return
		texts = [line.decode('utf-8', errors='backslashreplace') for line in lines]
		if replace_first:
			target.pop()
		target.extend(texts)
		if not quiet:
			_display.lines(texts, decor, replace_first, ends_with_cr)
	def settle():
		# a '\n' right after '\r' ( a CRLF ending ): the redrawn line is complete, nothing may overwrite it now
		if not quiet and not task._finalized:
			_display.settle()
	# bytearray: a long line arriving in many small chunks grows in linear time
	current = bytearray()
	committed = True
	try:
		while True:
			try:
				chunk = stream.read1(65536)
			except OSError as e:
				# a pty master reports EIO once the command's side is closed
				if e.errno == errno.EIO:
					break
				raise
			if not chunk:
				break
			task._last_output = time.monotonic()
			if b'\r' not in chunk:
				# fast path: split the whole chunk in one pass
				parts = chunk.split(b'\n')
				if len(parts) == 1:
					current += chunk
					continue
				parts[0] = bytes(current) + parts[0]
				current = bytearray(parts.pop())
				if committed:
					emit(parts, False, False)
				elif parts[0]:
					# completes the line that '\r' updates were redrawing
					emit(parts, True, False)
				else:
					settle()
					if len(parts) > 1:
						emit(parts[1:], False, False)
				committed = True
				continue
			# a chunk with '\r': handle it separator by separator
			pos = 0
			size = len(chunk)
			while pos < size:
				newline = chunk.find(b'\n', pos)
				carriage = chunk.find(b'\r', pos)
				end = newline if carriage < 0 or 0 <= newline < carriage else carriage
				if end < 0:
					current += chunk[pos:]
					break
				current += chunk[pos:end]
				if chunk[end] == 10:  # '\n'
					if not committed and current:
						emit([bytes(current)], True, False)
					elif committed:
						emit([bytes(current)], False, False)
					else:
						settle()
					committed = True
				else:  # '\r'
					emit([bytes(current)], not committed, True)
					committed = False
				current = bytearray()
				pos = end + 1
		if current:
			emit([bytes(current)], not committed, False)
	finally:
		try:
			stream.close()
		except Exception:
			pass

def int_to_color(hash_value, min_brightness=100,max_brightness=220):
	'''
	Deterministically map an integer to a readable RGB color.

	The three low bytes of the value seed the hue, then the color is scaled so
	its perceived brightness lands within [min_brightness, max_brightness].
	Brightness scales linearly with the RGB vector, so we can hit the target in
	one step. This avoids the previous approach of recursing on hash(str(...)),
	which was non-deterministic (str hashing is randomized per process) and
	could recurse deeply before finding an in-range color.

	@params:
		hash_value: The integer to map to a color
		min_brightness: The minimum perceived brightness (0-255)
		max_brightness: The maximum perceived brightness (0-255)

	@returns:
		(int,int,int): The RGB color
	'''
	def _brightness(r, g, b):
		return math.sqrt(0.299 * r**2 + 0.587 * g**2 + 0.114 * b**2)
	r = (hash_value >> 16) & 0xFF
	g = (hash_value >> 8) & 0xFF
	b = hash_value & 0xFF
	brightness = _brightness(r, g, b)
	if brightness == 0:
		# pure black has no hue to preserve; fall back to a neutral mid gray
		gray = int((min_brightness + max_brightness) / 2)
		return (gray, gray, gray)
	if brightness > max_brightness:
		scale = max_brightness / brightness
		r, g, b = r * scale, g * scale, b * scale
	elif brightness < min_brightness:
		scale = min_brightness / brightness
		r, g, b = min(255, r * scale), min(255, g * scale), min(255, b * scale)
		# Channels can clamp at 255 (e.g. a saturated blue can never reach the
		# target on its own because of blue's low luminance weight), so blend
		# toward white to make up the difference. Brightness rises monotonically
		# toward white (255,255,255), so a bounded binary search converges
		# without recursion.
		if _brightness(r, g, b) < min_brightness:
			lo, hi = 0.0, 1.0
			for _ in range(20):
				t = (lo + hi) / 2
				if _brightness(r + t * (255 - r), g + t * (255 - g), b + t * (255 - b)) < min_brightness:
					lo = t
				else:
					hi = t
			r, g, b = r + hi * (255 - r), g + hi * (255 - g), b + hi * (255 - b)
	return (min(255, round(r)), min(255, round(g)), min(255, round(b)))

def set_sudo(use_sudo):
	'''
	Set whether to use sudo for commands

	@params:
		use_sudo: Whether to use sudo

	@returns:
		None
	'''
	global USE_SUDO
	global SUDO_PATH
	if not use_sudo:
		USE_SUDO = False
		return
	# os.geteuid() is POSIX only; on platforms without it we cannot be root.
	if getattr(os, 'geteuid', lambda: 1)() == 0:
		USE_SUDO = False
	elif SUDO_PATH:
		USE_SUDO = True
	else:
		print("sudo not found in PATH, cannot use sudo. ignoring it...", file=sys.stderr)
		USE_SUDO = False

def _spawn(argv, stdout, stderr, env=None):
	'''
	Start a command in a process group of its own ( internal )

	The group lets a timeout / stop / Ctrl-C reach everything the command starts. SIGTTIN and
	SIGTTOU are blocked while it starts and the command inherits that, so if it reads the
	terminal from its background group the read fails with EIO instead of the kernel stopping it.
	Python 3.11+ uses Popen(process_group=0); older Pythons run the command through the exec
	wrapper _PGROUP_EXEC. Windows gets a plain Popen.

	@params:
		argv: The command ( list[str] )
		stdout: Where the command's stdout goes ( subprocess.PIPE or a file descriptor )
		stderr: Where the command's stderr goes
		env: The environment, None to inherit

	@returns:
		( subprocess.Popen, int | None ): The process and its process group id ( None without a group of its own )
	'''
	kwargs = {'stdin': subprocess.DEVNULL, 'stdout': stdout, 'stderr': stderr, 'env': env}
	if not _IS_POSIX:
		return subprocess.Popen(argv, **kwargs), None
	grouped = True
	if _HAS_PROCESS_GROUP:
		kwargs['process_group'] = 0
	elif sys.executable:
		# the wrapper would only report a missing command through its exit code; fail like Popen does
		search_path = (env if env is not None else os.environ).get('PATH')
		if os.sep not in argv[0] and shutil.which(argv[0], path=search_path) is None:
			raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), argv[0])
		argv = [sys.executable, '-I', '-S', '-c', _PGROUP_EXEC] + list(argv)
	else:
		grouped = False
	old_mask = signal.pthread_sigmask(signal.SIG_BLOCK, _TTY_SIGNALS)
	try:
		proc = subprocess.Popen(argv, **kwargs)
	finally:
		signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)
	return proc, (proc.pid if grouped else None)

def _stdout_isatty():
	'''
	Whether sys.stdout is a terminal ( internal )
	'''
	try:
		return sys.stdout.isatty()
	except Exception:
		return False

def _quote_command(command):
	'''
	Join a command's arguments into one line a POSIX shell splits back the same way ( internal )
	@params:
		command: The command ( list[str] | str )
	@returns:
		str: The quoted command line
	'''
	if isinstance(command, str):
		return command
	return ' '.join(shlex.quote(str(arg)) for arg in command)

def _decor(index, width, prefix, color):
	'''
	The ( prefix, color start, color end ) a command's displayed lines get ( internal )
	@params:
		index: The command's 1-based position in its batch
		width: The width to right-align the index to ( 0: no padding )
		prefix: Whether to show '[index] '
		color: Whether to color the lines
	@returns:
		( str, str, str ): The prefix text, the color start sequence and the color end sequence
	'''
	pre = '[%*d] ' % (width, index) if prefix else ''
	if not color:
		return (pre, '', '')
	# spread consecutive indexes across the color space; the same index always gets the same color
	r, g, b = int_to_color((index * 0x9E3779B1) & 0xFFFFFF)
	return (pre, f'\x1b[38;2;{r};{g};{b}m', '\x1b[0m')

def _resolve_color(color, concurrent):
	'''
	Whether displayed lines get colors ( internal )
	@params:
		color: True / False to force, None for auto: only for concurrent runs on a terminal without NO_COLOR
		concurrent: Whether more than one command can run at once
	@returns:
		bool: Whether to color
	'''
	if color is not None:
		return bool(color)
	return concurrent and _stdout_isatty() and not os.environ.get('NO_COLOR')

def _register(proc, pgid):
	'''
	Track a running command so kill_all() can reach it ( internal )
	'''
	with _live_procs_lock:
		_live_procs[proc] = pgid

def _unregister(proc):
	'''
	Stop tracking a command once it is reaped ( internal )
	'''
	with _live_procs_lock:
		_live_procs.pop(proc, None)

def _send(proc, pgid, sig):
	'''
	Send sig to a command's process group, or to the process alone without a group ( internal )
	'''
	try:
		if pgid is not None:
			os.killpg(pgid, sig)
		elif proc.poll() is None:
			proc.send_signal(sig)
	except OSError:
		# already gone, or not ours to signal ( a root command under sudo: sudo relays the signal )
		pass

def _group_alive(proc, pgid):
	'''
	Whether anything in a command's process group is still running ( internal )
	'''
	if pgid is None:
		return proc.poll() is None
	try:
		os.killpg(pgid, 0)
	except ProcessLookupError:
		return False
	except OSError:
		pass
	return True

def _wait_all(commands, timeout):
	'''
	Wait up to timeout seconds in total for commands to exit ( internal )
	@returns:
		bool: Whether all of them exited
	'''
	deadline = time.monotonic() + timeout
	for proc, _ in commands:
		try:
			proc.wait(timeout=max(0, deadline - time.monotonic()))
		except subprocess.TimeoutExpired:
			pass
	return all(proc.poll() is not None for proc, _ in commands)

def _kill_commands(commands, first_signal):
	'''
	Stop commands and everything they started ( internal )

	POSIX: first_signal, 0.01s later SIGTERM and SIGCONT ( a stopped process acts on signals only
	once continued ), up to 1s for the commands to exit, then SIGKILL for any group that still has
	members and up to 1s more. Every signal goes to the whole process group, and each step goes to
	every command before waiting, so the total stays near 2s however many commands there are.
	Windows: terminate(), up to 1s, kill(), up to 1s.

	@params:
		commands: ( Popen, process group id | None ) pairs
		first_signal: The first signal to send ( POSIX only )
	'''
	commands = list(commands)
	if not commands:
		return
	if not _IS_POSIX:
		for proc, _ in commands:
			if proc.poll() is None:
				try:
					proc.terminate()
				except OSError:
					pass
		if not _wait_all(commands, 1):
			for proc, _ in commands:
				if proc.poll() is None:
					try:
						proc.kill()
					except OSError:
						pass
			_wait_all(commands, 1)
		return
	for proc, pgid in commands:
		_send(proc, pgid, first_signal)
	time.sleep(0.01)
	for proc, pgid in commands:
		_send(proc, pgid, signal.SIGTERM)
		_send(proc, pgid, signal.SIGCONT)
	_wait_all(commands, 1)
	leftovers = [(proc, pgid) for proc, pgid in commands if _group_alive(proc, pgid)]
	for proc, pgid in leftovers:
		_send(proc, pgid, signal.SIGKILL)
	_wait_all(leftovers, 1)

def kill_all(sig=None):
	'''
	Stop every command multiCMD started that is still running, and everything those commands started

	Commands run in process groups of their own, so signals sent to your program's group ( e.g. by
	`timeout`, or a closed terminal ) do not reach them. Call this from your own SIGTERM / SIGHUP
	handlers. A multiCMD call waiting on the main thread stops its own commands on Ctrl-C, but one
	waiting on another thread never sees the interrupt: a program that runs multiCMD from its own
	threads should call this from its KeyboardInterrupt handler. multiCMD also calls it when the
	interpreter exits.

	@params:
		sig: The first signal to send ( default SIGTERM; POSIX only ), followed by SIGTERM and SIGKILL as needed

	@returns:
		None
	'''
	with _live_procs_lock:
		commands = list(_live_procs.items())
	_kill_commands(commands, signal.SIGTERM if sig is None else sig)

atexit.register(kill_all)

def _resolve_pty(pty):
	'''
	Whether commands write to a pty: only on POSIX, with a one-time warning elsewhere ( internal )
	'''
	global _PTY_WARNED
	if not pty:
		return False
	if _IS_POSIX:
		return True
	if not _PTY_WARNED:
		_PTY_WARNED = True
		print('multiCMD: pty mode needs a POSIX system, using pipes', file=sys.stderr)
	return False

def _open_pty(reserved=0):
	'''
	Open a pseudo-terminal for a command's output ( internal )
	Output post-processing is off, so bytes arrive exactly as written ( no '\n' to '\r\n' ), and
	the window size is the real terminal's, less the reserved columns ( the [#] prefix ).
	The pty is only the command's output, never its controlling terminal.
	@params:
		reserved: Columns to take off the terminal width
	@returns:
		( int, int ) | ( None, None ): The master and slave file descriptors, or None when no pty can be opened
	'''
	global _PTY_WARNED
	try:
		import pty as _pty_module
		master, slave = _pty_module.openpty()
	except (ImportError, OSError) as e:
		if not _PTY_WARNED:
			_PTY_WARNED = True
			print(f'multiCMD: cannot open a pty ({e}), using pipes', file=sys.stderr)
		return None, None
	try:
		import fcntl
		import struct
		import termios
		attrs = termios.tcgetattr(slave)
		attrs[1] &= ~termios.OPOST
		termios.tcsetattr(slave, termios.TCSANOW, attrs)
		columns, rows = get_terminal_size()[:2]
		fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', rows, max(columns - reserved, 20), 0, 0))
	except Exception:
		pass
	return master, slave

def _watch(task, proc, timeout):
	'''
	Wait for a command to exit ( internal )
	@params:
		task: The Task ( its stop flag and _last_output are watched )
		proc: The command's process
		timeout: Inactivity timeout in seconds ( 0: none )
	@returns:
		None | str: None when it exited, 'stop' when task.stop was set, 'timeout' after timeout seconds without output
	'''
	sleep_time = 1.0e-7 # 100 nanoseconds
	while proc.poll() is None:  # while the process is still running
		if task.stop:
			return 'stop'
		if timeout > 0 and time.monotonic() - task._last_output > timeout:
			return 'timeout'
		time.sleep(sleep_time)
		# exponential backoff
		if sleep_time < 0.001:
			sleep_time *= 2
	return None

def _join(thread, timeout=None):
	'''
	Join a thread in short steps ( internal )
	A plain join() blocks in C, where a KeyboardInterrupt only arrives if the signal happens to
	land on this very thread; short steps let the waiting thread see it within 0.1s.
	@params:
		thread: The thread to join
		timeout: The longest to wait, in seconds ( None: until it ends )
	'''
	deadline = None if timeout is None else time.monotonic() + timeout
	while thread.is_alive():
		step = 0.1 if deadline is None else min(0.1, deadline - time.monotonic())
		if step <= 0:
			return
		thread.join(step)

def _join_readers(readers, limit):
	'''
	Wait for a command's output readers to reach the end of their streams ( internal )
	@params:
		readers: The reader threads
		limit: The longest to wait in total, in seconds ( None: no limit )
	'''
	deadline = None if limit is None else time.monotonic() + limit
	for reader in readers:
		_join(reader, None if deadline is None else max(0, deadline - time.monotonic()))

def _execute(task, timeout, quiet, decor, use_pty=False):
	'''
	Run a task's command to the end: start it, stream its output, enforce the timeout and the stop flag, reap it ( internal )

	After a timeout / stop / Ctrl-C the command is killed with everything it started, and its
	output gets at most one more second to end, so something that escaped the kill cannot hold the
	call. After a normal exit the output is read to its end, however long that takes. The command
	leaves the live registry as soon as it is reaped, so kill_all() never touches what it left
	running in the background on purpose.
	Sets task.returncode ( 124 after a timeout ), start_time and end_time. A KeyboardInterrupt is
	re-raised once the command is dead.

	@params:
		task: The Task
		timeout: Inactivity timeout in seconds ( 0: none )
		quiet: Whether to skip showing output
		decor: ( prefix, color start, color end ) for displayed lines
		use_pty: Whether the command writes to a pty instead of pipes ( POSIX only )
	'''
	master = slave = None
	if use_pty:
		master, slave = _open_pty(len(decor[0]))
	if master is not None:
		try:
			proc, pgid = _spawn(task.command, slave, slave, dict(os.environ, PAGER='cat', GIT_PAGER='cat'))
		except BaseException:
			os.close(master)
			raise
		finally:
			# only the command keeps the terminal side open, so the master sees EIO once it is gone
			os.close(slave)
		task._pty = True
		streams = [(io.open(master, 'rb'), task.stdout)]
	else:
		proc, pgid = _spawn(task.command, subprocess.PIPE, subprocess.PIPE)
		streams = [(proc.stdout, task.stdout), (proc.stderr, task.stderr)]
	task.start_time = time.time()
	task._last_output = time.monotonic()
	_register(proc, pgid)
	readers = [threading.Thread(target=_read_stream, args=(stream, target, task, decor, quiet), daemon=True) for stream, target in streams]
	for reader in readers:
		reader.start()
	interrupted = False
	try:
		reason = _watch(task, proc, timeout)
	except KeyboardInterrupt:
		reason, interrupted = 'stop', True
	if reason == 'timeout':
		task.stderr.append('Timeout!')
	if reason is not None:
		_kill_commands([(proc, pgid)], signal.SIGINT)
	_unregister(proc)
	task.returncode = proc.poll()
	try:
		_join_readers(readers, None if reason is None else 1)
	except KeyboardInterrupt:
		interrupted = True
		_kill_commands([(proc, pgid)], signal.SIGINT)
		_join_readers(readers, 1)
	if reason == 'timeout':
		task.returncode = 124
	elif task.returncode is None:
		# process been killed via sigkill or similar without a normal exit code
		if task.stderr and task.stderr[-1].strip().startswith('Ctrl C detected, Emergency Stop!'):
			task.returncode = 137
		else:
			task.returncode = -1
	task.end_time = time.time()
	task._finalized = True
	if interrupted:
		raise KeyboardInterrupt

def __run_command(task,sem, timeout=60, quiet=False,dry_run=False,with_stdErr=False,decor=('', '', ''),use_pty=False):
	'''
	Run a command ( internal )

	@params:
		task: The Task object
		sem: The semaphore
		timeout: Inactivity timeout in seconds. The command is killed, with everything it
			started, once it has produced no output at all for this many seconds. 0 disables it.
		quiet: Whether to suppress output
		dry_run: Whether to only show the command instead of running it
		with_stdErr: Whether to return the standard error output
		decor: ( prefix, color start, color end ) for this command's displayed lines
		use_pty: Whether the command writes to a pty instead of pipes ( POSIX only )

	@returns:
		list[str]: The output of the command
	'''
	with sem:
		if task.stop:
			# stopped before it started: it stays unfinished, like a skipped queued command
			return task.stdout + task.stderr if with_stdErr else task.stdout
		try:
			if dry_run:
				if not quiet:
					_display.message(_quote_command(task.command))
				return task.stdout + task.stderr if with_stdErr else task.stdout
			if not quiet:
				_display.message('Running command: ' + _quote_command(task.command), decor)
				_display.message('-' * 100, decor)
			_execute(task, timeout, quiet, decor, use_pty)
		# if file not found
		except FileNotFoundError as e:
			print(f'Command path not found: {task.command[0]}',file=sys.stderr,flush=True)
			task.stderr.append(str(e))
			task.returncode = 127
		except PermissionError as e:
			print(f'Command cannot be executed: {task.command[0]}',file=sys.stderr,flush=True)
			task.stderr.append(str(e))
			task.returncode = 126
		except Exception as e:
			import traceback
			print(f'Error running command: {task.command}',file=sys.stderr,flush=True)
			print(str(e).split('\n'))
			task.stderr.extend(str(e).split('\n'))
			task.stderr.extend(traceback.format_exc().split('\n'))
			task.returncode = -1
		finally:
			task._finalized = True
		if not quiet:
			_display.message('-' * 100, decor)
			_display.message(f'Process exited with return code {task.returncode}', decor)
		if with_stdErr:
			return task.stdout + task.stderr
		else:
			return task.stdout

def _run_executor_task(task, permit, timeout, quiet, dry_run, finished, decor=('', '', ''), use_pty=False):
	'''
	Run an AsyncExecutor command, then report this thread finished so the executor joins it and drops it from the task ( internal )
	'''
	try:
		__run_command(task, permit, timeout, quiet, dry_run, decor=decor, use_pty=use_pty)
	finally:
		finished.append((threading.current_thread(), task))

def __run_tracked_command(task, sem, timeout, quiet, dry_run, decor=('', '', ''), use_pty=False):
	'''
	Run a fire-and-forget command, then report this thread finished so it leaves __running_threads ( internal )
	'''
	try:
		__run_command(task, sem, timeout, quiet, dry_run, decor=decor, use_pty=use_pty)
	finally:
		__finished_threads.append(threading.current_thread())

def __reap_finished_threads():
	'''
	Join finished fire-and-forget threads and drop them from __running_threads ( internal )
	Joining releases the thread's stack: a finished thread that is still referenced but never
	joined keeps its stack mapped.
	'''
	while True:
		try:
			thread = __finished_threads.popleft()
		except IndexError:
			return
		thread.join()
		with __running_threads_lock:
			__running_threads.discard(thread)

def _split_command(command):
	'''
	Split a command line into arguments the way a POSIX shell would, quotes and backslashes included ( internal )
	@params:
		command: The command line ( str )
	@returns:
		list[str]: The arguments
	@raises:
		ValueError: The command line has an unbalanced quote
	'''
	try:
		return shlex.split(command)
	except ValueError as e:
		raise ValueError(f'cannot parse command {command!r}: {e}') from None

def _shell_argv(command):
	'''
	The argv that runs a command line through the system shell ( internal )
	@params:
		command: The command line, or arguments to quote and join ( str | list[str] )
	@returns:
		list[str]: /bin/sh -c ... on POSIX, %COMSPEC% /c ... on Windows
	'''
	if not isinstance(command, str):
		command = _quote_command(command)
	if _IS_POSIX:
		return ['/bin/sh', '-c', command]
	return [os.environ.get('COMSPEC', 'cmd.exe'), '/c', command]

def _build_tasks(commands, parse=False, use_sudo=..., shell=False):
	'''
	Format commands into Tasks, prefixing sudo when requested and available ( internal )

	@returns:
		tuple[list[Task], bool]: The tasks and whether sudo is used
	'''
	global USE_SUDO
	global SUDO_PATH
	if use_sudo is ...:
		use_sudo = USE_SUDO
	# Guard against requesting sudo when it is not available. Without this,
	# prepending a None SUDO_PATH would crash subprocess.Popen with a TypeError.
	if use_sudo and not SUDO_PATH:
		print("sudo not found in PATH, cannot use sudo. ignoring it...", file=sys.stderr)
		use_sudo = False
	# split the commands in commands if it is a string
	formatedCommands = []
	for command in commands:
		formatedCommands.extend(__format_command(command,expand=parse,shell=shell))
	if use_sudo:
		formatedCommands = [[SUDO_PATH] + command for command in formatedCommands]
	return [Task(command) for command in formatedCommands], bool(use_sudo)

def _validate_sudo():
	'''
	Validate sudo access before running sudo commands ( internal )
	It runs in the foreground on the terminal, so sudo can ask for a password and cache the
	per-terminal ticket that the commands ( in process groups of their own, same session ) then use.
	'''
	try:
		subprocess.run([SUDO_PATH, '-v'])
	except OSError as e:
		print(f'sudo validation failed: {e}', file=sys.stderr)

def __format_command(command,expand = False,shell = False):
	'''
	Format a command

	@params:
		command: The command to format
		expand: Whether to expand ranges
		shell: Whether to run the command through the system shell

	@returns:
		list[list[str]]: The formatted commands sequence
	'''
	if isinstance(command,str):
		if expand:
			commands = _expand_ranges_fast(command)
		else:
			commands = [command]
		if shell:
			return [_shell_argv(command) for command in commands]
		return [_split_command(command) for command in commands]
	# elif it is a iterable
	elif hasattr(command,'__iter__'):
		sanitized_command = []
		for field in command:
			if isinstance(field,str):
				sanitized_command.append(field)
			else:
				sanitized_command.append(repr(field))
		if not expand:
			commands = [sanitized_command]
		else:
			sanitized_expanded_command = [_expand_ranges_fast(field) for field in sanitized_command]
			# now the command had been expanded to list of list of fields with each field expanded to all possible options
			# we need to generate all possible combinations of the fields
			# we will use the cartesian product to do this
			commands = [list(command) for command in itertools.product(*sanitized_expanded_command)]
		if shell:
			return [_shell_argv(command) for command in commands]
		return commands
	else:
		return __format_command(str(command),expand=expand,shell=shell)

#%% core funcitons
def ping(hosts,timeout=1,max_threads=0,quiet=True,dry_run=False,with_stdErr=False,
				return_code_only=False,return_object=False,wait_for_return=True,return_true_false=True):
	'''
	Ping multiple hosts

	@params:
		hosts: The hosts to ping
		timeout: The timeout for the command
		max_threads: The maximum number of threads to use
		quiet: Whether to suppress output
		dry_run: Whether to simulate running the command
		with_stdErr: Whether to append the standard error output to the standard output
		return_code_only: Whether to return only the return code
		return_object: Whether to return the Task object
		wait_for_return: Whether to wait for the return of the command

	@returns:
		None | int | list[str] | Task: The output of the command
	'''
	single_host = False
	if isinstance(hosts,str):
		commands = [f'ping -c 1 {hosts}']
		single_host = True
	else:
		commands = [f'ping -c 1 {host}' for host in hosts]
	if return_true_false:
		return_code_only = True
	results = run_commands(commands, timeout=timeout, max_threads=max_threads, quiet=quiet,
						dry_run=dry_run, with_stdErr=with_stdErr, return_code_only=return_code_only, 
						return_object=return_object,wait_for_return=wait_for_return)
	if return_true_false:
		if single_host:
			return not results[0]
		else:
			return [not result for result in results]
	else:
		if single_host:
			return results[0]
		else:
			return results

def _stop_tasks(tasks):
	'''
	Ask every task's command to stop: running ones are killed, ones not started yet are skipped ( internal )
	'''
	for task in tasks:
		task.stop = True

def _run_pool(tasks, decors, sem, timeout, quiet, dry_run, max_threads, use_pty=False):
	'''
	Run tasks on at most max_threads worker threads and wait for all of them ( internal )
	Each worker takes the next task in order, so at most max_threads commands ( and their two
	output readers each ) exist at any time, however many tasks there are.
	On KeyboardInterrupt every task is stopped: running commands are killed, the rest never start.
	'''
	pending = iter(list(zip(tasks, decors)))
	lock = threading.Lock()
	def worker():
		while True:
			with lock:
				try:
					task, decor = next(pending)
				except StopIteration:
					return
			task.thread = threading.current_thread()
			__run_command(task, sem, timeout, quiet, dry_run, decor=decor, use_pty=use_pty)
	workers = [threading.Thread(target=worker, daemon=True) for _ in range(min(max_threads, len(tasks)))]
	for thread in workers:
		thread.start()
	try:
		for thread in workers:
			_join(thread)
	except KeyboardInterrupt:
		_stop_tasks(tasks)
		for thread in workers:
			_join(thread, 5)
		raise

def run_command(command, timeout=0,max_threads=1,quiet=False,dry_run=False,with_stdErr=False,
				return_code_only=False,return_object=False,wait_for_return=True, sem = None, 
				use_sudo = ..., raise_error = False, prefix = False, color = None, pty = False, shell = False):
	'''
	Run a command

	@params:
		command: The command to run
		timeout: Inactivity timeout in seconds. A command is killed, with everything it started,
			once it has produced no output at all for this many seconds. It is not a limit on
			the total runtime. Use 0 (the default) to disable it.
		max_threads: The maximum number of threads to use
		quiet: Whether to suppress output
		dry_run: Whether to simulate running the command
		with_stdErr: Whether to append the standard error output to the standard output
		return_code_only: Whether to return only the return code
		return_object: Whether to return the Task object
		wait_for_return: Whether to wait for the return of the command
		sem: The semaphore to use for threading
		use_sudo: Whether to use sudo for commands
		raise_error: Whether to raise an error if the command fails
		prefix: Whether to prefix output lines with [#] when commands run at once ( never for one command )
		color: Whether to color output lines ( None: when commands run at once on a terminal without NO_COLOR )
		pty: Whether the command writes to a pseudo-terminal, so it prints live ( POSIX only; stderr is merged into stdout )
		shell: Whether to run the command through the system shell ( /bin/sh -c ), so pipes, ;, > and globs work

	@returns:
		None | int | list[str] | Task: The output of the command
	'''
	return run_commands(commands=[command], timeout=timeout, max_threads=max_threads, quiet=quiet, 
					 dry_run=dry_run, with_stdErr=with_stdErr, return_code_only=return_code_only, 
					 return_object=return_object,parse=False,wait_for_return=wait_for_return,sem=sem,
					 use_sudo=use_sudo, raise_error=raise_error, prefix=prefix, color=color, pty=pty, shell=shell)[0]

def run_commands(commands, timeout=0,max_threads=1,quiet=False,dry_run=False,with_stdErr=False,
				 return_code_only=False,return_object=False, parse = False, wait_for_return = True, 
				 sem : threading.Semaphore = None, use_sudo=..., raise_error = False, prefix = False, color = None, pty = False, shell = False):
	'''
	Run multiple commands in parallel

	@params:
		commands: A list of commands to run ( list[str] | list[list[str]] )
		timeout: Inactivity timeout in seconds. A command is killed, with everything it started,
			once it has produced no output at all for this many seconds. It is not a limit on
			the total runtime. Use 0 (the default) to disable it.
		max_threads: The maximum number of commands to run at once ( 0: all of them )
		quiet: Whether to suppress output
		dry_run: Whether to simulate running the commands
		with_stdErr: Whether to append the standard error output to the standard output
		return_code_only: Whether to return only the return code
		return_object: Whether to return the Task object
		parse: Whether to parse ranged input
		wait_for_return: Whether to wait for the return of the commands
		sem: The semaphore to use for threading
		use_sudo: Whether to use sudo for commands
		raise_error: Whether to raise an error if the command fails
		prefix: Whether to prefix output lines with [#], the command's 1-based position, when commands run at once
		color: Whether to color output lines per command ( None: when commands run at once on a terminal without NO_COLOR )
		pty: Whether the command writes to a pseudo-terminal, so it prints live ( POSIX only; stderr is merged into stdout )
		shell: Whether to run each command through the system shell ( /bin/sh -c ), so pipes, ;, > and globs work

	@returns:
		list: The output of the commands ( list[None] | list[int] | list[list[str]] | list[Task] | None )
	'''
	# initialize the tasks
	tasks, use_sudo = _build_tasks(commands, parse=parse, use_sudo=use_sudo, shell=shell)
	# run the tasks with max_threads. if max_threads is 0, use the number of commands
	if max_threads < 1:
		max_threads = len(tasks)
	# concurrent runs get [#] prefixes ( when asked ) and colors ( on a terminal )
	concurrent = len(tasks) > 1 and max_threads != 1
	use_color = _resolve_color(color, concurrent)
	width = len(str(len(tasks)))
	decors = [_decor(index, width, prefix and concurrent, use_color) for index in range(1, len(tasks) + 1)]
	use_pty = _resolve_pty(pty)
	if not sem:
		sem = threading.Semaphore(max_threads)  # Limit concurrent sessions
	if use_sudo:
		_validate_sudo()
	if not wait_for_return:
		__reap_finished_threads()
		threads = [threading.Thread(target=__run_tracked_command, args=(task,sem,timeout,quiet,dry_run,decor,use_pty),daemon=True) for task, decor in zip(tasks, decors)]
		# tracked before they start, so a thread is always registered before it reports itself finished
		with __running_threads_lock:
			__running_threads.update(threads)
		for thread,task in zip(threads,tasks):
			task.thread = thread
			thread.start()
	elif max_threads > 1:
		_run_pool(tasks, decors, sem, timeout, quiet, dry_run, max_threads, use_pty)
	else:
		try:
			for task, decor in zip(tasks, decors):
				__run_command(task, sem, timeout, quiet, dry_run, decor=decor, use_pty=use_pty)
		except KeyboardInterrupt:
			_stop_tasks(tasks)
			raise
	# return the only the output for the tasks
	if raise_error:
		errors = [task for task in tasks if task.returncode != 0]
		if errors:
			raise Exception(f"Commands failed: {pformat(errors)}")
	if return_code_only:
		return [task.returncode for task in tasks]
	elif return_object:
		return tasks
	elif with_stdErr:
		return [task.stdout + task.stderr for task in tasks]
	else:
		return [task.stdout for task in tasks]

def join_threads(threads=...,timeout=None):
	'''
	Join threads

	@params:
		threads: The threads to join
		timeout: The timeout

	@returns:
		None
	'''
	if threads is ...:
		with __running_threads_lock:
			threads = list(__running_threads)
	for thread in threads:
		thread.join(timeout=timeout)
	__reap_finished_threads()

def main():
	parser = argparse.ArgumentParser(description='Run multiple commands in parallel')
	parser.add_argument('commands', metavar='command', type=str, nargs='+',help='commands to run')
	parser.add_argument('-p','--parse', action='store_true',help='Parse ranged input and expand them into multiple commands')
	parser.add_argument('-t','--timeout', metavar='timeout', type=int, default=60,help='timeout for each command')
	parser.add_argument('-m','--max_threads', metavar='max_threads', type=int, default=1,help='maximum number of threads to use')
	parser.add_argument('--sudo', action='store_true',help='use sudo for commands')
	parser.add_argument('-q','--quiet', action='store_true',help='quiet mode')
	parser.add_argument('-V','--version', action='version', version=f'%(prog)s {version} @ {COMMIT_DATE} by pan@zopyr.us')
	args = parser.parse_args()
	# set_sudo() validates that sudo exists and that we are not already root
	# before enabling it, and updates the USE_SUDO global accordingly.
	set_sudo(args.sudo)
	run_commands(args.commands, args.timeout, args.max_threads, args.quiet,parse = args.parse, with_stdErr=True)

if __name__ == '__main__':
	main()

#%% misc functions
def input_with_timeout_and_countdown(timeout, prompt='Please enter your selection'):
	"""
	Read an input from the user with a timeout and a countdown.

	@params:
		timeout: The timeout in seconds
		prompt: The prompt to display to the user

	@returns:
		str: The input from the user or None if no input was received

	"""
	# Print the initial prompt with the countdown
	print(f"{prompt} [{timeout}s]: ", end='', flush=True)
	# Loop until the timeout
	for remaining in range(timeout, 0, -1):
		# If there is an input, return it
		if sys.stdin in select.select([sys.stdin], [], [], 0)[0]:
			return input().strip()
		# Print the remaining time
		print(f"\r{prompt} [{remaining}s]: ", end='', flush=True)
		# Wait a second
		time.sleep(1)
	# If there is no input, return None
	return None

def pretty_format_table(data, delimiter=None, header=None, full=False, remove_empty_columns=False):
	import re
	version = 1.20
	_ = version
	if not data:
		return ""
	# Normalize input data structure
	if isinstance(data, str):
		data = data.strip("\n").split("\n")
		data = [line.split(delimiter) for line in data]
	elif isinstance(data, dict):
		if isinstance(next(iter(data.values())), dict):
			if not header:
				header = ["key"] + list(next(iter(data.values())).keys())
			data = [[key] + list(value.values()) for key, value in data.items()]
		else:
			data = [[key] + list(value) for key, value in data.items()]
	elif not isinstance(data, list):
		data = list(data)
	if isinstance(data[0], dict):
		if not header:
			header = list(data[0].keys())
		data = [list(item.values()) for item in data]
	elif isinstance(data[0], str):
		data = [row.split(delimiter) for row in data]
	data = [[str(item) for item in row] for row in data]
	if isinstance(header, str):
		header = header.split(delimiter)
	if not header or not any(header):
		header = data[0]
		data = data[1:]
	num_cols = len(header)
	def visible_len(s):
		return len(re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", s))
	# Ensure all rows have the same number of columns
	col_widths = [0] * num_cols
	data_invisible_length = []
	for row in data:
		dil = []
		for item, col_width, cwi in zip(row, col_widths, range(num_cols)):
			vl = visible_len(item)
			if vl > col_width:
				col_widths[cwi] = vl
			dil.append(len(item) - vl)
		data_invisible_length.append(dil)
	cols_to_remove = []
	if remove_empty_columns and not full:
		cols_to_remove = [i for i, col_width in enumerate(col_widths) if col_width == 0]
		header = [item for i, item in enumerate(header) if i not in cols_to_remove]
		col_widths = [w for i, w in enumerate(col_widths) if i not in cols_to_remove]
		num_cols = len(header)
	header_widths = [visible_len(h) for h in header]
	col_widths = [max(w, hw) for w, hw in zip(col_widths, header_widths)]
	header = [item.ljust(col_width + len(item) - visible_len(item)) for item, col_width in zip(header, col_widths)]
	normalized_padded_data = []
	new_data_invisible_length = []
	for row, invisible_lengths in zip(data, data_invisible_length):
		if cols_to_remove:
			row = [item for i, item in enumerate(row) if i not in cols_to_remove]
			invisible_lengths = [il for i, il in enumerate(invisible_lengths) if i not in cols_to_remove]
		if not any(row):
			row = ['-' * col_width for col_width in col_widths]
		elif len(row) < num_cols:
			row = [row[i].ljust(col_widths[i] + invisible_lengths[i]) if i < len(row) else "".ljust(col_widths[i]) for i in range(num_cols)]
		elif len(row) >= num_cols:
			#row = row[:num_cols]
			row = [item.ljust(col_width + invisible_length) for item, col_width, invisible_length in zip(row, col_widths, invisible_lengths)]
		normalized_padded_data.append(row)
		new_data_invisible_length.append(invisible_lengths)
	data = normalized_padded_data
	data_invisible_length = new_data_invisible_length
	column_separator = " | "
	horizontal_separator = "-+-"
	terminal_width = get_terminal_size()[0]
	def table_width(col_widths, sep_len):
		return sum(col_widths) + sep_len * (len(col_widths) - 1)
	def render(header, rows, column_widths, column_separator, horizontal_separator):
		return "\n".join(
		 [column_separator.join(header),
		 horizontal_separator.join("-" * width for width in column_widths),
		 *(column_separator.join(row) for row in rows)
		 ]) + "\n"
	if full or table_width(col_widths, len(column_separator)) <= terminal_width:
		return render(header, data, col_widths, column_separator, horizontal_separator)
	# Use compressed separators (no spaces)
	column_separator = "|"
	horizontal_separator = "+"
	if table_width(col_widths, len(column_separator)) <= terminal_width:
		return render(header, data, col_widths, column_separator, horizontal_separator)
	# Begin column compression
	# Track which columns have been compressed already to header width
	width_diff = [max(col_width - header_width, 0) for col_width, header_width in zip(col_widths, header_widths)]
	total_overflow_width = table_width(col_widths, len(column_separator)) - terminal_width
	for i, diff in sorted(enumerate(width_diff), key=lambda x: -x[1]):
		if total_overflow_width <= 0:
			break
		if diff <= 0:
			continue
		reduce_by = min(diff, total_overflow_width)
		col_widths[i] -= reduce_by
		total_overflow_width -= reduce_by
	def truncate_to_width(string, width,invisible_length):
		s_len = len(string) - invisible_length
		if s_len <= width:
			return string
		rstripedSize = len(string.rstrip()) - invisible_length
		if rstripedSize <= width:
			return string[:width + invisible_length]
		if width < 2:
			if width < 1:
				return ""
			else:
				return '.'
		return string[: width + invisible_length - 2] + ".."
	data = [
		[truncate_to_width(item, col_width, invisible_length) for item, col_width, invisible_length in zip(row, col_widths, dil)]
		for row, dil in zip(data, data_invisible_length)
	]
	header = [item[:col_width] for item, col_width in zip(header, col_widths)]
	return render(header, data, col_widths, column_separator, horizontal_separator)

def parseTable(data,sort=False,min_space=2):
	if isinstance(data, str):
		data = data.strip('\n').split('\n')
	header_line = data[0]
	# Use regex to find column names and their positions
	pattern = r'(\S(?:.*?\S)?)(?=\s{'+ str(min_space) + r',}|\s*$)'
	matches = list(re.finditer(pattern, header_line))
	data_list = [[]]
	columns = []
	for i, match in enumerate(matches):
		col_name = match.group(1)
		data_list[0].append(col_name)
		start = match.start()
		if i + 1 < len(matches):
			end = matches[i+1].start()
		else:
			end = None  # Last column goes till the end
		columns.append((col_name, start, end))	
	for line in data[1:]:
		if not line.strip():
			continue  # Skip empty lines
		row = []
		for col_name, start, end in columns:
			if end is not None:
				value = line[start:end].strip()
			else:
				value = line[start:].strip()
			row.append(value)
		data_list.append(row)
	# sort data_list[1:] by the first column
	if sort:
		data_list[1:] = sorted(data_list[1:], key=lambda x: x[0])
	return data_list

def slugify(value, allow_unicode=False):
	import unicodedata
	"""
	Taken from https://github.com/django/django/blob/master/django/utils/text.py
	Convert to ASCII if 'allow_unicode' is False. Convert spaces or repeated
	dashes to single dashes. Remove characters that aren't alphanumerics,
	underscores, or hyphens. Convert to lowercase. Also strip leading and
	trailing whitespace, dashes, and underscores.
	"""
	value = str(value)
	if allow_unicode:
		value = unicodedata.normalize('NFKC', value)
	else:
		value = unicodedata.normalize('NFKD', value).encode('ascii', 'ignore').decode('ascii')
	value = re.sub(r'[^\w\s-]', '', value.lower())
	return re.sub(r'[-\s]+', '-', value).strip('-_')

def get_terminal_size():
	'''
	Get the terminal size

	@params:
		None

	@returns:
		(int,int): the number of columns and rows of the terminal
	'''
	try:
		import os
		_tsize = os.get_terminal_size()
	except Exception:
		try:
			import fcntl
			import struct
			import termios
			packed = fcntl.ioctl(0, termios.TIOCGWINSZ, struct.pack('HHHH', 0, 0, 0, 0))
			_tsize = struct.unpack('HHHH', packed)[:2]
		except Exception:
			import shutil
			_tsize = shutil.get_terminal_size(fallback=(240, 50))
	return _tsize

def _genrate_progress_bar(iteration, total, prefix='', suffix='',columns=240):
	'''
	Generate a progress bar string

	@params:
		iteration   - Required  : current iteration (Int)
		total       - Required  : total iterations (Int)
		prefix      - Optional  : prefix string (Str)
		suffix      - Optional  : suffix string (Str)
		columns     - Optional  : number of columns in the terminal (Int)

	@returns:
		str: the progress bar string
	'''
	noPrefix = False
	noSuffix = False
	noPercent = False
	noBar = False
	# if total is 0, we don't want to divide by 0
	if total == 0:
		return f'{prefix} iteration:{iteration} {suffix}'.ljust(columns)
	percent = f'|{("{0:.1f}").format(100 * (iteration / float(total)))}% '
	length = columns - len(prefix) - len(suffix) - len(percent) - 3
	if length <= 0:
		length = columns - len(prefix) - len(suffix) - 3
		noPercent = True
	if length <= 0:
		length = columns - len(suffix) - 3
		noPrefix = True
	if length <= 0:
		length = columns - 3
		noSuffix = True
	if length <= 0:
		return f'{prefix}\niteration:\n {iteration}\ntotal:\n {total}\n| {suffix}\n'
	if iteration == 0:
		noBar = True
	filled_length = int(length * iteration // total)
	progress_chars = '▁▂▃▄▅▆▇█'
	fractional_progress = (length * iteration / total) - filled_length
	char_index = int(fractional_progress * (len(progress_chars) - 1))
	bar_char = progress_chars[char_index]
	if filled_length == length:
		bar = progress_chars[-1] * length
	else:
		bar = progress_chars[-1] * filled_length + bar_char + '_' * (length - filled_length)
	lineOut = ''
	if not noPrefix:
		lineOut += prefix
	if not noBar:
		lineOut += f'{bar}'
		if not noPercent:
			lineOut += percent
	else:
		if length >= 16:
			lineOut += ' Calculating... '
	if not noSuffix:
		lineOut += suffix
	return lineOut

def print_progress_bar(iteration, total, prefix='', suffix=''):
	'''
	Call in a loop to create terminal progress bar
	@params:
		iteration   - Required  : current iteration (Int)
		total       - Required  : total iterations (Int)
		prefix      - Optional  : prefix string (Str)
		suffix      - Optional  : suffix string (Str)

	@returns:
		None
	'''
	prefix += ' |' if not prefix.endswith(' |') else ''
	suffix = f'| {suffix}' if not suffix.startswith('| ') else suffix
	try:
		columns, _ = get_terminal_size()
		sys.stdout.write(f'\r{_genrate_progress_bar(iteration, total, prefix, suffix, columns)}')
		sys.stdout.flush()
		if iteration == total and total > 0:
			print(file=sys.stdout)
	except Exception:
		if iteration % 5 == 0:
			print(_genrate_progress_bar(iteration, total, prefix, suffix))

def format_bytes(size, use_1024_bytes=None, to_int=False, to_str=False, str_format=".2f"):
	if to_int or isinstance(size, str):
		if isinstance(size, int):
			return size
		elif isinstance(size, str):
			match = re.match(r"(\d+(\.\d+)?)\s*([a-zA-Z]*)", size)
			if not match:
				if to_str:
					return size
				print(
					"Invalid size format. Expected format: 'number [unit]', "
					"e.g., '1.5 GiB' or '1.5GiB'"
				)
				print(f"Got: {size}")
				return 0
			number, _, unit = match.groups()
			number = float(number)
			unit = unit.strip().lower().rstrip("b")
			if unit.endswith("i"):
				use_1024_bytes = True
			elif use_1024_bytes is None:
				use_1024_bytes = False
			unit = unit.rstrip("i")
			if use_1024_bytes:
				power = 2**10
			else:
				power = 10**3
			unit_labels = {
				"": 0,
				"k": 1,
				"m": 2,
				"g": 3,
				"t": 4,
				"p": 5,
				"e": 6,
				"z": 7,
				"y": 8,
			}
			if unit not in unit_labels:
				if to_str:
					return size
			else:
				if to_str:
					return format_bytes(
						size=int(number * (power**unit_labels[unit])),
						use_1024_bytes=use_1024_bytes,
						to_str=True,
						str_format=str_format,
					)
				return int(number * (power**unit_labels[unit]))
		else:
			try:
				return int(size)
			except Exception:
				return 0
	elif to_str or isinstance(size, int) or isinstance(size, float):
		if isinstance(size, str):
			try:
				size = size.rstrip("B").rstrip("b")
				size = float(size.lower().strip())
			except Exception:
				return size
		if use_1024_bytes or use_1024_bytes is None:
			power = 2**10
			n = 0
			power_labels = {
				0: "",
				1: "Ki",
				2: "Mi",
				3: "Gi",
				4: "Ti",
				5: "Pi",
				6: "Ei",
				7: "Zi",
				8: "Yi",
			}
			while size >= power:
				size /= power
				n += 1
			return f"{size:{str_format}} {' '}{power_labels[n]}".replace("  ", " ")
		else:
			power = 10**3
			n = 0
			power_labels = {
				0: "",
				1: "K",
				2: "M",
				3: "G",
				4: "T",
				5: "P",
				6: "E",
				7: "Z",
				8: "Y",
			}
			while size >= power:
				size /= power
				n += 1
			return f"{size:{str_format}} {' '}{power_labels[n]}".replace("  ", " ")
	else:
		try:
			return format_bytes(float(size), use_1024_bytes)
		except Exception:
			pass
		return 0

