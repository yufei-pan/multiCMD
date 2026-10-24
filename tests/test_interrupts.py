'''
A Ctrl-C ( or what a program's signal handler raises ) while multiCMD starts, runs and stops commands: it is raised,
and the commands are stopped, or left registered for kill_all() and the exit hook
'''
import os
import signal
import subprocess
import sys
import threading
import time
import _thread

import pytest

import multiCMD
from conftest import pid_gone, sigint_main_after
from multiCMD import AsyncExecutor, run_commands


def _ctrl_c():
	'''A real Ctrl-C to the whole process ( any thread not blocking SIGINT may take it ), and time for it to land'''
	os.kill(os.getpid(), signal.SIGINT)
	deadline = time.monotonic() + 0.2
	while time.monotonic() < deadline:
		pass


@pytest.fixture
def another_thread():
	'''A thread alive throughout: a SIGINT the main thread blocked would be taken by it'''
	release = threading.Event()
	thread = threading.Thread(target=release.wait, daemon=True)
	thread.start()
	yield thread
	release.set()
	thread.join(5)


# ---- a command interrupted as it starts is registered while it is killed

def test_a_ctrl_c_before_the_registration_still_registers_the_command_while_it_is_killed(monkeypatch):
	real_register = multiCMD._register
	armed = [True]
	def register(proc, pgid):
		if armed[0]:
			armed[0] = False
			raise KeyboardInterrupt
		real_register(proc, pgid)
	monkeypatch.setattr(multiCMD, '_register', register)
	registered = []
	real_kill = multiCMD._kill_commands
	def kill(commands, first_signal, *args):
		commands = list(commands)
		registered.extend(proc in multiCMD._live_procs for proc, _ in commands)
		real_kill(commands, first_signal, *args)
	monkeypatch.setattr(multiCMD, '_kill_commands', kill)
	with pytest.raises(KeyboardInterrupt):
		multiCMD.run_command(['sleep', '30'], quiet=True)
	# before: a second Ctrl-C during the kill left a command neither dead nor known to kill_all()
	assert registered == [True]
	assert not multiCMD._live_procs


@pytest.mark.skipif(not sys.platform.startswith('linux'), reason='checks processes through /proc')
def test_a_ctrl_c_while_the_command_starts_still_stops_it(monkeypatch, tmp_path):
	pidfile = tmp_path / 'pid'
	real_spawn = multiCMD._spawn
	def spawn(*args, **kwargs):
		result = real_spawn(*args, **kwargs)
		deadline = time.monotonic() + 5
		while not pidfile.exists() and time.monotonic() < deadline:
			time.sleep(0.01)
		# lands once the command runs, before the spawn has even returned
		_ctrl_c()
		return result
	monkeypatch.setattr(multiCMD, '_spawn', spawn)
	with pytest.raises(KeyboardInterrupt):
		multiCMD.run_command(['sh', '-c', f'echo $$ > {pidfile}.tmp; mv {pidfile}.tmp {pidfile}; exec sleep 30'], quiet=True)
	# before ( 5f7468d ): the Ctrl-C lost the command, which ran on in its own process group, unknown to kill_all()
	assert pid_gone(int(pidfile.read_text()))
	assert not multiCMD._live_procs


def _children():
	'''The pids of this process's children, whichever of its threads started them'''
	pids = set()
	for tid in os.listdir('/proc/self/task'):
		try:
			with open(f'/proc/self/task/{tid}/children') as f:
				pids.update(int(pid) for pid in f.read().split())
		except OSError:
			pass
	return pids


def _zombie(pid):
	try:
		with open(f'/proc/{pid}/stat') as f:
			return f.read().rsplit(')', 1)[1].split()[0] == 'Z'
	except OSError:
		return True


def _describe(pid):
	'''A child left behind, for the failure message: its state, process group, command line, and whether it is registered'''
	try:
		with open(f'/proc/{pid}/stat') as f:
			fields = f.read().rsplit(')', 1)[1].split()
		with open(f'/proc/{pid}/cmdline', 'rb') as f:
			cmdline = f.read().replace(b'\0', b' ')[:80]
	except OSError as e:
		return (pid, repr(e))
	registered = [(proc.returncode, pgid) for proc, pgid in multiCMD._live_procs.copy().items() if proc.pid == pid]
	return (pid, 'state', fields[0], 'pgrp', fields[2], cmdline, 'registered', registered)


