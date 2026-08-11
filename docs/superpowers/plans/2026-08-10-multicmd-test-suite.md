# multiCMD Complete Test Suite Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the single-file smoke suite with a split, full-stack pytest suite covering every exportable multiCMD surface plus named regressions for recent fixes.

**Architecture:** Behavior-first tests under `tests/` call the real `multiCMD` module and real subprocesses (`echo`, `sleep`, `false`, `cat`). Optional capabilities (`sudo`, `ping`) use markers + `skipif`. Harness fixtures restore sudo globals, clear range-expansion variables, and join leftover threads. No production code changes unless a test exposes a clear bug (stop and confirm with the user before fixing).

**Tech Stack:** Python ≥3.6 (package floor), pytest, stdlib `subprocess`/`threading`/`time`. No new test dependencies.

**Spec:** `docs/superpowers/specs/2026-08-10-multicmd-test-suite-design.md`

## Global Constraints

- Work only inside the `multiCMD/` git repo (package root).
- Do not modify `multiCMD.py` unless a test reveals a clear bug — then stop and ask before changing production code.
- Do not test `multiCMD_min.py`, `multiCMD_min_lzma_a85.py`, or `compressedEmbeddedCodeGenerator.py`.
- Leave `test_expand_ranges_perf.py` at repo root; exclude it via `testpaths = tests`.
- Prefer real subprocesses; mock only to force env states (e.g. `SUDO_PATH = None`).
- Assert current documented behavior; do not invent “should” semantics for undefined edges.
- Markers: `sudo`, `ping`, `slow`. Quick lane: `pytest -m "not slow"`.
- Commit after each task with a focused message.

---

## File Structure

| Path | Responsibility |
|---|---|
| `pytest.ini` | `testpaths`, `pythonpath`, marker registration |
| `tests/conftest.py` | sudo global restore, `__variables` clear, `join_threads` cleanup, `run_cli` helper, skip predicates |
| `tests/test_expand_ranges.py` | `_expand_ranges` / alias / README matrix |
| `tests/test_run_command.py` | single-command API |
| `tests/test_run_commands.py` | batch API, threads, parse, `sem`, `raise_error` |
| `tests/test_async_executor.py` | `AsyncExecutor` + `join_threads` |
| `tests/test_task.py` | `Task` object surface |
| `tests/test_sudo.py` | `set_sudo` / `use_sudo` (marked) |
| `tests/test_ping.py` | `ping` (marked) |
| `tests/test_cli.py` | CLI subprocess smoke |
| `tests/test_helpers.py` | formatting / terminal helpers |
| `tests/test_regressions.py` | named bug-fix locks |
| `test_multiCMD.py` | **delete** after port (Task 10) |
| `README.md` | Development blurb update (Task 10) |

---

### Task 1: Pytest harness

**Files:**
- Create: `pytest.ini`
- Create: `tests/conftest.py`
- Create: `tests/test_harness_smoke.py` (temporary smoke; deleted in Task 10 or absorbed — keep until Task 2 lands and then delete in Task 2 Step 1)

**Interfaces:**
- Consumes: `multiCMD` module attributes `USE_SUDO`, `SUDO_PATH`, `__variables`, `join_threads`
- Produces: fixture `restore_module_globals` (autouse), fixture `run_cli(args, **kwargs) -> subprocess.CompletedProcess`, helpers `has_sudo()`, `loopback_ping_works()`

- [ ] **Step 1: Create `pytest.ini`**

```ini
[pytest]
testpaths = tests
pythonpath = .
markers =
    sudo: needs a usable sudo binary / non-root enable path
    ping: needs ping binary and working loopback
    slow: intentional sleeps ≥1s (concurrency / timeout)
```

- [ ] **Step 2: Create `tests/conftest.py`**

