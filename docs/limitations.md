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

### A Ctrl-C inside Popen during a serial start can orphan the command (3.11+) or leave a zombie (any Python)
- Kind: known limitation
- What the operator sees: on Python 3.11+, on Windows, in a frozen app (whose `sys.executable` is the app
  itself) and in an embedded Python whose `sys.executable` is empty (neither has a Python to run the exec
  wrapper with, on any Python), a single Ctrl-C (or a signal handler's exception) landing
  while Popen starts a command in a serial run can leave that command running, never registered, after the
  program exits. About 51 µs per start on Python 3.14 (1.56's measurement); in hpcp's early-stop test (Docker,
  3.14, 8 copy workers, 400 small files) 3 commands in 200 stops ran so, with hpcp 9.86 and 9.87 alike, and each
  ended before hpcp did. 1.59 closed it before 3.11: the exec wrapper runs the command only once it is
  registered (66 and 33 such commands in 200 stops on 3.6 and 3.10 with 1.58, none with 1.59).
  On every Python, a program that catches such a stop and goes on also keeps, until it exits, a zombie for each
  stop that lands as the fork returns (3.6 to 3.10: the exec wrapper, exit 125), and one or two descriptors for
  each stop that lands in Popen's own setup. In 5,000 stops of a serial `true`, each aimed at a random moment of the
  call: 150 zombies and 37 descriptors on 3.6, 812 and 242 on 3.14 (measured on 1.61's module during the 1.62 work;
  1.59's code for a start is the same on 3.11+, and on 3.6 it left 143 and 23). Pools are not affected (no
  leftovers in 2,000 stops): their starts run off the main thread. At the measured rates, reaching the usual limit
  of 1,024 descriptors takes about 21,000 such stops on 3.14 and 140,000 on 3.6.
- Why: from 3.11 Popen runs the command itself, with no wrapper to wait in, and the fork's own window cannot be
  closed from Python: blocking the stops in the calling thread does not hold them while another thread exists
  (Python runs the handler on the main thread whichever thread the kernel picks), and holding the handlers
  (1.56's `_HoldCtrlC`) drops a C-level handler, keeps a Ctrl-C waiting on an exec that cannot finish, and
  turns a missing command's 127 into a stop.
- Revisit when: someone hits it, or CPython offers a way to start a process with its pid known before any
  Python code can raise.
- Approved: 2026-10-23, the user chose "Drop _Popen (Recommended)". Narrowed 2026-11-10 by 1.59 to Python 3.11+,
  Windows, frozen apps and an embedded Python with no `sys.executable` (the 2026-10-23 approval covered every Python; 1.59 closed it only where the exec wrapper runs).
  Widened 2026-11-11 with the zombies and descriptors such stops leave on every Python: the user chose "Add the
  sentence (Recommended)".

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
  - The start of a pool's workers or of a fire-and-forget batch's threads, on the main thread, can still lose such
    an exception in its first few microseconds, about 4 µs per start, before the helper that starts the thread
    exists. The exception is then taken for a thread that could not start: the pool (unless quiet) says "started 7
    of 8 threads" and runs its commands to the end, a fire-and-forget batch records that command as failed (-1) and
    runs it anyway, and the program's own timeout never fires.
  - Measured by the fix round, with 1,000 exceptions aimed at random into the first 2 ms (6 ms on 3.6) of an
    8-command batch: 7 to 16 of the pools and 8 to 13 of the fire-and-forget batches lost it, on 3.6, 3.12 and
    3.14; without the fix, 928 of 1,000 pools did, on 3.12. 1.59 lost it for the whole of its starts too (a median
    1.3 ms of an 8-worker batch's), and 1.60 without the fix for 2.2 ms.
- Why: telling it apart took about 50 lines that read every installed handler and search the traceback for its
  code, with a blind spot of their own; none of our tools installs such a handler.
- Revisit when: someone hits it.
- Approved: 2026-10-23, the user asked "will this happen? what's the cost of keeping?", then chose "Drop it
  (Recommended)".
  Pool and fire-and-forget starts added 2026-11-11 after the 1.60 fix (N1): the user chose "Fix now, note the rest (Recommended)".

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
- What the operator sees: a Ctrl-C landing on one instruction of a `with lock:` can leave that lock held, or, for a
  semaphore, take a permit that is never given back. Which Pythons depends on the lock:
  - **A C-level lock** (`threading.Lock` and the locks multiCMD keeps on it): on Python 3.6 - 3.9 and 3.14.
    multiCMD starts its threads where no handler runs (1.60), so its own starts no longer reach `threading`'s
    locks; a program's own `with` on one of them still can. For multiCMD's: an `AsyncExecutor`'s queued
    commands never start and a later `run_commands()` or `cleanup()` on it blocks, and so does `wait()` if its dispatcher was running; with the live-output lock, the next call that is not
    quiet hangs at its first output line ("Running command: ..."), before it starts its command, and commands still
    running are no longer read, so one that fills its pipe stalls. Only a program that catches the Ctrl-C and keeps
    going (an interactive prompt, say) gets that far; nothing is orphaned, and the exit does not hang. Measured by
    the 1.56 review with real signals aimed at the executor's locked region: about 60% of those interrupts left the
    lock held on 3.14, 0.2 - 0.5% on 3.6 - 3.9, none on 3.10 and 3.12. The same in 1.55.
  - **A Python-level primitive** (`threading.Semaphore`, `Condition`): on every Python, 3.10 to 3.13 included,
    because their `acquire`, `release`, `__enter__` and `__exit__` are Python code, where the Ctrl-C can land
    between any two calls. multiCMD reaches one only through a caller's own `sem=` (a `threading.Semaphore`) in a
    serial `run_command()` or `run_commands()` (`max_threads=1`, waiting) on the main thread, where `with sem:` takes
    the permit. A program that catches the Ctrl-C and keeps going can then find the permit lost (with `Semaphore(1)`
    its next call waits for ever, or until another Ctrl-C if it waits on the main thread) or the semaphore's internal
    lock jammed (every later `acquire()` or `release()` of it blocks, on any thread, and no Ctrl-C reaches another
    thread's run that shares it). Measured by the 1.60 review (3.11 and 3.13 by its fixer, with the same probe): a serial `run_command(['true'], sem=S)` with
    `S = Semaphore(1)`, stopped at a random moment of its 0.2 - 5 ms by a SIGALRM handler raising `KeyboardInterrupt`,
    5,000 times per Python (Docker):

    | Python | stopped | permits lost | locks jammed | together |
    |---|---|---|---|---|
    | 3.6 | 4,996 | 1 | 0 | 0.02 % |
    | 3.9 | 5,000 | 2 | 2 | 0.08 % |
    | 3.10 | 4,997 | 1 | 1 | 0.04 % |
    | 3.11 | 4,972 | 13 | 8 | 0.42 % |
    | 3.12 | 4,973 | 5 | 8 | 0.26 % |
    | 3.13 | 4,986 | 4 | 3 | 0.14 % |
    | 3.14 | 4,953 | 8 | 10 | 0.36 % |

    The window is a few µs per command, so a command that runs longer meets it proportionally less often. It is
    not new: 1.60 leaves `with sem:` as it was, and 1.59 on 3.6 lost a permit at the same rate (1 in 4,992).
    Not reached: the default per-call semaphore (it dies with the call that the Ctrl-C ends); pools, fire-and-forget
    runs and executors, which take theirs off the main thread, where no handler runs; hpcp, statblk and statbtrfs,
    which pass no `sem=`.
- Why: writing the acquire and `try` out by hand leaves the same gap on 3.14; only lock-free structures would
  close it, which is new machinery for a one-instruction window in a microsecond call. For a caller's `sem=`, the
  fix would be to take its permit on a helper thread, as `_Thread.start` starts threads: a thread start for each
  serial command that has one (about 20 - 40 µs, about 10 % of a 230 µs `true`), and logic to give back a permit that
  the helper gets after the caller was interrupted.
- Revisit when: someone hits it, or CPython moves the signal check out of the gap.
- Approved: 2026-10-23, the user chose "Known limitation (Recommended)". Amended 2026-11-11 (a caller's `sem=` in a serial run; every Python for `Semaphore` and `Condition`): the user chose "Amend the KL (Recommended)".

### A first Ctrl-C can be dropped: in a fire-and-forget batch's start, or in a finished command's Popen cleanup
- Kind: known limitation
- What the operator sees: in the first milliseconds of a fire-and-forget batch (`run_commands(...,
  wait_for_return=False)`), a first Ctrl-C can do nothing: Python prints "Exception ignored" with the
  `KeyboardInterrupt`'s traceback (naming `WeakSet.__init__.<locals>._remove`) on stderr, and the program goes on
  with every command still running. The next Ctrl-C works. CPython ignores an exception raised inside a weakref
  callback, and `threading` keeps every `Thread` in a `WeakSet` (`threading._dangling`), whose callback runs on the
  main thread when a finished thread is freed there.
  - Measured by the 1.60 review: stops at random moments in the first 4 ms of an 8-command batch, 1.60 against 1.59.
  - 3 times 5,000 stops: 3.12 dropped 23 (0.15 %) against 11 (0.07 %); 3.14 dropped 22 (0.15 %), and 1.59 hung in
    all three runs (the thread-start hang that 1.60 fixes).
  - 1,500 stops: 3.6 dropped 1 against 0, 3.13 2 against 0, 3.14 3 against 0, and the free-threaded 3.13t 53
    against 52.
  - Serial runs drop fewer stops with 1.60 than with 1.59, since a command's readers are no longer `Thread`s in that
    `WeakSet`; pools dropped none with either.
  - Also in a serial run (widened by the 1.61 review): a Ctrl-C that lands in `Popen`'s cleanup (`Popen.__del__`) of a
    command that has finished, on the main thread, is dropped the same way. Python prints "Exception ignored in: ...
    Popen.__del__" with the `KeyboardInterrupt`'s traceback on stderr, the run goes on, and the next Ctrl-C works: press
    it again. Measured with SIGINTs aimed at random into back-to-back serial `run_command(['true'])` and
    `['sleep', '0.005']`: 12 of about 6,000 dropped with 1.60 and 3 with 1.61.
- Why: CPython ignores an exception raised in a `__del__` as it does in a weakref callback, and a serial run frees each
  finished command's `Popen` on the main thread. For the batch: 1.60's helper that starts a thread allocates a few
  objects on the main thread at each start, so the cyclic garbage collection that frees finished fire-and-forget threads (each points at its `Task` and the `Task` at it)
  often runs on the main thread now, with the `WeakSet` callbacks it sets off. Keeping that garbage off the main
  thread means breaking the documented `task.thread` link or allocating nothing there per start; neither is worth a
  loss of about 1 in 700 of the first Ctrl-Cs that land in those few ms, with the next one working.
- Revisit when: someone hits it, or CPython stops ignoring an exception in a weakref callback or a `__del__`.
- Approved: 2026-11-11, the user chose "Release; note it (Recommended)". Widened to serial runs and `Popen.__del__`
  2026-11-11 (the 1.61 review's P1): the user chose "Widen the entry (Recommended)".

### On Python 3.6 - 3.10 a Ctrl-C as a command starts waits for the exec wrapper's Popen
- Kind: known limitation
- What the operator sees: on Python 3.6 - 3.10 (macOS 3.9 included), in a serial run, a Ctrl-C that arrives while
  multiCMD starts a command waits for the exec wrapper's `Popen` to return (the fork, the exec of the Python binary
  itself, and the read of Popen's error pipe), then stops the command as usual. Normally that is about 1 ms, and 2 - 5 ms
  at p99: the 1.61 review aimed 1,000 SIGINTs at random into back-to-back serial `true` and `sleep 0.005` commands, and
  the time from a SIGINT until the `KeyboardInterrupt` reached the caller was p50 / p90 / p99 11.6 / 14.4 / 20.8 ms on 3.6
  and 11.5 / 14.4 / 19.6 ms on 3.10, against 10.4 / 12.3 / 17.3 and 10.5 / 11.8 / 14.5 ms with 1.60 (about 10 ms of it is
  the stop's own wait between SIGINT and SIGTERM). If the exec of the Python binary itself hangs (a venv on an NFS mount
  that stopped answering), Ctrl-C does nothing until the mount answers, where 1.60 exited 130 (read from the code, not
  reproduced). On Python 3.11+ there is no wrapper, and nothing changes.
- Why: before 3.11 the child's signal mask can only be set through the calling thread's, which the child inherits. 1.61
  blocks SIGINT there for the whole `Popen`, so that a stop reaching the wrapper's Python while it initializes cannot
  crash it (core dumps on 3.6 and 3.7, "Fatal Python error" on 3.8 - 3.10), and a Ctrl-C that arrives meanwhile stays
  pending until the mask is restored. `preexec_fn` was rejected, and `os.posix_spawn(setsigmask=)` exists from Python 3.8
  only.
- Revisit when: `Popen` gains a way to set the child's signal mask.
- Approved: 2026-11-11, the user chose "Approve as KL (Recommended)".

### Under ksh93 on Linux, a reader that quit is found by the next write
- Kind: known limitation
- What the operator sees: on Linux, ksh93 builds its pipelines from `socketpair()`, so in `mcmd ... | head` run from ksh93
  `mcmd`'s stdout is a socket, which 1.62 does not ask (only a pipe is). A reader that quits while the commands print nothing
  stops nothing until the next write, or until the commands end, as in 1.61: with ksh 93u+m on Debian 13,
  `mcmd 'sleep 3' | head -1` ended by SIGPIPE after 3.04 s and `mcmd -q 'sleep 3' | sleep 0.3` ended 0 after 3.05 s (bash,
  dash, zsh, mksh and busybox on Linux, and macOS's ksh, give a pipe: those end at once). A write that fails still ends the
  run, as in 1.61. The README says so in one clause. EL7 and EL8 ship ksh-20120801, from the same AT&T code: whether it does
  the same is not checked.
- Why: asking a socket on Linux too would bring back a platform-by-kind branch in `_watch_stdout`, the kind that made the
  first poll stop jobs it should not (a hung-up terminal, a half-closed socket on macOS), for one shell's pipes.
- Revisit when: ksh93 on Linux builds its pipelines from pipes, or a run is reported that this left running.
- Approved: 2026-11-12, the user chose "Known limitation + README clause (Recommended)".
