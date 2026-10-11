# multiCMD 1.52 Overhaul Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship multiCMD 1.52. Timeouts, stop and Ctrl-C kill whole process groups. Output is read in chunks, fast and live. Parallel output is serialized with prefixes, colors and in-place redraws. Commands can run under a pty. Strings are split with shell quoting. `AsyncExecutor` is fixed. The CLI gets real exit codes, `--dry-run`, input files and an end-of-run summary.

**Architecture:** All runtime changes stay inside the single module `multiCMD.py`, built from small internal units:
- `_spawn`: start a command in its own process group.
- `_kill_commands` / `kill_all`: the kill sequence and the live registry.
- `_Display`: one lock for all live output.
- `_read_stream`: the chunked reader.
- `_execute`: supervise one command.
- `_run_pool`: bounded workers.

Public functions keep their signatures and gain keyword parameters. Tests are real-subprocess pytest files under `tests/`. Cross-repo work (dropping `argparse`, re-embedding) is mechanical and comes last.

**Tech Stack:** Python ≥ 3.6 standard library only. pytest. Linux for the process-group and tty tests. uv for the Python 3.9 run, the minifier (Python 3.11) and `vermin`.

**Spec:** `docs/superpowers/specs/2026-10-10-multicmd-overhaul-design.md` (commit `93720df` on branch `multicmd-1.52`)

## Global Constraints

- **Where to work:**
  - multiCMD tasks happen in `/mnt/klein/work/cas/multiCMD` on branch `multicmd-1.52` (already checked out).
  - Tasks 18–19 also edit the sibling repos. Create a `multicmd-1.52` branch in each one before editing.
- **Python ≥ 3.6 syntax:**
  - No walrus, no `shlex.join` (use `_quote_command`), no positional-only parameters, no runtime-evaluated `list[str]` annotations.
  - `process_group=` is only ever passed through a kwargs dict, on 3.11+.
- **Imports:** standard library only. `pty`, `termios`, `fcntl` and `msvcrt` are imported inside the functions that use them, never at module top level.
- **Single file:** all runtime code lives in `multiCMD.py`. `multiCMD_min.py` and `multiCMD_min_lzma_a85.py` are regenerated in Task 17, never hand-edited.
- **Style:**
  - Tabs, and `'''` docstrings with `@params:` / `@returns:` blocks, as in the existing file. Private helpers say `( internal )`.
  - Never call a `__double_underscore` module function from inside a class body: name mangling breaks it. Go through a single-underscore module function, as `_run_executor_task` does.
- **Exported names stay:** `_expand_ranges` stays as an alias of `_expand_ranges_fast`.
- **Green on two interpreters after every task:**
  - Python 3.13 (the `process_group` path): `python3 -m pytest -q`
  - Python 3.9 (the exec-wrapper path): `uv run --no-project --python 3.9 --with pytest python -m pytest -q`

  Baseline before Task 1: `66 passed, 2 skipped` on both.
- **Commits:** one per task, in the repo's `type(scope): summary` style, ending with the line `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- **Off limits:** no `git push`, no `uvpack`, no `cas-toolbox` changes.

## Review Focus

These are the inputs most likely to hurt a real user that the spec implies but no feature test would hit on its own. Each one has a pinned test in the owning task.

1. **Output that isn't valid UTF-8** (binary data, latin-1 logs). It must be captured with backslash escapes and never crash. Task 4's equivalence cases include `b'\xff\xfe bad utf8\n'`.
2. **One very long line arriving in tiny pieces** (progress dots, a 200 KB JSON line). It must become one captured line in linear time. Task 4 feeds a 200 KB line in 1-byte chunks; that's why the reader keeps the partial line in a `bytearray`.
3. **An empty command list** (`run_commands([])`, or a `-f` file with no commands). It must return `[]` or give a usage error, never hang. See Task 6 `test_empty_command_list` and Task 13 `test_empty_command_file_is_a_usage_error`.
4. **Ctrl-C during a sequential run** (`-m 1`). The running command is killed and the rest never start. See Task 12 `test_ctrl_c_in_a_sequential_run_skips_the_rest`.
5. **Input files edited on Windows** (CRLF). No stray `\r` may end up in commands or arguments. See Task 13 `test_input_files_with_crlf_line_endings`.

---

## File Structure

| Path | Responsibility |
|---|---|
| `multiCMD.py` | All runtime code |
| `tests/conftest.py` | Adds the `launcher` and `fresh_display` fixtures and `pid_gone()`; removes the `__variables` save/restore |
| `tests/test_process_groups.py` (new) | `_spawn`, tty protection, kill sequence, registry, `kill_all` |
| `tests/test_display.py` (new) | `_Display`, `_decor`, `_resolve_color`, `_quote_command` |
| `tests/test_reader.py` (new) | `_read_stream` against the 1.51 reader; activity timestamp, EIO, detach |
| `tests/test_supervision.py` (new) | `__run_command`/`_execute`: timeouts, stop, Ctrl-C, timing, sudo validation, 126/127 |
| `tests/test_pool.py` (new) | Pool thread bound, empty list, prefix/color wiring, Ctrl-C in the pool |
| `tests/test_pty.py` (new) | pty mode |
| `tests/test_parsing.py` (new) | `shlex` splitting, shell mode |
| `tests/test_expand_ranges.py` | New range rules, per-call variables |
| `tests/test_api.py` (new) | Dry-run format, `CommandError` |
| `tests/test_async_executor.py` | `wait`/`join`/`stop` timing, report, new parameters |
| `tests/test_cli.py` | Exit status, `-n`, `--shell`, signals |
| `tests/test_cli_input.py` (new) | `-f` / `-a` |
| `tests/test_cli_summary.py` (new) | Summary, prefix/color/pty flags |
| `tests/test_windows.py` (new) | Windows branches, via monkeypatch |
| `README.md` | Docs (Task 16) |
| `setup.py` | Drop `argparse` (Task 18) |
| `multiCMD_min.py`, `multiCMD_min_lzma_a85.py` | Regenerated (Task 17) |
| `../multiSSH3/setup.py`, `../TSVZ/setup.py`, `../hpcp/setup.py`, `../statblk/setup.py`, `../statbtrfs/setup.py`, `../simple-iotest/pyproject.toml` (+ `uv.lock`) | Drop `argparse` (Task 18) |
| `../hpcp/hpcp.py`, `../statblk/statblk.py`, `../statbtrfs/statbtrfs.py` | Re-embedded snippet (Task 19) |

---

### Task 1: Launcher (own process group, tty signals blocked)

**Files:**
- Modify: `multiCMD.py` (imports, the global vars block after `USE_SUDO = False`, the new `_spawn` after `set_sudo`)
- Modify: `tests/conftest.py`
- Create: `tests/test_process_groups.py`

**Interfaces:**
- Consumes: nothing new.
- Produces:
  - `_IS_POSIX: bool`, `_HAS_PROCESS_GROUP: bool`, `_PGROUP_EXEC: str`, `_TTY_SIGNALS: set`
  - `_spawn(argv, stdout, stderr, env=None) -> (subprocess.Popen, int | None)`: the second item is the process group id, or `None` when the command has no group of its own (Windows, or no `sys.executable`).
  - conftest fixture `launcher`, parametrized `'native'` / `'wrapper'`. It sets `multiCMD._HAS_PROCESS_GROUP`, and `'native'` is skipped below 3.11.

- [ ] **Step 1: Add the `launcher` fixture to `tests/conftest.py`**

Append:

```python
@pytest.fixture(params=['native', 'wrapper'])
def launcher(request, monkeypatch):
	'''Run a test once with Popen(process_group=0) ( Python 3.11+ ) and once with the exec wrapper'''
	if request.param == 'native':
		if sys.version_info < (3, 11):
			pytest.skip('Popen(process_group=) needs Python 3.11+')
		monkeypatch.setattr(multiCMD, '_HAS_PROCESS_GROUP', True)
	else:
		monkeypatch.setattr(multiCMD, '_HAS_PROCESS_GROUP', False)
	return request.param
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_process_groups.py`:

```python
import os
import shlex
import shutil
import signal
import subprocess
import sys
import textwrap

import pytest

import multiCMD
from conftest import PACKAGE_ROOT

pytestmark = pytest.mark.skipif(not sys.platform.startswith('linux'), reason='process groups are checked on Linux')


def test_spawn_puts_command_in_its_own_group(launcher):
	proc, pgid = multiCMD._spawn(['sh', '-c', 'ps -o pgid= -p $$'], subprocess.PIPE, subprocess.DEVNULL)
	out, _ = proc.communicate(timeout=10)
	assert pgid == proc.pid
	assert int(out) == proc.pid
	assert int(out) != os.getpgrp()


def test_spawn_missing_command_raises_file_not_found(launcher):
	with pytest.raises(FileNotFoundError):
		multiCMD._spawn(['no-such-command-xyz'], subprocess.PIPE, subprocess.PIPE)


def test_spawn_blocks_tty_signals_in_the_command(launcher):
	proc, _ = multiCMD._spawn(['sh', '-c', 'grep SigBlk /proc/$$/status'], subprocess.PIPE, subprocess.DEVNULL)
	out, _ = proc.communicate(timeout=10)
	blocked = int(out.split()[1], 16)
	assert blocked & (1 << (signal.SIGTTIN - 1))
	assert blocked & (1 << (signal.SIGTTOU - 1))


def test_spawn_restores_the_callers_signal_mask(launcher):
	proc, _ = multiCMD._spawn(['true'], subprocess.DEVNULL, subprocess.DEVNULL)
	proc.wait(timeout=10)
	assert signal.SIGTTIN not in signal.pthread_sigmask(signal.SIG_BLOCK, set())


TTY_PROBE = textwrap.dedent(r"""
	import os, signal, subprocess, sys
	sys.path.insert(0, {root!r})
	import multiCMD
	multiCMD._HAS_PROCESS_GROUP = {native!r}
	proc, pgid = multiCMD._spawn(['sh', '-c', 'read x </dev/tty; echo rc=$?'], subprocess.PIPE, subprocess.STDOUT)
	try:
		print('RESULT', proc.communicate(timeout=5)[0].decode().strip())
	except subprocess.TimeoutExpired:
		os.killpg(pgid, signal.SIGKILL)
		print('RESULT STOPPED')
""")


@pytest.mark.skipif(shutil.which('script') is None, reason='needs util-linux script for a controlling terminal')
def test_reading_the_terminal_fails_instead_of_stopping(launcher, tmp_path):
	probe = tmp_path / 'probe.py'
	probe.write_text(TTY_PROBE.format(root=str(PACKAGE_ROOT), native=launcher == 'native'))
	command = f'{shlex.quote(sys.executable)} {shlex.quote(str(probe))}'
	result = subprocess.run(['script', '-qec', command, '/dev/null'],
		stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
	assert 'RESULT rc=1' in result.stdout.replace('\r', '')
```

- [ ] **Step 3: Run the tests and watch them fail**

Run: `python3 -m pytest tests/test_process_groups.py -v`
Expected: every test FAILs with `AttributeError: module 'multiCMD' has no attribute '_spawn'`.

- [ ] **Step 4: Implement**

In `multiCMD.py`, add `import errno` to the imports (alphabetically, after `import collections`).

After `USE_SUDO = False` in the global vars block, add:

```python
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
```

After `set_sudo`, add:

```python
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
```

- [ ] **Step 5: Run the tests and watch them pass on both interpreters**

Run: `python3 -m pytest tests/test_process_groups.py -v`
Expected: 10 passed (5 tests × native/wrapper).

Run: `uv run --no-project --python 3.9 --with pytest python -m pytest tests/test_process_groups.py -v`
Expected: 5 passed, 5 skipped ("native" needs 3.11+).

Run the full suite on both interpreters (commands under Global Constraints).
Expected: no regressions.

- [ ] **Step 6: Commit**

```bash
git add multiCMD.py tests/conftest.py tests/test_process_groups.py
git commit -m "feat(spawn): start commands in their own process group with tty signals blocked

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Kill sequence, live registry, `kill_all` at exit

**Files:**
- Modify: `multiCMD.py` (`import atexit`, the registry globals, the functions after `_spawn`)
- Modify: `tests/conftest.py` (`pid_gone`)
- Modify: `tests/test_process_groups.py`

**Interfaces:**
- Consumes: `_spawn` (Task 1).
- Produces:
  - `_live_procs: dict` (`Popen -> pgid | None`) and `_live_procs_lock`
  - `_register(proc, pgid)`, `_unregister(proc)`
  - `_kill_commands(commands, first_signal)`: `commands` is an iterable of `(Popen, pgid | None)`. It returns after at most about 2 s.
  - `kill_all(sig=None)`: public; the first signal defaults to SIGTERM. `atexit.register(kill_all)`.
  - conftest `pid_gone(pid, wait=3.0) -> bool`: a zombie counts as gone.

- [ ] **Step 1: Add `pid_gone` to `tests/conftest.py`**

Add `import os` and `import time` to the imports, then append:

```python
def pid_gone(pid, wait=3.0):
	'''Whether a process has exited within wait seconds ( a zombie counts as exited )'''
	deadline = time.monotonic() + wait
	while True:
		try:
			os.kill(pid, 0)
		except ProcessLookupError:
			return True
		try:
			with open(f'/proc/{pid}/stat') as f:
				if f.read().rsplit(')', 1)[1].split()[0] == 'Z':
					return True
		except OSError:
			return True
		if time.monotonic() > deadline:
			return False
		time.sleep(0.05)
```

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_process_groups.py`. Also add `import time` to its imports, and change `from conftest import PACKAGE_ROOT` to `from conftest import PACKAGE_ROOT, pid_gone`.