```python
import subprocess
import sys
from pathlib import Path

import pytest

import multiCMD

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
MULTICMD_PY = PACKAGE_ROOT / "multiCMD.py"


@pytest.fixture(autouse=True)
def restore_module_globals():
	saved_use_sudo = multiCMD.USE_SUDO
	saved_sudo_path = multiCMD.SUDO_PATH
	saved_vars = dict(multiCMD.__variables)
	yield
	multiCMD.USE_SUDO = saved_use_sudo
	multiCMD.SUDO_PATH = saved_sudo_path
	multiCMD.__variables.clear()
	multiCMD.__variables.update(saved_vars)
	multiCMD.join_threads(timeout=2)


def run_cli(args, timeout=30, check=False):
	return subprocess.run(
		[sys.executable, str(MULTICMD_PY), *args],
		capture_output=True,
		text=True,
		timeout=timeout,
		check=check,
	)


def has_sudo_binary():
	return bool(multiCMD.SUDO_PATH)


def loopback_ping_works():
	try:
		r = subprocess.run(
			["ping", "-c", "1", "-W", "1", "127.0.0.1"],
			capture_output=True,
			timeout=5,
		)
		return r.returncode == 0
	except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
		return False
```


- [ ] **Step 3: Add a harness smoke test**

Create `tests/test_harness_smoke.py`:

```python
import multiCMD
from conftest import run_cli

def test_import_version():
	assert multiCMD.version
	assert multiCMD.__version__ == multiCMD.version

def test_run_cli_help():
	proc = run_cli(["-h"])
	assert proc.returncode == 0
	assert "max_threads" in proc.stdout
```

- [ ] **Step 4: Run harness tests**

Run: `cd /mnt/klein/work/cas/multiCMD && pytest tests/test_harness_smoke.py -v`  
Expected: PASS (2 tests). Confirm root `test_multiCMD.py` is **not** collected: `pytest --collect-only -q` should only list `tests/...`.

- [ ] **Step 5: Commit**

```bash
git add pytest.ini tests/conftest.py tests/test_harness_smoke.py
git commit -m "$(cat <<'EOF'
Add pytest harness for the split multiCMD test suite.

EOF
)"
```

---

### Task 2: Range expansion tests

**Files:**
- Create: `tests/test_expand_ranges.py`
- Delete: `tests/test_harness_smoke.py` (version check moves here or into helpers later — delete smoke file; `-h` coverage lives in Task 8 CLI)

**Interfaces:**
- Consumes: `multiCMD._expand_ranges`, `multiCMD._expand_ranges_fast`
- Produces: full README expansion matrix locked as tests

- [ ] **Step 1: Delete harness smoke file** (CLI help reappears in Task 8)

```bash
rm tests/test_harness_smoke.py
```

- [ ] **Step 2: Write `tests/test_expand_ranges.py`**

```python
import multiCMD
from multiCMD import _expand_ranges, _expand_ranges_fast


def test_alias_is_fast_implementation():
	assert _expand_ranges is _expand_ranges_fast


def test_numeric_alpha_and_comma():
	assert _expand_ranges("file[1-3].txt") == ["file1.txt", "file2.txt", "file3.txt"]
	assert _expand_ranges("file[a-c].txt") == ["filea.txt", "fileb.txt", "filec.txt"]
	assert _expand_ranges("file[1-2,a-b].txt") == [
		"file1.txt", "file2.txt", "filea.txt", "fileb.txt"
	]
	assert _expand_ranges("file[1-3,5].txt") == [
		"file1.txt", "file2.txt", "file3.txt", "file5.txt"
	]


def test_zero_padding_shorter_endpoint_rule():
	assert _expand_ranges("file[01-03].txt") == [
		"file01.txt", "file02.txt", "file03.txt"
	]
	assert _expand_ranges("[0-10]") == [str(i) for i in range(11)]
	assert _expand_ranges("[01-10]") == [f"{i:02d}" for i in range(1, 11)]


def test_hex_style_alpha_range():
	assert _expand_ranges("v[a-f]") == [f"v{c}" for c in "abcdef"]


def test_multi_bracket_cartesian():
	assert _expand_ranges("[1-2]-[a-b]") == ["1-a", "1-b", "2-a", "2-b"]


def test_variable_assignment_and_reference():
	multiCMD.__variables.clear()
	assert _expand_ranges("[n:3]host[1-n]") == ["host1", "host2", "host3"]


def test_brace_expression_eval():
	multiCMD.__variables.clear()
	assert _expand_ranges("host[{2+3}]") == ["host5"]


def test_passthrough_without_brackets():
	assert _expand_ranges("plainhost") == ["plainhost"]
	assert _expand_ranges("") == [""]
```

