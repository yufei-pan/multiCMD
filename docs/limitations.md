# Limitations

Approved known limitations and deferrals. Reviewers: read this first, and don't raise these again.

### pip 9 cannot upgrade from or uninstall 1.57 and earlier
- Kind: known limitation
- What the operator sees: With pip 9, the pip of EL7 and EL8 (`pip3 --version`), upgrading from 1.57 or earlier and `pip3 uninstall multiCMD` of those releases stop with `configparser.DuplicateOptionError: ... option 'multicmd' in section 'console_scripts' already exists`, and nothing changes: the installed release keeps working. Run `pip3 install -U pip` once, then upgrade or uninstall. With `--user` the same happens, except that `pip3 install --user -U multiCMD` with no version does not stop: it installs the new release, which works, and leaves the old release's metadata behind. `pip3 list` then shows the old release after `pip3 uninstall multiCMD`, and the next uninstall stops with that error. To avoid this, run `pip3 install --user -U pip` once first. If it has already happened, run that, run `pip3 uninstall multiCMD` until it says multiCMD is not installed, and install again. Without `--user`, with multiCMD 1.57 or earlier installed, `pip3 install -U hpcp`, `statblk` or `statbtrfs` stops the same way once a newer multiCMD is out, because pip 9 upgrades dependencies too. `pip3 install -U --no-deps hpcp` gets past it.
- Why: pip 9 parses the installed release's `entry_points.txt` with a parser that folds case, and those releases list `multiCMD` and `multicmd`. Nothing in a new release can change the old metadata. 1.58 and later have no such pair.
- Revisit when: EL's stock pip stops being 9, or never.
- Approved: 2026-11-06, the user chose "Known limitation (Recommended)". The dependents' line: 2026-11-06, the user chose "Extend it (Recommended)".

### The `multiCMD` spelling in a venv with a space in its path
- Kind: known limitation
- What the operator sees: With pip 10 or later, in a venv whose path has a space or is too long for the kernel's `#!` line, the `multiCMD` spelling does not start (the error varies: "bad interpreter", `import: command not found`), because pip gives a script file a plain `#!` line: use `multicmd`. With pip 9 no spelling starts in such a venv, as in 1.57. uv and system or `--user` installs are not affected.
- Why: pip writes a plain `#!<python>` line into a script file and a `/bin/sh` wrapper only for an entry point. A launcher that delegates to the lowercase twin overwrites it on a case-insensitive disk under uv. One that execs `mcmd` or `mssh` works, but prints that name for `-V`, breaks `python3 $(which multiCMD)`, and TSVZ has no such sibling.
- Revisit when: someone hits it.
- Approved: 2026-11-06, the user chose "Known limitation (Recommended)".

