# multiCMD

Run many commands at the same time — from the shell or from Python.

`multiCMD` is a small, dependency-light helper (a single `multiCMD.py` module) for
launching and supervising multiple subprocesses concurrently. It streams each
command's output in real time (optionally colorized per command), captures
stdout/stderr/return codes, supports per-command inactivity timeouts, and can
expand range patterns like `host[1-10]` into many commands at once.

It works both as a command-line tool and as an importable wrapper around
`subprocess` for your own automation scripts.

## Features

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

## Install

```bash
pip install multiCMD
```

This installs the module and three equivalent console entry points:

```bash
mcmd
multiCMD
multicmd
```

You can also just drop `multiCMD.py` into your project and import it directly.

## Command-line usage

```bash
$ mcmd -h
usage: mcmd [-h] [-p] [-t SECONDS] [-m max_threads] [--sudo] [-q] [-n]
            [--shell] [-e NAME=VALUE] [-f FILE] [-a FILE] [--summary |
            --no-summary] [--no-prefix] [--no-color] [--pty | --no-pty] [-V]
            [command ...]

Run multiple commands in parallel

positional arguments:
  command               commands to run ( with -a: templates, {} is replaced
                        by each input line )

options:
  -h, --help            show this help message and exit
  -p, --parse           Parse ranged input and expand them into multiple
                        commands
  -t, --timeout SECONDS
                        kill a command after SECONDS with no output at all ( 0
                        = never, the default )
  -m, --max_threads max_threads
                        maximum number of commands to run at once ( 0 = all )
  --sudo                use sudo for commands
  -q, --quiet           quiet mode: hide command output ( the summary still
                        shows )
  -n, --dry-run         print each command, quoted, instead of running it
  --shell               run each command through /bin/sh -c ( pipes, ;, > and
                        globs work )
  -e, --env NAME=VALUE  set NAME to VALUE in the environment of every command
                        ( repeatable ); NAME alone removes it
  -f, --file FILE       read commands from FILE, one per line ( - for stdin;
                        blank lines and # comments are skipped )
  -a, --arg-file FILE   run each command template once per line of FILE, with
                        {} replaced by the line ( - for stdin )
  --summary             list every command in the end-of-run summary (
                        default: only failed ones )
  --no-summary          do not print the end-of-run summary
  --no-prefix           do not prefix output lines with [#] when commands run
                        at once
  --no-color            do not color output lines per command
  --pty                 give commands a pseudo-terminal so they print live (
                        default when stdout is a terminal )
  --no-pty              give commands plain pipes
  -V, --version         show program's version number and exit

exit status:
  0      every command succeeded, or --dry-run
  N      one command: its own exit code ( killed by signal S: 128+S )
  N      several commands: how many failed, capped at 101
  2      usage error
  130    interrupted with Ctrl-C ( 128+S after SIGTERM / SIGHUP )
```

### Examples

Run two commands sequentially (default `--max_threads 1`):

```bash
mcmd "echo hello" "echo world"
```

Run them concurrently:

```bash
mcmd -m 4 "echo hello" "echo world"
```

Expand a range into many commands and run 8 at a time:

```bash
mcmd -p -m 8 "ping -c1 192.168.1.[1-254]"
```

Kill any command that goes silent for more than 30 seconds (along with everything it started):

```bash
mcmd -t 30 -m 16 "long-running-task [1-100]"
```

Run with `sudo` (falls back gracefully if `sudo` is unavailable or you are already root):

```bash
mcmd --sudo "systemctl restart myservice"
```

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

> Each positional argument is one command. It is split into arguments with shell
> quoting rules (`shlex`), but not run through a shell unless you pass `--shell`.
> With `-m/--max_threads > 1`, at most that many commands run at once, each with two
> threads draining its output. `stdin` is connected to `/dev/null` — multiCMD does
> not feed input to commands.

### Exit status

| Situation | Exit code |
|---|---|
| Every command succeeded, or `--dry-run` | `0` |
| Exactly one command (after `-p` expansion and input files) | its own exit code; killed by signal N → `128+N` |
| Several commands | the number that failed, capped at `101` |
| Usage error (bad option, unbalanced quote, no commands) | `2` |
| Ctrl-C | `130` (`128+N` after SIGTERM / SIGHUP) |

### Input files and templates

