# multiCMD Complete Test Suite Design

**Date:** 2026-08-10  
**Status:** Approved for planning  
**Package:** `multiCMD` (single-module library + CLI)

## Goal

Build a complete pytest suite that:

1. Covers every public / exportable surface documented in the README (including helpers and the `_expand_ranges` compat alias used by dependents).
2. Adds explicit regression tests for known fixes (sudo `None` crash, stdin hang, color recursion, stream race, inactivity-timeout semantics).
3. Exercises the full stack: in-process library API, real subprocess commands, and CLI process smoke tests.
4. Stays green when optional capabilities are missing (`sudo`, `ping`/network, TTY) via `skipif`, not soft failures.

## Constraints (agreed)

| Choice | Decision |
|---|---|
| Purpose | Full API coverage **and** named regression cases |
| Depth | Library + CLI subprocesses + real `echo`/`sleep`/`false` integration |
| Surface | Everything exportable, including `_expand_ranges` |
| Optional env | Skip when unavailable |
| Layout | Split by area under `tests/` |
| Approach | Behavior-first with real subprocesses (no mock-heavy unit layer) |

## Non-goals

- No CI wiring in this work unless requested later.
- No Hypothesis, coverage gates, or new test-only runtime dependencies beyond pytest.
- No testing of `multiCMD_min.py`, `multiCMD_min_lzma_a85.py`, or `compressedEmbeddedCodeGenerator.py`.
- `test_expand_ranges_perf.py` remains a manual/perf script and is **not** collected by pytest.
- No production behavior changes unless a test reveals a clear bug — stop and confirm before fixing.

## Layout

```
multiCMD/
  multiCMD.py
  test_expand_ranges_perf.py   # not collected by pytest
  tests/
    conftest.py
    test_expand_ranges.py
    test_run_command.py
    test_run_commands.py
    test_async_executor.py
    test_task.py
    test_sudo.py
    test_ping.py
    test_cli.py
    test_helpers.py
    test_regressions.py
  pytest.ini                   # markers + pythonpath
```

Root `test_multiCMD.py` is deleted after its cases are ported and green.

README Development section gets a one-line note that the suite lives under `tests/` and the perf script is separate.

## Harness

### `pytest.ini`

- `testpaths = tests`
- `pythonpath = .` (so `import multiCMD` works without install)
- Markers: `sudo`, `ping`, `slow`

### `conftest.py`

- Save/restore `multiCMD.USE_SUDO` and `multiCMD.SUDO_PATH` around sudo-related tests so globals do not leak.
- Autouse finalizer: `multiCMD.join_threads(...)` with a bounded timeout so fire-and-forget threads do not pollute later tests.
- `run_cli(args, **kwargs)` helper: `subprocess.run([sys.executable, path_to_multiCMD.py, *args], capture_output=True, text=True, timeout=...)`.

### Execution style

- Prefer real subprocesses for execution tests (`echo`, `false`, `sleep`, and short scripts that write continuously or read stdin).
- Do **not** mock `subprocess.Popen` for core execution paths. Mocking is allowed only to force otherwise hard-to-reach env states (e.g. temporarily set `SUDO_PATH = None`).
- Timing tests use short sleeps (target ≤1s where practical) and assert invariants (completion order, presence of `Timeout!`, return code `124`), not exact wall-clock duration.
- CLI tests assert exit code and key substrings, not full argparse help snapshots.

### Skip policy

| Marker / area | Skip when |
|---|---|
| `sudo` | `sudo` missing from `PATH`, or environment cannot meaningfully exercise sudo (e.g. already root when the test requires the non-root enable path — use the path that still asserts documented behavior, skip the rest) |
| `ping` | `ping` binary missing, or loopback/`127.0.0.1` ping fails |
| TTY helpers | Non-fatal: `get_terminal_size` must still return a usable fallback; skip only if the platform cannot support the code path under test |

Default `pytest` runs all non-skipped tests. Quick lane: `pytest -m "not slow"`.

## Coverage map

### `test_expand_ranges.py`

Owns `_expand_ranges` (and confirms `_expand_ranges_fast` / alias behavior as exported today).

Cases from README and edges:

- Numeric and alpha ranges, hex-style alpha (`[a-f]`)
- Zero-padding rules: `[0-10]` unpadded vs `[01-10]` / `[01-03]` padded (shorter-endpoint rule)
- Comma lists: `[1-2,a-b]`
- Multi-bracket cartesian product: `[1-2]-[a-b]`
- Variables: `[n:3]host[1-n]`
- `{expr}` evaluation with variables in scope
- No-bracket passthrough; empty / edge inputs as **currently implemented** (document actual behavior in asserts; do not invent desired semantics)

### `test_run_command.py`

