# multiCMD 1.52 Overhaul Design

**Date:** 2026-10-10
**Status:** Draft for review
**Package:** `multiCMD` (single-module library + CLI), plus follow-on edits in hpcp, statblk, statbtrfs and four other CAS packages
**Target version:** `1.52`

## Goal

Fix the verified bugs found in the 2026-10-10 review of multiCMD 1.51, and add the CLI features agreed in the same session. All of it ships in one release. The 19 decisions were made one at a time with the maintainer; this spec turns them into concrete behaviour.

## Verified findings this design responds to

Every row was reproduced on Linux with Python 3.13 (and 3.9 where noted) against 1.51.

| # | Finding | Evidence |
|---|---|---|
| 1 | CLI always exits 0 | `mcmd -q false; echo $?` → `0` |
| 2 | CLI kills silent commands after 60s | `mcmd "sleep 63"` → rc 124 at 60s |
| 3 | Timeout/stop only signals the direct child; the call blocks until grandchildren exit | `timeout=1`, `sh -c 'sleep 13.37 & wait'` → returned after 13.4s |
| 4 | `\r`-only progress output counts as inactivity | `\r1`…`\r5` every 0.4s, `timeout=1` → rc 124 |
| 5 | Per-command colors never apply in `run_commands` | `...` lands in `with_stdErr`, not `identity` (`multiCMD.py:924`) |
| 6 | One thread per command up front | 5,000 × `true`, `max_threads=4` → peak 2,915 live threads |
| 7 | String commands split on whitespace | `"printf '%s|' 'a b'"` → `''a|''b'|'` |
| 8a | Letter ranges lose case, hex loses padding | `host[A-C]` → `hosta…`; `n[08-0b]` → `n8 n9 na nb` |
| 8b | Range variables leak between calls | `[n:3]host[1-n]` then `disk[n]` → `disk3` |
| 8c | Any `:` in brackets is an assignment | `ping [fe80::1,fe80::2]` → `ping ` |
| 9 | Reader is CPU-bound (byte-at-a-time Python loop) | `seq 1 3000000`: 1.53s vs prototype 0.12s |
| 16 | `raise_error` raises bare `Exception` with full output dump | — |
| 17 | `AsyncExecutor.join()`/`wait()` don't wait by default | default executor: `join()` returned after 0.00s, reported running commands as failed |
| 18 | Windows `send_signal(SIGINT)` raises `ValueError` | CPython `Popen.send_signal` Windows branch |
| — | Process groups need care below Python 3.11 | `process_group` absent on 3.9/3.10; `preexec_fn` documented thread-unsafe; new session breaks sudo `tty` tickets (`man sudoers`: default `timestamp_type` is `tty`) |
| — | A background process group reading `/dev/tty` gets stopped | Under a real tty: stopped (`T`) without mask; `read rc=1` (EIO) with SIGTTIN blocked, on both 3.9 and 3.13 |

## Decisions (agreed)

| # | Decision |
|---|---|
| 1 | CLI exit: one command → its code (signal deaths → 128+N); several → number failed, capped at 101 |
| 2 | CLI `-t` default 0, help text says "inactivity" |
| 3 | Kill whole process group; forward Ctrl-C; kill leftovers at exit; prompting commands fail, not hang; bounded wait after a kill |
| 3b | Process group: `Popen(process_group=0)` on 3.11+, exec wrapper on older Pythons |
| 4 | Any output byte resets the inactivity timer |
| 5 | Fix color bug; color only on a TTY and without `NO_COLOR`; `--no-color` |
| 6 | Bounded worker pool for `wait_for_return=True`; fire-and-forget unchanged |
| 7 | `shlex.split` for strings; opt-in shell mode |
| 8 | Ranges keep case and hex padding; variables per `run_commands` call; one-colon identifier rule plus `\:` escape |
| 9 | `read1` + C-split reader |
| 10 | `-n/--dry-run`; library `dry_run` uses the same one-line format |
| 11 | `-f FILE` command file, `-a FILE` `{}` template input; `-` = stdin |
| 12 | End-of-run summary: failures by default, `--summary`/`--no-summary`; `-q` keeps it |
| 13 | `[#]` prefix for concurrent runs only, `--no-prefix`; print lock; shared-line `\r` rule |
| 14 | Move `__main__` block to end of file |
| 15 | Drop the PyPI `argparse` dependency from all seven packages |
| 16 | `CommandError` with `.tasks` |
| 17 | Executor waits until done by default; explicit timeout is one overall limit; accurate report |
| 18 | Windows best-effort, no crash |
| 19 | pty mode, stdout+stderr merged; on automatically in the CLI on a terminal |