`-f FILE` reads one command per line (blank lines and `#` comments are skipped).
`-a FILE` turns each positional command into a template and runs it once per line of
FILE, with `{}` replaced by the line; a template without `{}` gets the line as its
last argument. `-` reads stdin. The line always stays one argument, and with `-p` only
the template is range-expanded, never the line.

With `--shell`, an `-a` line is never pasted into the shell code: it is passed to
`/bin/sh -c` as `"$1"`, so `{}` may be written bare, as `"{}"` or as `'{}'`, and the
line is still only data. A dry run shows this as `/bin/sh -c 'echo "$1"' mcmd LINE`.

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

## Range / pattern expansion (`-p` / `parse=True`)

When parsing is enabled, bracketed patterns are expanded into the cartesian
product of all options:

| Pattern              | Expands to                                  |
| -------------------- | ------------------------------------------- |
| `host[1-3]`          | `host1`, `host2`, `host3`                   |
| `host[01-03]`        | `host01`, `host02`, `host03` (zero-padded)  |
| `item[a-c]`          | `itema`, `itemb`, `itemc`                   |
| `v[a-f]`             | `va` … `vf` (hex range)                      |
| `x[1-2,a-b]`         | `x1`, `x2`, `xa`, `xb` (comma list)          |
| `[1-2]-[a-b]`        | `1-a`, `1-b`, `2-a`, `2-b` (multiple groups) |
| `host[A-C]`          | `hostA`, `hostB`, `hostC` (case kept)       |
| `n[08-0b]`           | `n08`, `n09`, `n0a`, `n0b` (hex, padded)    |
| `[web1\:8080]`       | `web1:8080` (`\:` is a literal colon)        |
| `[n:3]host[1-n]`     | `host1`, `host2`, `host3` (variables)        |
| `host[{2+3}]`        | `host5` (`{...}` is evaluated as Python)     |

Notes:

- Decimal padding follows the **shorter** endpoint, so `[0-10]` yields
  `0..10` (unpadded) while `[01-10]` yields `01..10`.
- `name:value` inside brackets assigns a variable (the bracket itself produces
  no output) that later brackets can reference.
- Variables are shared by the commands of one call (one `mcmd` run, one `run_commands`
  call) and never leak into the next.
- `name:value` is an assignment only with exactly one `:` and a valid name before it;
  IPv6 addresses like `[fe80::1]` pass through, and `\:` gives a literal colon.
- `{expr}` is evaluated as a Python expression with the current variables in
  scope. Only use this with trusted input.

## Python API

```python
import multiCMD

# Run a single command, return its stdout lines
out = multiCMD.run_command(["echo", "hello"], quiet=True)
# -> ["hello"]

# Run several in parallel
results = multiCMD.run_commands(
    [["echo", "hello"], ["echo", "world"]],
    max_threads=4, quiet=True,
)
# -> [["hello"], ["world"]]
```

### Getting return codes and the full `Task`

```python
# Just the return code
rc = multiCMD.run_command(["false"], return_code_only=True, quiet=True)  # -> 1

# The full Task object (command, returncode, stdout, stderr)
task = multiCMD.run_command(["echo", "hi"], return_object=True, quiet=True)
print(task.returncode, task.stdout, task.stderr)  # 0 ['hi'] []
```

### Shell mode, prefixes, colors and pty

```python
multiCMD.run_command("dmesg | tail -n 3", shell=True)            # run through /bin/sh -c
multiCMD.run_commands(cmds, max_threads=8, prefix=True)           # [#] on each line
multiCMD.run_commands(cmds, max_threads=8, color=False)           # never color
multiCMD.run_command(["python3", "train.py"], pty=True)           # live output, stderr merged
```

### Environment per call

```python
multiCMD.run_command(["xfs_info", "/data"], extra_env={"LC_ALL": "C"})      # add / override variables
multiCMD.run_command(["env"], extra_env={"HTTP_PROXY": None})               # None removes one
multiCMD.run_command(["env"], env={"PATH": "/usr/bin", "HOME": "/tmp"})     # the whole environment, as subprocess's env
```