def _left_running(before, wait=6.0):
	'''
	The children started since before that still run after up to wait seconds, or that are dead but neither
	reaped nor registered ( kill_all() and the exit hook reap what is registered ). A command cut short as it starts
	is stopped within about 2 s ( the kill ); wait leaves room for load.
	'''
	deadline = time.monotonic() + wait
	while True:
		registered = {proc.pid for proc in multiCMD._live_procs.copy()}
		left = {pid for pid in _children() - before if not _zombie(pid) or pid not in registered}
		if not left or time.monotonic() > deadline:
			return left
		time.sleep(0.02)


# on Python 3.6 - 3.9 and 3.14 a Ctrl-C right after a C lock's acquire in a `with lock:` leaves the lock held, and
# Thread.start() takes threading's own _active_limbo_lock so: every thread then hangs as it ends ( CPython's own, see
# the README ). The stress test below keeps its Ctrl-Cs out of the output readers' Thread.start() there: one that would
# land inside it is taken right after it
_WITH_MAY_JAM = sys.version_info < (3, 10) or sys.version_info >= (3, 14)


@pytest.mark.skipif(not sys.platform.startswith('linux'), reason='checks processes through /proc')
def test_a_ctrl_c_while_the_command_starts_loses_it_only_inside_popen(launcher, monkeypatch, request):
	# a real SIGINT from another thread lands anywhere in the start ( 0 - 0.5 ms after the spawn begins ). A command is
	# left running, unknown to kill_all(), only when it landed inside _popen ( Popen, once it has forked ); anywhere
	# else it is registered and stopped
	import random
	main = threading.main_thread().ident
	# per SIGINT: whether it landed inside _popen
	landed = []
	armed = [False]
	def handler(signum, frame):
		inside = False
		while frame is not None:
			inside = inside or frame.f_code is multiCMD._popen.__code__
			frame = frame.f_back
		landed.append(inside)
		if armed[0]:
			raise KeyboardInterrupt
	previous = signal.signal(signal.SIGINT, handler)
	request.addfinalizer(lambda: signal.signal(signal.SIGINT, previous))
	if hasattr(sys, 'unraisablehook'):
		# ( CPython drops one raised in a finalizer: not pytest's to report )
		monkeypatch.setattr(sys, 'unraisablehook', lambda dropped: None)
	if _WITH_MAY_JAM:
		real_start = threading.Thread.start
		def start(self):
			if getattr(self, '_target', None) is multiCMD._read_stream:
				# ( unblocked whatever happens: a Ctrl-C that landed before the block is raised right after it )
				try:
					signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT})
					real_start(self)
				finally:
					signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGINT})
			else:
				real_start(self)
		monkeypatch.setattr(threading.Thread, 'start', start)
	spawning = threading.Event()
	real_spawn = multiCMD._spawn
	def spawn(*args, **kwargs):
		spawning.set()
		return real_spawn(*args, **kwargs)
	monkeypatch.setattr(multiCMD, '_spawn', spawn)
	before = _children()
	lost, inside_popen = [], []
	request.addfinalizer(lambda: (multiCMD.kill_all(), multiCMD._live_procs.clear()))
	for _ in range(100):
		# ( a spawn takes some 50 - 150 us: the timer's own wake-up takes about as long )
		delay = random.choice([0, random.uniform(0, 0.0005)])
		spawning.clear()
		sent = []
		def send():
			try:
				spawning.wait(1)
				time.sleep(delay)
				signal.pthread_kill(main, signal.SIGINT)
			finally:
				sent.append(1)
		del landed[:]
		try:
			try:
				armed[0] = True
				# ( a bare thread: Thread.start() and join() take threading's own locks with `with`, see _WITH_MAY_JAM )
				_thread.start_new_thread(send, ())
				multiCMD.run_command(['sleep', '3'], quiet=True)
			finally:
				# a Ctrl-C from here on is only recorded; wait until it is sent and its handler has run
				armed[0] = False
				waited = time.monotonic() + 1
				while not sent or (not landed and time.monotonic() < waited):
					time.sleep(0.001)
		except KeyboardInterrupt:
			pass
		inside = any(landed)
		left = _left_running(before, wait=0 if inside else 6.0)
		if inside and len(left) <= 1:
			# the one window that loses a command: kill it, and reap it ( nothing else holds it )
			inside_popen.extend(left)
			for pid in left:
				try:
					os.killpg(pid, signal.SIGKILL)
				except OSError:
					os.kill(pid, signal.SIGKILL)
				os.waitpid(pid, 0)
		else:
			lost.extend(left)
		if lost:
			break
	described = [_describe(pid) for pid in lost]
	for pid in lost:
		for kill in (os.killpg, os.kill):
			try:
				kill(pid, signal.SIGKILL)
			except OSError:
				pass
	assert described == [], len(inside_popen)