## Non-goals

- No change to the fire-and-forget threading model (`wait_for_return=False`). statblk depends on `task.thread` existing and being per-task when `run_command` returns.
- No Windows process-tree kill and no ConPTY. Windows stays best-effort and untested.
- No new runtime dependencies. Everything used (`shlex`, `pty`, `termios`, `fcntl`, `atexit`, `signal`) is standard library. POSIX-only modules are imported lazily, so `import multiCMD` keeps working on Windows.
- No publishing (`uvpack`), pushing, or `cas-toolbox` re-cut as part of implementation. Those happen only on explicit request.
- No changes to the `Task.__iter__` keys (`dict(task)` stays `command`/`returncode`/`stdout`/`stderr`).

## Compatibility constraints

- **Python ≥ 3.6 syntax.** No walrus, no `shlex.join` (3.8+; use `' '.join(shlex.quote(a) for a in argv)`), no positional-only parameters, and no runtime-evaluated `list[str]` annotations.
- **Single file**, copy-transportable, importable standalone.
- **The minifier must still work:** python-minifier 3.4.0 under Python 3.11 (CLAUDE.md), so no 3.12-only syntax.
- **`_expand_ranges` stays exported** as the compat alias.

---

## A. Process launch and termination (#3, #3b, #18)

### Launcher

One internal function, `_spawn(argv, *, stdout, stderr, env=None)`, creates every command process except the `sudo -v` check.