If `[01-10]` or hex assertions disagree with runtime, **do not “fix” production** — adjust the assert to match actual behavior and note the observed result in a one-line comment above the assert.

- [ ] **Step 3: Run expansion tests**

Run: `pytest tests/test_expand_ranges.py -v`  
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add tests/test_expand_ranges.py
git add -u tests/test_harness_smoke.py
git commit -m "$(cat <<'EOF'
Add range-expansion tests covering the README matrix.

EOF
)"
```

---

### Task 3: `run_command` tests

**Files:**
- Create: `tests/test_run_command.py`

**Interfaces:**
- Consumes: `multiCMD.run_command`
- Produces: locked single-command return shapes + timeout/async basics

- [ ] **Step 1: Write `tests/test_run_command.py`**

Port and extend the single-command cases from `test_multiCMD.py`:

```python
import time

import pytest

from multiCMD import run_command


def test_echo_stdout_list():
	assert run_command(["echo", "hello"], quiet=True) == ["hello"]


def test_string_command_form():
	assert run_command("echo hello", quiet=True) == ["hello"]


def test_dry_run_empty():
	assert run_command(["echo", "hello"], dry_run=True, quiet=True) == []


def test_return_code_only_success_and_failure():
	assert run_command(["echo", "hello"], return_code_only=True, quiet=True) == 0
	assert run_command(["false"], return_code_only=True, quiet=True) == 1


def test_return_object():
	task = run_command(["echo", "hello"], return_object=True, quiet=True)
	assert task.returncode == 0
	assert task.stdout == ["hello"]
	assert task.stderr == []


def test_with_stderr_merged():
	# python writes to stderr
	out = run_command(
		["python3", "-c", "import sys; sys.stderr.write('e\\n'); print('o')"],
		quiet=True,
		with_stdErr=True,
	)
	assert "o" in out
	assert "e" in out


@pytest.mark.slow
def test_timeout_silent_command():
	result = run_command(["sleep", "2"], timeout=0.3, quiet=True, with_stdErr=False)
	assert "Timeout!" not in result
	result = run_command(["sleep", "2"], timeout=0.3, quiet=True, with_stdErr=True)
	assert "Timeout!" in "\n".join(result)
	task = run_command(
		["sleep", "2"], timeout=0.3, quiet=True, return_object=True
	)
	assert task.returncode == 124
	assert task.stderr and task.stderr[-1].strip().startswith("Timeout!")


@pytest.mark.slow
def test_wait_for_return_false():
	task = run_command(
		["sleep", "0.4"],
		wait_for_return=False,
		quiet=True,
		return_object=True,
	)
	assert task.returncode is None
	deadline = time.time() + 2
	while task.returncode is None and time.time() < deadline:
		time.sleep(0.05)
	assert task.returncode == 0


def test_raise_error_on_failure():
	with pytest.raises(Exception, match="Commands failed"):
		run_command(["false"], quiet=True, raise_error=True)
```

- [ ] **Step 2: Run**

Run: `pytest tests/test_run_command.py -v`  
Expected: PASS. If `timeout=0.3` is flaky on a loaded host, raise to `0.5` but keep marked `slow`.

- [ ] **Step 3: Commit**

```bash
git add tests/test_run_command.py
git commit -m "$(cat <<'EOF'
Add run_command API tests including timeout and raise_error.

EOF
)"
```

---

### Task 4: `run_commands` tests

**Files:**
- Create: `tests/test_run_commands.py`

**Interfaces:**
- Consumes: `multiCMD.run_commands`, `threading.Semaphore`
- Produces: batch/concurrency/parse/`sem` coverage

- [ ] **Step 1: Write `tests/test_run_commands.py`**

```python
import threading
import time