`env` is the whole environment of the command, as `subprocess`'s `env` ( names and values
are `str`; on POSIX `bytes` and `os.PathLike` work too ). `extra_env` is applied on top of the
inherited environment, or of `env` when both are given; a `None` value removes the variable,
and an empty `extra_env` is the same as none. "Inherited" is `os.environ` as the process holds
it when the command starts: a variable set behind Python's back ( `os.putenv`, `os.unsetenv`, a
C library's `setenv` ) reaches commands started without `env` / `extra_env`, not the ones
given them. An inherited variable with an empty name ( a corrupt `environ` entry such as `=x` ) is left out of the environment of a command given `env` / `extra_env`, and out of an `env` copied from `os.environ`; an empty name in `extra_env` is a `ValueError`. Names are case-insensitive on Windows, so `extra_env={"Path": ...}` replaces `PATH`
there. On POSIX a command is looked up in the `PATH` of the environment it gets, on every Python
version; Windows searches the calling process's `PATH` whatever the command's environment says.
Before Python 3.11 the launcher starts in this process's environment and is handed the command's on
top of it, so the two together must fit the system's argument limit ( `getconf ARG_MAX`; 2 MiB on Linux
with the default stack, 128 KiB for one string: before Python 3.11 an entry travels as `_MULTICMD_ENV_<i>=NAME=VALUE`, 15 bytes
plus the digits of `i` more than its `NAME=VALUE` ); one that does not fit fails with return
code 126, `Argument list too long`, on every Python.
On Python 3.7-3.10 a command started with
neither `env` nor `extra_env` still gets the `LC_CTYPE=C.UTF-8` that interpreter's locale coercion
sets in a C locale ( as before ); a command given one of them gets exactly the environment described.
Both are per command: they never change `os.environ` and never reach another command, so
concurrent commands each see their own. `AsyncExecutor` takes them too, as defaults a call
overrides. `sudo` resets the environment by default, so with `use_sudo` they may not reach the
command. On the command line: `mcmd -e LC_ALL=C -e UNSET_ME "cmd"`; `-n` shows them as
`env LC_ALL=C cmd` ( `env -i ...` for a whole environment ).

`raise_error=True` raises `multiCMD.CommandError`; its `.tasks` holds the failed `Task`
objects and its message lists each one with its return code and last error line.
`Task` also records `start_time`, `end_time` and `duration`. With `dry_run=True` nothing
runs; each command is printed once, quoted so it can be pasted into a shell.
A command that is not found (return code 127), cannot be executed (126) or that multiCMD
itself could not run (-1, `task._error`) says why in its `task.stderr`; multiCMD also
prints a line saying so on stderr, unless `quiet=True`. Before Python 3.11 the exec of a
command given by its path, or of a file found in `PATH` that may not be executed, happens in
the launcher's process: it exits 127 / 126 with the reason as the command's own stderr, which
then shows with the command's output ( unless quiet ) instead of as a line of multiCMD's own.

### Asynchronous / fire-and-forget

Use `quiet=True` with `wait_for_return=False` to launch commands on daemon
threads. The returned `Task` objects are updated in place as commands finish:

```python
tasks = multiCMD.run_commands(
    [["sleep", "2"], ["sleep", "1"]],
    max_threads=2, quiet=True,
    wait_for_return=False, return_object=True,
)
# tasks[i].returncode is None until that command completes; tasks[i].thread.join(timeout)
# waits for one command ( a command whose thread could not start fails with -1, its thread already done )

# Later, block until everything launched this way has finished:
multiCMD.join_threads()
```

For managing larger or repeated batches, use `AsyncExecutor`:

```python
ex = multiCMD.AsyncExecutor(max_threads=8, timeout=30, quiet=True)
ex.run_command(["./worker", "--job", "1"])
ex.run_commands([["./worker", "--job", "2"], ["./worker", "--job", "3"]])
ex.join()                 # wait and print any failures
print(ex.get_return_codes())
print(ex.get_results())
```