| Platform | How the process gets its own group |
|---|---|
| POSIX, Python ≥ 3.11 | `subprocess.Popen(argv, ..., process_group=0)` |
| POSIX, Python < 3.11 | `Popen([sys.executable, '-I', '-S', '-c', _PGROUP_EXEC, *argv], ...)`: the wrapper calls `os.setpgid(0, 0)`, then `os.execvp(argv[0], argv)`. `FileNotFoundError` → message on stderr, exit 127. Other `OSError` → message, exit 126. Measured cost: about 5 ms per spawn on 3.9. If `sys.executable` is empty, fall back to a plain `Popen` with no group (today's behaviour). |
| Windows | Plain `Popen`, no group |

**TTY protection (POSIX).** Around the `Popen` call, the spawning thread blocks `SIGTTIN` and `SIGTTOU` with `signal.pthread_sigmask` and restores its mask afterwards. Children inherit the blocked mask, so a background-group read of `/dev/tty` fails with `EIO` instead of stopping the process. Verified on 3.9 (wrapper) and 3.13 (`process_group`). This covers grandchildren too, because the mask is inherited across `fork`/`exec`.

All commands keep `stdin=DEVNULL`, as today.

**Missing commands.** On the 3.11+ path, `Popen` still raises `FileNotFoundError` and the existing handler sets rc 127. On the wrapper path, the wrapper exits 127. The result is the same either way.

### Live-group registry

`_live_groups`: a module-level `{pgid: Popen}` guarded by a lock. A group is registered right after spawn and removed once its process is reaped. `kill_all(sig=signal.SIGTERM)` is a new public helper that runs the kill sequence on every registered group, so library users can call it from their own signal handlers. An `atexit` hook calls it, which covers normal exit and unhandled exceptions, including an uncaught `KeyboardInterrupt`.

### Kill sequence (timeout, stop, Ctrl-C, exit)

The POSIX sequence, used everywhere a command is stopped. The first signal is SIGINT for timeout, stop and Ctrl-C (as today); for `kill_all(sig)` it's `sig`, and the `atexit` hook uses SIGTERM. `kill_all` sends each step to every registered group before waiting, so its total time stays at about 2s however many groups there are.

1. `os.killpg(pgid, <first signal>)`, then wait 0.01s (the same timing as today).
2. `os.killpg(pgid, SIGTERM)` and `os.killpg(pgid, SIGCONT)`. SIGCONT lets a stopped member act on the signal.
3. Wait up to 1s for the leader to exit. Then `os.killpg(pgid, SIGKILL)` and wait up to 1s more.

`ProcessLookupError` and `PermissionError` from `killpg` are ignored. `PermissionError` happens with `--sudo`: the root-owned command can't be signalled by the user, but `sudo` relays SIGINT/SIGTERM to it, as it does today.

On Windows: `proc.terminate()`, wait 1s, then `proc.kill()`.

### Waiting for output after the process exits

- **Normal exit:** wait for both streams to reach EOF, with no time limit. This keeps today's guarantee that `task.stdout` is complete, and matches `subprocess.run(capture_output=True)`.
- **After a kill** (timeout, stop, interrupt): wait at most 1s for EOF, then finalize the task and return. Readers still blocked are *detached*: they discard further data and close their own stream at EOF. The call no longer closes a stream another thread is blocked on, which is what made 1.51 block.

### Ctrl-C and termination signals

- **Waiting library calls** (`run_command`/`run_commands` with `wait_for_return=True`, and `AsyncExecutor.wait`/`join`): on `KeyboardInterrupt`, mark that call's tasks `stop=True` and run the kill sequence for any that are running, then re-raise. This keeps 1.51's effective behaviour, where the terminal's SIGINT hit the commands too.
- **Fire-and-forget commands:** killed by the `atexit` hook, or by `kill_all()`/`AsyncExecutor.stop()`.
- **CLI:**
  - On `KeyboardInterrupt`, the commands are already killed by the rule above; print `Interrupted` to stderr and exit 130.
  - The CLI also installs handlers for **SIGTERM** and **SIGHUP** that call `kill_all()` and exit 128+N. Without them, `timeout 10 mcmd …` or a closed SSH session would leave the commands running, since they are no longer in mcmd's process group. This extends #3 beyond what was discussed item by item, and is flagged for review.
  - A signal mcmd was started with *ignored* keeps being ignored: no handler is installed for it. `nohup mcmd … &` leaves SIGHUP ignored, and its commands must keep running when the session drops, as they do in 1.51.
- The library never installs signal handlers.

### `--sudo` validation

`_validate_sudo` no longer goes through `__run_command`. It runs `subprocess.run([SUDO_PATH, '-v'])` in the foreground: no new group, no mask, terminal inherited. That way sudo can still prompt for a password, and the ticket it creates is the per-terminal ticket the commands will match. It's skipped entirely in dry-run (#10). If the ticket expires mid-run, a command's sudo password prompt now fails fast (EIO) instead of hanging.

## B. Output reading and activity (#4, #9)

The reader replaces `__handle_stream`. The prototype was verified identical to 1.51 on 11 tricky inputs, on byte-exact live display, and with 1/2/3/5-byte chunk boundaries.

- Loop on `stream.read1(65536)`. It returns as soon as any bytes are available, so liveness is unchanged.
- **Fast path**, for a chunk with no `\r`: `chunk.split(b'\n')` in C. The pending partial line is joined to the first piece and the last piece becomes the new partial line. The completed lines are decoded (`utf-8`, `backslashreplace`) and appended in one `extend`, and shown with one display call.
- **Slow path**, for a chunk containing `\r`: the per-separator state machine from 1.51 (committed / redrawable line), unchanged in behaviour.
- Every non-empty chunk sets `task._last_output = time.monotonic()`.
- At EOF the partial line is flushed as in 1.51. The reader closes its own stream. `OSError(EIO)` counts as EOF (needed for pty masters).
- If the task is finalized (detached, §A), the reader drains without recording.

**Inactivity timeout:** the monitor loop compares `time.monotonic() - task._last_output` against `timeout`. `_last_output` starts at spawn time. Any byte on either stream (or the pty) counts. Docstrings and README change from "committed line" to "no output at all for N seconds".

## C. Display (#13, #5)

A module-level display writer owns all of multiCMD's live output to `sys.stdout`: command lines, the `Running command:` header, separators and the exit line.

- **One lock:** every write is a single `write()` + `flush()` under it.
- **State:** `redrawable`, which is true when the last screen line was written as a `\r` redraw by any command.
- **New line:** write `'\n' + prefix + color + text + reset`, then set `redrawable = False`.
- **Redraw** (a `\r` update, or the `\n` that completes a redrawn line):
  - If `redrawable`, write `'\r' + prefix + color + text + reset`, plus `'\x1b[K'` (clear to end of line) when stdout is a TTY.
  - Otherwise write it as a new line.
  - A `\r` update leaves `redrawable = True`; the completing `\n` sets it to `False`, so final states become permanent lines.
- Headers, separators and exit lines are new lines.
- **Caveat:** writes that multiCMD doesn't make (e.g. a host program's own `print`) aren't tracked. That's documented, not handled.