```python
def _state(pid):
	try:
		with open(f'/proc/{pid}/stat') as f:
			return f.read().rsplit(')', 1)[1].split()[0]
	except OSError:
		return '?'


def test_kill_commands_reaches_grandchildren(launcher):
	proc, pgid = multiCMD._spawn(['sh', '-c', 'sleep 30 & echo $!; wait'], subprocess.PIPE, subprocess.DEVNULL)
	child = int(proc.stdout.readline())
	start = time.monotonic()
	multiCMD._kill_commands([(proc, pgid)], signal.SIGINT)
	assert proc.poll() is not None
	assert pid_gone(child)
	assert time.monotonic() - start < 3
	proc.stdout.close()


def test_kill_commands_continues_stopped_commands(launcher):
	proc, pgid = multiCMD._spawn(['sh', '-c', 'kill -STOP $$; sleep 30'], subprocess.DEVNULL, subprocess.DEVNULL)
	deadline = time.monotonic() + 5
	while _state(proc.pid) != 'T' and time.monotonic() < deadline:
		time.sleep(0.02)
	assert _state(proc.pid) == 'T'
	multiCMD._kill_commands([(proc, pgid)], signal.SIGINT)
	assert proc.poll() is not None


def test_kill_commands_escalates_to_sigkill(launcher):
	proc, pgid = multiCMD._spawn(['sh', '-c', 'trap "" INT TERM; echo ready; while :; do sleep 0.1; done'],
		subprocess.PIPE, subprocess.DEVNULL)
	proc.stdout.readline()
	start = time.monotonic()
	multiCMD._kill_commands([(proc, pgid)], signal.SIGINT)
	elapsed = time.monotonic() - start
	assert proc.returncode == -signal.SIGKILL
	assert 0.9 < elapsed < 3
	proc.stdout.close()


def test_kill_all_stops_registered_commands():
	proc, pgid = multiCMD._spawn(['sh', '-c', 'sleep 30 & echo $!; wait'], subprocess.PIPE, subprocess.DEVNULL)
	multiCMD._register(proc, pgid)
	try:
		child = int(proc.stdout.readline())
		multiCMD.kill_all()
		assert proc.poll() is not None
		assert pid_gone(child)
	finally:
		multiCMD._unregister(proc)
		proc.stdout.close()


def test_kill_all_with_nothing_running_returns_at_once():
	start = time.monotonic()
	multiCMD.kill_all()
	assert time.monotonic() - start < 0.5


EXIT_PROBE = textwrap.dedent(r"""
	import subprocess, sys
	sys.path.insert(0, {root!r})
	import multiCMD
	proc, pgid = multiCMD._spawn(['sh', '-c', 'sleep 30 & echo $!; wait'], subprocess.PIPE, subprocess.DEVNULL)
	multiCMD._register(proc, pgid)
	print(proc.pid, proc.stdout.readline().decode().strip(), flush=True)
""")


def test_kill_all_runs_when_the_interpreter_exits(tmp_path):
	probe = tmp_path / 'probe.py'
	probe.write_text(EXIT_PROBE.format(root=str(PACKAGE_ROOT)))
	out = subprocess.run([sys.executable, str(probe)], capture_output=True, text=True, timeout=30).stdout
	leader, child = (int(pid) for pid in out.split())
	assert pid_gone(leader)
	assert pid_gone(child)
```

- [ ] **Step 3: Run the tests and watch them fail**

Run: `python3 -m pytest tests/test_process_groups.py -v -k "kill"`
Expected: FAIL with `AttributeError: ... '_kill_commands'` / `'kill_all'` / `'_register'`.

- [ ] **Step 4: Implement**

Add `import atexit` to the imports (alphabetically, after `import argparse`).

After the `_TTY_SIGNALS` line in the global vars block, add:

```python
# commands still running: Popen -> process group id ( None when the command has no group of its own )
_live_procs = {}
_live_procs_lock = threading.Lock()
```

After `_spawn`, add:

```python
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
	handlers; multiCMD also calls it when the interpreter exits.

	@params:
		sig: The first signal to send ( default SIGTERM; POSIX only ), followed by SIGTERM and SIGKILL as needed

	@returns:
		None
	'''
	with _live_procs_lock:
		commands = list(_live_procs.items())
	_kill_commands(commands, signal.SIGTERM if sig is None else sig)

atexit.register(kill_all)
```

- [ ] **Step 5: Run the tests on both interpreters**

Run: `python3 -m pytest tests/test_process_groups.py -v`
Expected: all pass. The kill tests run once per launcher, plus the three registry tests.

Run the full suite on both interpreters.
Expected: no regressions.

- [ ] **Step 6: Commit**

```bash
git add multiCMD.py tests/conftest.py tests/test_process_groups.py
git commit -m "feat(kill): kill whole process groups and stop leftovers at exit

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Display writer, line decoration, color resolution

**Files:**
- Modify: `multiCMD.py` (`import shlex`; the `_Display` class after `_HeldPermit`; the module-level `_display` right after the class; helpers after `set_sudo`)
- Modify: `tests/conftest.py` (`fresh_display`)
- Create: `tests/test_display.py`

**Interfaces:**
- Consumes: `int_to_color` (existing).
- Produces:
  - `_Display` with:
    - `.lines(texts, decor=('', '', ''), replace_first=False, ends_with_cr=False)`
    - `.message(text, decor=('', '', ''))`
    - `.settle()`
  - The module-level `_display = _Display()`. Callers must look up `multiCMD._display` at call time, so tests can swap it.
  - `_stdout_isatty() -> bool`
  - `_quote_command(command) -> str`
  - `_decor(index, width, prefix, color) -> (prefix_text, color_start, color_end)`
  - `_resolve_color(color, concurrent) -> bool`

- [ ] **Step 1: Add the `fresh_display` fixture to `tests/conftest.py`**

```python
@pytest.fixture(autouse=True)
def fresh_display(monkeypatch):
	'''Every test starts with a display that has written nothing yet'''
	monkeypatch.setattr(multiCMD, '_display', multiCMD._Display())
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_display.py`:

```python
import io
import sys

import pytest

import multiCMD


@pytest.fixture
def out(monkeypatch):
	buffer = io.StringIO()
	monkeypatch.setattr(sys, 'stdout', buffer)
	monkeypatch.setattr(multiCMD, '_stdout_isatty', lambda: False)
	return buffer


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
```

- [ ] **Step 3: Run the tests and watch them fail**

Run: `python3 -m pytest tests/test_display.py -v`
Expected: errors from the autouse fixture: `AttributeError: module 'multiCMD' has no attribute '_Display'`. The whole suite errors the same way until Step 4.

- [ ] **Step 4: Implement**

Add `import shlex` to the imports (alphabetically, after `import select`).

After the `_HeldPermit` class, add:

```python
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
```

After `set_sudo`, add:

```python
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
```

- [ ] **Step 5: Run the tests on both interpreters**

Run: `python3 -m pytest tests/test_display.py -v`
Expected: 13 passed.

Run the full suite on both interpreters.
Expected: no regressions.

- [ ] **Step 6: Commit**

```bash
git add multiCMD.py tests/conftest.py tests/test_display.py
git commit -m "feat(display): serialize live output and track redrawable lines

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Chunked output reader

**Files:**
- Modify: `multiCMD.py` (`Task.__init__`, and the new `_read_stream` next to the existing `__handle_stream`, which stays until Task 5)
- Create: `tests/test_reader.py`

**Interfaces:**
- Consumes: `_display` (Task 3).
- Produces:
  - Task attributes:
    - `_last_output` (float from `time.monotonic()`, or `None`)
    - `_finalized` (bool; once true the reader records nothing)
    - `_pty` (bool)
  - `_read_stream(stream, target, task, decor=('', '', ''), quiet=False)`:
    - Reads `stream` to EOF, or to `OSError(EIO)`.
    - Appends decoded lines to `target`, showing them live unless `quiet`.
    - Closes `stream`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_reader.py`:

```python
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
	b'p\rq', '\u2713 ok\n'.encode(), b'\n\n\n', b'p\r\n\nq\n', b'p\rq\nr\n',
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
```

- [ ] **Step 2: Run the tests and watch them fail**

Run: `python3 -m pytest tests/test_reader.py -v`
Expected: FAIL with `AttributeError: ... '_read_stream'` / `'Task' object has no attribute '_last_output'`.

- [ ] **Step 3: Implement**

In `Task.__init__`, after `self.stop = False`, add:

```python
		# monotonic time of the latest output; drives the inactivity timeout
		self._last_output = None
		# set once the command is done with; readers still draining after a kill stop recording
		self._finalized = False
		# whether stdout and stderr were merged into one pty
		self._pty = False
```

Directly after the existing `__handle_stream` function, add:

```python
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
```

- [ ] **Step 4: Run the tests on both interpreters**

Run: `python3 -m pytest tests/test_reader.py -v`
Expected: 69 passed (13 cases × 5 chunk sizes, plus 4).

Run the full suite on both interpreters.
Expected: no regressions.

- [ ] **Step 5: Commit**

```bash
git add multiCMD.py tests/test_reader.py
git commit -m "feat(reader): read output in chunks, identical to the byte-at-a-time reader

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Supervise commands with the new launcher, reader and kill sequence

**Files:**
- Modify: `multiCMD.py`:
  - `Task.__init__` and a new `Task.duration`
  - Replace `__run_command`; delete `__handle_stream`
  - Add `_execute`, `_watch`, `_join_readers`
  - Replace `_validate_sudo`
  - Update `__run_tracked_command`, `_run_executor_task`, the three `__run_command` call sites in `run_commands`, and the `_validate_sudo` calls in `run_commands` and `AsyncExecutor.run_commands`
- Create: `tests/test_supervision.py`

**Interfaces:**
- Consumes:
  - `_spawn`, `_register`, `_unregister`, `_kill_commands` (Tasks 1–2)
  - `_display`, `_quote_command` (Task 3)
  - `_read_stream` and the Task fields (Task 4)
- Produces:
  - `__run_command(task, sem, timeout=60, quiet=False, dry_run=False, with_stdErr=False, decor=('', '', ''), use_pty=False)`. The `identity` parameter is gone.
  - `_execute(task, timeout, quiet, decor, use_pty=False)`
  - `_watch(task, proc, timeout) -> None | 'stop' | 'timeout'`
  - `_join_readers(readers, limit)`
  - `_validate_sudo()`: takes no arguments now.
  - Task attributes `start_time`, `end_time` (from `time.time()`, or `None`), and the `duration` property.
  - `__run_tracked_command(task, sem, timeout, quiet, dry_run, decor=('', '', ''), use_pty=False)`
  - `_run_executor_task(task, permit, timeout, quiet, dry_run, finished, decor=('', '', ''), use_pty=False)`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_supervision.py`:

```python
import _thread
import subprocess
import sys
import threading
import time

import pytest

import multiCMD
from conftest import pid_gone
from multiCMD import AsyncExecutor, run_command

linux_only = pytest.mark.skipif(not sys.platform.startswith('linux'), reason='checks processes through /proc')


@linux_only
@pytest.mark.slow
def test_timeout_kills_background_grandchild_and_returns_promptly(launcher):
	start = time.monotonic()
	task = run_command(['sh', '-c', 'sleep 30 & echo $!; wait'], timeout=1, quiet=True, return_object=True)
	assert task.returncode == 124
	assert 'Timeout!' in task.stderr
	assert time.monotonic() - start < 3.5
	assert pid_gone(int(task.stdout[0]))


@pytest.mark.slow
def test_timeout_does_not_wait_for_a_foreground_grandchild(launcher):
	start = time.monotonic()
	task = run_command(['sh', '-c', 'sleep 13; echo done'], timeout=1, quiet=True, return_object=True)
	assert task.returncode == 124
	assert time.monotonic() - start < 3.5


@pytest.mark.slow
def test_progress_redraws_count_as_activity():
	task = run_command(['sh', '-c', 'for i in 1 2 3 4 5; do printf "\\r$i"; sleep 0.4; done; echo'],
		timeout=1, quiet=True, return_object=True)
	assert task.returncode == 0
	assert task.stdout == ['5']


@pytest.mark.slow
def test_partial_line_output_counts_as_activity():
	task = run_command(['sh', '-c', 'for i in 1 2 3 4 5; do printf .; sleep 0.4; done'],
		timeout=1, quiet=True, return_object=True)
	assert task.returncode == 0
	assert task.stdout == ['.....']


@linux_only
@pytest.mark.slow
def test_executor_stop_kills_grandchildren_promptly():
	ex = AsyncExecutor(quiet=True)
	task = ex.run_command(['sh', '-c', 'sleep 30 & echo $!; wait'])
	deadline = time.monotonic() + 5
	while not task.stdout and time.monotonic() < deadline:
		time.sleep(0.02)
	start = time.monotonic()
	ex.stop(timeout=10)
	assert time.monotonic() - start < 3.5
	assert task.returncode is not None
	assert pid_gone(int(task.stdout[0]))


def test_timing_fields():
	task = run_command(['sleep', '0.2'], quiet=True, return_object=True)
	assert task.start_time <= task.end_time
	assert task.duration >= 0.2
	assert multiCMD.Task(['x']).duration is None


@pytest.mark.slow
def test_fire_and_forget_reports_exit_before_background_output_ends():
	task = run_command(['sh', '-c', '(sleep 2; echo late) & echo started'],
		quiet=True, wait_for_return=False, return_object=True)
	deadline = time.monotonic() + 1.5
	while task.returncode is None and time.monotonic() < deadline:
		time.sleep(0.02)
	assert task.returncode == 0
	assert 'late' not in task.stdout
	# the leader is reaped and dropped from the registry, so kill_all() leaves the background job alone
	assert not multiCMD._live_procs
	multiCMD.join_threads(timeout=5)
	assert task.stdout == ['started', 'late']


@linux_only
def test_ctrl_c_kills_the_running_command_and_reraises(tmp_path):
	pidfile = tmp_path / 'pid'
	timer = threading.Timer(0.5, _thread.interrupt_main)
	timer.start()
	start = time.monotonic()
	with pytest.raises(KeyboardInterrupt):
		run_command(['sh', '-c', f'sleep 30 & echo $! > {pidfile}; wait'], quiet=True)
	assert time.monotonic() - start < 3.5
	assert pid_gone(int(pidfile.read_text()))


def test_missing_command_is_127(launcher):
	assert run_command(['no-such-command-xyz'], quiet=True, return_code_only=True) == 127


def test_command_that_is_not_executable_is_126(launcher, tmp_path):
	script = tmp_path / 'script.sh'
	script.write_text('#!/bin/sh\necho hi\n')
	script.chmod(0o644)
	assert run_command([str(script)], quiet=True, return_code_only=True) == 126


def test_sudo_validation_runs_in_the_foreground(monkeypatch):
	calls = []
	monkeypatch.setattr(multiCMD, 'SUDO_PATH', '/usr/bin/sudo')
	monkeypatch.setattr(subprocess, 'run', lambda *args, **kwargs: calls.append((args, kwargs)))
	multiCMD._validate_sudo()
	assert calls == [((['/usr/bin/sudo', '-v'],), {})]