`run_command(s)` returns right away and queues the commands. `wait()`, `join()` and
`stop()` wait until everything is done by default; a `timeout=` is one overall limit in
seconds (the constructor's `timeout` is only the per-command inactivity timeout).
`AsyncExecutor` takes the same `shell`, `prefix`, `color` and `pty` options as
`run_commands`, as constructor defaults or per call. One dispatcher thread
starts each queued command only after taking a permit from the semaphore, so the
executor holds at most one thread per permit plus the dispatcher, however many
commands are queued. A command's `task.thread` is `None` until it starts, and goes
back to `None` once it has finished and the executor has joined its thread, so a
large batch does not keep a `Thread` object per finished command. `stop()` skips
commands that have not started yet, and so does a Ctrl-C ( or an error ) that interrupts
`run_command(s)`, for that call's commands.

### Range expansion from Python

```python
multiCMD.run_commands([["echo", "[0-10]"]], quiet=True, parse=True)
# -> [["0"], ["1"], ..., ["10"]]
```

### Using sudo

```python
multiCMD.set_sudo(True)            # validates sudo is present and you aren't root
multiCMD.run_command(["systemctl", "restart", "nginx"])
# or per-call:
multiCMD.run_command(["id"], use_sudo=True)
```

If `sudo` is not on `PATH`, or you are already root, the request is ignored with
a warning instead of failing.

## Timeout semantics

`timeout` is an **inactivity timeout**, not a maximum total runtime. A command is
killed once it has produced **no output at all** on stdout or stderr for `timeout`
seconds. Any byte counts, including a progress line redrawn with `\r` and a partial
line without a newline.

On timeout, the command **and everything it started** are killed, the task's return
code is set to `124`, and `Timeout!` is appended to its stderr. `timeout=0` (the
default, in the API and on the command line) disables it.

## Process groups and signals

Every command runs in a process group of its own, so a timeout, `stop()` or Ctrl-C reaches the
whole process tree, not just the first process. On Python 3.11+ this uses `Popen(process_group=0)`;
older Pythons start the command through a tiny exec wrapper. A command that reads the terminal (a
password prompt, for example) fails at once instead of hanging: multiCMD blocks SIGTTIN/SIGTTOU for
it, and `mcmd` also ignores them, which every command inherits through any shell.

Signals sent to your program's group do not reach the commands, so multiCMD stops them itself. One
Ctrl-C (or a SIGTERM handler's `sys.exit()`) in a call waiting on the main thread, `join_threads()`
and `AsyncExecutor.wait()` included, stops every command, skips the queued ones and
re-raises, giving them up to 5 s to exit; a second Ctrl-C ends that wait at once. `mcmd` stops its
commands on SIGTERM / SIGHUP (unless started with them ignored, as under `nohup`). At interpreter exit
the commands still running are killed, and a command queued on one of multiCMD's threads fails (-1,
"not started: the interpreter is exiting") instead of outliving the program; one run from an `atexit`
handler after that, on the main thread, runs as usual. A program that handles SIGTERM itself, or runs
multiCMD from its own threads (where Ctrl-C never arrives), can call `multiCMD.kill_all()` from its
handler. A command not seen to exit a second after SIGKILL is no longer tracked; its pid is named on
stderr, unless quiet.

multiCMD never touches SIGINT's handler (`mcmd` sets its own SIGTERM / SIGHUP handlers, nothing
else). What your handler raises while a call waits for its commands is handled as a Ctrl-C.

A command is registered from the moment `Popen` returns it: a Ctrl-C that Python raises inside
`Popen` as a serial run starts a command (some 50-70 µs each) can leave it running, unknown to
`kill_all()`. Two Ctrl-Cs microseconds apart can leave a command running, still registered, until
`kill_all()` or the exit stops it. On Python 3.6-3.9, which raise a Ctrl-C at nearly any instruction,
even one at a few points between the steps of a run can leave its command registered but not signalled.
On Python 3.6-3.9 and 3.14 a Ctrl-C at the wrong instruction of a `with` on a lock can leave it held: one of
`threading`'s can hang the program as its threads end (CPython's own), and one of multiCMD's (an
`AsyncExecutor`'s queue, the live output) hangs that executor, or the next call that is not quiet, if the
program keeps going.
These and the other known limits are in [docs/limitations.md](docs/limitations.md).

## Key functions and objects

```python
run_command(command, timeout=0, max_threads=1, quiet=False, dry_run=False,
            with_stdErr=False, return_code_only=False, return_object=False,
            wait_for_return=True, sem=None, use_sudo=..., raise_error=False,
            prefix=False, color=None, pty=False, shell=False, env=None, extra_env=None)

run_commands(commands, timeout=0, max_threads=1, quiet=False, dry_run=False,
             with_stdErr=False, return_code_only=False, return_object=False,
             parse=False, wait_for_return=True, sem=None, use_sudo=...,
             raise_error=False, prefix=False, color=None, pty=False, shell=False,
             env=None, extra_env=None)

ping(hosts, timeout=1, max_threads=0, ...)   # returns True/False reachability
join_threads(threads=..., timeout=None)      # join fire-and-forget threads
kill_all(sig=None)                           # stop every running command and what it started
set_sudo(use_sudo)                           # enable/disable sudo globally

class Task:          # command, returncode, stdout, stderr, start_time, end_time, duration
class CommandError:  # raised by raise_error=True; .tasks = the failed Tasks
class AsyncExecutor: # run_command(s), wait, join, stop, cleanup, get_results, get_return_codes;
                     # takes the options above, env / extra_env as defaults a call overrides
```