**"Concurrent"**, as used below, means the call has more than one task and `max_threads != 1`. For an `AsyncExecutor`, it means `max_threads != 1`, since its task count grows over time.

**Prefix.** `'[%*d] ' % (width, index)`: a 1-based index into the call's task list (or the executor's `tasks` list), right-aligned to the width of the largest index. It's the same `#` the summary table shows.
- **Library:** `prefix=False` parameter on `run_command`, `run_commands`, and `AsyncExecutor` (constructor default plus per-call override).
- **CLI:** on when the run is concurrent. Off for a single command or a sequential run, and off with `--no-prefix`.

**Colors.**
- **Library:** `color=None` parameter (same places as `prefix`). `None` means auto: on when `sys.stdout.isatty()` and `NO_COLOR` is unset or empty, *and* the run is concurrent. `True`/`False` force it.
- **CLI:** `--no-color` → `color=False`.
- **Choice of color:** `int_to_color((index * 0x9E3779B1) & 0xFFFFFF)`. That's deterministic per index and well spread. It replaces the per-thread `threading.get_ident()` choice, and the off-by-one positional argument bug goes away with the explicit parameter.

## D. pty mode (#19)

- **Library:** `pty=False` parameter (same places as `prefix`).
- **CLI:** auto-on when `sys.stdout.isatty()`, not `-q`, not dry-run, and POSIX. `--pty` forces it on and `--no-pty` forces it off.

**Mechanics (POSIX):**
1. `master, slave = pty.openpty()`.
2. On the slave, turn off output post-processing (`OPOST`), so `\n` isn't turned into `\r\n` and bytes arrive exactly as written.
3. Set the slave's window size (`TIOCSWINSZ`) to the real terminal's size, minus the prefix width when the prefix is on.
4. `_spawn(argv, stdout=slave, stderr=slave, env=os.environ + {PAGER: cat, GIT_PAGER: cat})`.
5. Close the slave in the parent and read the master with the §B reader.

- **Not the controlling terminal:** the pty is only the command's output. No `setsid` is used, so `/dev/tty` stays the user's terminal. `sudo` tickets keep working and prompts fail via §A's TTY protection.
- **Merged output:** stdout and stderr share the pty, so `task.stderr` stays empty. Consumers that show the last error line (summary, `CommandError`) fall back to the last `task.stdout` line in pty mode.
- **Fallback:** if `openpty()` fails (e.g. no ptys left), that command uses pipes, with a one-time warning on stderr.
- **Windows:** `pty=True` gives a one-time warning and is ignored.

## E. Concurrency (#6)