# ---- a thread that cannot start: the pool tries the next worker once, and a Ctrl-C start() turns into RuntimeError is raised

def _failing_worker_starts(monkeypatch, failing):
	'''Make the pool's worker starts whose 1-based number is in failing raise; returns the list of starts tried'''
	calls = []
	real_start = multiCMD._Thread.start
	def start(self):
		if getattr(self, '_target', None) is not None and self._target.__name__ == 'worker':
			calls.append(self)
			if len(calls) in failing:
				raise RuntimeError("can't start new thread")
		real_start(self)
	monkeypatch.setattr(multiCMD._Thread, 'start', start)
	return calls


def test_the_pool_runs_the_tasks_on_the_workers_that_start_when_the_first_cannot(monkeypatch, capfd):
	# ( review round 8, finding 2 ) a passing failure ( EAGAIN ) of the very first start
	calls = _failing_worker_starts(monkeypatch, {1})
	tasks = run_commands([['true']] * 4, max_threads=4, quiet=False, return_object=True)
	# before ( a96b922 ): no other worker was tried, and every task failed with -1
	assert len(calls) == 4
	assert [task.returncode for task in tasks] == [0] * 4
	assert "multiCMD: started 3 of 4 threads: can't start new thread" in capfd.readouterr().err


def test_the_pool_stops_starting_workers_after_two_in_a_row_cannot_start(monkeypatch, capfd):
	calls = _failing_worker_starts(monkeypatch, {2, 3, 4, 5})
	tasks = run_commands([['true']] * 6, max_threads=5, quiet=False, return_object=True)
	# before ( 5f7468d ): every one of the 5 workers was tried
	assert len(calls) == 3
	assert [task.returncode for task in tasks] == [0] * 6
	assert "multiCMD: started 1 of 5 threads: can't start new thread" in capfd.readouterr().err


def test_a_pool_that_cannot_start_two_workers_in_a_row_fails_every_task(monkeypatch):
	calls = _failing_worker_starts(monkeypatch, set(range(1, 10)))
	tasks = run_commands([['true']] * 4, max_threads=4, quiet=True, return_object=True)
	assert len(calls) == 2
	assert [task.returncode for task in tasks] == [-1] * 4 and all(task._error for task in tasks)


def test_a_ctrl_c_that_thread_start_turns_into_another_error_is_still_raised(monkeypatch):
	# ( seen on 3.12 and free-threaded 3.13t ) a Ctrl-C inside Condition.wait()'s _acquire_restore(), as Thread.start()
	# waits for an output reader: the Condition's release then raises RuntimeError, and the reader runs all the same.
	# Taken for a reader that cannot start, the Ctrl-C was lost
	real_start = threading.Thread.start
	armed = [True]
	def start(self):
		real_start(self)
		if armed[0] and getattr(self, '_target', None) is multiCMD._read_stream:
			armed[0] = False
			try:
				raise KeyboardInterrupt
			except KeyboardInterrupt:
				raise RuntimeError('release unlocked lock')
	monkeypatch.setattr(threading.Thread, 'start', start)
	started = time.monotonic()
	with pytest.raises(KeyboardInterrupt):
		multiCMD.run_command(['sleep', '2'], quiet=True)
	# before: the command ran to its end and the call returned
	assert time.monotonic() - started < 1.5 and not multiCMD._live_procs