import pytest

from multiCMD import run_commands


def test_batch_stdout():
	assert run_commands(
		[["echo", "hello"], ["echo", "world"]], quiet=True
	) == [["hello"], ["world"]]


def test_dry_run_and_return_codes():
	cmds = [["echo", "hello"], ["echo", "world"]]
	assert run_commands(cmds, dry_run=True, quiet=True) == [[], []]
	assert run_commands(cmds, return_code_only=True, quiet=True) == [0, 0]


def test_return_objects_preserve_order():
	tasks = run_commands(
		[["echo", "hello"], ["false"], ["echo", "world"]],
		return_object=True,
		quiet=True,
	)
	assert [t.returncode for t in tasks] == [0, 1, 0]
	assert tasks[0].stdout == ["hello"]
	assert tasks[2].stdout == ["world"]


def test_parse_expands_commands():
	result = run_commands([["echo", "[0-10]"]], quiet=True, parse=True)
	assert result == [[str(i)] for i in range(11)]


@pytest.mark.slow
def test_timeout_mixed_batch():
	result = run_commands(
		[["sleep", "2"], ["echo", "done"]],
		timeout=0.3,
		quiet=True,
		with_stdErr=True,
		max_threads=2,
	)
	assert "Timeout!" in "\n".join(result[0])
	assert result[1] == ["done"]


@pytest.mark.slow
def test_max_threads_scheduling():
	# Port of legacy test_run_commands_max_threads with slightly tighter sleeps
	cmds = [["sleep", "0.6"], ["sleep", "0.6"], ["sleep", "0.6"]]
	tasks = run_commands(
		cmds,
		wait_for_return=False,
		timeout=0,
		quiet=True,
		return_object=True,
		max_threads=2,
	)
	time.sleep(0.2)
	alive = sum(1 for t in tasks if t.returncode is None)
	assert alive == 3
	deadline = time.time() + 3
	while any(t.returncode is None for t in tasks) and time.time() < deadline:
		time.sleep(0.05)
	assert [t.returncode for t in tasks] == [0, 0, 0]


@pytest.mark.slow
def test_shared_semaphore_bounds_concurrency():
	sem = threading.Semaphore(1)
	# With sem=1, two 0.3s sleeps cannot overlap even if max_threads=2
	t0 = time.time()
	run_commands(
		[["sleep", "0.3"], ["sleep", "0.3"]],
		quiet=True,
		max_threads=2,
		sem=sem,
	)
	elapsed = time.time() - t0
	assert elapsed >= 0.55


def test_raise_error_batch():
	with pytest.raises(Exception, match="Commands failed"):
		run_commands([["true"], ["false"]], quiet=True, raise_error=True)
```


- [ ] **Step 2: Run**

Run: `pytest tests/test_run_commands.py -v`  
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/test_run_commands.py
git commit -m "$(cat <<'EOF'
Add run_commands batch, parse, semaphore, and concurrency tests.

EOF
)"
```

---

### Task 5: `Task` + `AsyncExecutor` + `join_threads`

**Files:**
- Create: `tests/test_task.py`
- Create: `tests/test_async_executor.py`

**Interfaces:**
- Consumes: `multiCMD.Task`, `multiCMD.AsyncExecutor`, `multiCMD.run_commands`, `multiCMD.join_threads`
- Produces: object-surface and async lifecycle coverage

- [ ] **Step 1: Write `tests/test_task.py`**

```python
from multiCMD import Task, run_command


def test_task_defaults_and_iter():
	t = Task(["echo", "x"])
	assert t.returncode is None
	assert t.stdout == []
	assert t.stderr == []
	assert t.stop is False
	assert dict(t) == {
		"command": ["echo", "x"],
		"returncode": None,
		"stdout": [],
		"stderr": [],
	}


def test_task_repr_str_and_alive():
	t = run_command(["echo", "hi"], return_object=True, quiet=True)
	assert "hi" in str(t)
	assert "Task(" in repr(t)
	assert t.is_alive() is False
```