`run_commands(wait_for_return=True)` with `max_threads != 1` starts `min(max_threads, len(tasks))` worker threads; `max_threads=0` still means one per command. Workers take the next task index from a shared counter under a lock and run it. `task.thread` is set to the worker that runs it. The `sem` argument is still acquired around each command, so a caller-shared semaphore keeps limiting concurrency across calls. The `max_threads == 1` path stays serial in the calling thread. The `wait_for_return=False` path is unchanged.

## F. Command parsing (#7, #8)

### Splitting and shell mode

- **String commands:** `shlex.split(cmd)` (POSIX mode). An unbalanced quote raises `ValueError("cannot parse command {cmd!r}: {shlex error}")` from `run_command(s)`/`AsyncExecutor.run_command(s)` before any command starts. Lists are unchanged.
- **Shell mode:** new `shell=False` parameter (same places as `prefix`), and `--shell` in the CLI. When on, the command runs as:
  - POSIX: `['/bin/sh', '-c', s]`
  - Windows: `[os.environ.get('COMSPEC', 'cmd.exe'), '/c', s]`

  Here `s` is the string itself, or for a list, its arguments quoted and joined. Range expansion runs on the string before wrapping. `--sudo` prefixes the whole thing: `[sudo, /bin/sh, -c, s]`.

### Range expansion

**Order of evaluation** for each comma-separated piece inside `[...]`:

1. **Assignment:** the piece contains exactly one *unescaped* `:` and the text before it is a valid Python identifier → `name:value` assignment, no output (unchanged semantics). Otherwise the piece is not an assignment.
2. **Range** `a-b` (split on the first `-`):
   1. Both ends decimal → decimal range padded to the shorter end's width (unchanged).
   2. Both ends single characters from `0-9a-zA-Z` → character range in `0-9a-zA-Z` order (now *before* hex, so `[A-C]` → `A B C`).
   3. Both ends hex → hex range, zero-padded to the shorter end's width. Uppercase if some end has an uppercase `A-F` letter and none has a lowercase one; lowercase otherwise. Example: `[08-0b]` → `08 09 0a 0b`.
3. **Plain token or variable reference** (unchanged).

**Escape:** `\:` inside brackets means a literal colon and is unescaped in the output: `[web1\:8080,web2\:8080]` → `web1:8080`, `web2:8080`. A piece with two or more unescaped colons (IPv6) isn't an assignment and passes through literally. Escaping only applies inside brackets.

**Variable scope:**
- The module-global `__variables` is removed.
- `_expand_ranges_fast(inStr, variables=None)`: `None` means a fresh dict for this call.
- `run_commands`/`AsyncExecutor.run_commands` create one dict per call and pass it through `_build_tasks` → `__format_command`, so variables are shared across that call's commands and list fields, and nowhere else.
- Thread-safe, because nothing is shared between calls.

## G. Library API changes (#10, #16, #17, Task timing)

### Dry run (#10, library)

- **`run_command(s)` with `dry_run=True`:** handled before any thread starts. It prints, in order, one line per expanded command: the arguments shell-quoted and joined, including the `sudo` prefix and shell wrapper, so it can be pasted into a shell. It prints nothing when `quiet=True`, never runs `sudo -v`, and returns as today. Return codes stay `None`.
- **`AsyncExecutor` dry run:** still goes through its dispatcher, which preserves the permit/queue semantics an existing test relies on. It prints the same one-line format through the display writer. Line order follows dispatch and isn't guaranteed.
- **Dry-run tasks** (`returncode is None`) are not failures for `raise_error`, the summary, or the CLI exit code.
- **Downstream:** hpcp's dry-run output changes format, as agreed.

### `CommandError` (#16)

```python
class CommandError(Exception):
	def __init__(self, tasks):  # tasks: the failed Task objects
		self.tasks = tasks
```

Message format. It still starts with `Commands failed`, so existing `match=` users and tests keep working:

```
Commands failed (2 of 3):
  [rc 1] false
  [rc 3] sh -c 'echo oops >&2; exit 3': oops
```