### A second Ctrl-C microseconds after the first
- Kind: known limitation
- What the operator sees: two interrupts (Ctrl-Cs, or a Ctrl-C and a signal handler's exception) a few
  microseconds apart. The second ends multiCMD's wait at once, and a command not yet signalled keeps running,
  still registered, until `kill_all()` or the program's exit stops it. 1.55 stopped it at once. On Python
  3.6 - 3.10, which raise an interrupt at more points, the second can also land before a starting command is
  registered, which then runs on unknown to `kill_all()`, or leave what a finished command left running with
  its output open to `kill_all()` alone ( the exit hook does not reach it ), as in 1.55.
- Why: covering it took `_settle`, the settle loops in `_execute`'s handler and about 1,500 test lines, for a
  gap no person can double-tap into.
- Revisit when: someone hits it, for example a program that sends signals in bursts.
- Approved: 2026-10-23, the user chose "Simple design (Recommended)" and "Drop _settle (Recommended)". The
  3.6 - 3.10 part added after the 1.56 review: 2026-10-23, the user chose "Accept both (Recommended)".

### On Python 3.11+ a Ctrl-C inside Popen during a serial start can orphan the command
- Kind: known limitation
- What the operator sees: on Python 3.11+, on Windows, and in a frozen app (whose `sys.executable` is the app
  itself, so it has no wrapper to run, on any Python), a single Ctrl-C (or a signal handler's exception) landing
  while Popen starts a command in a serial run can leave that command running, never registered, after the
  program exits. About 51 µs per start on Python 3.14 (1.56's measurement); in hpcp's early-stop test (Docker,
  3.14, 8 copy workers, 400 small files) 3 commands in 200 stops ran so, with hpcp 9.86 and 9.87 alike, and each
  ended before hpcp did. 1.59 closed it before 3.11: the exec wrapper runs the command only once it is
  registered (66 and 33 such commands in 200 stops on 3.6 and 3.10 with 1.58, none with 1.59).
- Why: from 3.11 Popen runs the command itself, with no wrapper to wait in, and the fork's own window cannot be
  closed from Python: blocking the stops in the calling thread does not hold them while another thread exists
  (Python runs the handler on the main thread whichever thread the kernel picks), and holding the handlers
  (1.56's `_HoldCtrlC`) drops a C-level handler, keeps a Ctrl-C waiting on an exec that cannot finish, and
  turns a missing command's 127 into a stop.
- Revisit when: someone hits it, or CPython offers a way to start a process with its pid known before any
  Python code can raise.
- Approved: 2026-10-23, the user chose "Drop _Popen (Recommended)". Narrowed 2026-11-10 by 1.59 to Python 3.11+
  and Windows.

### A jammed Popen lock slows the stop by about 2 s
- Kind: known limitation
- What the operator sees: a Ctrl-C landing in the few bytecodes of CPython's `Popen.poll()` between its
  lock's `acquire(False)` and its `try`, during a serial run, leaves that lock held. The stop takes about 2 s
  longer and, unless the run was quiet, says the command's pid was "not seen to exit 1 s after SIGKILL: no
  longer tracked", though it has exited ( its zombie stays until the program exits ). The task is recorded as -1
  and nothing keeps running; the exit is not slowed, unless a second Ctrl-C left the command registered. 1.55
  measured it at 1-3% of Ctrl-Cs on free-threaded builds; it is rarer with the GIL.
- Why: recovering took about 114 lines (`_peek`, `_inside*`, `_exit_code`) reaching into CPython's internals,
  to save 2 s.
- Revisit when: someone hits it on a free-threaded build, or CPython fixes the poll lock.
- Approved: 2026-10-23, the user chose "Known limitation (Recommended)". Re-worded after the 1.56 review
  measured it: 2026-10-23, the user chose "Accept both (Recommended)".

### A program's own handler exception during a serial start counts as a start failure
- Kind: known limitation
- What the operator sees: a program that imports multiCMD installs a signal handler raising an ordinary
  exception (say `TimeoutError` from a SIGALRM handler). If that signal lands in the ~100 µs of a serial command
  start, the command is stopped and recorded as -1 with the exception's message, and the exception is not
  raised, so the program's own timeout never fires. While multiCMD only waits (the pool, `join_threads()`, the
  executor) such an exception is still raised as an interrupt; a `KeyboardInterrupt` or `SystemExit` always
  is. 1.54 behaved the same.
- Why: telling it apart took about 50 lines that read every installed handler and search the traceback for its
  code, with a blind spot of their own; none of our tools installs such a handler.
- Revisit when: someone hits it.
- Approved: 2026-10-23, the user asked "will this happen? what's the cost of keeping?", then chose "Drop it
  (Recommended)".

### join_threads() on another thread can return before a batch still starting
- Kind: known limitation
- What the operator sees: while one thread's `run_commands(..., wait_for_return=False)` is still starting its
  batch, a `join_threads()` on a different thread can return before the threads not started yet, which then run
  their commands after it returned. Called on the same thread once the batch call has returned (as statblk
  does), it waits for every command.
- Why: waiting for threads not started yet took the claim/abandon machinery and `_start_threads` (about 120
  code lines and 370 test lines).
- Revisit when: a program needs a cross-thread join while a batch starts.
- Approved: 2026-10-23, the user chose "Simplify, keep pool retry (Recommended)".

### A first Ctrl-C between the steps of a run, on Python 3.6 - 3.9
- Kind: known limitation
- What the operator sees: on Python 3.6 - 3.9, which raise a Ctrl-C at nearly any instruction, a single Ctrl-C
  landing at one of a few points between the steps of a run can leave its command registered but not
  signalled: it keeps running until `kill_all()` or the program's exit stops it. From 3.10 on, no such point is
  known. The same in 1.55, whose README named it.
- Why: closing it would mean guarding every step against an interrupt at any instruction, which is what the
  machinery removed in 1.56 tried to do; the Pythons involved are past their end of life.
- Revisit when: someone hits it on 3.6 - 3.9.
- Approved: 2026-10-23, the user chose "Add the entry (Recommended)".

### A Ctrl-C as a lock is taken or released can leave it held
- Kind: known limitation
- What the operator sees: on Python 3.6 - 3.9 and 3.14, a Ctrl-C landing on one instruction of a `with lock:` can
  leave that lock held. For `threading`'s own locks the program can hang as its threads end (CPython's own). For
  multiCMD's: an `AsyncExecutor`'s queued commands never start and a later `run_commands()` or `cleanup()` on it
  blocks, and so does `wait()` if its dispatcher was running; with the live-output lock, the next call that is not
  quiet hangs at its first output line ("Running command: ..."), before it starts its command, and commands still
  running are no longer read, so one that fills its pipe stalls. Only a program that catches the Ctrl-C and keeps
  going (an interactive prompt, say) gets that far; nothing is orphaned, and the exit does not hang. Measured by
  the 1.56 review with real signals aimed at the executor's locked region: about 60% of those interrupts left the
  lock held on 3.14, 0.2 - 0.5% on 3.6 - 3.9, none on 3.10 and 3.12. The same in 1.55.
- Why: writing the acquire and `try` out by hand leaves the same gap on 3.14; only lock-free structures would
  close it, which is new machinery for a one-instruction window in a microsecond call.
- Revisit when: someone hits it, or CPython moves the signal check out of the gap.
- Approved: 2026-10-23, the user chose "Known limitation (Recommended)".
