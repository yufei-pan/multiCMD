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

- Run a batch of commands in parallel with a bounded thread pool.
- Live, non-blocking, optionally per-command colorized output.
- Capture `stdout`, `stderr`, and return code per command via the `Task` object.
- Per-command **inactivity timeout** (see [Timeout semantics](#timeout-semantics)).
- Range/pattern expansion: `host[1-3]`, `[01-03]`, `[a-f]`, `[1-2,a-b]`, variables, and `{...}` expressions.
- Fire-and-forget async mode (`wait_for_return=False`) plus an `AsyncExecutor` for managing long-lived batches.
- Optional `sudo` wrapping.
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
usage: mcmd [-h] [-p] [-t timeout] [-m max_threads] [--sudo] [-q] [-V]
            command [command ...]

Run multiple commands in parallel

positional arguments:
  command               commands to run

options:
  -h, --help            show this help message and exit
  -p, --parse           Parse ranged input and expand them into multiple commands
  -t, --timeout timeout
                        timeout for each command
  -m, --max_threads max_threads
                        maximum number of threads to use
  --sudo                use sudo for commands
  -q, --quiet           quiet mode
  -V, --version         show program's version number and exit
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

Kill any command that goes silent for more than 30 seconds:

```bash
mcmd -t 30 -m 16 "long-running-task [1-100]"
```

Run with `sudo` (falls back gracefully if `sudo` is unavailable or you are already root):

```bash
mcmd --sudo "systemctl restart myservice"
```

> Each positional argument is one command. With `-m/--max_threads > 1`, a worker
> thread is spawned per command; each worker uses `subprocess` to run the command
> and two extra threads to drain stdout/stderr without blocking. `stdin` is
> connected to `/dev/null` — multiCMD does not feed live input to commands.

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
| `[n:3]host[1-n]`     | `host1`, `host2`, `host3` (variables)        |
| `host[{2+3}]`        | `host5` (`{...}` is evaluated as Python)     |

Notes:

- Decimal padding follows the **shorter** endpoint, so `[0-10]` yields
  `0..10` (unpadded) while `[01-10]` yields `01..10`.
- `name:value` inside brackets assigns a variable (the bracket itself produces
  no output) that later brackets can reference.
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

### Asynchronous / fire-and-forget

Use `quiet=True` with `wait_for_return=False` to launch commands on daemon
threads. The returned `Task` objects are updated in place as commands finish:

```python
tasks = multiCMD.run_commands(
    [["sleep", "2"], ["sleep", "1"]],
    max_threads=2, quiet=True,
    wait_for_return=False, return_object=True,
)
# tasks[i].returncode is None until that command completes

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
killed only after it has produced **no new committed output line** for `timeout`
seconds. An output line is "committed" when the stream handler encounters a `\n`
or `\r`.

This means a command that keeps printing output will keep running, while one that
hangs silently will be terminated after `timeout` seconds. Set `timeout=0` (the
default in the API) to disable the timeout entirely. On timeout, the task's
return code is set to `124` and `Timeout!` is appended to its stderr.

## Key functions and objects

```python
run_command(command, timeout=0, max_threads=1, quiet=False, dry_run=False,
            with_stdErr=False, return_code_only=False, return_object=False,
            wait_for_return=True, sem=None, use_sudo=..., raise_error=False)

run_commands(commands, timeout=0, max_threads=1, quiet=False, dry_run=False,
             with_stdErr=False, return_code_only=False, return_object=False,
             parse=False, wait_for_return=True, sem=None, use_sudo=...,
             raise_error=False)

ping(hosts, timeout=1, max_threads=0, ...)   # returns True/False reachability
join_threads(threads=..., timeout=None)      # join fire-and-forget threads
set_sudo(use_sudo)                            # enable/disable sudo globally

class Task:    # command, returncode, stdout (list[str]), stderr (list[str])
class AsyncExecutor:  # run_command(s), wait, join, stop, cleanup, get_results, get_return_codes
```

The module also bundles a few terminal/formatting helpers used internally and
reusable on their own: `pretty_format_table`, `parseTable`, `print_progress_bar`,
`format_bytes`, `get_terminal_size`, `input_with_timeout_and_countdown`, and
`slugify`.

## Development

Run the test suite with [pytest](https://pytest.org):

```bash
pip install pytest
pytest
```

## License

GPLv3+ — see the package metadata. Authored by Yufei Pan (<pan@zopyr.us>).
```