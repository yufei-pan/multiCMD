#!/usr/bin/env python3
# /// script
# requires-python = ">=3.6"
# dependencies = []
# ///
#%% imports
import argparse
import atexit
import collections
import collections.abc
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
import traceback
from pprint import pformat

#%% global vars
version = '1.54'
__version__ = version
COMMIT_DATE = '2026-10-12'
# fire-and-forget threads (run_commands with wait_for_return=False) that join_threads() waits on. multiCMD takes no
# lock for it: each update is one set operation and each snapshot one set.copy(), as for _live_procs below
__running_threads = set()
# unused: multiCMD 1.54 took it around __running_threads. Kept only for code that imported it; never take it
__running_threads_lock = threading.Lock()
# fire-and-forget threads append themselves here when done, so they get joined and dropped from __running_threads
__finished_threads = collections.deque()
# before 3.9.8, and on 3.10.0, a Thread.join() that a Ctrl-C interrupts right after it took the thread's tstate lock
# leaves the lock held, and every later Thread.join() without a timeout blocks for good ( bpo-45274, fixed in 3.9.8 and 3.10.1 )
_JOIN_MAY_HANG = sys.version_info < (3, 9, 8) or sys.version_info[:3] == (3, 10, 0)

# immutable helpers compiled once at import time
_BRACKET_RX   = re.compile(r'\[([^\]]+)\]')
_ALPHANUM     = string.digits + string.ascii_letters
_ALPHA_IDX    = {c: i for i, c in enumerate(_ALPHANUM)}
# a ':' not preceded by a backslash; '\:' is a literal colon inside [...]
_UNESCAPED_COLON_RX = re.compile(r'(?<!\\):')

SUDO_PATH = shutil.which("sudo")
USE_SUDO = False
_IS_POSIX = os.name == 'posix'
# Popen(process_group=) exists from Python 3.11; older Pythons start commands through _PGROUP_EXEC
_HAS_PROCESS_GROUP = sys.version_info >= (3, 11)
# exec wrapper for Python < 3.11: joins a new process group, then becomes the command.
# preexec_fn is not safe with threads, and a new session ( setsid ) would hide the terminal from sudo.
# It runs in this process's own environment ( so it starts wherever this Python does, LD_LIBRARY_PATH and the
# like included ). Its first argument is '-' ( the command inherits the wrapper's environment as it is ) or N:
# the command's own environment is then the N NAME=VALUE entries in the wrapper's variables
# _MULTICMD_ENV_0 .. _MULTICMD_ENV_<N-1>, handed over by exec itself ( nothing can be cut short ), and it
# runs with exactly those, found in their PATH like Python 3.11+'s Popen does. A missing or malformed entry
# stops it with 126 before anything runs. Python 3.7-3.10 coerce a C locale for themselves and set LC_CTYPE
# in the wrapper's environment: only a command started with '-' inherits that ( as it always has ).
_ENV_VAR = '_MULTICMD_ENV_'
_PGROUP_EXEC = (
	"import os,signal,sys\n"
	# Python ignores SIGPIPE / SIGXFSZ at startup and an ignored signal survives exec; give the command the defaults
	"for n in ('SIGPIPE','SIGXFSZ'):\n"
	"\tif hasattr(signal,n):signal.signal(getattr(signal,n),signal.SIG_DFL)\n"
	"os.setpgid(0,0)\n"
	"a=sys.argv\n"
	"c=a[2]\n"
	"v=None\n"
	"if a[1]!='-':\n"
	"\ttry:\n"
	# read, not pop: the command is started with exactly the environment built here, and every removal is a scan of the whole environment
	"\t\tv=dict(os.environb[b'_MULTICMD_ENV_%d'%i].split(b'=',1) for i in range(int(a[1])))\n"
	"\texcept(KeyError,ValueError):\n"
	"\t\tsys.stderr.write('multiCMD: incomplete environment for %r\\n'%c);sys.exit(126)\n"
	"def x(f):\n"
	"\tif v is None:os.execv(f,a[2:])\n"
	"\tos.execve(f,a[2:],v)\n"
	"try:\n"
	"\tif os.sep in c:x(c)\n"
	# a bare name: try the regular files by that name in the PATH directories in turn ( see _spawn ); the first
	# failure other than ENOENT / ENOTDIR says why it cannot run, and without one it is not found
	"\tp=os.environ.get('PATH') if v is None else v.get(b'PATH')\n"
	"\tr=None\n"
	"\tfor d in os.fsdecode(os.defpath if p is None else p).split(os.pathsep):\n"
	"\t\tf=os.path.join(d or os.curdir,c)\n"
	"\t\tif os.path.isfile(f):\n"
	"\t\t\ttry:x(f)\n"
	"\t\t\texcept(FileNotFoundError,NotADirectoryError):pass\n"
	"\t\t\texcept OSError as e:r=r or e\n"
	"\traise r or FileNotFoundError(2,os.strerror(2),c)\n"
	# a plain command reports as it always has; one with an environment as Popen does ( Python 3.11+: the error with the command as given )
	"except OSError as e:\n"
	"\tsys.stderr.write((str(e) if a[1]=='-' else str(OSError(e.errno,e.strerror,c)))+'\\n')\n"
	"\tsys.exit(127 if isinstance(e,FileNotFoundError) else 126)\n"
)
# blocked while a command starts and inherited by it: reading the terminal from a background
# process group then fails with EIO instead of the kernel stopping the command
_TTY_SIGNALS = {signal.SIGTTIN, signal.SIGTTOU} if _IS_POSIX else set()
# commands still running: Popen -> process group id ( None when the command has no group of its own )
_live_procs = {}
# commands that exited while something they started still holds their output open, and a waiting
# call is still reading it: Popen -> process group id. kill_all() reaches these, the exit hook does not
_draining = {}
# multiCMD takes no lock for these: each update is one dict operation and each snapshot one dict.copy(), atomic
# with the GIL and under the dict's own lock without it. A `with lock:` on the main thread can be left holding the
# lock by a Ctrl-C landing right after the acquire ( Python 3.6 - 3.9 and 3.14 ), which would hang kill_all() and
# the exit hook.
# unused: multiCMD 1.54 took it around _live_procs and _draining. Kept only for code that imported it; never take it
_live_procs_lock = threading.Lock()
# set once the "cannot use a pty" warning has been printed
_PTY_WARNED = False
# set by the exit hook: from then on a thread other than the main one starts no command asked for before it, and one
# that started meanwhile stops ( see _kill_running_at_exit and _cut_by_exit )
_exiting = False

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
		# whether multiCMD itself failed to run or reap the command ( returncode -1 is then not a SIGHUP death )
		self._error = False
		# this command's own environment ( see _check_env ): env replaces the inherited one, extra_env is applied on top
		self._env = None
		self._extra_env = None
		# whether the main thread asked for it once the exit hook had begun ( an atexit handler's call: see _cut_by_exit )
		self._at_exit = False

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

class CommandError(Exception):
	'''
	Raised by run_command / run_commands with raise_error=True when commands fail

	The message starts with "Commands failed" and lists each failed command with its return code
	and last error line.
	@attributes:
		tasks: The failed Task objects ( list[Task] )
	'''
	def __init__(self, tasks, total=None):
		self.tasks = list(tasks)
		total = len(self.tasks) if total is None else total
		lines = [f'Commands failed ({len(self.tasks)} of {total}):']
		for task in self.tasks:
			line = f'  [rc {task.returncode}] {_quote_command(task.command)}'
			last = _last_output_line(task)
			if last:
				line += ': ' + last
			lines.append(line)
		super().__init__('\n'.join(lines))

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

class _Done:
	'''
	A flag that is set once, and that threads can wait for ( internal ): a threading.Event no interrupt can jam

	threading.Event waits and sets under a Condition, whose Python-level __enter__ takes its lock in C and
	returns: a KeyboardInterrupt landing in between ( on the main thread ) leaves that lock held for good, and
	every later set() or wait() then blocks ( seen on free-threaded 3.13t; the window exists on every CPython ).
	Here flag is the state, and the bare lock _wake is only there to wake waiters early: it is held from the
	start, and set() stores flag, then releases _wake, once ( setdefault() says which set() does: an unlocked lock
	cannot be released again ). A waiter never holds it with `with`: it tries to take it for one 0.1 s step at a
	time, lets it go at once when it got it, and looks at flag after every step. A lock a waiter was interrupted
	holding, or that a set() cut short never released, only costs the other waiters one step each.
	@attributes:
		flag: Whether it is set ( bool )
	'''
	def __init__(self):
		self.flag = False
		self._wake = threading.Lock()
		self._wake.acquire()
		# ( who releases _wake: the first set() to get there )
		self._released = {}

	def is_set(self):
		return self.flag

	def set(self):
		self.flag = True
		mine = object()
		if self._released.setdefault('by', mine) is mine:
			self._wake.release()

	def wait(self, timeout=None):
		'''
		Wait until it is set, up to timeout seconds ( None: for as long as it takes ), in 0.1 s steps
		@returns:
			bool: Whether it is set
		'''
		deadline = None if timeout is None else time.monotonic() + timeout
		while not self.flag:
			step = 0.1 if deadline is None else min(0.1, deadline - time.monotonic())
			if step <= 0:
				return False
			if self._wake.acquire(True, step):
				# ( only to wake up: the next waiter gets it at once )
				self._wake.release()
		return True

class _Thread(threading.Thread):
	'''
	A daemon thread that reports itself done, for every thread multiCMD starts and waits for ( internal )

	On Python 3.9.8 through 3.12 a Thread.join() that a KeyboardInterrupt interrupts leaves the thread flagged
	as stopped while it still runs ( CPython bpo-45274 ): Thread.is_alive() is then False and Thread.join()
	returns at once, for good. done is set by the thread itself once its target has returned or raised, and
	is_alive() and join() go by it, so they stay right on every Python, whatever join was interrupted before:
	- join(timeout) waits for done ( _Done.wait(), in 0.1 s steps, so a Ctrl-C reaches the waiting thread ), bounded
	  by timeout, and once done is set it joins the thread itself, which releases its stack ( from Python 3.13 a
	  finished thread nobody joined keeps its stack mapped for as long as the Thread object lives ), within
	  what is left of timeout: a thread whose target has finished is done, whether or not it has left yet;
	- is_alive() is True until done is set, from the moment the thread is created: one registered before it
	  starts counts as running, so a wait from another thread meanwhile waits for it.
	A thread is therefore either started or abandoned, never left in between: start() abandons it when it
	cannot start, or when a Ctrl-C lands while it waits for the thread to come up and the thread has not got
	to its target yet; whoever meant to start it abandons it when it did not ( see _start_threads ). Whichever
	comes first, run() or _abandon(), decides whether the target runs, so the answer never depends on timing
	afterwards, and _abandon() gives the same answer however often it is asked, a Ctrl-C between two included.
	done is a _Done, not a threading.Event, so no interrupt can leave it blocking anyone.
	@attributes:
		task: The Task the thread runs ( None for a dispatcher, pool worker or output reader )
		done: Set once the target has returned or raised, or the thread was abandoned ( a _Done )
	'''
	def __init__(self, target, args=(), task=None):
		super().__init__(target=target, args=args, daemon=True)
		self.task = task
		self.done = _Done()
		# ( in a child forked meanwhile only the forking thread exists )
		self._pid = os.getpid()
		# who decided whether the target runs: 'run' or 'abandon'. setdefault() is one atomic step that also
		# says who won, so no interrupt can leave the decision taken but unrecorded
		self._claim = {}

	def start(self):
		# what the caller is handling already ( an except or finally block ): an error raised while handling it is not
		# one raised during this start
		handling = sys.exc_info()[1]
		try:
			super().start()
		except BaseException as e:
			# it cannot start, or a Ctrl-C landed while start() waited for it to come up: it runs if it got that far
			self._abandon()
			interrupt = _interrupt_in(e, handling)
			if interrupt is not None and interrupt is not e:
				# a Ctrl-C that start()'s wait turned into another error: Condition.wait() releases its lock on the
				# way out, and when the Ctrl-C cut short the _acquire_restore() that takes it back, that release
				# raises RuntimeError ( seen on free-threaded 3.13t ). The Ctrl-C is what happened
				raise interrupt
			raise

	def _abandon(self):
		'''
		Make sure the thread never runs its target, unless it already does ( by the thread that starts it )
		@returns:
			bool: Whether it never runs its target ( done is then set )
		'''
		if self._claim.setdefault('by', 'abandon') != 'abandon':
			return False
		self.done.set()
		return True

	def run(self):
		try:
			if self._claim.setdefault('by', 'run') == 'run':
				super().run()
		finally:
			self.done.set()

	def is_alive(self):
		return not self.done.flag and self._pid == os.getpid()

	def join(self, timeout=None):
		# ( in a child forked meanwhile the thread does not exist: nothing ever sets done there )
		if self._pid != os.getpid():
			return
		deadline = None if timeout is None else time.monotonic() + timeout
		if not self.done.wait(timeout):
			return
		# ( an abandoned thread may never have come up )
		if self._started.is_set():
			left = None if deadline is None else max(0, deadline - time.monotonic())
			if _JOIN_MAY_HANG:
				# its target has finished, so it is only leaving: never block on a lock an interrupted join left held
				left = 1 if left is None else min(left, 1)
			super().join(left)

def _interrupt_in(error, before=None):
	'''
	The KeyboardInterrupt or SystemExit error is, or that it was raised while handling, if any ( internal )
	The walk down error's __context__ chain stops at before: what the caller was handling already ( sys.exc_info()
	before the call that raised error ), whose own context is older still. A Ctrl-C the caller is handling, from its
	except block, is not one that landed during the call.
	'''
	seen = set()
	while error is not None and error is not before and id(error) not in seen:
		if isinstance(error, _INTERRUPTS):
			return error
		seen.add(id(error))
		error = error.__context__
	return None