The module also bundles a few terminal/formatting helpers used internally and
reusable on their own: `pretty_format_table`, `parseTable`, `print_progress_bar`,
`format_bytes`, `get_terminal_size`, `input_with_timeout_and_countdown`, and
`slugify`.

## Platform support

Linux and macOS are fully supported. Windows is best-effort and untested: commands run
and time out, but a timeout or stop only reaches the first process, pty mode is not
available, and shell mode needs command strings (`-a` with `--shell` is not supported).

## Development

Run the test suite with [pytest](https://pytest.org):

```bash
pip install pytest
pytest                 # full suite under tests/
pytest -m "not slow"   # skip intentional sleep-heavy cases
```

`test_expand_ranges_perf.py` is a manual performance microbenchmark and is not collected by pytest.

## Upgrading to 1.56 from 1.55

- Fix: a command that times out while it redraws a `\r` progress line on stderr keeps its `Timeout!`
  line; 1.55 could replace it with the progress line, leaving return code 124 without the marker.
- Simpler interrupt handling, some 700 lines lighter. One Ctrl-C still stops every command.
  What changes is a few rare corners, each in [docs/limitations.md](docs/limitations.md):
  - two Ctrl-Cs microseconds apart can leave a command running, still registered, until `kill_all()`
    or the exit stops it; 1.55 stopped it at once;
  - a Ctrl-C can lose a serial run's starting command anywhere inside `Popen` (about 51 µs per start on
    Python 3.14), not only inside the fork (about 41 µs);
  - a Ctrl-C that leaves `Popen.poll()`'s lock held makes that command's stop about 2 s slower;
  - an ordinary exception your own signal handler raises as a serial run starts a command is recorded as
    a start failure (-1) and not raised, as in 1.54;
  - `join_threads()` on another thread can return before a batch that is still starting.
- `__running_threads_lock` and `_live_procs_lock`, unused since 1.55 and kept only for code that imported
  them, are gone.

## Upgrading to 1.55 from 1.54

- New: a per-call environment. `env=` (the whole environment, as `subprocess`'s) and `extra_env=`
  (added on top; a `None` value removes a variable) on `run_command`, `run_commands` and
  `AsyncExecutor` (constructor defaults, overridden per call), and `mcmd -e NAME=VALUE` / `-e NAME`;
  see [Environment per call](#environment-per-call).
- `quiet=True` now hides the lines about a command that could not be run: `Command path not found`
  (127), `Command cannot be executed` (126) and the error of a command multiCMD could not run (-1),
  which 1.54 printed even when quiet, partly on stdout. The reasons are still in `task.stderr`. Warnings
  about the run's setup (sudo not found or not usable, no pty) still print.
- A Ctrl-C in `join_threads()` stops the commands it waits for, skips the ones still queued, waits up
  to 5 s for them and re-raises; 1.54 re-raised and left them running. A Ctrl-C that interrupts
  `AsyncExecutor.run_command(s)` skips that call's commands.
- A SIGTERM handler's `sys.exit()`, or whatever your own signal handler raises while a call waits for
  its commands, stops them and records them as stopped before it is raised on, as a Ctrl-C does; 1.54
  did that only for `KeyboardInterrupt`.
- A Ctrl-C while a command starts, times out or is stopped, or while multiCMD reads the output that
  a finished command's background processes still hold open, no longer leaves the command or those
  processes running unknown to `kill_all()` and the exit hook, or known to them but never signalled.
  A second Ctrl-C ends the wait for the commands at once; each is still sent SIGTERM, and `kill_all()`
  and the exit hook finish the kill. The limits below are the exceptions.
- Once the interpreter exits, a command queued on one of multiCMD's threads that has not started fails
  with -1 ("not started: the interpreter is exiting") instead of starting and outliving the program.
- A command not seen to exit a second after SIGKILL is no longer tracked, so later `kill_all()` calls and the
  exit do not wait for it again; its pid is named on stderr, unless quiet.
- Known limits: see [Process groups and signals](#process-groups-and-signals) and
  [docs/limitations.md](docs/limitations.md).

## License

GPLv3+ — see the package metadata. Authored by Yufei Pan (<pan@zopyr.us>).
```