- [ ] **Step 2: Write `tests/test_async_executor.py`**

```python
import time

import pytest

import multiCMD
from multiCMD import AsyncExecutor, run_commands


@pytest.mark.slow
def test_executor_run_wait_results_codes():
	ex = AsyncExecutor(max_threads=2, timeout=0, quiet=True)
	ex.run_command(["echo", "a"])
	ex.run_commands([["echo", "b"], ["false"]])
	assert len(ex) == 3
	assert bool(ex)
	ex.wait(timeout=2)
	assert ex.get_return_codes() == [0, 0, 1]
	assert ex.get_results() == [["a"], ["b"], []]


@pytest.mark.slow
def test_executor_join_prints_errors(capsys):
	ex = AsyncExecutor(max_threads=1, quiet=True)
	ex.run_command(["false"])
	ex.join(timeout=2, print_error=True)
	captured = capsys.readouterr()
	assert "failed with return code" in captured.out


@pytest.mark.slow
def test_executor_stop_and_cleanup():
	ex = AsyncExecutor(max_threads=1, quiet=True)
	ex.run_command(["sleep", "5"])
	time.sleep(0.1)
	ex.stop(timeout=2)
	# stop signals task.stop; process should end
	assert all(not t.is_alive() for t in ex.tasks)
	ex.cleanup(timeout=1)
	assert list(ex) == []


@pytest.mark.slow
def test_join_threads_module_helper():
	tasks = run_commands(
		[["sleep", "0.3"]],
		wait_for_return=False,
		return_object=True,
		quiet=True,
	)
	assert tasks[0].returncode is None
	multiCMD.join_threads(timeout=2)
	assert tasks[0].returncode == 0
```

- [ ] **Step 3: Run**

Run: `pytest tests/test_task.py tests/test_async_executor.py -v`  
Expected: PASS. If `stop` leaves a zombie longer than expected, widen join timeout; do not change production stop semantics without asking.

- [ ] **Step 4: Commit**

```bash
git add tests/test_task.py tests/test_async_executor.py
git commit -m "$(cat <<'EOF'
Add Task, AsyncExecutor, and join_threads tests.

EOF
)"
```

---

### Task 6: Sudo + ping (skip-friendly)

**Files:**
- Create: `tests/test_sudo.py`
- Create: `tests/test_ping.py`

**Interfaces:**
- Consumes: `multiCMD.set_sudo`, `multiCMD.run_command`, `multiCMD.ping`, `conftest.has_sudo_binary`, `conftest.loopback_ping_works`
- Produces: env-gated coverage for sudo/ping

- [ ] **Step 1: Write `tests/test_sudo.py`**

```python
import os

import pytest

import multiCMD
from multiCMD import run_command, set_sudo


def test_set_sudo_false():
	multiCMD.USE_SUDO = True
	set_sudo(False)
	assert multiCMD.USE_SUDO is False


def test_set_sudo_true_when_root():
	if os.geteuid() != 0:
		pytest.skip("requires root to exercise root-disables-sudo path")
	set_sudo(True)
	assert multiCMD.USE_SUDO is False


def test_set_sudo_true_without_sudo_binary(capsys):
	multiCMD.SUDO_PATH = None
	if os.geteuid() == 0:
		pytest.skip("root path short-circuits before SUDO_PATH check")
	set_sudo(True)
	assert multiCMD.USE_SUDO is False
	err = capsys.readouterr().err
	assert "sudo not found" in err


@pytest.mark.sudo
def test_set_sudo_true_non_root_with_binary():
	if os.geteuid() == 0 or not multiCMD.SUDO_PATH:
		pytest.skip("needs non-root and sudo in PATH")
	set_sudo(True)
	assert multiCMD.USE_SUDO is True


def test_use_sudo_none_path_does_not_crash():
	multiCMD.SUDO_PATH = None
	# Must not raise TypeError from Popen([None, ...])
	result = run_command(["echo", "ok"], quiet=True, use_sudo=True)
	assert result == ["ok"]
```

