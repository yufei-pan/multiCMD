'''
A Ctrl-C ( or what a program's signal handler raises ) while multiCMD starts, runs and stops commands: it is raised,
and the commands are stopped, or left registered for kill_all() and the exit hook
'''
import os
import re
import signal
import subprocess
import sys
import threading
import time
import _thread

import pytest

import multiCMD
from conftest import PACKAGE_ROOT, pid_gone, sigint_main_after
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
				# ( a bare thread: Thread.start() and join() on the main thread take threading's own locks, which a Ctrl-C
				# can leave held, see docs/limitations.md )
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


# ---- a thread that cannot start: the pool tries the next worker once

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


@pytest.mark.skipif(not sys.platform.startswith('linux'), reason='POSIX commands')
@pytest.mark.parametrize('command', [['true'], ['mcmd-no-such-command-xyz']], ids=['true', 'missing'])
def test_a_ctrl_c_at_any_point_of_a_serial_run_is_raised(command):
	# ( matrix of round 12, finding 4 ) one KeyboardInterrupt at each call, return and C-call return of a serial run in
	# turn, in every frame of the main thread ( subprocess's, os's, threading's ... ): run_command() raises it every time,
	# for a command that runs and for one that is not found. Up to 1.59 threading's own frames were left out on 3.6 - 3.9
	# and 3.14: a Ctrl-C at one instruction of the output readers' Thread.start() could leave _active_limbo_lock held, and
	# every later thread start hung. From 1.60 the readers start by one C call ( see _Reader )
	def counted(frame, event):
		return True
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
	# ( the matrix went through the run: 'missing' on 3.6 has the fewest points, some 70 and 10 per PATH entry looked in )
	assert state['k'] > 50 and not multiCMD._live_procs


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


# ---- a Ctrl-C as a thread starts never leaves threading's own locks held ( 1.60 )

# a serial run_command(['true']) stopped at a random moment by a SIGALRM handler that raises KeyboardInterrupt, many times
# in one process; after each stop threading._active_limbo_lock must be free, and the next call must finish
STOP_PROBE = r'''
import _thread, os, random, signal, sys, threading, time, traceback
sys.path.insert(0, sys.argv[1])
import multiCMD
n = int(sys.argv[2])
done = [0]
def watchdog():
	# no call finished for 20 s is a hang: a call takes 0.2 - 5 ms, a stop at most about 3 s ( the jammed Popen lock in
	# docs/limitations.md ). A bare thread, as it starts while nothing is stopped yet
	last, since = -1, time.monotonic()
	while True:
		time.sleep(1)
		if done[0] != last:
			last, since = done[0], time.monotonic()
		elif time.monotonic() - since > 20:
			stack = sys._current_frames().get(threading.main_thread().ident)
			print('HANG after %d calls; the main thread:\n%s' % (done[0], ''.join(traceback.format_stack(stack))), flush=True)
			os._exit(3)
_thread.start_new_thread(watchdog, ())
armed = [False]
def handler(signum, frame):
	# ( one that the kernel hands to another thread can run on the main thread after the call: only while armed )
	if armed[0]:
		armed[0] = False
		raise KeyboardInterrupt
signal.signal(signal.SIGALRM, handler)
times = []
for _ in range(50):
	start = time.perf_counter()
	multiCMD.run_command(['true'], quiet=True)
	times.append(time.perf_counter() - start)
window = sorted(times)[len(times) // 2] * 1.5
stops = others = 0
for i in range(n):
	try:
		try:
			armed[0] = True
			signal.setitimer(signal.ITIMER_REAL, random.uniform(1e-6, window))
			multiCMD.run_command(['true'], quiet=True)
			# the alarm lands here if it did not land in the call
			time.sleep(window)
		finally:
			armed[0] = False
			signal.setitimer(signal.ITIMER_REAL, 0)
	except KeyboardInterrupt:
		stops += 1
	except Exception as e:
		others += 1
		print('not a KeyboardInterrupt: %r' % (e,), flush=True)
	if not threading._active_limbo_lock.acquire(True, 2):
		print('LEAK: threading._active_limbo_lock held after %d calls' % (i + 1), flush=True)
		os._exit(2)
	threading._active_limbo_lock.release()
	done[0] = i + 1
assert multiCMD.run_command(['true'], quiet=True, return_code_only=True) == 0
print('%d calls, %d stopped, %d other errors' % (n, stops, others), flush=True)
'''


@pytest.mark.slow
@pytest.mark.skipif(not hasattr(signal, 'setitimer'), reason='POSIX timers')
def test_stops_in_serial_commands_never_jam_thread_starts(tmp_path):
	# ( the 1.60 plan, K; seen in hpcp 9.87's re-review ) a stop inside an output reader's Thread.start() on the main thread
	# left threading's _active_limbo_lock held, and every later thread start hung: up to 1.59 on 3.14 this probe hung in 6 of
	# 6 runs of 20,000 calls, after 30 to 9,000 calls ( three runs by the plan, three by the build ). 3.6 never hung so in
	# 60,000 calls. 5,000 calls take about 75 s on 3.6 ( a call and the sleep after it ): 600 s leaves eight times that
	# under -n auto
	probe = tmp_path / 'probe.py'
	probe.write_text(STOP_PROBE)
	run = subprocess.run([sys.executable, '-I', str(probe), str(PACKAGE_ROOT), '5000'], stdout=subprocess.PIPE,
		stderr=subprocess.STDOUT, universal_newlines=True, timeout=600)
	# ( Python 3.6 - 3.8 buffer stderr, where the stops that landed in a finalizer are told, until the exit )
	assert run.returncode == 0 and re.search(r'^5000 calls, \d+ stopped, 0 other errors$', run.stdout, re.M), run.stdout[-4000:]