Each row shows the quoted command and the last non-empty line of `task.stderr`, or of `task.stdout` in pty mode, truncated to 200 characters. Raised by `run_command`/`run_commands` when `raise_error=True`.

### `AsyncExecutor` timing and report (#17)

- **`wait(timeout=None, threads=...)`:**
  - `None` or negative waits until every queued and running command is done.
  - A positive number is one overall deadline for the whole call: each thread join gets the *remaining* time.
  - Returns the still-running threads, as today.
- **`join(timeout=None, threads=..., print_error=True)`:** waits as above, then reports:
  - finished with non-zero rc → `failed with return code N`, plus output as today;
  - stopped before starting (`thread is None`, `stop`, `returncode is None`) → `skipped`;
  - still running → `still running`.
- **`stop(timeout=None)` and `cleanup(timeout=None)`:** same waiting rule.
- **The constructor's `timeout`** is now only the per-command inactivity timeout. It's no longer used as a wait limit.
- **`KeyboardInterrupt` during `wait`/`join`:** stops the executor's commands (§A), then re-raises.

### `Task` additions

- `start_time` (wall-clock `time.time()` at spawn; `None` if never started).
- `end_time` (set when finalized).
- `duration` property (`end_time - start_time`, or `None`).
- Private `_last_output` (monotonic) and `_finalized`.
- `__iter__`/`dict(task)` unchanged.

### New public names and parameters

| Name | Kind |
|---|---|
| `CommandError` | exception class |
| `kill_all(sig=signal.SIGTERM)` | function |
| `shell=False`, `prefix=False`, `color=None`, `pty=False` | parameters on `run_command`, `run_commands`, `AsyncExecutor.__init__`, `AsyncExecutor.run_command(s)` (override pattern as today) |
| `Task.start_time`, `Task.end_time`, `Task.duration` | attributes |

## H. CLI (#1, #2, #10, #11, #12, #14)

```
mcmd [-h] [-p] [-t SECONDS] [-m N] [--sudo] [-q] [-n] [--shell]
     [-f FILE] [-a FILE] [--summary | --no-summary] [--no-prefix]
     [--no-color] [--pty | --no-pty] [-V] [command ...]
```

| Flag | Behaviour |
|---|---|
| `-t SECONDS` | Default **0**. Help: "kill a command after SECONDS with no output at all (0 = never)" |
| `-n`, `--dry-run` | Print each expanded command once, quoted, one per line, **even with `-q`**; no `sudo -v`; exit 0 |
| `--shell` | Run each command through `/bin/sh -c` |
| `-f FILE` | Repeatable. Each line is a command, like a positional argument. Blank lines and lines whose first non-space character is `#` are skipped. `-` = stdin |
| `-a FILE` | Repeatable (lines concatenated in order). Each positional command is a template; one command per template × input line. `-` = stdin |
| `--summary` / `--no-summary` | All rows / no summary |
| `--no-prefix`, `--no-color` | See §C |
| `--pty` / `--no-pty` | See §D |

### Input files (#11)

- **Order:** positional commands first, then `-f` lines in file order. With `-a`, for each template in order, for each input line in order.
- **Validation:** `-f -` and `-a -` together is a usage error (exit 2). No commands at all is a usage error (exit 2). The positional `nargs` changes from `+` to `*`.
- **`{}` substitution:**
  - Happens *after* splitting, per argument (`arg.replace('{}', line)`), so an input line is always exactly one argument, even with spaces. `--file={}` works.
  - In `--shell` mode, `shlex.quote(line)` is substituted into the template string.
  - A template without `{}` gets the line appended as a final argument (GNU parallel style).
- **With `-p`:** the template is range-expanded first. Input lines are data and are never range-expanded.
- **Input lines:** only the trailing newline is stripped, and blank lines are skipped. `#` isn't special in `-a` input.

### Exit status (#1)