@pytest.mark.skipif(not hasattr(signal, 'pthread_sigmask'), reason='POSIX signals')
def test_a_thread_start_error_while_the_caller_handles_a_ctrl_c_is_not_that_ctrl_c(monkeypatch, another_thread):
	# ( finding 3; probe r10probes/stale_ctx.py ) a program cleans up after a real Ctrl-C it caught, running commands
	# from its except block, and one pool worker cannot start ( EAGAIN ): that RuntimeError has the Ctrl-C being
	# handled as its context, and was taken for a Ctrl-C that landed during the start
	real_start = threading.Thread.start
	calls = []
	def start(self):
		if getattr(self, '_target', None) is not None and getattr(self._target, '__name__', '') == 'worker':
			calls.append(self)
			if len(calls) == 1:
				raise RuntimeError("can't start new thread")
		real_start(self)
	monkeypatch.setattr(threading.Thread, 'start', start)
	try:
		_ctrl_c()
	except KeyboardInterrupt:
		try:
			tasks = run_commands([['true']] * 4, max_threads=4, quiet=True, return_object=True)
		except KeyboardInterrupt:
			# before ( fdc45b9 ): the Ctrl-C being handled was raised again, and the tasks never ran
			pytest.fail('the Ctrl-C the caller handles was raised again')
	assert len(calls) == 4 and [task.returncode for task in tasks] == [0] * 4


# ---- a normal run touches no signal state

def _normal_runs():
	'''A successful run through every API, the failure-free paths only'''
	assert multiCMD.run_command(['echo', 'a'], quiet=True) == ['a']
	assert multiCMD.run_command(['sh', '-c', 'echo $MCMD_X'], quiet=True, extra_env={'MCMD_X': 'x'}) == ['x']
	assert multiCMD.run_command('echo b | cat', quiet=True, shell=True) == ['b']
	if os.name != 'nt':
		assert multiCMD.run_command(['echo', 'c'], quiet=True, pty=True) == ['c']
	assert run_commands([['true']] * 4, max_threads=2, quiet=True, return_code_only=True) == [0] * 4
	assert run_commands([['true']] * 3, max_threads=0, quiet=True, return_code_only=True) == [0] * 3
	tasks = run_commands([['true']] * 3, quiet=True, wait_for_return=False, return_object=True)
	multiCMD.join_threads()
	assert [task.returncode for task in tasks] == [0] * 3
	ex = AsyncExecutor(max_threads=2, quiet=True)
	ex.run_commands([['true']] * 3)
	ex.wait()
	assert [task.returncode for task in ex.tasks] == [0] * 3
	ex.cleanup()
	# a timeout kills the command: still no signal handler set
	assert multiCMD.run_command(['sleep', '5'], quiet=True, timeout=0.3, return_code_only=True) == 124
	# ( review of round 9, finding 4 ) nothing to run: statbtrfs.execute_commands([]) and hpcp do this
	assert run_commands([], max_threads=4, quiet=True) == [] and run_commands([], quiet=True) == []
	# the executor stopped, and cleaned up, while a command runs and others are queued
	for finish in ('stop', 'cleanup'):
		ex = AsyncExecutor(max_threads=1, quiet=True)
		ex.run_commands([['sleep', '5']] * 3)
		deadline = time.monotonic() + 5
		while not multiCMD._live_procs and time.monotonic() < deadline:
			time.sleep(0.01)
		getattr(ex, finish)()
		assert not multiCMD._live_procs


def test_a_normal_run_never_sets_a_signal_handler(monkeypatch):
	# ( review round 8, finding 1 ) round 8 held a Ctrl-C around every spawn, swapping SIGINT's handler each time
	calls = []
	real = signal.signal
	def record(signum, handler):
		calls.append((signum, handler))
		return real(signum, handler)
	monkeypatch.setattr(signal, 'signal', record)
	before = signal.getsignal(signal.SIGINT)
	_normal_runs()
	assert calls == []
	assert signal.getsignal(signal.SIGINT) is before


# ---- what a program's own signal handler raises while a serial run waits stops the run

class _Cancelled(Exception):
	'''What a program's own SIGINT handler raises instead of KeyboardInterrupt'''