- Default stdout-list return for `echo`
- `return_code_only`, `return_object` (`Task` fields)
- `with_stdErr` merging
- `dry_run` → empty output, no side effects
- `wait_for_return=False` → `returncode is None` then becomes set
- Inactivity timeout: silent command → `Timeout!` on stderr when requested, rc `124`
- Chatty command beyond timeout window does **not** get killed for inactivity
- String vs list command forms
- `raise_error=True` raises on non-zero

### `test_run_commands.py`

- Parallel batch return shapes matching `run_command`
- `max_threads` concurrency / scheduling invariants (port and tighten existing tests)
- `parse=True` expands ranged commands into N tasks
- Mixed success/failure ordering preserved relative to input order
- Optional `sem=` argument: callers can pass a shared `threading.Semaphore` and observe bounded concurrency

### `test_async_executor.py`

- `run_command` / `run_commands` accumulate `Task`s
- `wait`, `join` (including failure printing when `print_error=True`), `stop`, `cleanup`
- `get_results` / `get_return_codes`
- `__len__`, `__bool__`, `__iter__`
- Interaction with module-level `join_threads()` for fire-and-forget `run_commands`

### `test_task.py`

- Field defaults; `__iter__` / `dict(task)`; `__str__` / `__repr__`; `is_alive` before and after a run

### `test_sudo.py` (`sudo` marker)

- `set_sudo(False)` clears flag
- `set_sudo(True)` when already root → `USE_SUDO` stays false
- `set_sudo(True)` when `SUDO_PATH` missing → warning, `USE_SUDO` false
- `set_sudo(True)` when sudo present and non-root → `USE_SUDO` true (skipif otherwise)
- Per-call `use_sudo=True` with `SUDO_PATH is None` does not crash (prepend guard)

### `test_ping.py` (`ping` marker)

- Single host and list hosts
- Default `return_true_false` boolean (or list of booleans)
- Non-boolean mode (`return_true_false=False`) returns underlying `run_commands` shape
- Skip if `ping` unavailable or loopback probe fails

### `test_cli.py`

Subprocess invocations of `multiCMD.py`:

- `-V` / `--version` (exit 0, version string present)
- `-h` (exit 0)
- Two simple echoes (exit 0, output contains both)
- `-p` range expansion
- `-q`, `-m`, `-t` smoke (exit 0 or documented timeout behavior)
- `--sudo` routes through `set_sudo` without crashing when sudo absent (may share assertions with regressions)

### `test_helpers.py`

- `pretty_format_table` including `remove_empty_columns`
- `parseTable`
- `slugify` (ascii / unicode flag)
- `format_bytes` (to_str / to_int / base 1024 vs 1000 as implemented)
- `int_to_color` — deterministic for a given input; RGB channels within documented brightness band; no recursion
- `print_progress_bar` / `_genrate_progress_bar` (spelling as in module) via `capsys`
- `get_terminal_size` returns a 2-tuple of positive ints
- `input_with_timeout_and_countdown` with monkeypatched `input` / `select`

### `test_regressions.py`

One focused test per known fix (name reflects the bug):

| Test focus | Fixed behavior to lock |
|---|---|
| Sudo `None` prepend | `use_sudo=True` with `SUDO_PATH is None` does not raise `TypeError` from `Popen` |
| Stdin hang | A command that reads stdin (`cat` with no args, or equivalent) completes quickly with EOF; does not sit until inactivity timeout |
| Color recursion | `int_to_color` returns stably for many hashes; no `RecursionError` |
| Stream race | Command producing substantial stdout+stderr concurrently completes with consistent captured lines (no crash / hang) |
| Inactivity vs total time | Process that keeps printing past `timeout` seconds is not killed solely for total runtime |

## Migration plan

1. Add `tests/`, `conftest.py`, `pytest.ini`.
2. Port existing `test_multiCMD.py` cases into the owning files (preserve behavior; tighten sleeps where safe).
3. Fill coverage gaps and add `test_regressions.py`.
4. Delete root `test_multiCMD.py` once the new suite is green.
5. Note suite location + perf-script exclusion in README Development.

## Success criteria

- `pytest` from the package root collects only `tests/`.
- All non-skipped tests pass on a normal Linux host without root or external network.
- Every README “Key functions and objects” entry and listed helper has an owning test file/case.
- Each regression row above has a dedicated test.
- `pytest -m "not slow"` finishes in a few seconds; full suite target roughly ≤30–45s on the current development machine (existing suite alone is ~15s).

## Testing the tests

During implementation, run:

```bash
cd multiCMD
pytest                 # full suite
pytest -m "not slow"   # quick lane
pytest tests/test_regressions.py -v
```

No production code changes are required for a green suite if current `main` behavior matches the README and recent fix commit (`e40d329`).