| Situation | Exit code |
|---|---|
| Dry run, or every command succeeded | 0 |
| Exactly one command (after expansion and input) | Its return code. Killed by signal N (rc −N) → 128+N |
| Several commands | Number of failed commands, capped at 101 |
| Usage error (argparse, unbalanced quote, `-f -` with `-a -`, no commands) | 2 |
| Ctrl-C | 130 |
| SIGTERM / SIGHUP received | 128+N |

Documented in `mcmd -h` (argparse epilog) and the README.

### Summary (#12)

- **When:** printed by the CLI after all output when there is more than one command and it's not a dry run. `-q` doesn't hide it; `--no-summary` does.
- **Default:** one table row per failed command, then a tally line. With no failures, only the tally (`50 commands: all succeeded`). `--summary` adds a row for every command.
- **Columns:** `#` | `Command` (quoted) | `Return code` | `Time`.
- **Return-code labels:** 124 `timeout`, 126 `cannot execute`, 127 `not found`, negative −N `SIG<name>`.
- **Time:** `duration` with 2 decimals.
- Rendered with `pretty_format_table` (already in the module), so it fits the terminal width.
- **Tally format:** `N commands: S succeeded, F failed`.

### `__main__` placement (#14)

`if __name__ == '__main__': main()` moves to the last lines of the file, because the summary calls helpers defined after the current position. The embed path is unaffected: it executes into a module namespace named `multiCMD`, not `__main__`.

## I. Windows best-effort (#18)

- Launch and kill as in §A: no groups, `terminate()`/`kill()`, no signal mask, no `killpg`. `kill_all` terminates the registered `Popen`s.
- `ping()` uses `-n 1` on Windows.
- No pty (§D).
- `input_with_timeout_and_countdown` polls `msvcrt.kbhit()` when `select` can't watch stdin.
- POSIX-only imports (`pty`, `termios`, `fcntl`) happen lazily inside POSIX branches.
- README: "Windows: best-effort and untested".

## J. Packaging and release prep (#15)

- **Remove `argparse`:**
  - multiCMD: `install_requires` in `setup.py` and the `# /// script` dependency list.
  - Same removal in multiSSH3, TSVZ, hpcp, statblk, statbtrfs (`setup.py`) and simple-iotest (`pyproject.toml`).
  - One commit per repo, no version bumps or releases for those six.
- **multiCMD release prep:**
  - Bump `version = '1.52'` and `COMMIT_DATE`.
  - Regenerate `multiCMD_min.py` and the embed snippet with the documented command (Python 3.11, python-minifier 3.4.0).
  - Paste the new snippet into hpcp.py, statblk.py and statbtrfs.py. Their version gate becomes `(1, 52)`. Commit in each repo; don't bump those tools' versions.
- **README:** the timeout definition, new flags, exit status, pty mode, the Windows note, and the "no third-party runtime dependencies" line (now true).
- **Not done without explicit approval:** `uvpack`, `git push`, `cas-toolbox` pin changes.

## Downstream impact

| Tool | Effect |
|---|---|
| hpcp | Dry-run output format changes (agreed). Non-quiet parallel output gets colors on a TTY. Commands are killed by process group. Ctrl-C still stops its commands (waiting calls forward it); its `except KeyboardInterrupt` exit path triggers the `atexit` cleanup for any rest. |
| statblk | Fire-and-forget path unchanged (`task.thread.join` / `is_alive` keep working). `timeout=` kills now reach grandchildren. String commands parse identically under `shlex`. |
| statbtrfs | `run_commands(max_threads=0, wait_for_return=...)` unchanged in behaviour. |
| All three | Embedded snippet must be re-pasted (gate `(1, 52)`). |

## Testing

All new tests live under `tests/`, use real subprocesses (per the 2026-08-10 test-suite spec), and assert invariants, not exact timings.