# a KeyboardInterrupt ( a Ctrl-C ), or a SystemExit from a signal handler ( mcmd's own SIGTERM / SIGHUP one )
_INTERRUPTS = (KeyboardInterrupt, SystemExit)
# how many errors other than _INTERRUPTS a settled step may raise before it is taken to fail on its own ( see _settle )
_STEP_FAILURES = 3

# per thread: the failure messages to show once the outermost _settle() on that thread is done ( None: show at once )
_settling = threading.local()

def _settle(step, cuts=None):
	'''
	Run step() to its end, whatever interrupt lands meanwhile ( internal )

	For multiCMD's rare paths only: a thread that cannot start, stopping commands, cancelling or abandoning a batch,
	recording a task that failed. Nothing here touches signal state: whatever a signal handler raises into step cuts it
	short ( a KeyboardInterrupt from a Ctrl-C, on the main thread; a SystemExit from a handler such as mcmd's SIGTERM
	one; anything a program's own SIGINT handler raises ), and makes it run again from the start. The first such
	exception is raised once step has completed, so step must give the same result run again after being cut short
	anywhere ( idempotent ), and must take no lock an interrupt could leave held ( none at all: see _Done ). A
	program's own SIGINT handler is called as the Ctrl-C lands. A step that fails on its own ( _STEP_FAILURES errors
	other than a KeyboardInterrupt or SystemExit, one per run ) is not run again: the first exception is raised then.
	Anything step records ( a return code ) is recorded before the interrupt is raised, so it is raised from here
	and a wait the caller would start next is skipped, as a second Ctrl-C during it would end it. The failure
	messages step shows ( see _show_error ) are shown after it, each once: a step run again shows nothing twice.
	An interrupt pending as _settle is called can still be raised before step starts ( from the call itself: no
	Python code can take it earlier ); a caller that must have step run then calls it again with that interrupt in
	cuts ( see _execute ).
	@params:
		step: The step ( called with no arguments )
		cuts: A list each exception that cuts step short is appended to before step runs again, so step can tell it
			was ( None: one of its own ). The first one in it is raised once step has completed, one it held already
			included.
	'''
	if cuts is None:
		cuts = []
	failures = 0
	own = False
	try:
		if getattr(_settling, 'messages', None) is None:
			own = True
			_settling.messages = {}
		# ( two loops: an interrupt that lands on the inner loop's way back is taken by the outer one )
		while failures < _STEP_FAILURES:
			try:
				while failures < _STEP_FAILURES:
					try:
						step()
						break
					except BaseException as e:
						cuts.append(e)
						if not isinstance(e, _INTERRUPTS):
							failures += 1
				break
			except BaseException as e:
				cuts.append(e)
				if not isinstance(e, _INTERRUPTS):
					failures += 1
	finally:
		if own:
			messages, _settling.messages = getattr(_settling, 'messages', None), None
			for text in (messages or {}).values():
				_write_error(text)
	if cuts:
		raise cuts[0]

def _abandon_all(threads, forget=None):
	'''
	Abandon every thread that has not got to its target, and forget each one abandoned ( forget(thread), see
	_start_threads ) ( internal ); idempotent, for a settled step
	'''
	for thread in threads:
		if thread._abandon() and forget is not None:
			forget(thread)