@pytest.fixture
def cancelling_handler(request):
	'''A program's SIGINT handler that raises _Cancelled, an Exception; it counts the Ctrl-Cs it gets'''
	calls = []
	def handler(signum, frame):
		calls.append(signum)
		raise _Cancelled(len(calls))
	previous = signal.signal(signal.SIGINT, handler)
	request.addfinalizer(lambda: signal.signal(signal.SIGINT, previous))
	return calls


def _builds(monkeypatch):
	'''The tasks every run_commands() call builds, in a list'''
	tasks = []
	real_build = multiCMD._build_tasks
	monkeypatch.setattr(multiCMD, '_build_tasks', lambda *a, **k: (lambda built: (tasks.extend(built[0]), built)[1])(real_build(*a, **k)))
	return tasks


@pytest.mark.skipif(not sys.platform.startswith('linux'), reason='checks processes through /proc')
@pytest.mark.parametrize('where', ['waiting', 'draining'])
def test_what_a_programs_handler_raises_into_a_serial_run_stops_the_run(monkeypatch, tmp_path, cancelling_handler, where):
	# ( review of round 11, finding 6 ) a program's SIGINT handler that raises an Exception of its own while a serial run
	# waits for its command: _execute stopped the command, and __run_command's `except Exception` recorded the
	# handler's exception as multiCMD's own failure ( -1 ) and swallowed it, so the run went on with the next command
	tasks = _builds(monkeypatch)
	pidfile = tmp_path / 'pid'
	first = ['sleep', '30'] if where == 'waiting' else ['sh', '-c', f'sleep 30 & echo $! > {pidfile}; echo hi']
	main = threading.main_thread().ident
	timer = threading.Timer(0.5, signal.pthread_kill, (main, signal.SIGINT))
	timer.start()
	try:
		# before ( fc0c782 ): nothing raised, [-1, 0]
		with pytest.raises(_Cancelled):
			run_commands([first, ['true']], quiet=True)
	finally:
		timer.cancel()
	assert cancelling_handler == [signal.SIGINT]
	if where == 'waiting':
		# recorded as stopped ( killed by the kill sequence's SIGINT ), not as a failure of multiCMD's
		assert tasks[0].returncode == -signal.SIGINT and not tasks[0]._error
	else:
		# the command itself had exited; what held its output is stopped
		assert tasks[0].returncode == 0 and tasks[0].stdout == ['hi']
		pid = int(pidfile.read_text())
		gone = pid_gone(pid, wait=2)
		if not gone:
			os.kill(pid, signal.SIGKILL)
		assert gone
	assert tasks[0]._finalized and tasks[1].returncode is None and tasks[1].stop
	assert not multiCMD._live_procs and not multiCMD._draining


# ---- a Ctrl-C as _execute stops a command: it is sent SIGTERM and stays tracked

@pytest.mark.skipif(not sys.platform.startswith('linux'), reason='checks processes through /proc')
@pytest.mark.parametrize('how', ['timed out', 'drained'])
def test_a_ctrl_c_as_the_command_is_stopped_has_it_sent_sigterm_and_still_registered(monkeypatch, tmp_path, how):
	# ( review of rounds 14 and 15 ) the only Ctrl-C of the run lands as _execute begins the stop of a timed-out command,
	# or of the group of one that exited while something it started held its output: before, the group was forgotten,
	# or the command left unsignalled
	spawned = []
	real_spawn = multiCMD._spawn
	monkeypatch.setattr(multiCMD, '_spawn', lambda *a, **k: (lambda result: (spawned.append(result), result)[1])(real_spawn(*a, **k)))
	def stop(*args, **kwargs):
		raise KeyboardInterrupt
	monkeypatch.setattr(multiCMD, '_stop_command', stop)
	pidfile = tmp_path / 'pid'
	command = ['sh', '-c', f'echo $$ > {pidfile}; exec sleep 30'] if how == 'timed out' else ['sh', '-c', f'sleep 30 & echo $! > {pidfile}; echo hi']
	try:
		with pytest.raises(KeyboardInterrupt):
			multiCMD.run_command(command, timeout=0.3, quiet=True)
		(proc, pgid), = spawned
		assert proc in multiCMD._live_procs and proc not in multiCMD._draining
		assert pid_gone(int(pidfile.read_text()))
	finally:
		monkeypatch.undo()
		multiCMD.kill_all(signal.SIGKILL)
	assert not multiCMD._live_procs