def test_stopped_task_is_skipped_before_it_starts():
	task = multiCMD.Task(['true'])
	task.stop = True
	multiCMD.__dict__['__run_command'](task, threading.Semaphore(1), quiet=True)
	assert task.returncode is None
	assert task.start_time is None
	assert not task._finalized
```

- [ ] **Step 2: Run the tests and watch them fail**

Run: `python3 -m pytest tests/test_supervision.py -v`
Expected: failures. The grandchild timeouts return after 13 s / 4 s, progress output gets rc 124, `duration` is missing, `_validate_sudo()` raises a missing-argument `TypeError`, and so on.

- [ ] **Step 3: Implement the Task fields**

In `Task.__init__`, after `self.stop = False`, add:

```python
		# wall-clock times the command started and finished ( None until then )
		self.start_time = None
		self.end_time = None
```

After `Task.is_alive`, add:

```python
	@property
	def duration(self):
		'''
		How long the command ran, in seconds ( None if it has not both started and finished )
		'''
		if self.start_time is None or self.end_time is None:
			return None
		return self.end_time - self.start_time
```

- [ ] **Step 4: Replace `__run_command` and delete `__handle_stream`**

Delete the whole `__handle_stream` function. Replace the whole `__run_command` function with:

```python
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

def _join_readers(readers, limit):
	'''
	Wait for a command's output readers to reach the end of their streams ( internal )
	@params:
		readers: The reader threads
		limit: The longest to wait in total, in seconds ( None: no limit )
	'''
	deadline = None if limit is None else time.monotonic() + limit
	for reader in readers:
		reader.join(None if deadline is None else max(0, deadline - time.monotonic()))

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
```

- [ ] **Step 5: Update the callers and `_validate_sudo`**

Replace `_run_executor_task` with:

```python
def _run_executor_task(task, permit, timeout, quiet, dry_run, finished, decor=('', '', ''), use_pty=False):
	'''
	Run an AsyncExecutor command, then report this thread finished so the executor joins it and drops it from the task ( internal )
	'''
	try:
		__run_command(task, permit, timeout, quiet, dry_run, decor=decor, use_pty=use_pty)
	finally:
		finished.append((threading.current_thread(), task))
```

Replace `__run_tracked_command` with:

```python
def __run_tracked_command(task, sem, timeout, quiet, dry_run, decor=('', '', ''), use_pty=False):
	'''
	Run a fire-and-forget command, then report this thread finished so it leaves __running_threads ( internal )
	'''
	try:
		__run_command(task, sem, timeout, quiet, dry_run, decor=decor, use_pty=use_pty)
	finally:
		__finished_threads.append(threading.current_thread())
```

Replace `_validate_sudo` with:

```python
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
```

In `run_commands`:
- Replace `_validate_sudo(sem)` with `_validate_sudo()`.
- Replace `args=(task,sem,timeout,quiet,dry_run,...)` with `args=(task,sem,timeout,quiet,dry_run)`. It occurs twice: the `__run_tracked_command` threads and the `__run_command` threads.
- Replace `__run_command(task,sem,timeout,quiet,dry_run,identity=None)` with `__run_command(task,sem,timeout,quiet,dry_run)`.

In `AsyncExecutor.run_commands`, replace `_validate_sudo(sem)` with `_validate_sudo()`.

In the `run_command` and `run_commands` docstrings, replace the `timeout:` entry with:

```
		timeout: Inactivity timeout in seconds. A command is killed, with everything it started,
			once it has produced no output at all for this many seconds. It is not a limit on
			the total runtime. Use 0 (the default) to disable it.
```

- [ ] **Step 6: Run the tests on both interpreters**

Run: `python3 -m pytest tests/test_supervision.py -v`
Expected: all pass.

Run the full suite on both interpreters.
Expected: no regressions. Commands run without color until Task 6 wires it back in.

- [ ] **Step 7: Commit**

```bash
git add multiCMD.py tests/test_supervision.py
git commit -m "feat(run): supervise commands in process groups with an activity-based timeout

A timeout, stop or Ctrl-C now kills everything a command started and returns
within the kill sequence instead of waiting for leftover processes. Any output
byte counts as activity, so progress redraws no longer trip the timeout.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Bounded worker pool, prefix and color wiring, Ctrl-C in the pool

**Files:**
- Modify: `multiCMD.py` (`run_command`, `run_commands`, plus the new `_run_pool` and `_stop_tasks` before `run_command`)
- Create: `tests/test_pool.py`

**Interfaces:**
- Consumes: `__run_command`, `__run_tracked_command` (Task 5); `_decor`, `_resolve_color` (Task 3).
- Produces:
  - `_run_pool(tasks, decors, sem, timeout, quiet, dry_run, max_threads, use_pty=False)`
  - `_stop_tasks(tasks)`
  - `run_commands(..., prefix=False, color=None)` and `run_command(..., prefix=False, color=None)`
  - "Concurrent" means `len(tasks) > 1 and max_threads != 1`, after `max_threads=0` becomes `len(tasks)`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_pool.py`:

```python
import _thread
import sys
import threading
import time

import pytest

from conftest import pid_gone
from multiCMD import run_commands


@pytest.mark.slow
def test_pool_keeps_threads_bounded():
	baseline = threading.active_count()
	peak = [0]
	done = threading.Event()
	def watch():
		while not done.is_set():
			peak[0] = max(peak[0], threading.active_count())
			time.sleep(0.001)
	watcher = threading.Thread(target=watch, daemon=True)
	watcher.start()
	try:
		codes = run_commands([['true']] * 1000, max_threads=4, quiet=True, return_code_only=True)
	finally:
		done.set()
		watcher.join()
	assert codes == [0] * 1000
	# 4 workers, 2 output readers per running command, and the watcher itself
	assert peak[0] <= baseline + 4 + 2 * 4 + 1


def test_empty_command_list():
	assert run_commands([], max_threads=4, quiet=True) == []
	assert run_commands([], max_threads=0, quiet=True) == []
	assert run_commands([], quiet=True) == []


def test_prefix_only_when_commands_run_at_once(capsys):
	run_commands([['echo', 'a'], ['echo', 'b']], max_threads=2, prefix=True)
	out = capsys.readouterr().out
	assert '[1] a' in out and '[2] b' in out
	assert '[1] Running command: echo a' in out
	run_commands([['echo', 'a'], ['echo', 'b']], max_threads=1, prefix=True)
	assert '[1]' not in capsys.readouterr().out
	run_commands([['echo', 'a']], max_threads=4, prefix=True)
	assert '[1]' not in capsys.readouterr().out


def test_colors_follow_the_color_argument(capsys):
	# 1.51 never colored this path: its color argument landed in with_stdErr
	run_commands([['echo', 'a'], ['echo', 'b']], max_threads=2, color=True)
	assert '\x1b[38;2;' in capsys.readouterr().out
	run_commands([['echo', 'a'], ['echo', 'b']], max_threads=2)
	assert '\x1b[' not in capsys.readouterr().out


@pytest.mark.slow
@pytest.mark.skipif(not sys.platform.startswith('linux'), reason='checks processes through /proc')
def test_ctrl_c_stops_every_running_command_in_the_pool(tmp_path):
	pidfiles = [tmp_path / f'pid{i}' for i in range(2)]
	commands = [['sh', '-c', f'sleep 30 & echo $! > {path}; wait'] for path in pidfiles] + [['sleep', '30']] * 4
	timer = threading.Timer(0.5, _thread.interrupt_main)
	timer.start()
	start = time.monotonic()
	with pytest.raises(KeyboardInterrupt):
		run_commands(commands, max_threads=2, quiet=True)
	assert time.monotonic() - start < 5
	for path in pidfiles:
		assert pid_gone(int(path.read_text()))
```

- [ ] **Step 2: Run the tests and watch them fail**

Run: `python3 -m pytest tests/test_pool.py -v`
Expected:
- `test_pool_keeps_threads_bounded` fails, with a peak in the hundreds.
- The prefix and color tests fail with `TypeError: run_commands() got an unexpected keyword argument 'prefix'`.
- The Ctrl-C test fails: the pool's main thread doesn't stop its tasks.

- [ ] **Step 3: Implement**

Before `run_command` (in the `#%% core funcitons` section, after `ping`), add:

```python
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
			thread.join()
	except KeyboardInterrupt:
		_stop_tasks(tasks)
		for thread in workers:
			thread.join(timeout=5)
		raise
```

Replace `run_command` with:

```python
def run_command(command, timeout=0,max_threads=1,quiet=False,dry_run=False,with_stdErr=False,
				return_code_only=False,return_object=False,wait_for_return=True, sem = None, 
				use_sudo = ..., raise_error = False, prefix = False, color = None):
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

	@returns:
		None | int | list[str] | Task: The output of the command
	'''
	return run_commands(commands=[command], timeout=timeout, max_threads=max_threads, quiet=quiet, 
					 dry_run=dry_run, with_stdErr=with_stdErr, return_code_only=return_code_only, 
					 return_object=return_object,parse=False,wait_for_return=wait_for_return,sem=sem,
					 use_sudo=use_sudo, raise_error=raise_error, prefix=prefix, color=color)[0]
```

In `run_commands`:

1. Extend the signature's last line to `sem : threading.Semaphore = None, use_sudo=..., raise_error = False, prefix = False, color = None):`.
2. Add two docstring entries after `raise_error:`:

```
		prefix: Whether to prefix output lines with [#], the command's 1-based position, when commands run at once
		color: Whether to color output lines per command ( None: when commands run at once on a terminal without NO_COLOR )
```

3. Change the `max_threads:` docstring entry to `The maximum number of commands to run at once ( 0: all of them )`.
4. Replace everything from `if max_threads < 1:` through the end of the `else:` serial loop with:

```python
	if max_threads < 1:
		max_threads = len(tasks)
	# concurrent runs get [#] prefixes ( when asked ) and colors ( on a terminal )
	concurrent = len(tasks) > 1 and max_threads != 1
	use_color = _resolve_color(color, concurrent)
	width = len(str(len(tasks)))
	decors = [_decor(index, width, prefix and concurrent, use_color) for index in range(1, len(tasks) + 1)]
	if not sem:
		sem = threading.Semaphore(max_threads)  # Limit concurrent sessions
	if use_sudo:
		_validate_sudo()
	if not wait_for_return:
		__reap_finished_threads()
		threads = [threading.Thread(target=__run_tracked_command, args=(task,sem,timeout,quiet,dry_run,decor),daemon=True) for task, decor in zip(tasks, decors)]
		# tracked before they start, so a thread is always registered before it reports itself finished
		with __running_threads_lock:
			__running_threads.update(threads)
		for thread,task in zip(threads,tasks):
			task.thread = thread
			thread.start()
	elif max_threads > 1:
		_run_pool(tasks, decors, sem, timeout, quiet, dry_run, max_threads)
	else:
		try:
			for task, decor in zip(tasks, decors):
				__run_command(task, sem, timeout, quiet, dry_run, decor=decor)
		except KeyboardInterrupt:
			_stop_tasks(tasks)
			raise
```

- [ ] **Step 4: Run the tests on both interpreters**

Run: `python3 -m pytest tests/test_pool.py tests/test_run_commands.py -v`
Expected: all pass. The existing `max_threads` scheduling and shared-semaphore tests still hold.

Run the full suite on both interpreters.
Expected: no regressions.

- [ ] **Step 5: Commit**

```bash
git add multiCMD.py tests/test_pool.py
git commit -m "feat(run): bounded worker pool, [#] prefixes and per-command colors

Fixes the 1.51 bug where run_commands passed its color flag into with_stdErr.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: pty mode

**Files:**
- Modify: `multiCMD.py`:
  - `import io` and the `_PTY_WARNED` global
  - New `_open_pty` and `_resolve_pty` before `_watch`
  - The `_execute` spawn block
  - `run_command` / `run_commands` (the `pty` parameter)
- Create: `tests/test_pty.py`

**Interfaces:**
- Consumes: `_spawn`, `_read_stream`, `_execute`, `get_terminal_size` (existing).
- Produces:
  - `_open_pty(reserved=0) -> (master_fd, slave_fd) | (None, None)`
  - `_resolve_pty(pty) -> bool`
  - `run_commands(..., pty=False)` and `run_command(..., pty=False)`
  - In pty mode, `task._pty = True`, `task.stderr == []` (except multiCMD's own `Timeout!` note), and `PAGER`/`GIT_PAGER` are set to `cat`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_pty.py`:

```python
import os
import struct
import sys

import pytest

import multiCMD
from multiCMD import run_command

pytestmark = pytest.mark.skipif(os.name != 'posix', reason='pty mode is POSIX only')

TTY_CHECK = ['sh', '-c', '[ -t 1 ] && echo tty || echo pipe']


def test_pty_command_sees_a_terminal():
	assert run_command(TTY_CHECK, quiet=True, pty=True) == ['tty']
	assert run_command(TTY_CHECK, quiet=True) == ['pipe']


def test_pty_merges_stderr_into_stdout():
	task = run_command(['sh', '-c', 'echo out; echo err >&2'], quiet=True, pty=True, return_object=True)
	assert task.stdout == ['out', 'err']
	assert task.stderr == []
	assert task._pty is True


def test_pty_sets_pager_to_cat():
	assert run_command(['sh', '-c', 'echo "$PAGER $GIT_PAGER"'], quiet=True, pty=True) == ['cat cat']


def test_pty_turns_off_output_processing():
	import termios
	master, slave = multiCMD._open_pty(0)
	try:
		assert not termios.tcgetattr(slave)[1] & termios.OPOST
	finally:
		os.close(master)
		os.close(slave)


def test_pty_window_size_leaves_room_for_the_prefix(monkeypatch):
	import fcntl
	import termios
	monkeypatch.setattr(multiCMD, 'get_terminal_size', lambda: (100, 40))
	master, slave = multiCMD._open_pty(4)
	try:
		rows, columns = struct.unpack('HHHH', fcntl.ioctl(slave, termios.TIOCGWINSZ, b'\0' * 8))[:2]
		assert (rows, columns) == (40, 96)
	finally:
		os.close(master)
		os.close(slave)


def test_pty_falls_back_to_pipes_when_none_can_be_opened(monkeypatch, capsys):
	import pty
	def no_pty():
		raise OSError('out of ptys')
	monkeypatch.setattr(pty, 'openpty', no_pty)
	monkeypatch.setattr(multiCMD, '_PTY_WARNED', False)
	assert run_command(TTY_CHECK, quiet=True, pty=True) == ['pipe']
	assert 'cannot open a pty' in capsys.readouterr().err


def test_pty_is_ignored_with_a_warning_off_posix(monkeypatch, capsys):
	monkeypatch.setattr(multiCMD, '_IS_POSIX', False)
	monkeypatch.setattr(multiCMD, '_PTY_WARNED', False)
	assert multiCMD._resolve_pty(True) is False
	assert 'needs a POSIX system' in capsys.readouterr().err


@pytest.mark.slow
def test_pty_timeout_still_kills():
	task = run_command(['sh', '-c', 'echo hi; sleep 10'], quiet=True, pty=True, timeout=1, return_object=True)
	assert task.returncode == 124
	assert task.stdout == ['hi']
```