def _dummy_threads():
	return {thread for thread in threading.enumerate() if isinstance(thread, threading._DummyThread)}


def test_a_main_thread_start_runs_in_a_helper(monkeypatch):
	# ( 1.60 ) from the main thread, where Python runs signal handlers, threading's own start runs on a helper thread; from
	# another thread it runs right there; and what it raises in the helper reaches the caller
	real_start = threading.Thread.start
	ran_on, fail = [], [None]
	def spy(self):
		ran_on.append(_thread.get_ident())
		if fail[0] is not None:
			raise fail[0]
		real_start(self)
	monkeypatch.setattr(threading.Thread, 'start', spy)
	dummies = _dummy_threads()
	main = threading.main_thread().ident
	thread = multiCMD._Thread(time.sleep, (0,))
	thread.start()
	assert multiCMD._join(thread, 5)
	assert ran_on[-1] != main and thread.done.flag
	# from another thread ( a bare one, whose own start is not what is checked; it waits on done, as Thread.join() would
	# make threading register it )
	started_by, finished = [], multiCMD._Done()
	def start_one():
		try:
			started_by.append(_thread.get_ident())
			inner = multiCMD._Thread(time.sleep, (0,))
			inner.start()
			inner.done.wait(5)
		finally:
			finished.set()
	_thread.start_new_thread(start_one, ())
	assert finished.wait(5)
	assert ran_on[-1] == started_by[0] != main
	# a thread that cannot start, from the main thread: the helper's error is raised in the caller
	fail[0] = RuntimeError("can't start new thread")
	thread = multiCMD._Thread(time.sleep, (0,))
	with pytest.raises(RuntimeError, match="can't start new thread"):
		thread.start()
	assert not thread._started.is_set() and not thread.is_alive()
	# the helper never makes threading register a thread of its own ( a _DummyThread )
	assert _dummy_threads() <= dummies


@pytest.mark.skipif(not hasattr(signal, 'pthread_kill'), reason='POSIX signals')
@pytest.mark.parametrize('what', ['a thread', 'the dispatcher'])
def test_an_interrupted_start_waits_for_its_helper(monkeypatch, request, what):
	# ( the 1.60 plan, G3 ) a Ctrl-C while the main thread waits for its helper to start a thread is raised once the start
	# is done ( _START_WAIT at most ), so _started says whether the thread runs. Without that wait it was still unset, and
	# an AsyncExecutor took a dispatcher that came up a moment later for one that never would: the next run_commands()
	# started a second one, which wait() then did not cover
	previous = signal.signal(signal.SIGINT, signal.default_int_handler)
	request.addfinalizer(lambda: signal.signal(signal.SIGINT, previous))
	main = threading.main_thread().ident
	real_helper = multiCMD._start_in_helper
	armed = [True]
	def slow_helper(*args):
		# the Ctrl-C lands once the helper exists, while the main thread waits for it
		if armed[0]:
			armed[0] = False
			signal.pthread_kill(main, signal.SIGINT)
		time.sleep(0.05)
		real_helper(*args)
	monkeypatch.setattr(multiCMD, '_start_in_helper', slow_helper)
	if what == 'a thread':
		release = threading.Event()
		thread = multiCMD._Thread(release.wait, (10,))
		with pytest.raises(KeyboardInterrupt):
			thread.start()
		# before: unset, though the thread came up a moment later
		assert thread._started.is_set() and thread.is_alive()
		release.set()
		assert multiCMD._join(thread, 5)
		return
	class Gate:
		'''A semaphore with no permit that records the threads waiting on it'''
		def __init__(self):
			self.waiting = set()
		def acquire(self, blocking=True, timeout=None):
			self.waiting.add(threading.current_thread())
			time.sleep(timeout or 0)
			return False
		def release(self):
			pass
	gate = Gate()
	ex = AsyncExecutor(semaphore=gate, quiet=True)
	dispatchers = []
	real_track = ex._track
	def track(thread):
		if thread._target == ex._dispatch:
			dispatchers.append(thread)
		real_track(thread)
	monkeypatch.setattr(ex, '_track', track)
	try:
		with pytest.raises(KeyboardInterrupt):
			ex.run_commands([['true']])
		# two commands: two dispatchers would each take one and wait for a permit
		ex.run_commands([['true'], ['true']])
		deadline = time.monotonic() + 5
		while not all(d._started.is_set() and (d.done.flag or d in gate.waiting) for d in dispatchers) and time.monotonic() < deadline:
			time.sleep(0.01)
		# before: two
		assert sum(d.is_alive() for d in dispatchers) == 1
	finally:
		ex.stop()
	assert not any(d.is_alive() for d in dispatchers)