- [ ] **Step 2: Write `tests/test_ping.py`**

```python
import shutil

import pytest

from conftest import loopback_ping_works
from multiCMD import ping

pytestmark = [
	pytest.mark.ping,
	pytest.mark.skipif(shutil.which("ping") is None, reason="ping not installed"),
	pytest.mark.skipif(not loopback_ping_works(), reason="loopback ping failed"),
]


def test_ping_single_true_false():
	assert ping("127.0.0.1", timeout=2, quiet=True) is True


def test_ping_list_true_false():
	assert ping(["127.0.0.1", "127.0.0.1"], timeout=2, quiet=True) == [True, True]


def test_ping_raw_return_codes():
	rcs = ping(
		["127.0.0.1"],
		timeout=2,
		quiet=True,
		return_true_false=False,
		return_code_only=True,
	)
	assert rcs == [0]
```

- [ ] **Step 3: Run**

Run: `pytest tests/test_sudo.py tests/test_ping.py -v`  
Expected: non-skipped tests PASS; skipped tests show clear reasons.

- [ ] **Step 4: Commit**

```bash
git add tests/test_sudo.py tests/test_ping.py
git commit -m "$(cat <<'EOF'
Add skip-friendly sudo and ping tests.

EOF
)"
```

---

### Task 7: CLI subprocess tests

**Files:**
- Create: `tests/test_cli.py`

**Interfaces:**
- Consumes: `conftest.run_cli`, `multiCMD.version`
- Produces: CLI flag smoke coverage

- [ ] **Step 1: Write `tests/test_cli.py`**

```python
from conftest import run_cli
import multiCMD


def test_version_flag():
	proc = run_cli(["-V"])
	assert proc.returncode == 0
	assert multiCMD.version in (proc.stdout + proc.stderr)


def test_help_flag():
	proc = run_cli(["-h"])
	assert proc.returncode == 0
	assert "max_threads" in proc.stdout


def test_two_echoes():
	proc = run_cli(["-q", "echo hello", "echo world"])
	assert proc.returncode == 0
	combined = proc.stdout + proc.stderr
	assert "hello" in combined
	assert "world" in combined


def test_parse_flag_expands():
	proc = run_cli(["-q", "-p", "echo [1-2]"])
	assert proc.returncode == 0
	combined = proc.stdout + proc.stderr
	assert "1" in combined
	assert "2" in combined


def test_max_threads_and_timeout_smoke():
	proc = run_cli(["-q", "-m", "2", "-t", "5", "echo a", "echo b"], timeout=20)
	assert proc.returncode == 0


def test_sudo_flag_without_sudo_binary_does_not_crash(monkeypatch):
	# Force child env: PATH without sudo by invoking python -c wrapper is heavy;
	# instead assert current host behavior: --sudo with missing sudo should not
	# abort with traceback. If sudo exists, command still succeeds for echo.
	proc = run_cli(["-q", "--sudo", "echo ok"], timeout=20)
	assert proc.returncode == 0
	assert "Traceback" not in (proc.stdout + proc.stderr)
```

- [ ] **Step 2: Run**

Run: `pytest tests/test_cli.py -v`  
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/test_cli.py
git commit -m "$(cat <<'EOF'
Add CLI subprocess smoke tests for multiCMD flags.

EOF
)"
```

---

### Task 8: Helper tests

**Files:**
- Create: `tests/test_helpers.py`

**Interfaces:**
- Consumes: helpers listed in README Key functions / misc section
- Produces: unit coverage for formatting utilities

- [ ] **Step 1: Write `tests/test_helpers.py`**

```python
import builtins
import select
import sys

import pytest

from multiCMD import (
	_genrate_progress_bar,
	format_bytes,
	get_terminal_size,
	input_with_timeout_and_countdown,
	int_to_color,
	parseTable,
	pretty_format_table,
	print_progress_bar,
	slugify,
)