- [ ] **Step 2: Run the tests and watch them fail**

Run: `python3 -m pytest tests/test_pty.py -v`
Expected: FAIL with `TypeError: run_command() got an unexpected keyword argument 'pty'` / `AttributeError: ... '_open_pty'`.

- [ ] **Step 3: Implement**

Add `import io` to the imports (alphabetically, after `import errno`). After the `_live_procs_lock` line in the global vars block, add:

```python
# set once the "cannot use a pty" warning has been printed
_PTY_WARNED = False
```

Before `_watch`, add:

```python
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
```

In `_execute`, replace these two lines:

```python
	proc, pgid = _spawn(task.command, subprocess.PIPE, subprocess.PIPE)
	streams = [(proc.stdout, task.stdout), (proc.stderr, task.stderr)]
```

with:

```python
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
```

In `run_command`:
- Add `, pty = False` after `color = None` in the signature.
- Add the docstring line `		pty: Whether the command writes to a pseudo-terminal, so it prints live ( POSIX only; stderr is merged into stdout )`.
- Pass `pty=pty` in the `run_commands(...)` call.

In `run_commands`:
- Add `, pty = False` after `color = None` in the signature, and the same docstring line.
- After the `decors = [...]` line, add `use_pty = _resolve_pty(pty)`.
- In the fire-and-forget thread args, use `args=(task,sem,timeout,quiet,dry_run,decor,use_pty)`.
- Change the pool call to `_run_pool(tasks, decors, sem, timeout, quiet, dry_run, max_threads, use_pty)`.
- Change the serial call to `__run_command(task, sem, timeout, quiet, dry_run, decor=decor, use_pty=use_pty)`.

- [ ] **Step 4: Run the tests on both interpreters**

Run: `python3 -m pytest tests/test_pty.py -v`
Expected: 8 passed.

Run the full suite on both interpreters.
Expected: no regressions.

- [ ] **Step 5: Commit**

```bash
git add multiCMD.py tests/test_pty.py
git commit -m "feat(pty): optional pseudo-terminal output so commands print live

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: Shell-quoting splits and shell mode

**Files:**
- Modify: `multiCMD.py`:
  - New `_split_command` and `_shell_argv` before `_build_tasks`
  - `__format_command`, `_build_tasks`
  - `run_command` / `run_commands` (the `shell` parameter)
- Create: `tests/test_parsing.py`

**Interfaces:**
- Consumes: `_quote_command`, `_IS_POSIX`.
- Produces:
  - `_split_command(command) -> list[str]`; raises `ValueError('cannot parse command ...')`
  - `_shell_argv(command) -> list[str]`
  - `__format_command(command, expand=False, shell=False)`
  - `_build_tasks(commands, parse=False, use_sudo=..., shell=False)`
  - `run_command(..., shell=False)` and `run_commands(..., shell=False)`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_parsing.py`:

```python
import pytest

import multiCMD
from multiCMD import run_command, run_commands


def test_string_commands_follow_shell_quoting():
	assert run_command("printf '%s|' 'a b'", quiet=True) == ['a b|']
	assert run_command('printf "%s|" "it\'s"', quiet=True) == ["it's|"]


def test_simple_strings_split_as_before():
	tasks, _ = multiCMD._build_tasks(['lsblk -b -n -p -o NAME,SIZE -P', 'ping -c 1 host1'])
	assert [task.command for task in tasks] == [
		['lsblk', '-b', '-n', '-p', '-o', 'NAME,SIZE', '-P'],
		['ping', '-c', '1', 'host1'],
	]


def test_unbalanced_quote_fails_before_anything_runs(tmp_path):
	marker = tmp_path / 'ran'
	with pytest.raises(ValueError, match='cannot parse command'):
		run_commands([['touch', str(marker)], "echo 'oops"], quiet=True)
	assert not marker.exists()


def test_shell_mode_runs_pipelines():
	assert run_command('echo abc | tr a x', shell=True, quiet=True) == ['xbc']


def test_shell_mode_quotes_list_commands():
	assert run_command(['echo', 'a  b'], shell=True, quiet=True) == ['a  b']


def test_shell_mode_with_sudo_wraps_the_shell(monkeypatch):
	monkeypatch.setattr(multiCMD, 'SUDO_PATH', '/usr/bin/sudo')
	tasks, used = multiCMD._build_tasks(['echo hi'], use_sudo=True, shell=True)
	assert used is True
	assert tasks[0].command == ['/usr/bin/sudo', '/bin/sh', '-c', 'echo hi']


def test_shell_mode_expands_ranges_first():
	assert run_commands(['echo x[1-2] | tr x y'], parse=True, shell=True, quiet=True) == [['y1'], ['y2']]
```

- [ ] **Step 2: Run the tests and watch them fail**

Run: `python3 -m pytest tests/test_parsing.py -v`
Expected:
- The quoting test fails (`["''a|''b'|'"]`).
- The unbalanced-quote test fails: no `ValueError`, and `touch` ran.
- The shell tests fail with `unexpected keyword argument 'shell'`.

- [ ] **Step 3: Implement**

Before `_build_tasks`, add:

```python
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
```

Replace `__format_command` with:

```python
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
```

In `_build_tasks`:
- Change the signature to `def _build_tasks(commands, parse=False, use_sudo=..., shell=False):`.
- Change the loop body to `formatedCommands.extend(__format_command(command,expand=parse,shell=shell))`.

In `run_commands`:
- Add `, shell = False` after `pty = False` in the signature.
- Add the docstring line `		shell: Whether to run each command through the system shell ( /bin/sh -c ), so pipes, ;, > and globs work`.
- Change the first line of the body to `tasks, use_sudo = _build_tasks(commands, parse=parse, use_sudo=use_sudo, shell=shell)`.

In `run_command`, add `, shell = False` to the signature, the same docstring line, and pass `shell=shell`.

- [ ] **Step 4: Run the tests on both interpreters**

Run: `python3 -m pytest tests/test_parsing.py -v`
Expected: 7 passed.

Run the full suite on both interpreters.
Expected: no regressions.

- [ ] **Step 5: Commit**

```bash
git add multiCMD.py tests/test_parsing.py
git commit -m "feat(parse): split string commands with shell quoting; add opt-in shell mode

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 9: Range-expansion rules and per-call variables

**Files:**
- Modify: `multiCMD.py`:
  - Delete the global `__variables`; add the `_UNESCAPED_COLON_RX` constant
  - `_expand_piece`, `_expand_ranges_fast`, `__format_command`, `_build_tasks`
- Modify: `tests/conftest.py`, `tests/test_expand_ranges.py`

**Interfaces:**
- Consumes: nothing new.
- Produces:
  - `_expand_ranges_fast(inStr, variables=None)` (`_expand_ranges` stays the same object)
  - `__format_command(command, expand=False, shell=False, variables=None)`
  - `_build_tasks` creates one variables dict per call.

- [ ] **Step 1: Update the harness and write the failing tests**

In `tests/conftest.py`, delete these lines from `restore_module_globals`:

```python
	saved_vars = dict(multiCMD.__variables)
```

```python
	multiCMD.__variables.clear()
	multiCMD.__variables.update(saved_vars)
```

In `tests/test_expand_ranges.py`:
- Delete both `multiCMD.__variables.clear()` lines.
- Change the import line to `from multiCMD import _expand_ranges, _expand_ranges_fast, run_commands`.
- Append:

```python
def test_letter_ranges_keep_their_case():
	assert _expand_ranges('host[A-C]') == ['hostA', 'hostB', 'hostC']
	assert _expand_ranges('host[a-c]') == ['hosta', 'hostb', 'hostc']


def test_hex_ranges_keep_padding_and_case():
	assert _expand_ranges('n[08-0b]') == ['n08', 'n09', 'n0a', 'n0b']
	assert _expand_ranges('n[08-0B]') == ['n08', 'n09', 'n0A', 'n0B']
	assert _expand_ranges('[a-10]') == ['a', 'b', 'c', 'd', 'e', 'f', '10']


def test_range_with_an_empty_end_is_a_plain_token():
	assert _expand_ranges('[-5]') == ['-5']


def test_ipv6_addresses_pass_through():
	assert _expand_ranges('ping [fe80::1,fe80::2]') == ['ping fe80::1', 'ping fe80::2']


def test_escaped_colon_is_literal():
	assert _expand_ranges('curl [web1\\:8080,web2\\:8080]') == ['curl web1:8080', 'curl web2:8080']


def test_variables_do_not_leak_between_calls():
	assert _expand_ranges('[n:3]host[1-n]') == ['host1', 'host2', 'host3']
	assert _expand_ranges('disk[n]') == ['diskn']


def test_variables_are_shared_within_one_run_commands_call():
	assert run_commands(['echo [n:2]first', 'echo second[1-n]'], parse=True, quiet=True) == \
		[['first'], ['second1'], ['second2']]


def test_explicit_variables_dict_is_used():
	variables = {}
	_expand_ranges('[n:2]', variables)
	assert variables == {'n': '2'}
```

- [ ] **Step 2: Run the tests and watch them fail**

Run: `python3 -m pytest tests/test_expand_ranges.py -v`
Expected: the new tests fail (`hosta`, `n8`, `ping `, `disk3`, and `TypeError` for the `variables` argument).

- [ ] **Step 3: Implement**

In the global vars block, delete the line `__variables = {}`. Under `# immutable helpers compiled once at import time`, add:

```python
# a ':' not preceded by a backslash; '\:' is a literal colon inside [...]
_UNESCAPED_COLON_RX = re.compile(r'(?<!\\):')
```

Replace `_expand_piece` with:

```python
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
```

Replace the start of `_expand_ranges_fast` (its signature, the `global` line, and the docstring position) so the function begins:

```python
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
```

In the same function, change `expandedPieces = _expand_piece(sub, __variables)` to `expandedPieces = _expand_piece(sub, variables)`.

In `__format_command`:
- Change the signature to `def __format_command(command,expand = False,shell = False,variables = None):`.
- Add the docstring entry `		variables: The range variables shared with the other commands of the batch`.
- Make `if variables is None:\n\t\tvariables = {}` the first statement of the body.
- Change `_expand_ranges_fast(command)` to `_expand_ranges_fast(command, variables)`, and `_expand_ranges_fast(field)` to `_expand_ranges_fast(field, variables)`.
- Change the recursive call to `__format_command(str(command),expand=expand,shell=shell,variables=variables)`.

In `_build_tasks`, replace the loop with:

```python
	# one set of range variables per batch: shared by its commands, never by the next call
	variables = {}
	formatedCommands = []
	for command in commands:
		formatedCommands.extend(__format_command(command,expand=parse,shell=shell,variables=variables))
```

- [ ] **Step 4: Run the tests on both interpreters**

Run: `python3 -m pytest tests/test_expand_ranges.py -v`
Expected: all pass, the existing README-matrix tests included.

(`test_expand_ranges_perf.py` carries its own copies of the expansion code and never imports `multiCMD`, so it is unaffected.)

Run the full suite on both interpreters.
Expected: no regressions.

- [ ] **Step 5: Commit**

```bash
git add multiCMD.py tests/conftest.py tests/test_expand_ranges.py
git commit -m "fix(ranges): keep letter case and hex padding, scope variables per call

Also stops IPv6 addresses and host:port lists from being read as assignments;
'\\:' gives a literal colon.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 10: Dry-run format and `CommandError`

**Files:**
- Modify: `multiCMD.py`:
  - New `CommandError` class after `Task`
  - New `_last_output_line` after `_quote_command`
  - `run_commands` (dry-run handling, `raise_error`)
- Create: `tests/test_api.py`

**Interfaces:**
- Consumes: `_display`, `_quote_command`, `Task._pty`.
- Produces:
  - `CommandError(tasks, total=None)` with `.tasks`; its message starts `Commands failed (N of M):`.
  - `_last_output_line(task, limit=200) -> str`
  - `run_commands(dry_run=True)` prints one quoted line per command (unless `quiet`), runs nothing, and skips `sudo -v`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_api.py`:

```python
import pytest

import multiCMD
from multiCMD import CommandError, run_command, run_commands


def test_dry_run_prints_each_command_quoted_in_order(capsys):
	assert run_commands([['echo', 'a b'], 'echo [1-2]'], parse=True, dry_run=True) == [[], [], []]
	assert capsys.readouterr().out == "echo 'a b'\necho 1\necho 2\n"


def test_quiet_dry_run_prints_nothing(capsys):
	run_commands([['echo', 'a']], dry_run=True, quiet=True)
	assert capsys.readouterr().out == ''


def test_dry_run_skips_sudo_validation(monkeypatch, capsys):
	monkeypatch.setattr(multiCMD, 'SUDO_PATH', '/usr/bin/sudo')
	def fail():
		raise AssertionError('sudo -v must not run in a dry run')
	monkeypatch.setattr(multiCMD, '_validate_sudo', fail)
	run_commands([['id']], dry_run=True, use_sudo=True)
	assert capsys.readouterr().out == '/usr/bin/sudo id\n'


def test_dry_run_tasks_are_not_failures():
	assert run_commands([['false']], dry_run=True, quiet=True, raise_error=True) == [[]]


def test_command_error_lists_failed_commands():
	with pytest.raises(CommandError) as info:
		run_commands([['true'], ['false'], ['sh', '-c', 'echo oops >&2; exit 3']], quiet=True, raise_error=True)
	error = info.value
	assert isinstance(error, Exception)
	assert [task.returncode for task in error.tasks] == [1, 3]
	assert str(error).splitlines() == [
		'Commands failed (2 of 3):',
		'  [rc 1] false',
		"  [rc 3] sh -c 'echo oops >&2; exit 3': oops",
	]


def test_command_error_truncates_long_lines():
	with pytest.raises(CommandError) as info:
		run_command(['sh', '-c', 'printf "%0300d\\n" 0 >&2; exit 1'], quiet=True, raise_error=True)
	last = str(info.value).splitlines()[-1]
	assert last.endswith('...')
	assert len(last.split(': ', 1)[1]) == 200


def test_fire_and_forget_does_not_raise_for_unfinished_commands():
	run_commands([['sleep', '0.2']], wait_for_return=False, quiet=True, raise_error=True)
	multiCMD.join_threads(timeout=5)
```

