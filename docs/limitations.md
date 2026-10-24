# Limitations

Approved known limitations and deferrals. Reviewers: read this first, and don't raise these again.

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

### A Ctrl-C inside Popen during a serial start can orphan the command
- Kind: known limitation
- What the operator sees: a single Ctrl-C landing while Popen starts a command in a serial run (about 51 µs
  per start on Python 3.14) can leave that command running, never registered, after the program exits. 1.55
  narrowed this to the fork's own C call (about 41 µs).
- Why: the part after the fork took a 30-line hand-over class (`_Popen`) for about 10 µs per start, and the
  fork's own window cannot be closed from Python either way.
- Revisit when: someone hits it, or CPython offers a way to start a process with its pid known before any
  Python code can raise.
- Approved: 2026-10-23, the user chose "Drop _Popen (Recommended)".

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