def test_pretty_format_table_basic_and_remove_empty():
	data = [["a", ""], ["b", ""]]
	header = ["h1", "h2"]
	out = pretty_format_table(data, header=header, remove_empty_columns=True)
	assert "h1" in out
	assert "h2" not in out
	assert "a" in out


def test_parse_table():
	text = "col1  col2\nfoo   bar\n"
	table = parseTable(text)
	assert table[0] == ["col1", "col2"]
	assert table[1][0] == "foo"


def test_slugify_ascii_and_unicode():
	assert slugify("Hello World!") == "hello-world"
	assert "café" in slugify("café", allow_unicode=True) or "cafe" in slugify("café")


def test_format_bytes_roundtrip():
	assert format_bytes(1024, use_1024_bytes=True, to_str=True).lower().startswith("1")
	assert format_bytes("1 KiB", to_int=True) == 1024


def test_int_to_color_deterministic_and_in_range():
	c1 = int_to_color(0x123456)
	c2 = int_to_color(0x123456)
	assert c1 == c2
	assert all(0 <= ch <= 255 for ch in c1)
	# black / bright extremes should not raise
	assert len(int_to_color(0)) == 3
	assert len(int_to_color(0xFFFFFF)) == 3


def test_progress_bar_helpers(capsys):
	s = _genrate_progress_bar(5, 10, prefix="P", suffix="S", columns=80)
	assert "P" in s and "S" in s
	print_progress_bar(5, 10, prefix="Progress:", suffix="Complete")
	captured = capsys.readouterr()
	assert "Progress:" in captured.out
	assert "50.0%" in captured.out or "50%" in captured.out


def test_get_terminal_size_tuple():
	cols, rows = get_terminal_size()
	assert cols > 0 and rows > 0


def test_input_with_timeout_and_countdown(monkeypatch):
	monkeypatch.setattr(builtins, "input", lambda prompt="": "user input")
	monkeypatch.setattr(
		select, "select", lambda r, w, x, timeout=None: ([sys.stdin], [], [])
	)
	assert input_with_timeout_and_countdown(5) == "user input"
	monkeypatch.setattr(select, "select", lambda r, w, x, timeout=None: ([], [], []))
	assert input_with_timeout_and_countdown(1) is None
```

If `slugify("café", allow_unicode=True)` exact value differs, assert the precise current return value after one exploratory `python -c` run and lock that.

- [ ] **Step 2: Run**

Run: `pytest tests/test_helpers.py -v`  
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/test_helpers.py
git commit -m "$(cat <<'EOF'
Add unit tests for multiCMD helper utilities.

EOF
)"
```

---

### Task 9: Regression tests

**Files:**
- Create: `tests/test_regressions.py`

**Interfaces:**
- Consumes: `run_command`, `int_to_color`, sudo globals
- Produces: five named locks from the design spec

- [ ] **Step 1: Write `tests/test_regressions.py`**

```python
import time

import pytest

import multiCMD
from multiCMD import int_to_color, run_command


def test_regression_sudo_none_path_no_typeerror():
	"""use_sudo with SUDO_PATH=None must not prepend None into Popen argv."""
	multiCMD.SUDO_PATH = None
	out = run_command(["echo", "ok"], quiet=True, use_sudo=True)
	assert out == ["ok"]


@pytest.mark.slow
def test_regression_stdin_eof_no_hang():
	"""Commands that read stdin must see EOF (DEVNULL), not hang until timeout."""
	t0 = time.time()
	task = run_command(["cat"], quiet=True, return_object=True, timeout=2)
	elapsed = time.time() - t0
	assert elapsed < 1.5
	assert task.returncode == 0
	assert task.stdout == []


def test_regression_int_to_color_no_recursion():
	# Previously could RecursionError / non-deterministic str hashing
	colors = [int_to_color(i) for i in range(0, 5000, 97)]
	assert len(colors) == len(set(range(0, 5000, 97)))
	assert int_to_color(42) == int_to_color(42)
	for rgb in colors:
		assert all(0 <= c <= 255 for c in rgb)


@pytest.mark.slow
def test_regression_concurrent_stdout_stderr_no_race():
	code = (
		"import sys\n"
		"for i in range(200):\n"
		"    print('O', i, flush=True)\n"
		"    print('E', i, file=sys.stderr, flush=True)\n"
	)
	task = run_command(["python3", "-c", code], quiet=True, return_object=True)
	assert task.returncode == 0
	assert len(task.stdout) == 200
	assert len(task.stderr) == 200


@pytest.mark.slow
def test_regression_inactivity_timeout_not_total_runtime():
	"""A chatty process lasting longer than timeout must not be killed for wall time."""
	code = (
		"import time\n"
		"for _ in range(6):\n"
		"    print('tick', flush=True)\n"
		"    time.sleep(0.25)\n"
	)
	task = run_command(
		["python3", "-c", code],
		timeout=0.5,
		quiet=True,
		return_object=True,
	)
	assert task.returncode == 0
	assert not any(line.strip().startswith("Timeout!") for line in task.stderr)
	assert len(task.stdout) == 6
```