- [ ] **Step 2: Run the tests and watch them fail**

Run: `python3 -m pytest tests/test_api.py -v`
Expected: FAIL with `ImportError: cannot import name 'CommandError'`.

- [ ] **Step 3: Implement**

After the `Task` class, add:

```python
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
```

After `_quote_command`, add:

```python
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
```

In `run_commands`, replace the block from `if use_sudo:` (the `_validate_sudo()` call) through the end of the serial `else:` branch with:

```python
	if use_sudo and not dry_run:
		_validate_sudo()
	if dry_run:
		# nothing runs: show each command once, quoted so it can be pasted into a shell
		if not quiet:
			for task in tasks:
				_display.message(_quote_command(task.command))
	elif not wait_for_return:
		__reap_finished_threads()
		threads = [threading.Thread(target=__run_tracked_command, args=(task,sem,timeout,quiet,False,decor,use_pty),daemon=True) for task, decor in zip(tasks, decors)]
		# tracked before they start, so a thread is always registered before it reports itself finished
		with __running_threads_lock:
			__running_threads.update(threads)
		for thread,task in zip(threads,tasks):
			task.thread = thread
			thread.start()
	elif max_threads > 1:
		_run_pool(tasks, decors, sem, timeout, quiet, False, max_threads, use_pty)
	else:
		try:
			for task, decor in zip(tasks, decors):
				__run_command(task, sem, timeout, quiet, False, decor=decor, use_pty=use_pty)
		except KeyboardInterrupt:
			_stop_tasks(tasks)
			raise
```

Then replace the `raise_error` block with:

```python
	if raise_error:
		# dry-run and still-running commands have no return code yet: they did not fail
		failed = [task for task in tasks if task.returncode not in (0, None)]
		if failed:
			raise CommandError(failed, total=len(tasks))
```

- [ ] **Step 4: Run the tests on both interpreters**

Run: `python3 -m pytest tests/test_api.py tests/test_run_command.py tests/test_run_commands.py -v`
Expected: all pass. The existing `match="Commands failed"` tests still match.

Run the full suite on both interpreters.
Expected: no regressions.

- [ ] **Step 5: Commit**

```bash
git add multiCMD.py tests/test_api.py
git commit -m "feat(api): paste-able dry-run output and CommandError for raise_error

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 11: `AsyncExecutor` timing, report and new parameters

**Files:**
- Modify: `multiCMD.py`: in `AsyncExecutor`, `__init__`, `_dispatch`, `run_commands`, `run_command`, `wait`, `stop`, `cleanup`, `join`
- Modify: `tests/test_async_executor.py`

**Interfaces:**
- Consumes: `_build_tasks(shell=)`, `_decor`, `_resolve_color`, `_resolve_pty`, `_validate_sudo()`, `_run_executor_task(..., decor, use_pty)`.
- Produces:
  - `AsyncExecutor(max_threads=1, semaphore=..., timeout=0, quiet=True, dry_run=False, parse=False, shell=False, prefix=False, color=None, pty=False)`
  - `run_commands` / `run_command` accept per-call `shell`, `prefix`, `color`, `pty` overrides.
  - `wait`, `join`, `stop` and `cleanup` default to `timeout=None`, which waits until done. A positive timeout is one overall deadline; a negative one waits forever.
  - The queue items are `(task, sem, timeout, quiet, dry_run, decor, use_pty)`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_async_executor.py`. Add `import _thread` to its imports, keeping its existing `threading`, `time`, `pytest` and `AsyncExecutor` imports.

```python
@pytest.mark.slow
def test_join_waits_by_default(capsys):
	ex = AsyncExecutor(max_threads=2)
	ex.run_commands([['sleep', '0.5'], ['true']])
	start = time.monotonic()
	tasks = ex.join()
	assert time.monotonic() - start >= 0.4
	assert [task.returncode for task in tasks] == [0, 0]
	assert 'failed' not in capsys.readouterr().out


@pytest.mark.slow
def test_wait_timeout_is_one_overall_limit():
	ex = AsyncExecutor(max_threads=3)
	ex.run_commands([['sleep', '3']] * 3)
	start = time.monotonic()
	still_running = ex.wait(timeout=0.5)
	assert time.monotonic() - start < 1.2
	assert still_running
	ex.stop()


@pytest.mark.slow
def test_join_reports_failed_skipped_and_still_running(capsys):
	ex = AsyncExecutor(max_threads=1)
	ex.run_commands([['false']])
	ex.wait()
	running, queued = ex.run_commands([['sleep', '5'], ['true']])
	queued.stop = True
	ex.join(timeout=0.3)
	out = capsys.readouterr().out
	assert "Command: ['false'] failed with return code: 1" in out
	assert "Command: ['sleep', '5'] still running" in out
	assert "Command: ['true'] skipped: stopped before it started" in out
	ex.stop()


@pytest.mark.slow
def test_ctrl_c_during_wait_stops_the_executors_commands():
	ex = AsyncExecutor(max_threads=1)
	task = ex.run_command(['sleep', '30'])
	timer = threading.Timer(0.5, _thread.interrupt_main)
	timer.start()
	start = time.monotonic()
	with pytest.raises(KeyboardInterrupt):
		ex.wait()
	assert time.monotonic() - start < 4
	assert task.returncode is not None


def test_executor_shell_prefix_and_color(capsys):
	ex = AsyncExecutor(max_threads=2, quiet=False, shell=True, prefix=True, color=True)
	ex.run_commands(['echo abc | tr a x', 'echo two'])
	ex.wait()
	out = capsys.readouterr().out
	assert '[1] ' in out and '[2] ' in out and 'xbc' in out
	assert '\x1b[38;2;' in out


def test_executor_dry_run_prints_the_quoted_command(capsys):
	ex = AsyncExecutor(quiet=False, dry_run=True)
	ex.run_command(['echo', 'a b'])
	ex.wait()
	assert capsys.readouterr().out == "echo 'a b'\n"
```

- [ ] **Step 2: Run the tests and watch them fail**

Run: `python3 -m pytest tests/test_async_executor.py -v`
Expected:
- `test_join_waits_by_default` fails: it returns after about 0 s and prints "failed".
- The overall-limit test fails: it takes about 1.5 s.
- The report test fails on the wording.
- The parameter tests fail with `unexpected keyword argument 'shell'`.

- [ ] **Step 3: Implement**

Replace `AsyncExecutor.__init__` with:

```python
	def __init__(self, max_threads=1,semaphore=...,timeout=0,quiet=True,dry_run=False,parse=False,shell=False,prefix=False,color=None,pty=False):
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
		self._lock = threading.Lock()
		# (task, sem, timeout, quiet, dry_run, decor, use_pty) waiting for a permit
		self._queue = collections.deque()
		self._dispatching = False
		# started threads not yet joined, in start order ( dict used as an ordered set )
		self._threads = {}
		# threads append themselves here when done, so _reap() joins them without scanning
		self._finished = collections.deque()
```

In `_dispatch`, change the unpack line to `task, sem, timeout, quiet, dry_run, decor, use_pty = self._queue.popleft()`, and the start call to:

```python
					self._start_thread(_run_executor_task, (task, _HeldPermit(sem), timeout, quiet, dry_run, self._finished, decor, use_pty), task)
```

Replace `run_commands` and `run_command` with:

```python
	def run_commands(self, commands, timeout=...,max_threads=...,quiet=...,dry_run=...,parse = ...,sem = ...,shell = ...,prefix = ...,color = ...,pty = ...):
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
		taskObjects, use_sudo = _build_tasks(commands, parse=parse, shell=shell)
		if not sem:
			sem = threading.Semaphore(max_threads if max_threads >= 1 else max(len(taskObjects), 1))
		if use_sudo and not dry_run:
			_validate_sudo()
		# the executor's task list keeps growing, so commands count as concurrent whenever max_threads allows it
		concurrent = max_threads != 1
		use_color = _resolve_color(color, concurrent)
		use_pty = _resolve_pty(pty)
		self._reap()
		with self._lock:
			first = len(self.tasks) + 1
			queued = [(task, sem, timeout, quiet, dry_run, _decor(first + i, 0, prefix and concurrent, use_color), use_pty)
				for i, task in enumerate(taskObjects)]
			self.tasks.extend(taskObjects)
			self._queue.extend(queued)
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
	
	def run_command(self, command, timeout=...,max_threads=...,quiet=...,dry_run=...,parse = ...,sem = ...,shell = ...,prefix = ...,color = ...,pty = ...):
		'''
		Run a command in parallel asynchronously
		@params:
			command: The command to run ( str | list[str] )
			( the other parameters override the object defaults, as in run_commands )
		@returns:
			Task: The Task Object of the command
		'''
		return self.run_commands([command],timeout=timeout,max_threads=max_threads,quiet=quiet,dry_run=dry_run,parse=parse,sem=sem,shell=shell,prefix=prefix,color=color,pty=pty)[0]
```

Replace `wait`, `stop`, `cleanup` and `join` with:

```python
	def wait(self, timeout=None, threads = ...):
		'''
		Wait for the commands to finish
		@params:
			timeout: The longest to wait in total, in seconds ( None or negative: until everything is done )
			threads: The threads to join, default to all running threads managed by this object,
				including the ones the dispatcher starts while waiting
		@returns:
			list[threading.Thread]: The threads that are still running
		'''
		deadline = None if timeout is None or timeout < 0 else time.monotonic() + timeout
		try:
			while True:
				pending = self.runningThreads if threads is ... else list(threads)
				for thread in pending:
					thread.join(None if deadline is None else max(0, deadline - time.monotonic()))
				# once a pass finishes every thread, look again for commands the dispatcher started meanwhile
				if threads is not ... or not pending or any(thread.is_alive() for thread in pending):
					break
		except KeyboardInterrupt:
			# the commands run in process groups of their own, so the terminal's Ctrl-C did not reach them
			for task in self.tasks:
				task.stop = True
			self.wait(timeout=5)
			raise
		return self.runningThreads
	
	def stop(self,timeout=None):
		'''
		Stop all commands: running ones are killed with everything they started, queued ones are
		skipped ( they keep task.thread None and returncode None ). Then wait for them.
		@params:
			timeout: The longest to wait in total, in seconds ( None or negative: until everything is done )
		@returns:
			list[Task]: The list of tasks that are managed by this object
		'''
		for task in self.tasks:
			task.stop = True
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
```

- [ ] **Step 4: Run the tests on both interpreters**

Run: `python3 -m pytest tests/test_async_executor.py -v`
Expected: all pass, the existing queueing and thread-release tests included.

Run the full suite on both interpreters.
Expected: no regressions.

- [ ] **Step 5: Commit**

```bash
git add multiCMD.py tests/test_async_executor.py
git commit -m "fix(executor): wait until done by default, one overall timeout, accurate report

join()/wait() used the per-command inactivity timeout (default 0) as a join
timeout, so they returned at once and reported running commands as failed.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 12: CLI exit status, `-t` default, `-n`, `--shell`, signals, `__main__` at the end

**Files:**
- Modify: `multiCMD.py` (`main` and its helpers; move the `__main__` block)
- Modify: `tests/test_cli.py`

**Interfaces:**
- Consumes: `run_commands(..., shell=, dry_run=, return_object=True)`, `kill_all`.
- Produces:
  - `_EXIT_STATUS_HELP: str`
  - `_exit_status(tasks) -> int`
  - `_install_cli_signal_handlers()`
  - `main()` returns an int exit status.
  - The file ends with `if __name__ == '__main__':\n\tsys.exit(main())`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_cli.py`, change the imports to:

```python
import shutil
import signal
import subprocess
import sys
import time

import pytest

from conftest import MULTICMD_PY, pid_gone, run_cli
import multiCMD
```

and append:

```python
def test_exit_status_single_command_passes_its_code_through():
	assert run_cli(['-q', 'false']).returncode == 1
	assert run_cli(['-q', "sh -c 'exit 3'"]).returncode == 3


@pytest.mark.slow
def test_exit_status_timeout_is_124():
	assert run_cli(['-q', '-t', '1', 'sleep 5']).returncode == 124


def test_exit_status_signal_death_is_128_plus_n():
	assert run_cli(['-q', "sh -c 'kill -TERM $$'"]).returncode == 128 + 15


def test_exit_status_counts_failed_commands():
	assert run_cli(['-q', 'true', 'false', "sh -c 'exit 3'"]).returncode == 2


@pytest.mark.slow
def test_exit_status_failure_count_is_capped_at_101():
	assert run_cli(['-q', '-m', '0', '-p', 'false [1-105]'], timeout=120).returncode == 101


def test_help_documents_timeout_default_and_exit_status():
	help_text = run_cli(['-h']).stdout
	assert '0 = never' in help_text
	assert 'exit status' in help_text


def test_dry_run_prints_quoted_commands_even_when_quiet():
	proc = run_cli(['-n', '-q', '-p', 'echo host[1-2]', "echo 'a b'"])
	assert proc.returncode == 0
	assert proc.stdout.splitlines() == ['echo host1', 'echo host2', "echo 'a b'"]


def test_unbalanced_quote_is_a_usage_error():
	proc = run_cli(['-q', "echo 'oops"])
	assert proc.returncode == 2
	assert 'cannot parse command' in proc.stderr


def test_shell_flag_runs_pipelines():
	assert 'xbc' in run_cli(['--shell', 'echo abc | tr a x']).stdout


def _start_cli(*args):
	return subprocess.Popen([sys.executable, str(MULTICMD_PY), *args],
		stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def _read_pid(proc):
	'''The first output line that is only a number: the pid a test command printed'''
	for line in proc.stdout:
		if line.strip().isdigit():
			return int(line)
	raise AssertionError('the command printed no pid')


@pytest.mark.skipif(signal.getsignal(signal.SIGINT) == signal.SIG_IGN, reason='SIGINT is ignored here')
@pytest.mark.skipif(not sys.platform.startswith('linux'), reason='checks processes through /proc')
def test_ctrl_c_stops_the_commands():
	proc = _start_cli("sh -c 'sleep 30 & echo $!; echo started; wait'")
	child = _read_pid(proc)
	proc.send_signal(signal.SIGINT)
	assert proc.wait(timeout=10) == 130
	assert pid_gone(child)


@pytest.mark.skipif(not sys.platform.startswith('linux'), reason='checks processes through /proc')
def test_sigterm_stops_the_commands():
	proc = _start_cli("sh -c 'sleep 30 & echo $!; echo started; wait'")
	child = _read_pid(proc)
	proc.send_signal(signal.SIGTERM)
	assert proc.wait(timeout=10) == 128 + 15
	assert pid_gone(child)


@pytest.mark.skipif(shutil.which('nohup') is None, reason='needs nohup')
def test_nohup_keeps_the_commands_running_after_a_hangup():
	# nohup leaves SIGHUP ignored; mcmd must not replace that with its kill-everything handler
	proc = subprocess.Popen(['nohup', sys.executable, str(MULTICMD_PY), "sh -c 'echo $$; echo started; sleep 2'"],
		stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
	_read_pid(proc)
	proc.send_signal(signal.SIGHUP)
	assert proc.wait(timeout=10) == 0


@pytest.mark.skipif(signal.getsignal(signal.SIGINT) == signal.SIG_IGN, reason='SIGINT is ignored here')
def test_ctrl_c_in_a_sequential_run_skips_the_rest(tmp_path):
	marker = tmp_path / 'ran'
	proc = _start_cli("sh -c 'echo $$; echo started; sleep 30'", f'touch {marker}')
	_read_pid(proc)
	proc.send_signal(signal.SIGINT)
	assert proc.wait(timeout=10) == 130
	assert not marker.exists()
```