# ---- an interrupt at each point where Python raises one, in turn ( see _interrupt_at )

def _interrupt_at(state, counted):
	'''
	A profile function ( for _hook ) that raises KeyboardInterrupt at the state['k']-th point counted(frame, event)
	accepts, counting from 0: a Python function's 'call' and 'return', and a C function's 'c_return'. From Python 3.11
	that is where Python raises a pending Ctrl-C ( and on a loop's way back ), so k from 0 up to the last point puts one
	at each in turn. A hook that raises is unset by Python: one per run, and it raises nothing more once it has. It sets
	state['n'] ( points counted so far ), state['where'] ( the one it raised at ) and state['injected']. Nothing is
	counted inside a finalizer or weakref callback ( a __del__ or a _remove further up the stack ): CPython drops what is
	raised there.
	'''
	state.update(n=0, where=None, injected=False)
	def excused(frame):
		if frame.f_code is _hook.__code__ or frame.f_code is _unhook.__code__:
			# ( the test's own: setting the hook, and unsetting it )
			return True
		while frame is not None:
			if frame.f_code.co_name in ('__del__', '_remove'):
				return True
			frame = frame.f_back
		return False
	def profile(frame, event, arg):
		if state['injected'] or event not in ('call', 'return', 'c_return') or not counted(frame, event) or excused(frame):
			return
		if state['n'] == state['k']:
			state['injected'] = True
			state['where'] = (event, frame.f_code.co_name, frame.f_lineno, getattr(arg, '__name__', None) if event == 'c_return' else None)
			raise KeyboardInterrupt('injected')
		state['n'] += 1
	return profile


# the profile function set before _hook set its own ( a coverage tool's ), which _unhook sets back
_BEFORE_HOOK = []


def _hook(hook):
	'''Set a profile function from _interrupt_at on this thread'''
	if not _BEFORE_HOOK:
		_BEFORE_HOOK.append(sys.getprofile())
	sys.setprofile(hook)


def _unhook():
	'''Unset _hook's profile function, setting back what was set before it; once it is unset, again does nothing'''
	if _BEFORE_HOOK:
		sys.setprofile(_BEFORE_HOOK.pop())


@pytest.mark.skipif(not hasattr(signal, 'pthread_sigmask'), reason='POSIX signals')
def test_a_ctrl_c_anywhere_in_a_start_leaves_the_signal_mask_as_it_was(launcher):
	# ( matrix of round 12, finding 2 ) _spawn_process blocked SIGTTIN / SIGTTOU with signal.pthread_sigmask(), whose
	# Python wrapper turns the C call's result into a set in a generator expression once the mask is set: a Ctrl-C
	# landing there left them blocked on the main thread for good ( old_mask never assigned, the finally never entered:
	# 45 % of the Ctrl-Cs raised in that wrapper, by a probe ), and one landing as the restoring call began left them
	# blocked too. A Ctrl-C at each point of a start, in turn: in _spawn_process, and as what it calls begins and
	# returns ( it calls _signal.pthread_sigmask(), the C function, and no code of the signal module's )
	code = multiCMD._spawn_process.__code__
	def counted(frame, event):
		return frame.f_code is code or frame.f_back is not None and frame.f_back.f_code is code
	before = signal.pthread_sigmask(signal.SIG_BLOCK, [])
	state = {'k': 0}
	while True:
		started = []
		_hook(_interrupt_at(state, counted))
		try:
			multiCMD._spawn_process(['true'], subprocess.DEVNULL, subprocess.DEVNULL, started=started)
		except KeyboardInterrupt:
			pass
		finally:
			_unhook()
		mask = signal.pthread_sigmask(signal.SIG_BLOCK, [])
		signal.pthread_sigmask(signal.SIG_SETMASK, before)
		for proc, _ in started:
			if getattr(proc, 'pid', None) is not None and proc.returncode is None:
				proc.wait(10)
		# before ( f891332 ): SIGTTIN and SIGTTOU left blocked by a Ctrl-C in the wrapper's generator expression, or as
		# the restoring call began
		assert mask == before, state['where']
		if not state['injected']:
			break
		state['k'] += 1
	assert state['k'] > 5