- [ ] **Step 2: Run**

Run: `pytest tests/test_regressions.py -v`  
Expected: PASS on current `main` (post `e40d329` fixes).

- [ ] **Step 3: Commit**

```bash
git add tests/test_regressions.py
git commit -m "$(cat <<'EOF'
Add regression tests for sudo, stdin, color, streams, and timeout.

EOF
)"
```

---

### Task 10: Migrate away from legacy suite + README + final verify

**Files:**
- Delete: `test_multiCMD.py`
- Modify: `README.md` (Development section)

**Interfaces:**
- Consumes: full `tests/` suite
- Produces: single canonical pytest entrypoint

- [ ] **Step 1: Delete legacy file**

```bash
rm test_multiCMD.py
```

- [ ] **Step 2: Update README Development section**

Replace the Development block (around the `pytest` instructions) with:

```markdown
## Development

Run the test suite with [pytest](https://pytest.org):

```bash
pip install pytest
pytest                 # full suite under tests/
pytest -m "not slow"   # skip intentional sleep-heavy cases
```

`test_expand_ranges_perf.py` is a manual performance microbenchmark and is not collected by pytest.
```

Keep surrounding License section intact.

- [ ] **Step 3: Full verification**

Run:

```bash
cd /mnt/klein/work/cas/multiCMD
pytest --collect-only -q
pytest -m "not slow" -v
pytest -v
```

Expected:
- Collection lists only `tests/…` (no `test_multiCMD.py`, no perf script).
- `not slow` finishes in a few seconds.
- Full suite green aside from intentional `skip`s; wall time roughly ≤45s.

- [ ] **Step 4: Commit**

```bash
git add -u test_multiCMD.py README.md
git commit -m "$(cat <<'EOF'
Switch multiCMD testing to the split tests/ suite.

EOF
)"
```

---

## Spec coverage checklist (self-review)

| Spec requirement | Task |
|---|---|
| Split layout under `tests/` + `pytest.ini` | 1 |
| conftest globals / join / `run_cli` | 1 |
| `_expand_ranges` README matrix + alias | 2 |
| `run_command` shapes / timeout / raise_error | 3 |
| `run_commands` batch / parse / sem / threads | 4 |
| `Task`, `AsyncExecutor`, `join_threads` | 5 |
| sudo + ping with skipif | 6 |
| CLI full-stack | 7 |
| Helpers including `_genrate_progress_bar` | 8 |
| Five named regressions | 9 |
| Delete legacy file + README note | 10 |
| No minified / generator / perf-in-pytest | Global + Task 10 |
| Quick lane `-m "not slow"` | markers in Tasks 3–5, 9 |

## Implementer notes

- Import `run_cli` / helpers from `conftest` works when tests run via pytest from package root; do not rely on `python tests/test_cli.py` direct execution.
- If any assert disagrees with live behavior, lock actual behavior (comment why) rather than changing `multiCMD.py` without approval.