| Area | Tests |
|---|---|
| A | Timeout kills a backgrounded grandchild and returns within ~3s (`sh -c 'sleep 30 & wait'`, `timeout=1`); `stop()` likewise; `atexit`/`kill_all` kills a fire-and-forget group (checked in a child Python process); Ctrl-C forwarding (send SIGINT to a CLI subprocess running `sleep 30`, expect exit 130 and no surviving `sleep`); SIGTERM to the CLI kills its commands; `/dev/tty` read fails fast (under `script -qec`, skip if `script` is missing); missing command → 127 on both launcher paths |
| B | The reader's captured output and live display are byte-identical to the 1.51 implementation on the 11 recorded inputs and on 1/2/3/5-byte chunking (1.51's `__handle_stream` copied into the test as the reference); `\r`-only progress with `timeout=1` is **not** killed; partial-line dots are not killed |
| C | Prefix only when concurrent and >1 command; `--no-prefix`; colors off when not a TTY / `NO_COLOR` set; shared-line `\r` rule (scripted interleavings through the display writer into a `StringIO`) |
| D | Under pty: child sees `isatty(1)`; stderr is merged into `task.stdout`; no `\r\n` translation; `PAGER=cat`; pipe fallback when `openpty` is monkeypatched to fail |
| E | 5,000 × `true` with `max_threads=4` keeps live threads ≤ baseline + 4 + 2×4; shared semaphore still bounds concurrency |
| F | `shlex` quoting; unbalanced quote → `ValueError`; `--shell` pipes; range cases from §F (case, hex padding, IPv6, `\:`, per-call variables, no leak between calls) |
| G | Dry-run format, order, no `sudo -v`; `CommandError` fields and message; executor `join()` with default timeout waits; overall-deadline semantics; failed/skipped/still-running report; `Task.start_time`/`end_time`/`duration` |
| H | Exit status table; `-t` default; `-n` with `-q`; `-f`/`-a` incl. stdin, comments, `{}` append rule, `-f - -a -` error; summary rows/tally/labels, `--summary`, `--no-summary`, `-q` keeps it; running `python multiCMD.py` directly prints the summary without `NameError` |

**Existing tests to update:**
- `conftest.py` and `test_expand_ranges.py` stop touching `multiCMD.__variables` (removed).
- The `match="Commands failed"` assertions stay valid.
- `test_cli.py`'s `Running command:` assertions stay valid (sequential, so no prefix).

**Python versions:**
- Run the suite on **3.9** (wrapper path) and **3.13** (`process_group` path), both installed here.
- Check 3.6 syntax compatibility with `uvx vermin --target=3.6- multiCMD.py` (a dev-time tool, not a dependency).
- After regenerating, smoke-test `multiCMD_min.py` by importing it and running one command.

**Performance check (manual, not in pytest):** `seq 1 3000000` throughput before and after, recorded in the commit message.

## Implementation order

Each phase ends with the full suite green on 3.9 and 3.13.

1. **Launch/kill (§A)** + Windows guards for those paths.
2. **Reader + activity timer (§B).**
3. **Display, prefix, colors (§C).**
4. **Worker pool (§E).**
5. **pty mode (§D).**
6. **Parsing (§F).**
7. **Library API (§G).**
8. **CLI (§H)** + `__main__` move + README/help.
9. **Remaining Windows bits (§I).**
10. **Packaging and release prep (§J)** across repos (no publish).

## Risks and notes

- **SIGTERM/SIGHUP handling in the CLI and the public `kill_all()`** go slightly beyond the item-by-item discussion. They are the consequence of moving commands out of mcmd's process group (otherwise `timeout 10 mcmd …` leaves orphans). Flagged for review.
- **Exec wrapper on Python < 3.11:** about 5 ms per spawn, and the process briefly appears as `python` until `exec`.
- **Signal reach under `--sudo`:** signals reach root-owned commands only via sudo's own relaying. SIGKILL can't be relayed, which is no worse than 1.51.
- **The shared-line `\r` rule** can't see writes made by host programs (e.g. hpcp's own prints).
- **pty mode and colors:** commands see a TTY, so they may emit colors and progress bars into `task.stdout`. That's the intent for CLI use; the library stays opt-in.
- **The spec is large but confined to one module plus mechanical edits elsewhere.** It can be split into separate plans per phase group if preferred.