def _start_threads(threads, register=None, forget=None, fail=None, abort=None, wait=None, early=None):
	'''
	Start _Threads so that every one either runs or is abandoned, none left tracked that never runs ( internal )

	Each thread in turn is registered ( register(thread) ) and started. One that cannot start ( an Exception ) is
	abandoned, forgotten ( forget(thread) ) and failed ( fail(thread, error) ), and the next one starts, unless fail
	returned True: that ends the starts there, and the threads not started yet are abandoned and forgotten. Without
	fail, the error ends the starts early. When the starts end early ( that error, a Ctrl-C, register or fail
	raising ), every thread that does not run, registered yet or not, is abandoned and forgotten, a fail cut
	short is run again, abort() is called, then wait() and the exception is raised. fail and that bookkeeping run
	to their end whatever interrupt lands meanwhile ( see _settle ), so forget, fail and abort must be idempotent.
	An interrupt meanwhile is raised once they are done, with the exception as its __context__: after a Ctrl-C it
	is a second one, which skips wait(), as it would end it.
	@params:
		threads: The _Threads to start ( list )
		register: Called with each thread right before it starts ( None: nothing to do )
		forget: Undoes register for a thread that never runs; called for every such thread, registered or not
		fail: Called with a thread that cannot start and the error; True ends the starts ( None: the error ends them early )
		abort: Called once the starts end early ( None: nothing to do )
		wait: Called after abort, to wait for the threads that run ( None: nothing to do )
		early: A list True is appended to as soon as the starts end early, before abort and wait: the caller's own
			handling of the exception then knows the wait was done, or skipped by a second interrupt ( None: none )
	@returns:
		list: The threads that run ( or may yet get to their target )
	'''
	# a thread whose fail() has not completed yet -> its error ( None once it has )
	failing = {}
	# how many threads the starts got to, when a fail ended them
	ended = [None]
	def failed(thread, error, index):
		failing[thread] = error
		if fail(thread, error):
			ended[0] = index + 1
		failing[thread] = None
	try:
		for index, thread in enumerate(threads):
			if register is not None:
				register(thread)
			try:
				thread.start()
			except Exception as e:
				if not thread._abandon():
					# it got to its target all the same
					continue
				if forget is not None:
					forget(thread)
				if fail is None:
					raise
				_settle(lambda: failed(thread, e, index))
				if ended[0] is not None:
					# the ones after it never start
					_settle(lambda: _abandon_all(threads[ended[0]:], forget))
					break
	except BaseException as e:
		if early is not None:
			early.append(True)
		def settle():
			_abandon_all(threads, forget)
			for thread, error in failing.items():
				if error is not None:
					fail(thread, error)
					failing[thread] = None
			if abort is not None:
				abort()
		try:
			_settle(settle)
		except BaseException:
			# an interrupt meanwhile: the first one ( after a failure ) is waited for as any, a second one ends the wait
			if wait is not None and not isinstance(e, _INTERRUPTS):
				wait()
			raise
		if wait is not None:
			wait()
		raise
	return [thread for thread in threads[:ended[0]] if thread not in failing]

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
	def __init__(self, max_threads=1,semaphore=...,timeout=0,quiet=True,dry_run=False,parse=False,shell=False,prefix=False,color=None,pty=False,env=None,extra_env=None):
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
			timeout: The inactivity timeout for each command, in seconds ( 0: none ). Not a limit for wait() / join()
			quiet: Whether to suppress output ( bool )
			dry_run: Whether to simulate running the commands ( bool )
			parse: Whether to parse ranged input ( bool )
			shell: Whether to run commands through the system shell ( bool )
			prefix: Whether to prefix output lines with [#] when commands can run at once ( bool )
			color: Whether to color output lines per command ( None: only on a terminal without NO_COLOR )
			pty: Whether commands write to a pseudo-terminal ( bool, POSIX only )
			env: The whole environment of each command, replacing the inherited one ( Mapping[str, str] | None: inherit ), as subprocess's env
			extra_env: Variables added to the inherited environment, or overriding it ( Mapping[str, str | None] | None ); a None value removes the variable
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
		self.shell = shell
		self.prefix = prefix
		self.color = color
		self.pty = pty
		# plain dicts: a later change is checked and snapshotted by the next run_commands, never shared with queued commands
		env, extra = _check_env(env, 'env', drop_empty=True), _check_env(extra_env, 'extra_env', allow_none=True)
		self.env = None if env is None else env.copy()
		# an empty one ( which _check_env reduces to None ) stays a dict to add to, like env
		self.extra_env = None if extra_env is None else dict(extra or {})
		# held while tasks, _queue and _dispatching change together; never by a settled step ( see _settle )
		self._lock = threading.Lock()
		# (task, sem, timeout, quiet, dry_run, decor, use_pty) waiting for a permit
		self._queue = collections.deque()
		self._dispatching = False
		# started threads not yet joined, in start order ( dict used as an ordered set; no lock, see _track )
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
		return list(self._threads.copy())

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
			try:
				thread.join()
			finally:
				# also when the join is interrupted ( Ctrl-C ): the item is already off _finished, and
				# a finished thread needs no join to be forgotten, joining only frees its stack
				self._threads.pop(thread, None)
				# a finished Thread object costs ~2 KB, and the task outlives it in self.tasks
				if task is not None and task.thread is thread:
					task.thread = None

	def _track(self, thread):
		'''
		Track a _Thread about to start until it reports itself finished ( internal; register for _start_threads )
		Tracked before it starts, so it is tracked before it can report itself finished, and its task has it
		before it runs, so _reap() never sees the task without its thread. _threads takes no lock ( one dict operation
		each, and dict.copy() snapshots ), so no Ctrl-C can leave _track, _untrack or _reap blocking: _untrack is a
		settled step's ( see _settle ).
		'''
		self._threads[thread] = None
		if thread.task is not None:
			thread.task.thread = thread

	def _untrack(self, thread):
		'''
		Stop tracking a thread that never runs its target, as it never reports itself finished ( internal; forget for _start_threads )
		Its task drops it as a finished one does: an executor task has a thread only while it runs.
		'''
		self._threads.pop(thread, None)
		if thread.task is not None and thread.task.thread is thread:
			thread.task.thread = None

	def _dispatch(self):
		'''
		Start queued commands one permit at a time ( internal )
		A command whose thread cannot be made or started fails ( see _fail_task ), and the dispatcher goes on with the next one.
		'''
		try:
			while True:
				with self._lock:
					if not self._queue:
						self._dispatching = False
						break
					task, sem, timeout, quiet, dry_run, decor, use_pty = self._queue.popleft()
				# wait for a permit, but give up on a command that gets stopped meanwhile
				acquired = False
				while not task.stop and not acquired:
					acquired = sem.acquire(timeout=0.1)
				if task.stop:
					if acquired:
						sem.release()
					# never started: it keeps its place in the task list, not its environment
					task._env = task._extra_env = None
					continue
				# from here the permit is the thread's once it runs, and given back if it never does
				thread = None
				try:
					self._reap()
					thread = _Thread(_run_executor_task, (task, _HeldPermit(sem), timeout, quiet, dry_run, self._finished, decor, use_pty), task)
					_start_threads([thread], self._track, self._untrack)
				except BaseException as e:
					if thread is None or thread._abandon():
						sem.release()
						# it is off the queue and will never run: it failed, as a command multiCMD cannot start does
						_fail_task(task, f'cannot start its thread: {e}', e, quiet)
					if not isinstance(e, Exception):
						raise
		except BaseException:
			# let the next run_commands() start a new dispatcher for what is still queued
			with self._lock:
				self._dispatching = False
			raise
		finally:
			self._finished.append((threading.current_thread(), None))

	def run_commands(self, commands, timeout=...,max_threads=...,quiet=...,dry_run=...,parse = ...,sem = ...,shell = ...,prefix = ...,color = ...,pty = ...,env = ...,extra_env = ...):
		'''
		Run multiple commands in parallel asynchronously. Returns right away, the commands are queued
		@params:
			commands: A list of commands to run ( list[str] | list[list[str]] )
			timeout: The inactivity timeout for each command to override the object default
			max_threads: The maximum number of threads to use to override the object default
			quiet: Whether to suppress output to override the object default
			dry_run: Whether to simulate running the commands to override the object default
			parse: Whether to parse ranged input to override the object default
			sem: The semaphore to use for threading to override the object default
			shell: Whether to run commands through the system shell to override the object default
			prefix: Whether to prefix output lines with [#] to override the object default
			color: Whether to color output lines to override the object default
			pty: Whether commands write to a pseudo-terminal to override the object default
			env: The whole environment of the commands to override the object default ( None: inherit )
			extra_env: Variables added to / overriding the environment, to override the object default ( None: none )
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
		if shell is ...:
			shell = self.shell
		if prefix is ...:
			prefix = self.prefix
		if color is ...:
			color = self.color
		if pty is ...:
			pty = self.pty
		if env is ...:
			env = self.env
		if extra_env is ...:
			extra_env = self.extra_env
		taskObjects, use_sudo = _build_tasks(commands, parse=parse, shell=shell, env=env, extra_env=extra_env)
		if not sem:
			sem = threading.Semaphore(max_threads if max_threads >= 1 else max(len(taskObjects), 1))
		if use_sudo and not dry_run:
			_validate_sudo()
		# the executor's task list keeps growing, so commands count as concurrent whenever max_threads allows it
		concurrent = max_threads != 1
		use_color = _resolve_color(color, concurrent)
		use_pty = _resolve_pty(pty)
		self._reap()
		start_dispatcher = False
		dispatcher = None
		def settle():
			# a Ctrl-C or a failure from here on: this call's commands are skipped, whoever would start them ( so
			# the outcome does not depend on how far the dispatcher got ); a dispatcher that runs stays the
			# dispatcher, and without one the next run_commands() starts one for what is still queued
			_stop_tasks(taskObjects)
			if start_dispatcher and (dispatcher is None or dispatcher._abandon()):
				if dispatcher is not None:
					self._untrack(dispatcher)
				# ( no dispatcher runs, so nothing else clears it; one store, without the lock, which a settled step
				# never takes: a run_commands() from another thread that saw it still set leaves its commands queued
				# for the next one, as it would have had it taken the lock first )
				self._dispatching = False
		try:
			with self._lock:
				first = len(self.tasks) + 1
				queued = [(task, sem, timeout, quiet, dry_run, _decor(first + i, 0, prefix and concurrent, use_color), use_pty)
					for i, task in enumerate(taskObjects)]
				self.tasks.extend(taskObjects)
				self._queue.extend(queued)
				if not self._dispatching and self._queue:
					start_dispatcher = True
					# only the dispatcher clears this, under the lock, once it sees the queue empty ( or settle(), when there is none )
					self._dispatching = True
			if start_dispatcher:
				dispatcher = _Thread(self._dispatch)
				_start_threads([dispatcher], self._track, self._untrack, abort=settle)
		except BaseException:
			_settle(settle)
			raise
		return taskObjects
	
	def run_command(self, command, timeout=...,max_threads=...,quiet=...,dry_run=...,parse = ...,sem = ...,shell = ...,prefix = ...,color = ...,pty = ...,env = ...,extra_env = ...):
		'''
		Run a command in parallel asynchronously
		@params:
			command: The command to run ( str | list[str] )
			( the other parameters override the object defaults, as in run_commands )
		@returns:
			Task: The Task Object of the command
		'''
		return self.run_commands([command],timeout=timeout,max_threads=max_threads,quiet=quiet,dry_run=dry_run,parse=parse,sem=sem,shell=shell,prefix=prefix,color=color,pty=pty,env=env,extra_env=extra_env)[0]
	
	def wait(self, timeout=None, threads = ...):
		'''
		Wait for the commands to finish
		@params:
			timeout: The longest to wait in total, in seconds ( None or negative: until everything is done )
			threads: The threads to join, default to all running threads managed by this object,
				including the ones the dispatcher starts while waiting
		@returns:
			list[threading.Thread]: The threads that are still running
		A Ctrl-C ( or a SystemExit from a signal handler, as a SIGTERM handler's sys.exit() ) while it waits stops every
		command of the executor, queued ones skipped, waits up to 5 s for them ( a second Ctrl-C ends that wait at
		once ) and re-raises.
		'''
		deadline = None if timeout is None or timeout < 0 else time.monotonic() + timeout
		try:
			self._wait_until(deadline, threads)
		except _INTERRUPTS:
			# the commands run in process groups of their own, so the terminal's Ctrl-C did not reach them
			# not wait(): a second Ctrl-C must end this wait, not nest another one ( it is raised with the first as its
			# __context__ ); one that lands while the tasks are stopped is raised by _stop_tasks, before the wait
			_stop_tasks(self.tasks)
			self._wait_until(time.monotonic() + 5)
			raise
		return [thread for thread in self.runningThreads if thread.is_alive()]

	def _wait_until(self, deadline, threads=...):
		'''
		Wait for the threads ( default: the executor's, including the ones the dispatcher starts meanwhile ) until deadline ( internal )
		'''
		joined = set()
		while True:
			pending = self.runningThreads if threads is ... else list(threads)
			for thread in pending:
				_join(thread, None if deadline is None else max(0, deadline - time.monotonic()))
			# once a pass finishes every thread, look again for commands the dispatcher started meanwhile;
			# stop when nothing new showed up ( a stale entry for a dead thread must not keep this looping ) or time is up
			if threads is not ... or not pending or any(thread.is_alive() for thread in pending) \
				or all(thread in joined for thread in pending) or (deadline is not None and time.monotonic() >= deadline):
				return
			joined.update(pending)
	
	def stop(self,timeout=None):
		'''
		Stop all commands: running ones are killed with everything they started, queued ones are
		skipped ( they keep task.thread None and returncode None ). Then wait for them.
		@params:
			timeout: The longest to wait in total, in seconds ( None or negative: until everything is done )
		@returns:
			list[Task]: The list of tasks that are managed by this object
		'''
		_stop_tasks(self.tasks)
		self.wait(timeout)
		return self.tasks
	
	def cleanup(self,timeout=None):
		'''
		Cleanup the tasks and threads. This calls stop() and clears the tasks and threads
		@params:
			timeout: The longest to wait in total, in seconds ( None or negative: until everything is done )
		@returns:
			list[Task]: The list of tasks that are managed by this object
		'''
		self.stop(timeout)
		with self._lock:
			# the stop may have returned first: what is still queued never starts, so it keeps no environment
			for entry in self._queue:
				entry[0]._env = entry[0]._extra_env = None
			self.tasks = []
			self._queue.clear()
			self._threads.clear()
		return self.tasks
	
	def join(self, timeout=None, threads = ..., print_error=True):
		'''
		Wait for the commands to finish and report the ones that did not succeed
		@params:
			timeout: The longest to wait in total, in seconds ( None or negative: until everything is done )
			threads: The threads to wait for, default to all running threads managed by this object
			print_error: Whether to print failed, skipped and still running commands
		@returns:
			list[Task]: The list of tasks that are managed by this object
		'''
		self.wait(timeout=timeout, threads=threads)
		if print_error:
			for task in self.tasks:
				if not task._finalized:
					if task.stop and task.thread is None:
						print(f'Command: {task.command} skipped: stopped before it started')
					else:
						print(f'Command: {task.command} still running')
				elif task.returncode not in (0, None):
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

	# variable assignment  foo:BAR  ( exactly one unescaped ':' with an identifier before it,
	# so IPv6 addresses pass through and '\:' gives a literal colon )
	parts = _UNESCAPED_COLON_RX.split(piece)
	if len(parts) == 2 and parts[0].isidentifier():
		vars_[parts[0]] = __evaluate_value(parts[1].strip().replace('\\:', ':'), vars_, dereference=False)
		return
	piece = piece.replace('\\:', ':')

	# explicit range  start-end
	if '-' in piece:
		start, _, end = (p.strip() for p in piece.partition('-'))

		start = str(__evaluate_value(start, vars_))
		end   = str(__evaluate_value(end,   vars_))

		if start.isdigit() and end.isdigit():
			pad = min(len(start), len(end))
			return [f"{i:0{pad}d}" for i in range(int(start), int(end) + 1)]

		# single characters in 0-9a-zA-Z order, case kept ( before hex, so [A-C] stays upper case )
		if start in _ALPHA_IDX and end in _ALPHA_IDX:
			return [_ALPHANUM[i] for i in range(_ALPHA_IDX[start], _ALPHA_IDX[end] + 1)]

		# hex, zero-padded to the shorter end like decimal; upper case only when the input is
		if start and end and all(c in string.hexdigits for c in start + end):
			upper = ''.join(c for c in start + end if c.isalpha()).isupper()
			pad = min(len(start), len(end))
			return [format(i, f'0{pad}{"X" if upper else "x"}') for i in range(int(start, 16), int(end, 16) + 1)]

	# plain token or ${var}
	value = __evaluate_value(piece, vars_)
	if isinstance(value, str):
		return [value]
	else:
		try:
			return [str(v) for v in value]
		except Exception:
			return [str(value)] 

def _expand_ranges_fast(inStr, variables=None):
	'''
	Expand [...] ranges in a string into every combination
	@params:
		inStr: The string to expand
		variables: The range variables ( name:value assignments ) shared with the other expansions
			of the same batch; None gives this call its own
	@returns:
		list[str]: The expanded strings
	'''
	if variables is None:
		variables = {}
	segments = []
	pos = 0
	# split the template into literal pieces + expandable pieces
	for match in _BRACKET_RX.finditer(inStr):
		if match.start() > pos:
			segments.append([inStr[pos:match.start()]])
		choices = []
		for sub in match.group(1).split(','):
			expandedPieces = _expand_piece(sub, variables)
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

class _ExecRefused(OSError):
	'''The system refused to execute the command ( ENOEXEC, E2BIG, ELOOP, ... ), as opposed to not finding it or not being allowed to ( internal )'''

def _uses_wrapper():
	'''Whether commands start through the exec wrapper _PGROUP_EXEC ( internal )'''
	# ( a frozen app's sys.executable is the app itself, not a Python that can run the wrapper )
	return _IS_POSIX and not _HAS_PROCESS_GROUP and bool(sys.executable) and not getattr(sys, 'frozen', False)

def _search_dirs(env):
	'''
	The directories a bare command name is looked up in, as os.get_exec_path() gives them, without calling it ( internal ):
	the PATH of the command's environment ( None: os.environ ) split on os.pathsep, os.defpath when it has none; an
	empty entry, and so an empty PATH, is the current directory ( see _files_named ). os.get_exec_path() takes
	warnings.catch_warnings() around the lookup, which is not thread-safe before Python 3.14 and from 3.14 takes the
	warnings module's lock with `with`: a Ctrl-C right after the acquire ( a serial run starts its command on the main
	thread ) leaves it held for good, and every later warning, on any thread, hangs. env is always a str-keyed dict here
	( see _child_env ), so its bytes and b'PATH' handling has nothing to do.
	'''
	path = (os.environ if env is None else env).get('PATH')
	return (os.defpath if path is None else path).split(os.pathsep)

def _files_named(command, dirs):
	'''The regular files by that name in dirs, in order and one at a time ( internal ); '' is the current directory'''
	for d in dirs:
		path = os.path.join(d or os.curdir, command)
		if os.path.isfile(path):
			yield path

def _spawn(argv, stdout, stderr, env=None, explicit=False, started=None):
	'''
	Start a command in a process group of its own ( internal ); see _spawn_process for the arguments

	This is where a failure to start is told apart, as a shell does:
	- a command that is not found is FileNotFoundError ( 127 ), one that may not be executed PermissionError ( 126 );
	- a bare name ( no '/' ) is looked up in the directories of the PATH of the environment the command gets, as
	  os.get_exec_path() gives them ( no PATH: os.defpath; an empty entry, or an empty PATH, is the current
	  directory ), in order, trying only the regular files by that name ( a directory, a FIFO, or an entry that
	  cannot be searched holds none ). The first failure other than ENOENT / ENOTDIR decides ( EACCES is
	  PermissionError, ENOEXEC and the like _ExecRefused ); without one, or without any such file, the command is
	  not found, named as given. This is os.execvpe()'s rule, which reports the last error when no other decides,
	  but for the files it skips; it is not exactly any shell's: dash reports the last error other than ENOENT,
	  and both dash and bash run a file that fails with ENOEXEC as a shell script, which multiCMD does not
	  ( subprocess never did ): such a file cannot be executed ( 126 ). Like multiCMD, both search the current
	  directory for an empty PATH ( bash 5.2 and dash: `PATH= bash -c tool` runs ./tool ).
	  E2BIG ( the arguments and environment are too big ) does not depend on the file: it decides at once;
	- any other failure to exec the command ( ENOEXEC, E2BIG, ELOOP of its path, ... ) is _ExecRefused ( 126 );
	- a failure to start multiCMD's own launcher ( the exec wrapper's interpreter, fork, pipes ) is never the
	  command's: it stays an internal error whose message and traceback say what went wrong. Only E2BIG is the
	  command's even then, as its arguments and environment travel in the wrapper's.
	The exec wrapper applies the same rule in the command's process ( see _PGROUP_EXEC ). Popen ( Python 3.11+, or a
	frozen app ) would try every PATH entry and report one error from any of them, and look them up with
	os.get_exec_path() ( see _search_dirs ), so a bare name is looked up here, one regular file at a time, and Popen
	given its path ( executable ).
	'''
	command = argv[0]
	wrapped = _uses_wrapper()
	bare = _IS_POSIX and os.sep not in command
	if wrapped and bare and next(_files_named(command, _search_dirs(env)), None) is None:
		# the wrapper would only report a missing command through its exit code; fail like Popen does
		raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), command)
	if wrapped or not bare:
		try:
			return _spawn_process(argv, stdout, stderr, env, explicit, started=started)
		except OSError as e:
			if wrapped:
				if e.errno == errno.E2BIG:
					raise _ExecRefused(e.errno, e.strerror, command) from None
				if isinstance(e, (FileNotFoundError, PermissionError)):
					# it is the interpreter that is missing or may not run, not the command
					raise RuntimeError('cannot start the exec wrapper %r: %s' % (sys.executable, e)) from e
				raise
			if e.filename is None or isinstance(e, (FileNotFoundError, PermissionError)):
				# not the command's exec that failed ( fork, pipes, ... ), or one that says what it is
				raise
			raise _ExecRefused(e.errno, e.strerror, e.filename) from None
	failure = None
	for path in _files_named(command, _search_dirs(env)):
		try:
			# ( one that can run by now just runs, as it would have a moment ago )
			return _spawn_process(argv, stdout, stderr, env, explicit, executable=path, started=started)
		except OSError as e:
			if e.filename is None:
				raise
			if e.errno == errno.E2BIG:
				raise _ExecRefused(e.errno, e.strerror, command) from None
			if failure is None and not isinstance(e, (FileNotFoundError, NotADirectoryError)):
				failure = e
	if failure is None:
		raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), command)
	if isinstance(failure, PermissionError):
		raise PermissionError(failure.errno, failure.strerror, command)
	raise _ExecRefused(failure.errno, failure.strerror, command)

class _Popen(subprocess.Popen):
	'''
	subprocess.Popen that is handed over before it can have a child, so that no interrupt loses one ( internal )

	A KeyboardInterrupt ( or a SystemExit from a signal handler ) that lands after the fork, while Popen waits for
	the exec or anywhere before the Popen object is back with its caller, would lose the only handle on a command
	that runs on in a process group of its own, which the terminal's Ctrl-C does not reach. So the Popen, with
	whether its pid is its process group id, is appended to started before anything starts, and Popen then stores
	the child's pid on it in one step ( self.pid = <the fork's result> ): from that moment on the caller finds the
	command through started however Popen ends, and stops it ( see _started_command and _execute ). The pid is what
	counts, not _child_created: Windows stores it last, once the Popen has its process handle. An interrupt that
	lands between the fork's return and that store ( _fork_exec, _posixsubprocess.fork_exec or os.posix_spawn has
	returned the pid, and Python raises the interrupt before the next instruction stores it ) loses the command:
	nothing but that C call ever knew the pid. That is the only place.
	'''
	def __init__(self, args, started=None, grouped=False, **kwargs):
		if started is not None:
			started.append((self, grouped))
		super().__init__(args, **kwargs)

def _started_command(started):
	'''
	The command in started that may be running, as ( Popen, process group id | None ), or None ( internal; see _Popen )
	The last one whose Popen has stored a pid and has no returncode: one whose exec failed, or that is reaped, has one.
	'''
	for proc, grouped in reversed(started):
		pid = getattr(proc, 'pid', None)
		if pid is not None and getattr(proc, 'returncode', None) is None:
			return proc, (pid if grouped else None)
	return None

def _popen(argv, started=None, grouped=False, **kwargs):
	'''
	Start a process with _Popen ( internal ); see _Popen for started and grouped
	A subprocess.Popen replaced since multiCMD was imported ( a test double ) is called instead, and handed over once it returns.
	'''
	if subprocess.Popen is _Popen.__base__:
		return _Popen(argv, started=started, grouped=grouped, **kwargs)
	proc = subprocess.Popen(argv, **kwargs)
	if started is not None:
		started.append((proc, grouped))
	return proc

def _spawn_process(argv, stdout, stderr, env=None, explicit=False, executable=None, started=None):
	'''
	Start a command in a process group of its own ( internal )

	The group lets a timeout / stop / Ctrl-C reach everything the command starts. SIGTTIN and
	SIGTTOU are blocked while it starts and the command inherits that, so if it reads the
	terminal from its background group the read fails with EIO instead of the kernel stopping it.
	Python 3.11+ uses Popen(process_group=0); older Pythons run the command through the exec
	wrapper _PGROUP_EXEC. Windows gets a plain Popen.

	The wrapper itself always starts in this process's environment. A command given an environment of
	its own ( explicit ) gets it as reserved variables of the wrapper's, which the wrapper unpacks and
	passes to exec: it is encoded before anything starts and exec hands it over whole or not at all.
	The wrapper's environment is this process's plus the command's, so the command's environment and this
	process's own together must fit the system's limit ( ARG_MAX; on Linux 2 MiB for the default 8 MiB stack,
	and 128 KiB for one string; before 3.11 an entry travels as _MULTICMD_ENV_<i>=NAME=VALUE, so a NAME=VALUE entry may be
	15 bytes plus the digits of its number i shorter than that ); beyond it the command fails with 126
	'Argument list too long' ( on every Python ).

	@params:
		argv: The command ( list[str] )
		stdout: Where the command's stdout goes ( subprocess.PIPE or a file descriptor )
		stderr: Where the command's stderr goes
		env: The environment, None to inherit
		explicit: Whether env comes from the caller's env / extra_env ( the exec wrapper then looks the
			command up in env's PATH and hands env over whole ); otherwise env, if any, only adds to what
			is inherited and the wrapper just runs with it, as it always has
		executable: The file to run as the command, instead of looking argv[0] up ( Popen's executable; not with the wrapper )
		started: A list the ( Popen, whether its pid is its process group id ) is appended to before the command can start ( see _Popen )

	@returns:
		( subprocess.Popen, int | None ): The process and its process group id ( None without a group of its own )
	'''
	kwargs = {'stdin': subprocess.DEVNULL, 'stdout': stdout, 'stderr': stderr, 'env': env}
	if executable is not None:
		kwargs['executable'] = executable
	if not _IS_POSIX:
		return _popen(argv, started=started, **kwargs), None
	grouped = True
	if _HAS_PROCESS_GROUP:
		kwargs['process_group'] = 0
	elif _uses_wrapper():
		count = '-'
		if explicit:
			# the wrapper's environment is this process's, plus the command's entries
			entries = [os.fsencode(k) + b'=' + os.fsencode(v) for k, v in env.items()]
			kwargs['env'] = dict(_snapshot(os.environb, drop_empty=True))
			kwargs['env'].update((os.fsencode(_ENV_VAR + str(i)), entry) for i, entry in enumerate(entries))
			count = str(len(entries))
		argv = [sys.executable, '-I', '-S', '-c', _PGROUP_EXEC, count] + list(argv)
	else:
		grouped = False
	old_mask = signal.pthread_sigmask(signal.SIG_BLOCK, _TTY_SIGNALS)
	try:
		proc = _popen(argv, started=started, grouped=grouped, **kwargs)
	finally:
		signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)
	return proc, (proc.pid if grouped else None)

# whether an exited command can be looked at without reaping it ( os.waitid() with WNOWAIT; see _poll )
_CAN_PEEK = _IS_POSIX and hasattr(os, 'waitid') and hasattr(os, 'WNOWAIT')
# how long poll() must have been kept from an exited command by its Popen's lock before multiCMD reaps it ( see _poll )
_JAMMED_AFTER = 0.5

def _exit_code(status):
	'''A waitpid() status as Popen's returncode: the exit code, or -N after a death by signal N ( internal )'''
	if os.WIFSIGNALED(status):
		return -os.WTERMSIG(status)
	return os.WEXITSTATUS(status)

def _in_subprocess():
	'''Whether this thread runs inside subprocess's own code, further up its stack ( internal; see _poll )'''
	if not hasattr(sys, '_getframe'):
		return True
	frame = sys._getframe(1)
	while frame is not None:
		if frame.f_globals.get('__name__') == 'subprocess':
			return True
		frame = frame.f_back
	return False

def _inside(proc, call, *args):
	'''
	call(*args): a call of proc's that may take its Popen lock ( poll(), send_signal() ), with this thread marked as
	inside it meanwhile, so that _poll() on another thread knows the lock is held for real ( internal; see _poll )
	The mark is one dict entry per call, set inside the try that removes it: an interrupt before it leaves none. Only
	a second interrupt landing just as the call returns, before the finally removes it, can leave one behind: that
	Popen is then never reaped behind a jammed lock by other threads, only by this one.
	'''
	marks = proc.__dict__.setdefault('_multicmd_inside', {})
	token = object()
	try:
		marks[token] = threading.get_ident()
		return call(*args)
	finally:
		marks.pop(token, None)

def _inside_elsewhere(proc):
	'''Whether a thread other than this one is inside a call of proc's that may take its Popen lock ( internal; see _inside )'''
	me = threading.get_ident()
	return any(ident != me for ident in proc.__dict__.get('_multicmd_inside', {}).copy().values())

def _peek(proc):
	'''
	Whether a command has exited, looked at without reaping it ( os.waitid() with WNOWAIT ) ( internal )
	@returns:
		None | str: None while it runs ( or where it cannot be looked at so ), 'exited' once it waits to be reaped,
			'gone' when it is no longer a child this process can reap ( ECHILD: reaped already, by Popen or outside it )
	'''
	if not _CAN_PEEK:
		return None
	try:
		return None if os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is None else 'exited'
	except OSError:
		return 'gone'

def _poll(proc):
	'''
	proc.poll(), and the command reaped all the same once a jammed Popen lock keeps poll() from it ( internal )

	Popen.poll() and wait(timeout) take the Popen's _waitpid_lock with acquire(False), and only then enter the try
	that releases it. A KeyboardInterrupt landing in between ( on the main thread, which polls a serial run's command
	over and over ) leaves the lock held for good: poll() then returns None for ever, and the command would never be
	reaped, stay registered, and cost every wait for it its full timeout ( seen on free-threaded 3.13t and 3.14t, in
	1-3 % of the Ctrl-Cs of the start stress test; the window exists on every CPython ). So a command that has exited
	( os.waitid() with WNOWAIT looks without reaping ) while poll() could not take that lock for _JAMMED_AFTER seconds
	is reaped here, with os.waitpid(), and its returncode set; poll() and wait() look at returncode before the lock.
	Never while this thread runs inside subprocess's own code further up its stack: a signal handler that calls
	kill_all() as it lands in an interrupted Popen.wait() or poll(), which holds that lock and is about to reap, would
	leave it ECHILD, which Popen records as returncode 0. Nor while another thread is inside a poll() or send_signal()
	multiCMD made ( see _inside ): one whose signal handler runs as it holds the lock ( kill_all() takes 2 s ) is
	about to reap the command as well. multiCMD calls nothing else of Popen's that takes the lock, and nothing else
	has its commands' Popens, so a lock held that long by no such call is jammed; a thread that does hold it for real
	finds the returncode set as it goes on. The lock itself is only looked at.
	A command reaped outside Popen ( SIGCHLD ignored, which has the kernel reap it; a waitpid() of the program's
	own ) is no longer a child to reap ( ECHILD ): poll() records returncode 0 then, as its status is lost, and so
	does this, once the lock has kept poll() from it for _JAMMED_AFTER seconds.
	'''
	returncode = _inside(proc, proc.poll)
	if returncode is not None or not _CAN_PEEK:
		return returncode
	lock = getattr(proc, '_waitpid_lock', None)
	if lock is None or not lock.locked():
		return None
	state = _peek(proc)
	if state is None:
		# still running
		return None
	now = time.monotonic()
	# ( since when poll() is kept from it: an exited command whose lock is free is reaped by poll() itself )
	if now - proc.__dict__.setdefault('_multicmd_unreaped_since', now) < _JAMMED_AFTER or _in_subprocess() \
		or _inside_elsewhere(proc):
		# ( 'gone': reaped meanwhile by whoever holds the lock, which sets returncode as it goes on )
		return proc.returncode
	if state == 'exited':
		try:
			pid, status = os.waitpid(proc.pid, os.WNOHANG)
		except ChildProcessError:
			state = 'gone'
		except OSError:
			return proc.returncode
		else:
			if pid == proc.pid and proc.returncode is None:
				proc.returncode = _exit_code(status)
	if state == 'gone' and proc.returncode is None:
		# reaped outside Popen: its status is lost, and Popen records 0 then ( Popen._internal_poll, on ECHILD )
		proc.returncode = 0
	return proc.returncode

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

def _dry_run_line(task):
	'''
	The line a dry run shows for a task: the command, quoted, led by the environment it would start with ( internal )
	An environment of its own shows as `env -i NAME=VALUE ...`, extra variables as `env [-u NAME] NAME=VALUE ...`.
	'''
	line = _quote_command(task.command)
	if task._env is None and not task._extra_env:
		return line
	if task._env is not None:
		words = ['-i'] + [shlex.quote(f'{k}={v}') for k, v in _child_env(task).items()]
	else:
		words = ['-u ' + shlex.quote(k) for k, v in task._extra_env.items() if v is None]
		words += [shlex.quote(f'{k}={v}') for k, v in task._extra_env.items() if v is not None]
	return f'env {" ".join(words)} {line}'

def _last_output_line(task, limit=200):
	'''
	The last non-empty error line of a task, for messages ( internal )
	In pty mode stderr is merged into stdout, so stdout is used instead.
	@params:
		task: The Task
		limit: The longest line to return; longer ones are cut and end with '...'
	@returns:
		str: The line, or '' without one
	'''
	for line in reversed(task.stdout if task._pty else task.stderr):
		line = line.strip()
		if line:
			return line if len(line) <= limit else line[:limit - 3] + '...'
	return ''

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
	_live_procs[proc] = pgid

def _unregister(proc):
	'''
	Stop tracking a command once it is reaped ( internal ); one still running ( a kill cut short did not reach it ) stays
	tracked, until a full kill sequence has been tried on it ( see _give_up_on )
	'''
	if _poll(proc) is None:
		return
	_live_procs.pop(proc, None)

def _send(proc, pgid, sig):
	'''
	Send sig to a command's process group, or to the process alone without a group ( internal )
	Before Python 3.11 the exec wrapper's Python makes the group only once it runs: a command stopped right after
	it started may have no group yet, and then gets sig as a process, before it can become the command.
	'''
	try:
		if pgid is not None:
			try:
				os.killpg(pgid, sig)
				return
			except ProcessLookupError:
				pass
		if _poll(proc) is None:
			# ( send_signal() polls first, from Python 3.9 )
			_inside(proc, proc.send_signal, sig)
	except OSError:
		# already gone, or not ours to signal ( a root command under sudo: sudo relays the signal )
		pass

def _group_alive(proc, pgid):
	'''
	Whether anything in a command's process group is still running ( internal )
	A group that does not exist ( yet: see _send ) is gone only once the command itself is.
	'''
	if pgid is None:
		return _poll(proc) is None
	try:
		os.killpg(pgid, 0)
	except ProcessLookupError:
		return _poll(proc) is None
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
	# ( Popen.wait()'s own loop, with _poll )
	delay = 0.0005
	while not all(_poll(proc) is not None for proc, _ in commands):
		remaining = deadline - time.monotonic()
		if remaining <= 0:
			return False
		delay = min(delay * 2, remaining, 0.05)
		time.sleep(delay)
	return True

def _give_up_on(commands, quiet=False):
	'''
	Stop tracking the commands still running after a full kill sequence ( internal; see _kill_commands )
	One no signal stops ( in uninterruptible sleep, or not ours to signal: EPERM ) would otherwise stay registered for
	good, and cost every later kill_all() and the exit their full 2 s. It is dropped, with a message naming its pid
	once, unless quiet or the run that started it was quiet ( see _execute: kill_all() and the exit hook know no
	quiet of their own ). One that has exited stays, reaped or not: whoever may reap it does ( see _poll ). Only
	running commands are given up on: a draining one ( see _execute ) has exited, and kill_all() only reaches what it
	left running.
	'''
	for proc, _ in commands:
		if _poll(proc) is not None or _peek(proc) is not None:
			continue
		_live_procs.pop(proc, None)
		if quiet or proc.__dict__.get('_multicmd_quiet'):
			continue
		# ( once, whoever gives up on it at the same time: setdefault() says who stored first )
		mine = object()
		if proc.__dict__.setdefault('_multicmd_given_up', mine) is mine:
			_write_error(f'multiCMD: pid {proc.pid} is still running after {"SIGKILL" if _IS_POSIX else "kill()"}: '
				'no longer tracked')

def _kill_commands(commands, first_signal, quiet=False):
	'''
	Stop commands and everything they started ( internal )

	POSIX: first_signal, 0.01s later SIGTERM and SIGCONT ( a stopped process acts on signals only
	once continued ), up to 1s for the commands to exit, then SIGKILL for any group that still has
	members and up to 1s more. Every signal goes to the whole process group, and each step goes to
	every command before waiting, so the total stays near 2s however many commands there are.
	Windows: terminate(), up to 1s, kill(), up to 1s.
	A command still running after all that is no longer tracked ( see _give_up_on ), and one that is reaped leaves
	the registry, as its own call has it do once it is reaped ( a command recorded as reaped outside Popen, see
	_poll, or one whose call has gone, would otherwise cost every later kill_all() a look ).

	@params:
		commands: ( Popen, process group id | None ) pairs
		first_signal: The first signal to send ( POSIX only )
		quiet: Whether to say nothing of a command that is still running after all that
	'''
	commands = list(commands)
	if not commands:
		return
	if not _IS_POSIX:
		for proc, _ in commands:
			if _poll(proc) is None:
				try:
					proc.terminate()
				except OSError:
					pass
		if not _wait_all(commands, 1):
			for proc, _ in commands:
				if _poll(proc) is None:
					try:
						proc.kill()
					except OSError:
						pass
			if not _wait_all(commands, 1):
				_give_up_on(commands, quiet)
		for proc, _ in commands:
			_unregister(proc)
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
	if not _wait_all(leftovers, 1):
		_give_up_on(leftovers, quiet)
	for proc, _ in commands:
		_unregister(proc)

def kill_all(sig=None):
	'''
	Stop every command multiCMD started that is still running, and everything those commands started

	Commands run in process groups of their own, so signals sent to your program's group ( e.g. by
	`timeout`, or a closed terminal ) do not reach them. Call this from your own SIGTERM / SIGHUP
	handlers. A multiCMD call waiting on the main thread stops its own commands on Ctrl-C, but one
	waiting on another thread never sees the interrupt: a program that runs multiCMD from its own
	threads should call this from its KeyboardInterrupt handler. This also reaches what a finished
	command left running while a call is still reading its output. When the interpreter exits,
	multiCMD stops the commands still running itself.

	@params:
		sig: The first signal to send ( default SIGTERM; POSIX only ), followed by SIGTERM and SIGKILL as needed

	@returns:
		None
	'''
	commands = list(_live_procs.copy().items()) + list(_draining.copy().items())
	_kill_commands(commands, signal.SIGTERM if sig is None else sig)

def _cut_by_exit(task):
	'''
	Whether the exit hook keeps task's command from running ( internal ): it has begun, the command was asked for
	before it, and this thread is not the main one
	Daemon threads still run while the exit hook and the atexit handlers after it do ( fire-and-forget, executor and
	pool threads whose caller gave up waiting ), and are frozen once they are done: a command they start then outlives
	the program. The main thread is the one exiting: a command it runs from an atexit handler, a weakref.finalize
	callback or a __del__ that runs after the hook ( registered before multiCMD was imported ) runs as it always has,
	and so do the commands of a call it makes then, whichever thread starts them ( it is waiting for them, unless it
	did not ask to: what it leaves running then is not stopped at exit, as before 1.55 ).
	'''
	return _exiting and not task._at_exit and threading.current_thread() is not threading.main_thread()

# commands between their start's check of _cut_by_exit and their registration: Task -> None ( see _kill_running_at_exit )
_starting = {}
# how long the exit hook waits for those to be registered, in seconds
_EXIT_START_WAIT = 1.0

def _kill_running_at_exit():
	'''
	At interpreter exit, stop the commands that are still running ( internal )
	What a finished command left running in the background is left alone: a fire-and-forget
	command may start a long job on purpose ( `btrfs scrub start`, `nohup ...` ).
	Daemon threads still run meanwhile, so _exiting is set first: from then on such a thread starts no command asked
	for before it ( it fails, see __run_command and _cut_by_exit ), and one it registers meanwhile stops itself ( see
	_execute ). A start that passed that check just before is counted in _starting from before the check until its
	command is registered ( or it fails ): the hook waits for those, up to _EXIT_START_WAIT, so that its look at the
	registry sees their commands, and looks at the registry again after each kill, until nothing new is in it.
	'''
	global _exiting
	_exiting = True
	# ( bounded: a start held up there, a write to a full stdout say, must not hold up the exit )
	deadline = time.monotonic() + _EXIT_START_WAIT
	while _starting and time.monotonic() < deadline:
		time.sleep(0.005)
	killed = set()
	while True:
		commands = [(proc, pgid) for proc, pgid in _live_procs.copy().items() if proc not in killed]
		if not commands:
			return
		killed.update(proc for proc, _ in commands)
		_kill_commands(commands, signal.SIGTERM)

atexit.register(_kill_running_at_exit)

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

def _open_pty(reserved=0, fds=None):
	'''
	Open a pseudo-terminal for a command's output ( internal )
	Output post-processing is off, so bytes arrive exactly as written ( no '\n' to '\r\n' ), and
	the window size is the real terminal's, less the reserved columns ( the [#] prefix ).
	The pty is only the command's output, never its controlling terminal.
	@params:
		reserved: Columns to take off the terminal width
		fds: A dict the descriptors are stored in, as 'master' and 'slave', the moment openpty() returns them, so the
			caller can close them whatever interrupts the setup ( see _close_fds )
	@returns:
		( int, int ) | ( None, None ): The master and slave file descriptors, or None when no pty can be opened
	'''
	global _PTY_WARNED
	if fds is None:
		fds = {}
	try:
		import pty as _pty_module
		fds['master'], fds['slave'] = _pty_module.openpty()
		master, slave = fds['master'], fds['slave']
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

def _close_fds(fds):
	'''
	Close the file descriptors in a dict, taking each out first ( internal ); idempotent, for a settled step
	Taken out before it is closed, so an interrupt in between leaks it rather than have it closed twice ( by then
	the number may be another file's ).
	'''
	for name in list(fds):
		fd = fds.pop(name, None)
		if fd is not None:
			try:
				os.close(fd)
			except OSError:
				pass

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
	while _poll(proc) is None:  # while the process is still running
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
	Wait for a thread to finish, in short steps ( internal )
	A plain join() blocks in C, where a KeyboardInterrupt only arrives if the signal happens to
	land on this very thread; short steps let the waiting thread see it within 0.1s. A _Thread
	waits in such steps by itself ( see _Thread ). Like Thread.join(), this raises RuntimeError for
	a threading.Thread that was never started.
	@params:
		thread: The thread to wait for
		timeout: The longest to wait, in seconds ( None: until it ends )
	@returns:
		bool: Whether it finished
	'''
	deadline = None if timeout is None else time.monotonic() + timeout
	while True:
		step = 0.1 if deadline is None else min(0.1, deadline - time.monotonic())
		thread.join(max(0, step))
		if not thread.is_alive():
			return True
		if step <= 0:
			return False

def _drain(task, readers, timeout):
	'''
	Read a command's output to its end after its own process exited ( internal )
	@params:
		task: The Task ( its stop flag and _last_output are watched )
		readers: The reader threads
		timeout: Inactivity timeout in seconds ( 0: none )
	@returns:
		None | str: None once all output ended, 'stop' when task.stop was set, 'timeout' after timeout seconds without output
	'''
	while any(reader.is_alive() for reader in readers):
		if task.stop:
			return 'stop'
		for reader in readers:
			_join(reader, 0.05)
		# tested after a step, not on entry: a command that exits just past the threshold, before _watch() looked
		# again, has its output end a moment later, and is not a timeout
		if timeout > 0 and time.monotonic() - task._last_output > timeout and any(reader.is_alive() for reader in readers):
			return 'timeout'
	return None

def _join_all(threads, limit):
	'''
	Wait for threads ( a command's output readers, the threads of stopped commands ), up to limit seconds in all ( internal )
	@params:
		threads: The threads
		limit: The longest to wait in total, in seconds ( None: no limit )
	'''
	deadline = None if limit is None else time.monotonic() + limit
	for thread in threads:
		_join(thread, None if deadline is None else max(0, deadline - time.monotonic()))

def _snapshot(mapping, drop_empty=False):
	'''
	The ( key, value ) pairs of a mapping other threads may change, as os.environ ( internal )
	Looking each key up after listing them fails for one deleted in between; that one is left out.
	With drop_empty a variable with an empty name is left out too: only a corrupt os.environ has one, and a
	command given an environment of its own does not get it ( exec refuses it before Python 3.11 ).
	'''
	items = []
	for key in list(mapping):
		if drop_empty and not key:
			continue
		try:
			items.append((key, mapping[key]))
		except KeyError:
			pass
	return items

def _env_text(value, what):
	'''
	An environment name or value as str, from what subprocess accepts ( internal )
	POSIX takes str, bytes and os.PathLike ( os.fsencode ); Windows only str.
	'''
	if isinstance(value, str):
		return value
	if os.name != 'nt' and isinstance(value, (bytes, os.PathLike)):
		try:
			return os.fsdecode(value)
		except TypeError:
			pass
	raise TypeError(f'{what} must be a str{"" if os.name == "nt" else ", bytes or os.PathLike"}, not {type(value).__name__}')

class _EnvSnapshot(collections.abc.Mapping):
	'''A validated environment ( internal ): read-only, equal to the dict it holds, and picklable, unlike types.MappingProxyType'''
	__slots__ = ('_data',)

	def __init__(self, data):
		self._data = data

	def __getitem__(self, key):
		return self._data[key]

	def __iter__(self):
		return iter(self._data)

	def __len__(self):
		return len(self._data)

	def __repr__(self):
		return '_EnvSnapshot(%r)' % (self._data,)

	def __reduce__(self):
		return (_EnvSnapshot, (self._data,))

	def copy(self):
		return dict(self._data)

def _check_env(mapping, name, allow_none=False, drop_empty=False):
	'''
	Validate an environment mapping and take a snapshot of it, so the caller changing it later does not reach the command ( internal )
	Names and values are what subprocess accepts: str, and on POSIX bytes and os.PathLike too ( stored as str ).
	The snapshot is read-only ( an _EnvSnapshot over a dict nobody else holds ); it is checked again whenever it
	is passed on, as is any mapping the caller can still change ( an AsyncExecutor's env ).
	Everything is encoded here, so a name or value the file system encoding cannot represent is refused before any command starts.
	Only a variable inherited from os.environ can have an empty name ( and `env=dict(os.environ, ...)` copies it ): with
	drop_empty ( env ) it is left out, as _child_env leaves it out of the inherited environment; otherwise it is refused.
	A name holding '=' or NUL is always refused.

	@returns:
		_EnvSnapshot | None: The snapshot ( None when mapping is None, or is empty and allow_none: nothing to add )
	'''
	if mapping is None:
		return None
	if not isinstance(mapping, collections.abc.Mapping):
		raise TypeError(f'{name} must be a mapping of variable names to values, not {type(mapping).__name__}')
	env = {}
	for key, value in _snapshot(mapping, drop_empty=drop_empty):
		key = _env_text(key, f'{name}: variable name')
		# Windows has hidden variables such as '=C:': its names may start with '='
		if not key or '\0' in key or '=' in key[os.name == 'nt':]:
			raise ValueError(f'{name}: invalid variable name {key!r}')
		if value is not None or not allow_none:
			value = _env_text(value, f'{name}[{key!r}]' + (' ( or None )' if allow_none else ''))
			if '\0' in value:
				raise ValueError(f'{name}[{key!r}] holds a NUL character')
		if os.name != 'nt':
			try:
				os.fsencode(key)
				if value is not None:
					os.fsencode(value)
			except UnicodeEncodeError as e:
				raise ValueError(f'{name}[{key!r}] cannot be encoded: {e}') from None
		env[key] = value
	return _EnvSnapshot(env) if env or not allow_none else None

def _apply_extra_env(env, extra_env):
	'''
	Add extra_env to env in place; a None value removes the variable ( internal )
	Windows names are case-insensitive, so 'Path' replaces or removes PATH there.
	'''
	names = {key.upper(): key for key in env} if os.name == 'nt' else None
	for key, value in extra_env.items():
		if names is not None:
			old = names.pop(key.upper(), None)
			if old is not None:
				del env[old]
			if value is not None:
				names[key.upper()] = key
		if value is None:
			env.pop(key, None)
		else:
			env[key] = value

def _child_env(task, pager=False):
	'''
	The environment a task's command starts with ( internal )
	Built per command and never written to os.environ, so concurrent commands cannot see each other's.
	"Inherited" is os.environ as this process holds it when the command starts: variables set behind its
	back ( os.putenv, os.unsetenv, C setenv ) reach a command started without env / extra_env / pager only.

	@params:
		task: The Task
		pager: Whether to default PAGER / GIT_PAGER to cat ( for a pty ); a variable the caller set stays as it is

	@returns:
		dict | None: The environment ( a new dict ), None to inherit the parent's untouched
	'''
	base, extra = task._env, task._extra_env
	if base is None and extra is None and not pager:
		return None
	if base is None:
		env = dict(_snapshot(os.environ, drop_empty=extra is not None))
		if pager:
			env.update(PAGER='cat', GIT_PAGER='cat')
	elif extra is None and not pager:
		return base.copy()
	else:
		env = base.copy()
		if pager:
			env.setdefault('PAGER', 'cat')
			env.setdefault('GIT_PAGER', 'cat')
	if extra:
		_apply_extra_env(env, extra)
	return env

def _stop_command(proc, pgid, quiet=False):
	'''
	Kill a command with everything it started ( _kill_commands; quiet as there ) ( internal )
	An interrupt that ends the wait for it ( a second Ctrl-C, or whatever a signal handler raises ) leaves it sent
	SIGTERM at least, and still registered: kill_all() and the exit hook do the rest.
	'''
	try:
		_kill_commands([(proc, pgid)], signal.SIGINT, quiet)
	except BaseException:
		_send(proc, pgid, signal.SIGTERM)
		_send(proc, pgid, signal.SIGCONT)
		raise

def _execute(task, timeout, quiet, decor, use_pty=False):
	'''
	Run a task's command to the end: start it, stream its output, enforce the timeout and the stop flag, reap it ( internal )

	After a timeout / stop / Ctrl-C the command is killed with everything it started, and its
	output gets at most one more second to end, so something that escaped the kill cannot hold the
	call. After a normal exit the output is read to its end, however long that takes. The command
	leaves the live registry as soon as it is reaped, so kill_all() never touches what it left
	running in the background on purpose.
	Sets task.returncode ( 124 after a timeout ), start_time and end_time. A KeyboardInterrupt is
	re-raised once the command is dead; a second one ends that wait, the command sent SIGTERM and still registered.
	A KeyboardInterrupt ( or a SystemExit from a signal handler ) that lands while the command starts stops it like
	any other, from the moment Popen has stored the child's pid ( see _Popen ). One that Python raises as the fork's
	C call ( _fork_exec / posix_spawn ) returns, before Popen has stored the pid it returned, loses the command, which
	then runs on in a process group of its own, unknown to kill_all() and the exit hook, as in multiCMD 1.54; nowhere
	else is a command lost. Python raises it on the main thread only, and the window is the fork's own length: some
	60 - 150 us, until the child has exec'd ( a quarter of a run of `true` on Python 3.13, 0.3 % of a 50 ms command ).

	@params:
		task: The Task
		timeout: Inactivity timeout in seconds ( 0: none )
		quiet: Whether to skip showing output
		decor: ( prefix, color start, color end ) for displayed lines
		use_pty: Whether the command writes to a pty instead of pipes ( POSIX only )
	'''
	# whether the caller gave this command an environment ( rather than only the pty's pager defaults )
	explicit = task._env is not None or task._extra_env is not None
	# the pty's descriptors that no file object owns yet, from the moment they exist ( see _open_pty ): closed by
	# _close_fds whatever cuts the start short ( each taken out of here before it is closed ), the master taken out
	# once its file object has it
	fds = {}
	# the pty's file object, once there is one ( see below )
	wrapped = []
	proc = pgid = None
	# the command as soon as it may run, before _spawn returns ( see _Popen )
	started = []
	streams = []
	readers = []
	# the interrupt to raise once the command is dead ( see below )
	interrupted = None
	# from the pty on, an interrupt or any failure closes it; from the spawn on, it still stops the command, which runs
	# in a process group of its own
	try:
		if use_pty:
			_open_pty(len(decor[0]), fds)
		if fds:
			# the pty's side, wrapped in its file object before anything starts, and only ever closed through it. The
			# file object is made and kept in one step ( list.extend() runs map's io.open() in C, where no interrupt
			# lands ): whatever cuts this short, it is either in wrapped, owning the master, or was never made
			wrapped.extend(map(io.open, (fds['master'],), ('rb',)))
			fds.pop('master', None)
			try:
				proc, pgid = _spawn(task.command, fds['slave'], fds['slave'], _child_env(task, pager=True), explicit, started)
			finally:
				# only the command keeps the terminal side open, so the master sees EIO once it is gone
				_close_fds(fds)
			task._pty = True
			streams.append((wrapped[0], task.stdout))
		else:
			proc, pgid = _spawn(task.command, subprocess.PIPE, subprocess.PIPE, _child_env(task), explicit, started)
			streams.extend(((proc.stdout, task.stdout), (proc.stderr, task.stderr)))
		# ( for kill_all() and the exit hook, which know no quiet of their own: see _give_up_on )
		proc._multicmd_quiet = quiet
		_register(proc, pgid)
		_starting.pop(task, None)
		if _cut_by_exit(task):
			# started as the exit hook began: it may have given up waiting for this one ( see _kill_running_at_exit )
			task.stop = True
		# started: a long-lived executor keeps its tasks, not the environments they were started with
		task._env = task._extra_env = None
		task.start_time = time.time()
		task._last_output = time.monotonic()
		readers.extend(_Thread(_read_stream, (stream, target, task, decor, quiet)) for stream, target in streams)
		_start_threads(readers)
		reason = _watch(task, proc, timeout)
	except BaseException as e:
		first = e
		# the interrupts that cut settle short ( see _settle )
		cuts = []
		def settle():
			nonlocal proc, pgid
			found = _started_command(started)
			if found is not None:
				# what cut it short may have come before _spawn returned ( see _Popen ): the last one with a pid is the
				# command ( or an exec that failed, whose reaping was cut short: stopping it changes nothing )
				proc, pgid = found
			if proc is not None:
				# registered here, so kill_all() and the exit hook reach it whatever cuts the kill short
				proc._multicmd_quiet = quiet
				_live_procs[proc] = pgid
			_starting.pop(task, None)
			# the pty's descriptors no file object owns yet, and a stream no reader reads, are closed here ( a reader
			# closes its own; closing a file object again does nothing )
			if wrapped:
				fds.pop('master', None)
			_close_fds(fds)
			pending = streams
			if not pending:
				if wrapped:
					pending = [(wrapped[0], None)]
				elif proc is not None:
					pending = [(proc.stdout, None), (proc.stderr, None)]
			for i, (stream, _) in enumerate(pending):
				if stream is not None and (i >= len(readers) or readers[i]._abandon()):
					stream.close()
			if cuts and isinstance(first, _INTERRUPTS) and proc is not None:
				# a second interrupt ( of any kind ) while the first is dealt with: it ends the wait for the command,
				# which is sent SIGTERM now ( kill_all() and the exit hook do the rest )
				_send(proc, pgid, signal.SIGTERM)
				_send(proc, pgid, signal.SIGCONT)
		while True:
			try:
				_settle(settle, cuts)
				break
			except BaseException as c:
				if cuts and c is cuts[0]:
					# raised by _settle, once settle has completed
					break
				# pending as _settle was called, so settle never ran ( or landing as it returned ): it runs again, knowing of it
				cuts.append(c)
		cut = cuts[0] if cuts else None
		if proc is None:
			# it never started ( or _spawn raised ): nothing to undo, and the caller records why ( 127, 126, -1 )
			if cut is not None:
				raise cut
			raise
		if cut is not None:
			if isinstance(first, _INTERRUPTS):
				raise cut
			# the first Ctrl-C, while a failure was dealt with: from here on it is handled as any Ctrl-C
			first = cut
		if not isinstance(first, KeyboardInterrupt):
			_stop_command(proc, pgid, quiet)
			_unregister(proc)
			_join_all(readers, 1)
			raise first
		reason, interrupted = 'stop', first
	if reason == 'timeout':
		task.stderr.append('Timeout!')
	if reason is not None:
		_stop_command(proc, pgid, quiet)
	_unregister(proc)
	task.returncode = _poll(proc)
	if reason is None:
		# the command exited, but something it started may still hold its output open: keep reading,
		# while a stop, the inactivity timeout, Ctrl-C or kill_all() can still end it
		_draining[proc] = pgid
		try:
			reason = _drain(task, readers, timeout)
		except _INTERRUPTS as e:
			# ( a SystemExit from a SIGTERM handler as well: the background group is stopped before it is raised )
			reason, interrupted = 'stop', e
		finally:
			_draining.pop(proc, None)
		if reason == 'timeout':
			task.stderr.append('Timeout!')
		if reason is not None:
			_stop_command(proc, pgid, quiet)
	_join_all(readers, None if reason is None else 1)
	if reason == 'timeout':
		task.returncode = 124
	elif task.returncode is None:
		# process been killed via sigkill or similar without a normal exit code
		if task.stderr and task.stderr[-1].strip().startswith('Ctrl C detected, Emergency Stop!'):
			task.returncode = 137
		else:
			task.returncode = -1
			task._error = True
	task.end_time = time.time()
	task._finalized = True
	if interrupted is not None:
		raise interrupted

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
		try:
			# a start from here until its command is registered ( see _execute ) is one the exit hook waits for
			_starting[task] = None
			if task.stop:
				# stopped before it started: it stays unfinished, like a skipped queued command
				task._env = task._extra_env = None
				return task.stdout + task.stderr if with_stdErr else task.stdout
			try:
				if _cut_by_exit(task):
					# never skipped silently: it fails, with that reason, as a command multiCMD cannot start does
					raise RuntimeError('not started: the interpreter is exiting')
				if dry_run:
					if not quiet:
						_display.message(_dry_run_line(task))
					return task.stdout + task.stderr if with_stdErr else task.stdout
				if not quiet:
					_display.message('Running command: ' + _quote_command(task.command), decor)
					_display.message('-' * 100, decor)
				_execute(task, timeout, quiet, decor, use_pty)
			# ( each failure is recorded whatever Ctrl-C lands meanwhile, and the Ctrl-C raised after it: see _settle )
			except FileNotFoundError as e:
				_settle(lambda: _cannot_run(task, 127, 'Command path not found', e, quiet))
			except (PermissionError, _ExecRefused) as e:
				# _ExecRefused: the system refused to execute it ( ENOEXEC, E2BIG, ... ): 126 whichever way it was started, as a shell reports it
				_settle(lambda: _cannot_run(task, 126, 'Command cannot be executed', e, quiet))
			except Exception as e:
				_settle(lambda: _fail_task(task, str(e), e, quiet))
			finally:
				task._finalized = True
				# whatever happened, a long-lived executor keeps its tasks, not the environments they were given
				task._env = task._extra_env = None
		finally:
			_starting.pop(task, None)
		if not quiet:
			_display.message('-' * 100, decor)
			_display.message(f'Process exited with return code {task.returncode}', decor)
		if with_stdErr:
			return task.stdout + task.stderr
		else:
			return task.stdout

def _write_error(text):
	'''
	Write a failure message to stderr ( internal ): one that cannot be written ( stderr closed, a broken pipe ) is
	dropped, as the failure it is about is already recorded on the task
	'''
	try:
		print(text, file=sys.stderr, flush=True)
	except Exception:
		pass

def _show_error(text, key=None):
	'''
	Show a failure message on stderr ( internal ); see _write_error
	Inside _settle() it is shown once the step is done, and a step run again shows it once: the last message
	given for each key ( default: the text ).
	'''
	messages = getattr(_settling, 'messages', None)
	if messages is None:
		_write_error(text)
	else:
		messages[text if key is None else key] = text

def _cannot_run(task, returncode, what, error, quiet=False):
	'''
	Record that a command could not be run ( internal ): not found ( 127 ) or not executable ( 126 ), with the
	error in stderr; what it is about is shown on stderr unless quiet, as every failure multiCMD reports.
	Run again for the same failure ( after a Ctrl-C cut it short ), it records the error once.
	'''
	line = str(error)
	if task.stderr[-1:] != [line]:
		task.stderr.append(line)
	task.returncode = returncode
	if not quiet:
		_show_error(f'{what}: {task.command[0]}', task)

def _fail_task(task, message, error, quiet=False):
	'''
	Finalize a task multiCMD itself could not run ( internal ): return code -1 and _error ( not a SIGHUP death ),
	the message and error's traceback in stderr, end_time; then the message is shown on stderr unless quiet
	The failure is recorded before it is shown, so a message that cannot be shown changes nothing. Run again for
	the same failure ( after a Ctrl-C cut it short ), it records the lines once.
	@params:
		task: The Task
		message: What went wrong ( str )
		error: The exception that says so ( its traceback goes into stderr )
		quiet: Whether to skip showing the message
	'''
	lines = message.split('\n')
	record = lines + ''.join(traceback.format_exception(type(error), error, error.__traceback__)).split('\n')
	if task.stderr[-len(record):] != record:
		task.stderr.extend(record)
	task.returncode = -1
	task._error = True
	task.end_time = time.time()
	task._env = task._extra_env = None
	task._finalized = True
	if not quiet:
		_show_error(f'Error running command: {task.command}\n' + '\n'.join(lines), task)

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
		try:
			thread.join()
		finally:
			# also when the join is interrupted ( Ctrl-C ): the thread is already off __finished_threads
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
	if _IS_POSIX:
		if not isinstance(command, str):
			command = _quote_command(command)
		return ['/bin/sh', '-c', command]
	if not isinstance(command, str):
		# POSIX quoting means nothing to cmd.exe, and its own rules cannot be applied safely
		raise ValueError(f'shell mode on this system needs a command string, not an argument list ( a POSIX shell is required ): {command!r}')
	return [os.environ.get('COMSPEC', 'cmd.exe'), '/c', command]

def _shell_template(template):
	'''
	Turn a shell command template into a script that takes its input line as "$1" ( internal )
	Each {} is replaced to suit where it sits, so the line is never parsed as shell code:
	unquoted -> "$1", inside double quotes -> $1, inside single quotes -> '"$1"'.
	A template without {} gets "$1" as its last argument.
	@params:
		template: The command template ( str )
	@returns:
		str: The script for /bin/sh -c, to run with the line as its first positional parameter
	'''
	if '{}' not in template:
		return template + ' "$1"'
	out = []
	quote = None  # None: unquoted; "'" or '"': inside those quotes
	i = 0
	while i < len(template):
		if template.startswith('{}', i):
			out.append('"$1"' if quote is None else '$1' if quote == '"' else '\'"$1"\'')
			i += 2
			continue
		char = template[i]
		if char == '\\' and quote != "'" and i + 1 < len(template):
			out.append(template[i:i + 2])
			i += 2
			continue
		if quote is None and char in '\'"':
			quote = char
		elif char == quote:
			quote = None
		out.append(char)
		i += 1
	return ''.join(out)

def _build_tasks(commands, parse=False, use_sudo=..., shell=False, env=None, extra_env=None):
	'''
	Format commands into Tasks, prefixing sudo when requested and available ( internal )
	env / extra_env ( validated here, before anything runs ) are copied onto every Task

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
	# one set of range variables per batch: shared by its commands, never by the next call
	variables = {}
	formatedCommands = []
	for command in commands:
		formatedCommands.extend(__format_command(command,expand=parse,shell=shell,variables=variables))
	if use_sudo:
		formatedCommands = [[SUDO_PATH] + command for command in formatedCommands]
	env = _check_env(env, 'env', drop_empty=True)
	extra_env = _check_env(extra_env, 'extra_env', allow_none=True)
	# one snapshot shared by every task of the call
	tasks = [Task(command) for command in formatedCommands]
	# a call the main thread makes once the exit hook has begun ( from an atexit handler: see _cut_by_exit )
	at_exit = _exiting and threading.current_thread() is threading.main_thread()
	for task in tasks:
		task._env = env
		task._extra_env = extra_env
		task._at_exit = at_exit
	return tasks, bool(use_sudo)

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

def __format_command(command,expand = False,shell = False,variables = None):
	'''
	Format a command

	@params:
		command: The command to format
		expand: Whether to expand ranges
		shell: Whether to run the command through the system shell
		variables: The range variables shared with the other commands of the batch

	@returns:
		list[list[str]]: The formatted commands sequence
	'''
	if variables is None:
		variables = {}
	if isinstance(command,str):
		if expand:
			commands = _expand_ranges_fast(command, variables)
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
			sanitized_expanded_command = [_expand_ranges_fast(field, variables) for field in sanitized_command]
			# now the command had been expanded to list of list of fields with each field expanded to all possible options
			# we need to generate all possible combinations of the fields
			# we will use the cartesian product to do this
			commands = [list(command) for command in itertools.product(*sanitized_expanded_command)]
		if shell:
			return [_shell_argv(command) for command in commands]
		return commands
	else:
		return __format_command(str(command),expand=expand,shell=shell,variables=variables)

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
	# Windows ping counts with -n
	count = '-c' if _IS_POSIX else '-n'
	single_host = False
	if isinstance(hosts,str):
		commands = [f'ping {count} 1 {hosts}']
		single_host = True
	else:
		commands = [f'ping {count} 1 {host}' for host in hosts]
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
	Every task is asked, whatever interrupt lands meanwhile ( see _settle: it is raised after it ).
	'''
	def stop():
		for task in tasks:
			task.stop = True
	_settle(stop)

def _stop_and_wait(tasks, threads, limit=5, forget=None):
	'''
	After a Ctrl-C ( or a failure ): stop the tasks' commands, then wait for their threads, up to limit seconds in all ( internal )
	The threads that never got to their target are abandoned first, and forgotten ( forget(thread), see
	_start_threads ), in the same settled step, so the wait never waits for one that does not run. The commands
	run in process groups of their own, so the terminal's Ctrl-C did not reach them. A second Ctrl-C ends the wait
	at once ( raised while the first is handled, it has the first as its __context__ ); one that lands while the
	tasks are stopped is raised once they are, and skips it.
	'''
	def stop():
		_abandon_all(threads, forget)
		for task in tasks:
			task.stop = True
	_settle(stop)
	_join_all(threads, limit)

def _run_pool(tasks, decors, sem, timeout, quiet, dry_run, max_threads, use_pty=False):
	'''
	Run tasks on at most max_threads worker threads and wait for all of them ( internal )
	Each worker takes the next task in order, so at most max_threads commands ( and their two
	output readers each ) exist at any time, however many tasks there are.
	On KeyboardInterrupt every task is stopped: running commands are killed, the rest never start.
	'''
	if not tasks:
		return
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
			try:
				__run_command(task, sem, timeout, quiet, dry_run, decor=decor, use_pty=use_pty)
			finally:
				# the worker goes on to other tasks: this one is not alive with it, and keeps no Thread object
				task.thread = None
	wanted = min(max_threads, len(tasks))
	workers = []
	# a worker that could not start -> the error
	failed = {}
	def fail(thread, error):
		# that worker leaves its share of the commands to the others ( only without any do they fail ). One failed
		# start may be passing ( EAGAIN ): the next worker is tried; two in a row end the starts, as the next would
		# most likely fail the same way
		failed[thread] = error
		index = workers.index(thread)
		return index > 0 and workers[index - 1] in failed
	# whether the starts ended early: _start_threads has then waited, or a second interrupt ended that
	early = []
	try:
		workers.extend(_Thread(worker) for _ in range(wanted))
		# a Ctrl-C while they start stops every task, so the workers already started do not run them unsupervised,
		# and waits for those ( see _start_threads: a second Ctrl-C ends that wait )
		running = _start_threads(workers, fail=fail, abort=lambda: _stop_tasks(tasks), wait=lambda: _join_all(workers, 5), early=early)
		error = next(iter(failed.values()), None)
		if not running:
			_settle(lambda: [_fail_task(task, f'cannot start a thread to run it: {error}', error, quiet) for task in tasks])
			return
		if len(running) < wanted and not quiet:
			# commands that must run at the same time may then wait on each other for good: say so
			_show_error(f'multiCMD: started {len(running)} of {wanted} threads: {error}')
		for thread in running:
			_join(thread)
	except BaseException:
		# a Ctrl-C ( or anything else ) while the workers start or run: the commands not started yet never will,
		# nor do workers made but not started yet. When the starts ended early, _start_threads settled the ones it
		# was given and waited for the ones that run, unless a second interrupt ended that: it is only done again
		# here, without the wait, in case its own settling was cut short. Anywhere else ( right after the starts
		# included ) this is the first interrupt, and the commands are waited for
		_stop_and_wait(tasks, workers, 0 if early else 5)
		raise

def run_command(command, timeout=0,max_threads=1,quiet=False,dry_run=False,with_stdErr=False,
				return_code_only=False,return_object=False,wait_for_return=True, sem = None, 
				use_sudo = ..., raise_error = False, prefix = False, color = None, pty = False, shell = False,
				env = None, extra_env = None):
	'''
	Run a command

	A Ctrl-C ( or a signal handler's exception ) that Python raises as the fork returns, before Popen has stored the
	child's pid, leaves the command running unregistered, unknown to kill_all() and the exit hook, as in 1.54: the
	window is the fork's own length, some 60 - 150 us per command, and only a serial run ( max_threads=1 ) forks on
	the main thread, where Python raises them. Anywhere else the command is stopped before the interrupt is re-raised
	( see _execute ).

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
		env: The whole environment of the command ( the commands ), replacing the one inherited from this process, as subprocess's env ( Mapping[str, str] | None: inherit it )
		extra_env: Variables added to the environment, or overriding it ( Mapping[str, str | None] | None ); a None value removes the variable.
			Applied on top of env when both are given ( empty: nothing ). Per call and per command: it never changes os.environ nor reaches other commands.
			The inherited environment is os.environ when the command starts ( not variables set behind its back with os.putenv or C setenv ); an inherited variable with an empty name ( a corrupt os.environ entry ) is left out of the environment of a command given env / extra_env, and out of an env copied from os.environ ( `env=dict(os.environ)` ); a command given neither, with or without pty, inherits it as before. An empty name in extra_env is a ValueError.
			Names and values are str, on POSIX bytes and os.PathLike too; on POSIX the command is looked up in the PATH of the environment it gets ( Windows searches the calling process's PATH )
			sudo resets the environment by default ( env_reset ), so with use_sudo the variables may not reach the command

	@returns:
		None | int | list[str] | Task: The output of the command
	'''
	return run_commands(commands=[command], timeout=timeout, max_threads=max_threads, quiet=quiet, 
					 dry_run=dry_run, with_stdErr=with_stdErr, return_code_only=return_code_only, 
					 return_object=return_object,parse=False,wait_for_return=wait_for_return,sem=sem,
					 use_sudo=use_sudo, raise_error=raise_error, prefix=prefix, color=color, pty=pty, shell=shell, env=env, extra_env=extra_env)[0]

def run_commands(commands, timeout=0,max_threads=1,quiet=False,dry_run=False,with_stdErr=False,
				 return_code_only=False,return_object=False, parse = False, wait_for_return = True, 
				 sem : threading.Semaphore = None, use_sudo=..., raise_error = False, prefix = False, color = None, pty = False, shell = False,
				 env = None, extra_env = None):
	'''
	Run multiple commands in parallel

	A Ctrl-C that lands while a serial run's command is being forked can leave that command running unregistered, as
	in 1.54 ( see run_command ).

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
		env: The whole environment of each command, replacing the one inherited from this process, as subprocess's env ( Mapping[str, str] | None: inherit it )
		extra_env: Variables added to the environment, or overriding it ( Mapping[str, str | None] | None ); a None value removes the variable.
			Applied on top of env when both are given ( empty: nothing ). Per call and per command: it never changes os.environ nor reaches other commands.
			The inherited environment is os.environ when the command starts ( not variables set behind its back with os.putenv or C setenv ); an inherited variable with an empty name ( a corrupt os.environ entry ) is left out of the environment of a command given env / extra_env, and out of an env copied from os.environ ( `env=dict(os.environ)` ); a command given neither, with or without pty, inherits it as before. An empty name in extra_env is a ValueError.
			Names and values are str, on POSIX bytes and os.PathLike too; on POSIX the command is looked up in the PATH of the environment it gets ( Windows searches the calling process's PATH )
			sudo resets the environment by default ( env_reset ), so with use_sudo the variables may not reach the command

	@returns:
		list: The output of the commands ( list[None] | list[int] | list[list[str]] | list[Task] | None )
	'''
	# initialize the tasks
	tasks, use_sudo = _build_tasks(commands, parse=parse, use_sudo=use_sudo, shell=shell, env=env, extra_env=extra_env)
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
	if use_sudo and not dry_run:
		_validate_sudo()
	if dry_run:
		# nothing runs: show each command once, quoted so it can be pasted into a shell
		for task in tasks:
			if not quiet:
				_display.message(_dry_run_line(task))
			# nothing runs: the returned tasks do not keep their environments
			task._env = task._extra_env = None
	elif not wait_for_return:
		__reap_finished_threads()
		threads = []
		early = []
		def forget(thread):
			# it never runs: untracked, as it never reports itself finished, and its task keeps it ( done, so
			# task.thread.join() returns at once ) but not its environment
			__running_threads.discard(thread)
			thread.task.thread = thread
			thread.task._env = thread.task._extra_env = None
		def fail(thread, error):
			# that command fails, the others run
			_fail_task(thread.task, f'cannot start its thread: {error}', error, quiet)
		try:
			threads.extend(_Thread(__run_tracked_command, (task,sem,timeout,quiet,False,decor,use_pty), task) for task, decor in zip(tasks, decors))
			for thread in threads:
				thread.task.thread = thread
			# the whole batch is tracked before any starts, so a thread is registered before it reports itself finished
			# and a join_threads() from another thread meanwhile waits for all of it ( see _Thread )
			__running_threads.update(threads)
			# a Ctrl-C while they start: the commands not started yet never will ( skipped ), the others are stopped
			# and waited for ( see _start_threads: a second Ctrl-C ends that wait )
			_start_threads(threads, forget=forget, fail=fail, abort=lambda: _stop_tasks(tasks), wait=lambda: _join_all(threads, 5), early=early)
		except BaseException:
			# ( also when the starts' own settling was cut short: no thread that never runs stays tracked. When the
			# starts ended early the wait was _start_threads' own; one that lands as they return is the first, and
			# the commands are waited for )
			_stop_and_wait(tasks, threads, 0 if early else 5, forget)
			raise
	elif max_threads > 1:
		_run_pool(tasks, decors, sem, timeout, quiet, False, max_threads, use_pty)
	else:
		try:
			for task, decor in zip(tasks, decors):
				__run_command(task, sem, timeout, quiet, False, decor=decor, use_pty=use_pty)
		except _INTERRUPTS:
			_stop_tasks(tasks)
			raise
	# return the only the output for the tasks
	if raise_error:
		# dry-run and still-running commands have no return code yet: they did not fail
		failed = [task for task in tasks if task.returncode not in (0, None)]
		if failed:
			raise CommandError(failed, total=len(tasks))
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

	A Ctrl-C ( or a SystemExit from a signal handler, as a SIGTERM handler's sys.exit() ) while it waits stops the
	commands of the multiCMD threads it waits for ( they run in process groups of their own, so the terminal's
	Ctrl-C did not reach them ), the ones still queued skipped, waits up to 5 s in all for them ( a second Ctrl-C
	ends that wait at once ) and re-raises, as a waiting run_commands() does.

	@params:
		threads: The threads to join ( default: every fire-and-forget thread still running )
		timeout: The longest to wait for each thread, in seconds ( None: until it ends )

	@returns:
		None
	'''
	if threads is ...:
		threads = list(__running_threads.copy())
	else:
		threads = list(threads)
	try:
		for thread in threads:
			_join(thread, timeout)
	except _INTERRUPTS:
		ours = []
		def stop():
			ours[:] = [thread for thread in threads if isinstance(thread, _Thread) and thread.task is not None]
			_stop_tasks([thread.task for thread in ours])
		# ( an interrupt while they are stopped is raised by _settle: it ends the wait, as a second one would )
		_settle(stop)
		_join_all(ours, 5)
		raise
	__reap_finished_threads()

_EXIT_STATUS_HELP = '''exit status:
  0      every command succeeded, or --dry-run
  N      one command: its own exit code ( killed by signal S: 128+S )
  N      several commands: how many failed, capped at 101
  2      usage error
  130    interrupted with Ctrl-C ( 128+S after SIGTERM / SIGHUP )'''

def _exit_status(tasks):
	'''
	The CLI exit status for finished tasks ( internal )
	One command: its own return code ( killed by signal N: 128+N ). Several: how many failed, capped at 101.
	'''
	if len(tasks) == 1:
		returncode = tasks[0].returncode
		if returncode is None or tasks[0]._error:
			return 1
		return 128 - returncode if returncode < 0 else returncode
	return min(sum(1 for task in tasks if task.returncode not in (0, None)), 101)

def _install_cli_signal_handlers():
	'''
	Make SIGTERM / SIGHUP stop the commands too, and terminal reads fail in every command ( internal )
	The commands run in process groups of their own, so signals sent to mcmd's group ( e.g. by
	`timeout`, or a closed terminal ) would not reach them. A signal mcmd was started with
	ignored stays ignored: `nohup mcmd ...` must keep its commands running when the session ends.
	SIGTTIN / SIGTTOU are ignored, which every command inherits through exec and through any
	shell ( dash clears the blocked mask _spawn sets ), so a command that reads the terminal from
	its background group fails at once instead of hanging.
	'''
	def handler(signum, frame):
		kill_all(signum)
		sys.exit(128 + signum)
	for name in ('SIGTERM', 'SIGHUP'):
		signum = getattr(signal, name, None)
		if signum is not None and signal.getsignal(signum) != signal.SIG_IGN:
			signal.signal(signum, handler)
	for signum in _TTY_SIGNALS:
		signal.signal(signum, signal.SIG_IGN)

_RETURNCODE_LABELS = {124: 'timeout', 126: 'cannot execute', 127: 'not found'}

def _format_returncode(task):
	'''
	A task's return code with its meaning, for the summary ( internal )
	'''
	returncode = task.returncode
	if returncode is None:
		return 'not run'
	if task._error:
		return f'{returncode} (error)'
	if returncode < 0:
		try:
			name = signal.Signals(-returncode).name
		except ValueError:
			name = f'signal {-returncode}'
		return f'{returncode} ({name})'
	if returncode in _RETURNCODE_LABELS:
		return f'{returncode} ({_RETURNCODE_LABELS[returncode]})'
	return str(returncode)

def _summary(tasks, show_all=False):
	'''
	The end-of-run summary: a table of the failed commands ( or of all of them ) and a tally line ( internal )
	@params:
		tasks: The finished tasks
		show_all: Whether to list every command instead of only the failed ones
	@returns:
		str: The summary, without a trailing newline
	'''
	failed = [index for index, task in enumerate(tasks, 1) if task.returncode not in (0, None)]
	rows = range(1, len(tasks) + 1) if show_all else failed
	table = ''
	if rows:
		data = [['#', 'Command', 'Return code', 'Time']]
		for index in rows:
			task = tasks[index - 1]
			duration = task.duration
			data.append([str(index), _quote_command(task.command), _format_returncode(task),
				'-' if duration is None else f'{duration:.2f}s'])
		table = pretty_format_table(data)
	if failed:
		tally = f'{len(tasks)} commands: {len(tasks) - len(failed)} succeeded, {len(failed)} failed'
	else:
		tally = f'{len(tasks)} commands: all succeeded'
	return table + tally

def _read_input(path):
	'''
	The lines of an input file without their line endings ( '-' reads stdin ) ( internal )
	'''
	if path == '-':
		return sys.stdin.read().splitlines()
	with open(path) as f:
		return f.read().splitlines()

def _fill_templates(templates, lines, shell=False):
	'''
	One command per template and input line, with {} replaced by the line ( internal )
	The line is substituted after the template is split into arguments, so it always stays one
	argument; a template without {} gets the line as its last argument. In shell mode the line is
	never put into the shell code: it is passed as "$1" to /bin/sh -c ( see _shell_template ).
	@params:
		templates: The command templates ( list[str] )
		lines: The input lines ( list[str] )
		shell: Whether the commands run through the system shell
	@returns:
		list[list[str]]: The argument lists
	@raises:
		ValueError: shell mode without a POSIX shell, or a template with an unbalanced quote
	'''
	if shell and not _IS_POSIX:
		raise ValueError('-a with --shell needs a POSIX shell')
	commands = []
	for template in templates:
		script = _shell_template(template) if shell else None
		argv = None if shell else _split_command(template)
		for line in lines:
			if shell:
				commands.append(['/bin/sh', '-c', script, 'mcmd', line])
			elif any('{}' in arg for arg in argv):
				commands.append([arg.replace('{}', line) for arg in argv])
			else:
				commands.append(argv + [line])
	return commands

def _cli_commands(args):
	'''
	The commands one mcmd invocation runs ( internal )
	Positional commands come first ( or, with -a, the templates filled from its input ), then the
	-f commands. With -p every command and template is range-expanded, sharing one set of range
	variables; input lines themselves are never expanded.
	@returns:
		list[str | list[str]]: Command strings, and argument lists for filled templates
	'''
	variables = {}
	def expand(command):
		return _expand_ranges_fast(command, variables) if args.parse else [command]
	if args.arg_file:
		lines = []
		for path in args.arg_file:
			lines.extend(line for line in _read_input(path) if line.strip())
		templates = [template for command in args.commands for template in expand(command)]
		commands = _fill_templates(templates, lines, args.shell)
	else:
		commands = [expanded for command in args.commands for expanded in expand(command)]
	for path in args.file or []:
		for line in _read_input(path):
			if line.strip() and not line.lstrip().startswith('#'):
				commands.extend(expand(line))
	for command in commands:
		if isinstance(command, str) and not command.strip():
			raise ValueError('empty command')
	if args.shell:
		# filled templates are already argv lists that take their line as "$1"
		commands = [_shell_argv(command) if isinstance(command, str) else command for command in commands]
	return commands

def main():
	parser = argparse.ArgumentParser(description='Run multiple commands in parallel', epilog=_EXIT_STATUS_HELP,
		formatter_class=argparse.RawDescriptionHelpFormatter)
	parser.add_argument('commands', metavar='command', type=str, nargs='*',help='commands to run ( with -a: templates, {} is replaced by each input line )')
	parser.add_argument('-p','--parse', action='store_true',help='Parse ranged input and expand them into multiple commands')
	parser.add_argument('-t','--timeout', metavar='SECONDS', type=float, default=0,help='kill a command after SECONDS with no output at all ( 0 = never, the default )')
	parser.add_argument('-m','--max_threads', metavar='max_threads', type=int, default=1,help='maximum number of commands to run at once ( 0 = all )')
	parser.add_argument('--sudo', action='store_true',help='use sudo for commands')
	parser.add_argument('-q','--quiet', action='store_true',help='quiet mode: hide command output ( the summary still shows )')
	parser.add_argument('-n','--dry-run', action='store_true',help='print each command, quoted, instead of running it')
	parser.add_argument('--shell', action='store_true',help='run each command through /bin/sh -c ( pipes, ;, > and globs work )')
	parser.add_argument('-e','--env', metavar='NAME=VALUE', action='append',help='set NAME to VALUE in the environment of every command ( repeatable ); NAME alone removes it')
	parser.add_argument('-f','--file', metavar='FILE', action='append',help='read commands from FILE, one per line ( - for stdin; blank lines and # comments are skipped )')
	parser.add_argument('-a','--arg-file', metavar='FILE', action='append',help='run each command template once per line of FILE, with {} replaced by the line ( - for stdin )')
	summary = parser.add_mutually_exclusive_group()
	summary.add_argument('--summary', dest='summary', action='store_const', const='all',help='list every command in the end-of-run summary ( default: only failed ones )')
	summary.add_argument('--no-summary', dest='summary', action='store_const', const='none',help='do not print the end-of-run summary')
	parser.add_argument('--no-prefix', action='store_true',help='do not prefix output lines with [#] when commands run at once')
	parser.add_argument('--no-color', action='store_true',help='do not color output lines per command')
	pty_mode = parser.add_mutually_exclusive_group()
	pty_mode.add_argument('--pty', dest='pty', action='store_true',help='give commands a pseudo-terminal so they print live ( default when stdout is a terminal )')
	pty_mode.add_argument('--no-pty', dest='pty', action='store_false',help='give commands plain pipes')
	parser.set_defaults(summary='failed', pty=None)
	parser.add_argument('-V','--version', action='version', version=f'%(prog)s {version} @ {COMMIT_DATE} by pan@zopyr.us')
	args = parser.parse_args()
	if not args.commands and not args.file:
		parser.error('no commands given')
	if args.arg_file and not args.commands:
		parser.error('-a/--arg-file needs at least one command template')
	if '-' in (args.file or []) and '-' in (args.arg_file or []):
		parser.error('-f - and -a - cannot both read stdin')
	extra_env = {}
	for assignment in args.env or []:
		name, equals, value = assignment.partition('=')
		if not name:
			parser.error(f'-e/--env: invalid assignment {assignment!r}')
		extra_env[name] = value if equals else None
	# set_sudo() validates that sudo exists and that we are not already root
	# before enabling it, and updates the USE_SUDO global accordingly.
	set_sudo(args.sudo)
	_install_cli_signal_handlers()
	try:
		commands = _cli_commands(args)
		if not commands:
			print('mcmd: no commands to run', file=sys.stderr)
			return 2
		use_pty = args.pty if args.pty is not None else (_IS_POSIX and _stdout_isatty() and not args.quiet and not args.dry_run)
		tasks = run_commands(commands, timeout=args.timeout, max_threads=args.max_threads,
			quiet=args.quiet and not args.dry_run, dry_run=args.dry_run,
			with_stdErr=True, return_object=True,
			prefix=not args.no_prefix, color=False if args.no_color else None, pty=use_pty,
			extra_env=extra_env or None)
	except (OSError, ValueError) as e:
		print(f'mcmd: {e}', file=sys.stderr)
		return 2
	except KeyboardInterrupt:
		print('\nInterrupted', file=sys.stderr)
		return 130
	if args.dry_run:
		return 0
	if len(tasks) > 1 and args.summary != 'none':
		_display.message(_summary(tasks, show_all=args.summary == 'all'))
	return _exit_status(tasks)

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
	# select() cannot watch stdin on Windows; msvcrt can tell whether a key is waiting
	try:
		import msvcrt
	except ImportError:
		msvcrt = None
	# Loop until the timeout
	for remaining in range(timeout, 0, -1):
		# If there is an input, return it
		if msvcrt is not None:
			if msvcrt.kbhit():
				return input().strip()
		elif sys.stdin in select.select([sys.stdin], [], [], 0)[0]:
			return input().strip()
		# Print the remaining time
		print(f"\r{prompt} [{remaining}s]: ", end='', flush=True)
		# Wait a second
		time.sleep(1)
	# If there is no input, return None
	return None

def pretty_format_table(data, delimiter=None, header=None, full=False, remove_empty_columns=False):
	import re
	import unicodedata
	version = 1.21
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
	# Widths count terminal columns: an East Asian wide character takes two,
	# a combining mark and an ANSI color code none.
	ansi = re.compile(r"(\x1b\[[0-?]*[ -/]*[@-~])")
	def char_width(ch):
		if ch < "\u0300":  # ASCII and Latin-1
			return 1
		if unicodedata.combining(ch) or unicodedata.category(ch) in ("Mn", "Me", "Cf") or "\u1160" <= ch <= "\u11ff":
			return 0
		return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
	def visible_len(s):
		if "\x1b" in s:
			s = ansi.sub("", s)
		if not s or max(s) < "\u0300":
			return len(s)
		return sum(char_width(ch) for ch in s)
	def pad(s, width):
		return s + " " * (width - visible_len(s))
	def cut(s, width):
		# The characters that fit in ``width`` columns, and every color code.
		used, out = 0, []
		for i, piece in enumerate(ansi.split(s)):
			if i % 2:
				out.append(piece)
				continue
			for ch in piece:
				w = char_width(ch)
				if used + w > width:
					width = -1  # full: no further characters
					break
				out.append(ch)
				used += w
		return "".join(out)
	col_widths = [0] * num_cols
	for row in data:
		for cwi, item in enumerate(row[:num_cols]):
			vl = visible_len(item)
			if vl > col_widths[cwi]:
				col_widths[cwi] = vl
	cols_to_remove = []
	if remove_empty_columns and not full:
		cols_to_remove = [i for i, col_width in enumerate(col_widths) if col_width == 0]
		header = [item for i, item in enumerate(header) if i not in cols_to_remove]
		col_widths = [w for i, w in enumerate(col_widths) if i not in cols_to_remove]
		num_cols = len(header)
	header_widths = [visible_len(h) for h in header]
	col_widths = [max(w, hw) for w, hw in zip(col_widths, header_widths)]
	header = [pad(item, col_width) for item, col_width in zip(header, col_widths)]
	normalized_padded_data = []
	for row in data:
		if cols_to_remove:
			row = [item for i, item in enumerate(row) if i not in cols_to_remove]
		if not any(row):
			row = ['-' * col_width for col_width in col_widths]
		else:
			row = [pad(row[i] if i < len(row) else "", col_widths[i]) for i in range(num_cols)]
		normalized_padded_data.append(row)
	data = normalized_padded_data
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
	def truncate_to_width(string, width):
		if visible_len(string) <= width:
			return string
		stripped = string.rstrip()
		if visible_len(stripped) <= width:
			return pad(stripped, width)
		if width < 2:
			return "."[:width]
		return pad(cut(string, width - 2) + "..", width)
	data = [[truncate_to_width(item, col_width) for item, col_width in zip(row, col_widths)] for row in data]
	header = [truncate_to_width(item, col_width) for item, col_width in zip(header, col_widths)]
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
			rows, columns = struct.unpack('HHHH', packed)[:2]
			_tsize = (columns, rows)
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

if __name__ == '__main__':
	sys.exit(main())