- [ ] **Step 2: Run the tests and watch them fail**

Run: `python3 -m pytest tests/test_cli.py -v`
Expected:
- The exit-status tests fail (they get 0).
- The help test fails.
- `-n` fails with "unrecognized arguments".
- The signal tests fail: exit 1 or -15, and the sleep survives.
- The `nohup` test passes already, since 1.51 installs no handlers. It guards against the new handler overriding `nohup`.

- [ ] **Step 3: Implement**

Replace `main()` and the `if __name__ == '__main__':` block right after it with:

```python
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
		if returncode is None:
			return 1
		return 128 - returncode if returncode < 0 else returncode
	return min(sum(1 for task in tasks if task.returncode not in (0, None)), 101)

def _install_cli_signal_handlers():
	'''
	Make SIGTERM / SIGHUP stop the commands too ( internal )
	The commands run in process groups of their own, so signals sent to mcmd's group ( e.g. by
	`timeout`, or a closed terminal ) would not reach them. A signal mcmd was started with
	ignored stays ignored: `nohup mcmd ...` must keep its commands running when the session ends.
	'''
	def handler(signum, frame):
		kill_all(signum)
		sys.exit(128 + signum)
	for name in ('SIGTERM', 'SIGHUP'):
		signum = getattr(signal, name, None)
		if signum is not None and signal.getsignal(signum) != signal.SIG_IGN:
			signal.signal(signum, handler)

def main():
	parser = argparse.ArgumentParser(description='Run multiple commands in parallel', epilog=_EXIT_STATUS_HELP,
		formatter_class=argparse.RawDescriptionHelpFormatter)
	parser.add_argument('commands', metavar='command', type=str, nargs='+',help='commands to run')
	parser.add_argument('-p','--parse', action='store_true',help='Parse ranged input and expand them into multiple commands')
	parser.add_argument('-t','--timeout', metavar='SECONDS', type=float, default=0,help='kill a command after SECONDS with no output at all ( 0 = never, the default )')
	parser.add_argument('-m','--max_threads', metavar='max_threads', type=int, default=1,help='maximum number of commands to run at once ( 0 = all )')
	parser.add_argument('--sudo', action='store_true',help='use sudo for commands')
	parser.add_argument('-q','--quiet', action='store_true',help='quiet mode: hide command output')
	parser.add_argument('-n','--dry-run', action='store_true',help='print each command, quoted, instead of running it')
	parser.add_argument('--shell', action='store_true',help='run each command through /bin/sh -c ( pipes, ;, > and globs work )')
	parser.add_argument('-V','--version', action='version', version=f'%(prog)s {version} @ {COMMIT_DATE} by pan@zopyr.us')
	args = parser.parse_args()
	# set_sudo() validates that sudo exists and that we are not already root
	# before enabling it, and updates the USE_SUDO global accordingly.
	set_sudo(args.sudo)
	_install_cli_signal_handlers()
	try:
		tasks = run_commands(args.commands, timeout=args.timeout, max_threads=args.max_threads,
			quiet=args.quiet and not args.dry_run, parse=args.parse, dry_run=args.dry_run,
			with_stdErr=True, return_object=True, shell=args.shell)
	except ValueError as e:
		print(f'mcmd: {e}', file=sys.stderr)
		return 2
	except KeyboardInterrupt:
		print('\nInterrupted', file=sys.stderr)
		return 130
	if args.dry_run:
		return 0
	return _exit_status(tasks)
```

Then add these as the last lines of the file, after `format_bytes`:

```python

if __name__ == '__main__':
	sys.exit(main())
```

- [ ] **Step 4: Run the tests on both interpreters**

Run: `python3 -m pytest tests/test_cli.py -v`
Expected: all pass, the existing CLI tests included.

Run the full suite on both interpreters.
Expected: no regressions.

- [ ] **Step 5: Commit**

```bash
git add multiCMD.py tests/test_cli.py
git commit -m "feat(cli): real exit status, no default timeout, --dry-run, --shell, signal handling

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 13: CLI input files `-f` and `-a`

**Files:**
- Modify: `multiCMD.py` (new `_read_input`, `_fill_templates` and `_cli_commands` before `main`; `main`)
- Create: `tests/test_cli_input.py`

**Interfaces:**
- Consumes: `_expand_ranges_fast(inStr, variables)`, `_split_command`.
- Produces:
  - `_read_input(path) -> list[str]`
  - `_fill_templates(templates, lines, shell=False) -> list[str | list[str]]`
  - `_cli_commands(args) -> list[str | list[str]]`
  - The CLI now expands ranges itself and calls `run_commands(parse=False)`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_cli_input.py`:

```python
import subprocess
import sys

from conftest import MULTICMD_PY, run_cli


def test_command_file_skips_blank_lines_and_comments(tmp_path):
	jobs = tmp_path / 'jobs.txt'
	jobs.write_text('echo one\n\n# a comment\n   # indented comment\necho two\n')
	assert run_cli(['-n', '-f', str(jobs)]).stdout.splitlines() == ['echo one', 'echo two']


def test_command_file_from_stdin():
	proc = subprocess.run([sys.executable, str(MULTICMD_PY), '-n', '-f', '-'],
		input='echo a\necho b\n', capture_output=True, text=True, timeout=30)
	assert proc.stdout.splitlines() == ['echo a', 'echo b']


def test_positional_commands_come_before_file_commands(tmp_path):
	jobs = tmp_path / 'jobs.txt'
	jobs.write_text('echo file\n')
	assert run_cli(['-n', 'echo arg', '-f', str(jobs)]).stdout.splitlines() == ['echo arg', 'echo file']


def test_command_files_run(tmp_path):
	jobs = tmp_path / 'jobs.txt'
	jobs.write_text('echo one\necho two\n')
	proc = run_cli(['-f', str(jobs)])
	assert proc.returncode == 0
	assert 'one' in proc.stdout and 'two' in proc.stdout


def test_arg_file_fills_templates_one_argument_per_line(tmp_path):
	hosts = tmp_path / 'hosts.txt'
	hosts.write_text('web 1\nweb2\n\n')
	assert run_cli(['-n', '-a', str(hosts), 'ping -c1 {}']).stdout.splitlines() == ["ping -c1 'web 1'", 'ping -c1 web2']


def test_arg_file_without_placeholder_appends_the_line(tmp_path):
	hosts = tmp_path / 'hosts.txt'
	hosts.write_text('web1\nweb2\n')
	assert run_cli(['-n', '-a', str(hosts), 'echo']).stdout.splitlines() == ['echo web1', 'echo web2']


def test_arg_file_lines_are_not_range_expanded(tmp_path):
	values = tmp_path / 'values.txt'
	values.write_text('[1-2]\n')
	assert run_cli(['-n', '-p', '-a', str(values), 'echo x[1-2] {}']).stdout.splitlines() == \
		["echo x1 '[1-2]'", "echo x2 '[1-2]'"]


def test_arg_file_in_shell_mode_quotes_each_line(tmp_path):
	marker = tmp_path / 'pwned'
	values = tmp_path / 'values.txt'
	values.write_text(f'a; touch {marker}\n')
	proc = run_cli(['--shell', '-a', str(values), 'echo {}'])
	assert proc.returncode == 0
	assert not marker.exists()
	assert f'a; touch {marker}' in proc.stdout


def test_input_files_with_crlf_line_endings(tmp_path):
	hosts = tmp_path / 'hosts.txt'
	hosts.write_bytes(b'web1\r\nweb2\r\n')
	jobs = tmp_path / 'jobs.txt'
	jobs.write_bytes(b'echo a\r\n')
	assert run_cli(['-n', '-a', str(hosts), 'echo {}']).stdout.splitlines() == ['echo web1', 'echo web2']
	assert run_cli(['-n', '-f', str(jobs)]).stdout.splitlines() == ['echo a']


def test_both_inputs_from_stdin_is_a_usage_error():
	assert run_cli(['-f', '-', '-a', '-', 'echo {}']).returncode == 2


def test_arg_file_needs_a_template(tmp_path):
	hosts = tmp_path / 'hosts.txt'
	hosts.write_text('web1\n')
	assert run_cli(['-a', str(hosts)]).returncode == 2


def test_no_commands_is_a_usage_error():
	assert run_cli([]).returncode == 2


def test_empty_command_file_is_a_usage_error(tmp_path):
	jobs = tmp_path / 'jobs.txt'
	jobs.write_text('# nothing here\n\n')
	proc = run_cli(['-f', str(jobs)])
	assert proc.returncode == 2
	assert 'no commands to run' in proc.stderr


def test_missing_input_file_is_an_error(tmp_path):
	proc = run_cli(['-f', str(tmp_path / 'nope.txt')])
	assert proc.returncode == 2
	assert 'nope.txt' in proc.stderr
```

- [ ] **Step 2: Run the tests and watch them fail**

Run: `python3 -m pytest tests/test_cli_input.py -v`
Expected: FAIL with argparse "unrecognized arguments: -f" / "-a" (exit 2), and the outputs don't match.

- [ ] **Step 3: Implement**

Before `main`, add:

```python
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
	shell-quoted into the template string instead.
	@params:
		templates: The command templates ( list[str] )
		lines: The input lines ( list[str] )
		shell: Whether the commands run through the system shell
	@returns:
		list[str | list[str]]: Command strings in shell mode, argument lists otherwise
	'''
	commands = []
	for template in templates:
		argv = None if shell else _split_command(template)
		for line in lines:
			if shell:
				quoted = shlex.quote(line)
				commands.append(template.replace('{}', quoted) if '{}' in template else template + ' ' + quoted)
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
	return commands
```

In `main`:

1. Change the positional argument to:

```python
	parser.add_argument('commands', metavar='command', type=str, nargs='*',help='commands to run ( with -a: templates, {} is replaced by each input line )')
```

2. After the `--shell` argument, add:

```python
	parser.add_argument('-f','--file', metavar='FILE', action='append',help='read commands from FILE, one per line ( - for stdin; blank lines and # comments are skipped )')
	parser.add_argument('-a','--arg-file', metavar='FILE', action='append',help='run each command template once per line of FILE, with {} replaced by the line ( - for stdin )')
```

3. Replace everything from `args = parser.parse_args()` to the end of `main` with:

```python
	args = parser.parse_args()
	if not args.commands and not args.file:
		parser.error('no commands given')
	if args.arg_file and not args.commands:
		parser.error('-a/--arg-file needs at least one command template')
	if '-' in (args.file or []) and '-' in (args.arg_file or []):
		parser.error('-f - and -a - cannot both read stdin')
	# set_sudo() validates that sudo exists and that we are not already root
	# before enabling it, and updates the USE_SUDO global accordingly.
	set_sudo(args.sudo)
	_install_cli_signal_handlers()
	try:
		commands = _cli_commands(args)
		if not commands:
			print('mcmd: no commands to run', file=sys.stderr)
			return 2
		tasks = run_commands(commands, timeout=args.timeout, max_threads=args.max_threads,
			quiet=args.quiet and not args.dry_run, dry_run=args.dry_run,
			with_stdErr=True, return_object=True, shell=args.shell)
	except (OSError, ValueError) as e:
		print(f'mcmd: {e}', file=sys.stderr)
		return 2
	except KeyboardInterrupt:
		print('\nInterrupted', file=sys.stderr)
		return 130
	if args.dry_run:
		return 0
	return _exit_status(tasks)
```

- [ ] **Step 4: Run the tests on both interpreters**

Run: `python3 -m pytest tests/test_cli_input.py tests/test_cli.py -v`
Expected: all pass.

Run the full suite on both interpreters.
Expected: no regressions.

- [ ] **Step 5: Commit**

```bash
git add multiCMD.py tests/test_cli_input.py
git commit -m "feat(cli): read commands from files or stdin (-f) and fill {} templates (-a)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 14: CLI summary and output flags

**Files:**
- Modify: `multiCMD.py` (new `_RETURNCODE_LABELS`, `_format_returncode` and `_summary` before `main`; `main`)
- Create: `tests/test_cli_summary.py`

**Interfaces:**
- Consumes: `pretty_format_table`, `Task.duration`, `_quote_command`, `_display.message`, `run_commands(prefix=, color=, pty=)`.
- Produces:
  - `_format_returncode(returncode) -> str`
  - `_summary(tasks, show_all=False) -> str` (no trailing newline)
  - CLI flags `--summary` / `--no-summary`, `--no-prefix`, `--no-color`, `--pty` / `--no-pty`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_cli_summary.py`:

```python
import os
import shlex
import shutil
import subprocess
import sys

import pytest

from conftest import MULTICMD_PY, run_cli


def _rows(stdout):
	'''The summary table's data rows, as lists of cells'''
	rows = []
	for line in stdout.splitlines():
		cells = [cell.strip() for cell in line.split('|')]
		if len(cells) == 4 and cells[0].isdigit():
			rows.append(cells)
	return rows


def test_summary_lists_failed_commands_and_a_tally():
	proc = run_cli(['-q', 'true', 'false'])
	assert proc.returncode == 1
	assert [row[:3] for row in _rows(proc.stdout)] == [['2', 'false', '1']]
	assert proc.stdout.splitlines()[-1] == '2 commands: 1 succeeded, 1 failed'


def test_summary_when_everything_succeeded_is_one_line():
	proc = run_cli(['-q', 'true', 'true'])
	assert _rows(proc.stdout) == []
	assert proc.stdout.splitlines() == ['2 commands: all succeeded']


def test_summary_flag_lists_every_command():
	proc = run_cli(['-q', '--summary', 'true', 'false'])
	assert [row[:3] for row in _rows(proc.stdout)] == [['1', 'true', '0'], ['2', 'false', '1']]


def test_no_summary_flag():
	assert 'commands:' not in run_cli(['-q', '--no-summary', 'true', 'false']).stdout


def test_no_summary_for_one_command_or_a_dry_run():
	assert 'commands:' not in run_cli(['-q', 'false']).stdout
	assert 'commands:' not in run_cli(['-n', 'true', 'false']).stdout


@pytest.mark.slow
def test_summary_labels_return_codes():
	proc = run_cli(['-q', '-t', '1', 'sleep 5', 'no-such-command-xyz', "sh -c 'kill -TERM $$'", 'true'])
	assert proc.returncode == 3
	assert [row[:3] for row in _rows(proc.stdout)] == [
		['1', 'sleep 5', '124 (timeout)'],
		['2', 'no-such-command-xyz', '127 (not found)'],
		['3', "sh -c 'kill -TERM $$'", '-15 (SIGTERM)'],
	]
	assert proc.stdout.splitlines()[-1] == '4 commands: 1 succeeded, 3 failed'


def test_prefix_only_for_concurrent_runs():
	assert '[1] a' in run_cli(['-m', '2', 'echo a', 'echo b']).stdout
	assert '[1]' not in run_cli(['-m', '2', '--no-prefix', 'echo a', 'echo b']).stdout
	assert '[1]' not in run_cli(['-m', '1', 'echo a', 'echo b']).stdout
	assert '[1]' not in run_cli(['-m', '4', 'echo a']).stdout


@pytest.mark.skipif(os.name != 'posix', reason='pty mode is POSIX only')
def test_pty_flags():
	check = "sh -c '[ -t 1 ] && echo tty || echo pipe'"
	assert 'tty' in run_cli(['--pty', check]).stdout
	# run_cli's stdout is a pipe, so pty mode stays off by default
	assert 'pipe' in run_cli([check]).stdout
	assert 'pipe' in run_cli(['--no-pty', check]).stdout


@pytest.mark.skipif(shutil.which('script') is None or not sys.platform.startswith('linux'),
	reason='needs util-linux script for a terminal')
def test_colors_on_a_terminal_and_the_no_color_flag():
	env = {key: value for key, value in os.environ.items() if key != 'NO_COLOR'}
	def under_terminal(*args):
		command = ' '.join(shlex.quote(arg) for arg in [sys.executable, str(MULTICMD_PY), *args])
		return subprocess.run(['script', '-qec', command, '/dev/null'], stdin=subprocess.DEVNULL,
			capture_output=True, text=True, timeout=60, env=env).stdout
	assert '\x1b[38;2;' in under_terminal('-m', '2', 'echo a', 'echo b')
	assert '\x1b[38;2;' not in under_terminal('--no-color', '-m', '2', 'echo a', 'echo b')
```

- [ ] **Step 2: Run the tests and watch them fail**

Run: `python3 -m pytest tests/test_cli_summary.py -v`
Expected: FAIL. There's no summary or tally, and the new flags are unrecognized.

- [ ] **Step 3: Implement**

Before `main`, add:

```python
_RETURNCODE_LABELS = {124: 'timeout', 126: 'cannot execute', 127: 'not found'}

def _format_returncode(returncode):
	'''
	A return code with its meaning, for the summary ( internal )
	'''
	if returncode is None:
		return 'not run'
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
			data.append([str(index), _quote_command(task.command), _format_returncode(task.returncode),
				'-' if duration is None else f'{duration:.2f}s'])
		table = pretty_format_table(data)
	if failed:
		tally = f'{len(tasks)} commands: {len(tasks) - len(failed)} succeeded, {len(failed)} failed'
	else:
		tally = f'{len(tasks)} commands: all succeeded'
	return table + tally
```

In `main`:

1. Change the `-q` help to `'quiet mode: hide command output ( the summary still shows )'`.
2. After the `-a/--arg-file` argument, add:

```python
	summary = parser.add_mutually_exclusive_group()
	summary.add_argument('--summary', dest='summary', action='store_const', const='all',help='list every command in the end-of-run summary ( default: only failed ones )')
	summary.add_argument('--no-summary', dest='summary', action='store_const', const='none',help='do not print the end-of-run summary')
	parser.add_argument('--no-prefix', action='store_true',help='do not prefix output lines with [#] when commands run at once')
	parser.add_argument('--no-color', action='store_true',help='do not color output lines per command')
	pty_mode = parser.add_mutually_exclusive_group()
	pty_mode.add_argument('--pty', dest='pty', action='store_true',help='give commands a pseudo-terminal so they print live ( default when stdout is a terminal )')
	pty_mode.add_argument('--no-pty', dest='pty', action='store_false',help='give commands plain pipes')
	parser.set_defaults(summary='failed', pty=None)
```

3. Replace the `tasks = run_commands(...)` call with:

```python
		use_pty = args.pty if args.pty is not None else (_stdout_isatty() and not args.quiet and not args.dry_run)
		tasks = run_commands(commands, timeout=args.timeout, max_threads=args.max_threads,
			quiet=args.quiet and not args.dry_run, dry_run=args.dry_run,
			with_stdErr=True, return_object=True, shell=args.shell,
			prefix=not args.no_prefix, color=False if args.no_color else None, pty=use_pty)
```

4. Replace the final `return _exit_status(tasks)` with:

```python
	if len(tasks) > 1 and args.summary != 'none':
		_display.message(_summary(tasks, show_all=args.summary == 'all'))
	return _exit_status(tasks)
```

- [ ] **Step 4: Run the tests on both interpreters**

Run: `python3 -m pytest tests/test_cli_summary.py tests/test_cli.py tests/test_cli_input.py -v`
Expected: all pass. `run_cli` runs `multiCMD.py` directly, so these tests also prove that the `__main__` move left `pretty_format_table` defined in time.

Run the full suite on both interpreters.
Expected: no regressions.

- [ ] **Step 5: Commit**

```bash
git add multiCMD.py tests/test_cli_summary.py
git commit -m "feat(cli): end-of-run summary, [#] prefixes, color and pty flags

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 15: Windows best-effort branches

**Files:**
- Modify: `multiCMD.py` (`ping`, `input_with_timeout_and_countdown`)
- Create: `tests/test_windows.py`

**Interfaces:**
- Consumes: `_IS_POSIX`, `_spawn`, `_kill_commands`.
- Produces:
  - `ping` uses `-n 1` when not POSIX.
  - `input_with_timeout_and_countdown` polls `msvcrt.kbhit()` when `msvcrt` can be imported.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_windows.py`:

```python
import ast
import builtins
import subprocess
import sys
import types

import multiCMD
from conftest import PACKAGE_ROOT


def _capture_commands(monkeypatch):
	seen = []
	def fake_run_commands(commands, **kwargs):
		seen.extend(commands)
		return [0] * len(commands)
	monkeypatch.setattr(multiCMD, 'run_commands', fake_run_commands)
	return seen


def test_ping_uses_dash_n_on_windows(monkeypatch):
	seen = _capture_commands(monkeypatch)
	monkeypatch.setattr(multiCMD, '_IS_POSIX', False)
	assert multiCMD.ping(['h1', 'h2']) == [True, True]
	assert seen == ['ping -n 1 h1', 'ping -n 1 h2']


def test_ping_uses_dash_c_on_posix(monkeypatch):
	seen = _capture_commands(monkeypatch)
	multiCMD.ping('h1')
	assert seen == ['ping -c 1 h1']


class FakeProc:
	'''A Windows Popen: terminate() and kill(), no POSIX signals'''
	def __init__(self, dies_on_terminate):
		self.dies_on_terminate = dies_on_terminate
		self.calls = []
		self.returncode = None

	def poll(self):
		return self.returncode

	def terminate(self):
		self.calls.append('terminate')
		if self.dies_on_terminate:
			self.returncode = 1

	def kill(self):
		self.calls.append('kill')
		self.returncode = 9

	def wait(self, timeout=None):
		if self.returncode is None:
			raise subprocess.TimeoutExpired('fake', timeout)
		return self.returncode

	def send_signal(self, sig):
		raise AssertionError('no POSIX signals on Windows')


def test_kill_commands_on_windows_terminates_then_kills(monkeypatch):
	monkeypatch.setattr(multiCMD, '_IS_POSIX', False)
	polite, stubborn = FakeProc(True), FakeProc(False)
	multiCMD._kill_commands([(polite, None), (stubborn, None)], 2)
	assert polite.calls == ['terminate']
	assert stubborn.calls == ['terminate', 'kill']


def test_spawn_on_windows_has_no_group(monkeypatch):
	monkeypatch.setattr(multiCMD, '_IS_POSIX', False)
	proc, pgid = multiCMD._spawn(['true'], subprocess.DEVNULL, subprocess.DEVNULL)
	proc.wait(timeout=10)
	assert pgid is None


def test_input_countdown_uses_msvcrt_when_present(monkeypatch):
	monkeypatch.setitem(sys.modules, 'msvcrt', types.SimpleNamespace(kbhit=lambda: True))
	monkeypatch.setattr(builtins, 'input', lambda: ' yes ')
	assert multiCMD.input_with_timeout_and_countdown(3, 'pick') == 'yes'


def test_posix_only_modules_are_imported_lazily():
	tree = ast.parse((PACKAGE_ROOT / 'multiCMD.py').read_text())
	top_level = set()
	for node in tree.body:
		if isinstance(node, ast.Import):
			top_level.update(alias.name for alias in node.names)
		elif isinstance(node, ast.ImportFrom):
			top_level.add(node.module)
	assert not top_level & {'pty', 'termios', 'fcntl', 'msvcrt'}
```

- [ ] **Step 2: Run the tests and watch them fail**

Run: `python3 -m pytest tests/test_windows.py -v`
Expected:
- `test_ping_uses_dash_n_on_windows` fails: it builds `-c`.
- The `msvcrt` test fails or hangs for up to 3 s, then returns `None`.
- The other tests pass already (the code exists from Tasks 1–2).

- [ ] **Step 3: Implement**

In `ping`, replace the two command-building lines:

```python
	# Windows ping counts with -n
	count = '-c' if _IS_POSIX else '-n'
	single_host = False
	if isinstance(hosts,str):
		commands = [f'ping {count} 1 {hosts}']
		single_host = True
	else:
		commands = [f'ping {count} 1 {host}' for host in hosts]
```

(This replaces the existing `single_host = False` / `if isinstance(hosts,str):` block, which built `ping -c 1 ...`.)

In `input_with_timeout_and_countdown`, replace the body from `# Print the initial prompt with the countdown` to the end with:

```python
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
```

- [ ] **Step 4: Run the tests on both interpreters**

Run: `python3 -m pytest tests/test_windows.py tests/test_ping.py -v`
Expected: all pass (the ping tests skip without a working `ping`).

Run the full suite on both interpreters.
Expected: no regressions.

- [ ] **Step 5: Commit**

```bash
git add multiCMD.py tests/test_windows.py
git commit -m "fix(windows): best-effort ping and input countdown; cover the kill and spawn fallbacks

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 16: README

**Files:**
- Modify: `README.md`

**Interfaces:**
- Consumes: the finished behaviour from Tasks 1–15.
- Produces: user documentation. No code.

- [ ] **Step 1: Replace the Features list**

Replace the bullets under `## Features` with:

```markdown
- Run a batch of commands in parallel with a bounded pool of worker threads.
- Live output that stays readable in parallel runs: `[#]` prefixes, per-command colors on a terminal, and progress lines that redraw in place.
- Capture `stdout`, `stderr`, return code and timing per command via the `Task` object.
- Per-command **inactivity timeout** that kills the command and everything it started (see [Timeout semantics](#timeout-semantics)).
- Range/pattern expansion: `host[1-3]`, `[01-03]`, `[a-f]`, `[A-C]`, `[1-2,a-b]`, variables, and `{...}` expressions.
- Commands from files or stdin (`-f`), `{}` templates filled from a list (`-a`), a `--dry-run` preview, and an end-of-run summary.
- pty mode, so commands print live instead of in buffered bursts.
- Fire-and-forget async mode (`wait_for_return=False`) plus an `AsyncExecutor` for managing long-lived batches.
- Optional `sudo` wrapping, and an opt-in shell mode for pipes and redirects.
- No third-party runtime dependencies; requires Python >= 3.6.
```

- [ ] **Step 2: Replace the CLI help block**

Run: `python3 multiCMD.py -h`. Replace the contents of the `bash` code block under `## Command-line usage` (the 1.51 `$ mcmd -h` text) with `$ mcmd -h` followed by that exact output, with `multiCMD.py` changed to `mcmd` in the usage line.

- [ ] **Step 3: Update the examples**

Replace the `mcmd -t 30 -m 16 "long-running-task [1-100]"` example's heading with "Kill any command that goes silent for more than 30 seconds (along with everything it started):". After the sudo example, add:

````markdown
Preview what a range expands to without running anything:

```bash
mcmd -n -p "ping -c1 192.168.1.[1-5]"
```

Run one command per host from a file, 16 at a time:

```bash
mcmd -m 16 -a hosts.txt "ssh {} uptime"
```

Run a list of jobs from stdin:

```bash
generate_jobs | mcmd -m 8 -f -
```

Use pipes and redirects:

```bash
mcmd --shell -m 4 "dmesg | tail -n 5" "journalctl -n 5 > /tmp/j.log"
```
````

Replace the blockquote under the examples with:

```markdown
> Each positional argument is one command. It is split into arguments with shell
> quoting rules (`shlex`), but not run through a shell unless you pass `--shell`.
> With `-m/--max_threads > 1`, at most that many commands run at once, each with two
> threads draining its output. `stdin` is connected to `/dev/null` — multiCMD does
> not feed input to commands.
```

- [ ] **Step 4: Add the new CLI sections after the examples**

````markdown
### Exit status

| Situation | Exit code |
|---|---|
| Every command succeeded, or `--dry-run` | `0` |
| Exactly one command (after `-p` expansion and input files) | its own exit code; killed by signal N → `128+N` |
| Several commands | the number that failed, capped at `101` |
| Usage error (bad option, unbalanced quote, no commands) | `2` |
| Ctrl-C | `130` (`128+N` after SIGTERM / SIGHUP) |

### Output in parallel runs

When more than one command can run at once, each output line starts with `[#]` (the
command's position, the same `#` the summary shows), and on a terminal each command
gets its own color. `--no-prefix` and `--no-color` (or the `NO_COLOR` environment
variable) turn them off. A command's `\r` progress line redraws in place; if another
command printed in between, the update starts a new line instead of overwriting it.

When the run ends with more than one command, mcmd prints a summary: a table of the
failed commands (all of them with `--summary`) and a tally line. `-q` hides command
output but keeps the summary; `--no-summary` hides it too.

### pty mode

Many programs buffer their output in 4–8 KB blocks when it goes to a pipe, so they
look stuck and then print in bursts. When mcmd's own output is a terminal (and `-q` is
not set), commands get a pseudo-terminal instead, so they print line by line.
stdout and stderr are then merged, programs may print colors and progress bars, and
`PAGER`/`GIT_PAGER` are set to `cat` so nothing waits for a keypress. Use `--no-pty` to
turn it off, or `--pty` to force it. In Python, pass `pty=True`. POSIX only.
````

- [ ] **Step 5: Update range expansion, the Python API, timeouts and signals**

In the range table, add these rows:

```markdown
| `host[A-C]`          | `hostA`, `hostB`, `hostC` (case kept)       |
| `n[08-0b]`           | `n08`, `n09`, `n0a`, `n0b` (hex, padded)    |
| `[web1\:8080]`       | `web1:8080` (`\:` is a literal colon)        |
```

Add these notes:

```markdown
- Variables are shared by the commands of one call (one `mcmd` run, one `run_commands`
  call) and never leak into the next.
- `name:value` is an assignment only with exactly one `:` and a valid name before it;
  IPv6 addresses like `[fe80::1]` pass through, and `\:` gives a literal colon.
```

After the "Getting return codes" example, add:

````markdown
### Shell mode, prefixes, colors and pty

```python
multiCMD.run_command("dmesg | tail -n 3", shell=True)            # run through /bin/sh -c
multiCMD.run_commands(cmds, max_threads=8, prefix=True)           # [#] on each line
multiCMD.run_commands(cmds, max_threads=8, color=False)           # never color
multiCMD.run_command(["python3", "train.py"], pty=True)           # live output, stderr merged
```

`raise_error=True` raises `multiCMD.CommandError`; its `.tasks` holds the failed `Task`
objects and its message lists each one with its return code and last error line.
`Task` also records `start_time`, `end_time` and `duration`.
````

Replace the whole `## Timeout semantics` section with:

```markdown
## Timeout semantics

`timeout` is an **inactivity timeout**, not a maximum total runtime. A command is
killed once it has produced **no output at all** on stdout or stderr for `timeout`
seconds. Any byte counts, including a progress line redrawn with `\r` and a partial
line without a newline.

On timeout, the command **and everything it started** are killed, the task's return
code is set to `124`, and `Timeout!` is appended to its stderr. `timeout=0` (the
default, in the API and on the command line) disables it.

## Process groups and signals

Every command runs in a process group of its own, so a timeout, `stop()` or Ctrl-C
reaches the whole process tree, not just the first process. On Python 3.11+ this uses
`Popen(process_group=0)`; older Pythons start the command through a tiny exec wrapper.
Commands that try to read the terminal (a password prompt, for example) fail at once
instead of hanging.

Because the commands are not in your program's process group, signals sent to that
group do not reach them. multiCMD handles this for you: a waiting call that gets Ctrl-C
stops its commands before re-raising, `mcmd` stops them on SIGTERM / SIGHUP, and any
commands still running when the interpreter exits are killed. A program that handles
SIGTERM itself can call `multiCMD.kill_all()`.
```

In `## Key functions and objects`, replace the code block with:

```python
run_command(command, timeout=0, max_threads=1, quiet=False, dry_run=False,
            with_stdErr=False, return_code_only=False, return_object=False,
            wait_for_return=True, sem=None, use_sudo=..., raise_error=False,
            prefix=False, color=None, pty=False, shell=False)

run_commands(commands, timeout=0, max_threads=1, quiet=False, dry_run=False,
             with_stdErr=False, return_code_only=False, return_object=False,
             parse=False, wait_for_return=True, sem=None, use_sudo=...,
             raise_error=False, prefix=False, color=None, pty=False, shell=False)

ping(hosts, timeout=1, max_threads=0, ...)   # returns True/False reachability
join_threads(threads=..., timeout=None)      # join fire-and-forget threads
kill_all(sig=None)                           # stop every running command and what it started
set_sudo(use_sudo)                           # enable/disable sudo globally

class Task:          # command, returncode, stdout, stderr, start_time, end_time, duration
class CommandError:  # raised by raise_error=True; .tasks = the failed Tasks
class AsyncExecutor: # run_command(s), wait, join, stop, cleanup, get_results, get_return_codes
```

In the `AsyncExecutor` paragraph, add after the first sentence of its explanation: "`wait()`, `join()` and `stop()` wait until everything is done by default; a `timeout=` is one overall limit in seconds."

Before `## Development`, add:

```markdown
## Platform support

Linux and macOS are fully supported. Windows is best-effort and untested: commands run
and time out, but a timeout or stop only reaches the first process, and pty mode is
not available.
```

- [ ] **Step 6: Check the README's commands and API names against the code**

Run:

```bash
python3 multiCMD.py -n -p "ping -c1 192.168.1.[1-5]"
python3 -c "import multiCMD; print(multiCMD.CommandError, multiCMD.kill_all, multiCMD.Task(['x']).duration)"
```

Expected: five `ping -c1 192.168.1.N` lines, then `<class 'multiCMD.CommandError'> <function kill_all ...> None`.

- [ ] **Step 7: Commit**

```bash
git add README.md
git commit -m "docs: document 1.52 timeouts, process groups, CLI flags, exit status and pty mode

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 17: Version 1.52, regenerate the embeds, full verification

**Files:**
- Modify: `multiCMD.py` (`version`, `COMMIT_DATE`)
- Regenerate: `multiCMD_min.py`, `multiCMD_min_lzma_a85.py`

**Interfaces:**
- Consumes: everything above.
- Produces: the release artifacts. The embed snippet's gate reads `< (1, 52)`.

- [ ] **Step 1: Bump the version**

In `multiCMD.py`, set `version = '1.52'`, and set `COMMIT_DATE` to the date of this commit, in `YYYY-MM-DD` form.

- [ ] **Step 2: Check Python 3.6 syntax compatibility**

Run: `uvx vermin -t=3.6- --no-tips --violations multiCMD.py; echo "exit=$?"`
Expected: `Minimum required versions: 3.6` and `exit=0`, the same as the 1.51 baseline. If it reports a newer requirement, fix that construct; don't raise the floor.

- [ ] **Step 3: Regenerate the minified module and the embed snippet**

Run: `uv run --no-project --python 3.11 --with python-minifier==3.4.0 python compressedEmbeddedCodeGenerator.py multiCMD.py`
Expected: it prints `multiCMD 1.52`, then `Wrote .../multiCMD_min.py` and `Wrote .../multiCMD_min_lzma_a85.py`.

Run: `grep -n "(1, 52)" multiCMD_min_lzma_a85.py`
Expected: one hit, on the version-gate line.

- [ ] **Step 4: Smoke-test the minified module**

Run: `python3 -c "import multiCMD_min as m; print(m.version, m.run_command(['echo', 'ok'], quiet=True), m.run_command('echo a | tr a b', shell=True, quiet=True))"`
Expected: `1.52 ['ok'] ['b']`.

- [ ] **Step 5: Run the full suite on both interpreters, slow tests included**

Run: `python3 -m pytest -q` and `uv run --no-project --python 3.9 --with pytest python -m pytest -q`
Expected: all pass on both, with only environment-dependent skips (sudo, ping, `script`).

- [ ] **Step 6: Record the reader speed-up**

Run:

```bash
python3 - <<'EOF'
import time, multiCMD
start = time.time()
out = multiCMD.run_command(['seq', '1', '3000000'], quiet=True)
print(len(out), 'lines in %.2fs' % (time.time() - start))
EOF
```

Expected: `3000000 lines in` about 0.1–0.3 s, against 1.45 s for 1.51. Note the number for the commit message.

- [ ] **Step 7: Commit**

```bash
git add multiCMD.py multiCMD_min.py multiCMD_min_lzma_a85.py
git commit -m "chore: bump multiCMD to 1.52 and regenerate multiCMD_min and the embed snippet

Reader throughput (seq 1 3000000): <measured>s, was 1.45s in 1.51.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

Replace `<measured>` with the number from Step 6.

---

### Task 18: Drop the `argparse` dependency in seven packages

**Files:**
- Modify: `multiCMD/setup.py`, and the script header of `multiCMD/multiCMD.py` (lines 2–7)
- Modify: `../multiSSH3/setup.py`, `../TSVZ/setup.py`, `../hpcp/setup.py`, `../statblk/setup.py`, `../statbtrfs/setup.py`
- Modify: `../simple-iotest/pyproject.toml` (and `uv.lock`)

**Interfaces:**
- Consumes: nothing.
- Produces: packages that no longer pull the 2015 PyPI `argparse` backport.

- [ ] **Step 1: multiCMD**

In `setup.py`, delete these lines:

```python
    install_requires=[
        'argparse',
    ],
```

In `multiCMD.py`, replace the script header's dependency list:

```python
# dependencies = [
#     "argparse",
# ]
```

with:

```python
# dependencies = []
```

Run: `python3 -m pytest -q`
Expected: all pass.

Then commit:

```bash
git add setup.py multiCMD.py
git commit -m "chore: drop the PyPI argparse dependency (stdlib since Python 3.2)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

If `multiCMD.py`'s header change causes a diff in the minified files, rerun Task 17 Step 3 and include them in this commit.

- [ ] **Step 2: The five other `setup.py` packages**

For each of `multiSSH3`, `TSVZ`, `hpcp`, `statblk` and `statbtrfs`, inside `/mnt/klein/work/cas/<repo>`:

1. Run `git status --short` and confirm the tree is clean.
2. Run `git switch -c multicmd-1.52`.
3. Edit `setup.py`:
   - **multiSSH3, hpcp, statblk, statbtrfs:** delete only the line containing `'argparse',`.
   - **TSVZ:** delete the whole block below, which has no other dependency:

     ```python
         install_requires=[
             'argparse',
         ],
     ```

4. Run `python3 setup.py --version`. Expected: the package's version, with no error.
5. Commit:

```bash
git add setup.py
git commit -m "chore: drop the PyPI argparse dependency (stdlib since Python 3.2)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 3: simple-iotest**

In `/mnt/klein/work/cas/simple-iotest`:
1. Confirm a clean tree.
2. Run `git switch -c multicmd-1.52`.
3. Delete the line `    "argparse",` from `dependencies` in `pyproject.toml`.
4. Run `uv lock`. If the index is unreachable, run `uv lock --offline`.
5. Run `uv lock --check`. Expected: exit 0.
6. Commit:

```bash
git add pyproject.toml uv.lock
git commit -m "chore: drop the PyPI argparse dependency (stdlib since Python 3.2)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 19: Re-embed multiCMD 1.52 into hpcp, statblk and statbtrfs

**Files:**
- Modify: `../hpcp/hpcp.py`, `../statblk/statblk.py`, `../statbtrfs/statbtrfs.py` (the block from `try:\n\timport multiCMD  # type: ignore` through the `exec(lzma.decompress(...), multiCMD.__dict__)` line)

**Interfaces:**
- Consumes: `multiCMD/multiCMD_min_lzma_a85.py` from Task 17.
- Produces: tools whose embedded fallback is multiCMD 1.52.

- [ ] **Step 1: Replace the embedded block in all three files**

Run, from `/mnt/klein/work/cas`, on the `multicmd-1.52` branches created in Task 18:

```bash
python3 - <<'EOF'
from pathlib import Path
snippet = Path('multiCMD/multiCMD_min_lzma_a85.py').read_text()
end_marker = 'exec(lzma.decompress(base64.a85decode(_SRC_B85)).decode("utf-8"), multiCMD.__dict__)\n'
for name in ('hpcp/hpcp.py', 'statblk/statblk.py', 'statbtrfs/statbtrfs.py'):
	path = Path(name)
	text = path.read_text()
	start = text.index('try:\n\timport multiCMD  # type: ignore\n')
	end = text.index(end_marker, start) + len(end_marker)
	path.write_text(text[:start] + snippet + text[end:])
	print(name, 'updated')
EOF
```

Expected: three `updated` lines.

- [ ] **Step 2: Check that each embedded copy decodes to 1.52**

Run from `/mnt/klein/work/cas`:

```bash
for f in hpcp/hpcp.py statblk/statblk.py statbtrfs/statbtrfs.py; do
python3 -S - "$f" <<'EOF'
import sys
src = open(sys.argv[1]).read()
start = src.index('try:\n\timport multiCMD')
end = src.index('multiCMD.__dict__)\n', start) + len('multiCMD.__dict__)\n')
ns = {}
exec(src[start:end], ns)
print(sys.argv[1], ns['multiCMD'].version, ns['multiCMD'].run_command(['echo', 'ok'], quiet=True))
EOF
done
```

Expected: each file prints `1.52 ['ok']`. `-S` keeps any installed multiCMD out of the way, so the embedded copy is the one being tested.

- [ ] **Step 3: Smoke-test each tool on the embedded copy**

Run: `python3 -S hpcp/hpcp.py -V; python3 -S statblk/statblk.py -V; python3 -S statbtrfs/statbtrfs.py -V`
Expected: each prints its own version string with no traceback.

- [ ] **Step 4: Commit in each repo**

For each of `hpcp`, `statblk` and `statbtrfs`, inside its directory:

```bash
git add <tool>.py
git commit -m "chore: embed multiCMD 1.52

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

The tools' own versions are not bumped, and nothing is published or pushed. Releasing them, and the `cas-toolbox` pins, are separate steps that need the maintainer's go-ahead.