# ( a Ctrl-C inside Thread.start()'s wait can leave that thread's Event inconsistent, CPython's own: the thread fails
# as it comes up, and CPython reports that as unraisable )
@pytest.mark.filterwarnings('ignore::pytest.PytestUnraisableExceptionWarning')
@pytest.mark.skipif(not sys.platform.startswith('linux'), reason='POSIX commands')
@pytest.mark.parametrize('command', [['true'], ['mcmd-no-such-command-xyz']], ids=['true', 'missing'])
def test_a_ctrl_c_at_any_point_of_a_serial_run_is_raised(command):
	# ( matrix of round 12, finding 4 ) one KeyboardInterrupt at each call, return and C-call return of a serial run in
	# turn, in every frame of the main thread ( subprocess's, os's ... ): run_command() raises it every time, for a
	# command that runs and for one that is not found. It found a Ctrl-C that Thread.start() turns into RuntimeError on
	# 3.12 ( see _Thread.start ). threading's own frames are left out where a Ctrl-C right after a `with lock:` acquire
	# leaves the lock held ( 3.6 - 3.9, 3.14: CPython's own, see the README )
	skipped = {threading.__file__} if _WITH_MAY_JAM else set()
	def counted(frame, event):
		return frame.f_code.co_filename not in skipped
	state = {'k': 0}
	returned = []
	while True:
		_hook(_interrupt_at(state, counted))
		try:
			multiCMD.run_command(command, quiet=True)
			if state['injected']:
				returned.append(state['where'])
		except KeyboardInterrupt:
			pass
		finally:
			_unhook()
		multiCMD.kill_all(signal.SIGKILL)
		if not state['injected']:
			break
		state['k'] += 1
	assert returned == []
	assert state['k'] > 100 and not multiCMD._live_procs


@pytest.mark.skipif(not sys.platform.startswith('linux'), reason='POSIX commands')
@pytest.mark.parametrize('batch', [False, True], ids=['run_command', 'serial batch'])
def test_the_only_ctrl_c_anywhere_while_a_missing_command_is_dealt_with_is_raised(monkeypatch, batch):
	# ( review of round 13, B1 ) as the run deals with a failure, that is its only Ctrl-C: before, run_command() could
	# return 127 and a serial batch run its next command. The only Ctrl-C of the run at each point of multiCMD's code in
	# turn, from the moment the command is found missing until the next one would start: it is raised, and the next
	# command never starts
	mine = multiCMD.__file__
	def counted(frame, event):
		return frame.f_code.co_filename == mine or frame.f_back is not None and frame.f_back.f_code.co_filename == mine
	state = {'k': 0}
	spawns = []
	real_spawn = multiCMD._spawn
	def spawn(command, *args, **kwargs):
		# ( no Ctrl-C from the next command's start on )
		_unhook()
		spawns.append(command)
		try:
			return real_spawn(command, *args, **kwargs)
		except FileNotFoundError:
			_hook(_interrupt_at(state, counted))
			raise
	monkeypatch.setattr(multiCMD, '_spawn', spawn)
	lost = []
	while True:
		del spawns[:]
		try:
			if batch:
				outcome = run_commands([['mcmd-no-such-command-xyz'], ['true']], max_threads=1, quiet=True, return_code_only=True)
			else:
				outcome = multiCMD.run_command(['mcmd-no-such-command-xyz'], quiet=True, return_code_only=True)
		except KeyboardInterrupt:
			outcome = 'raised'
		finally:
			_unhook()
		if state['injected'] and (outcome != 'raised' or len(spawns) > 1):
			# before ( cf4a7c1 ): returned 127 ( [127, 0] ) after one on the way back of _execute's inner loop
			lost.append((state['where'], outcome, spawns[1:]))
		if not state['injected']:
			break
		state['k'] += 1
	assert lost == []
	assert state['k'] > 5 and not multiCMD._live_procs
