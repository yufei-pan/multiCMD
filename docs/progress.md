# multiCMD: progress

Open work lives here and nowhere else (RULES, "Long work: the progress file"). Edit rows in place, in the commit that changes the work.

State at 2026-11-16: v1.67 is the latest release (the merge of feat/167); 1.68 has one deferred entry, not started.

## Goal
- 2026-11-05, for every cas tool: "we improve the cas scripts iteratively. feel free to add scope / tasks if when doing some work, you noticed some other thing that can be improved with cas scripts." (the user; no goal for multiCMD alone is written in the repo)
- Picks quoted in the repo that shape the countdown: 2026-11-13 "Wait out the countdown" (`input_with_timeout_and_countdown()` gives its default when the countdown ends); 2026-11-14 "Terminal only (Recommended)" (it reads the descriptor itself only at a terminal) and "Read on up to a terminal's line length (Recommended)" (docs/lessons-full.md).

## Open work

### multiCMD 1.68 (no written plan: the "deferred (1.68)" entry in docs/limitations.md)
- **A terminal of no size counts as unknown in `get_terminal_size()`,** so a progress bar of a known total stays on one line at a 0-column pty; and a frame wider than the terminal is cut to it. State: built; final review (2026-11-16, Fable, scratchpad notes fr168m): fix first, one small round. Two things worse than 1.67, both narrow: a cut frame can end on a number without its unit (`Size: 123.45` of 123.45 GiB, since `format_bytes` writes `1.23 K` and the cut took any blank), and at an unsized terminal with a real screen (a serial console) a short line is padded to 239 and wraps; second opinions add a frame measured before unencodable characters become `?`, and a per-frame cost. Fix round 1 running (fresh fixer, scratchpad notes f168m): the cut never at a blank after a digit or a colon, with hpcp's real strings in the tests; a terminal that answered 0 is 80x24 (the main session's call, against its own plan's "same 240x50"; no terminal stays 240x50); the last bar frame as wide as the others (older, cheap); the measure after `_writable`; a fast path for the column count; docs. Then a re-read and release. The open test failure is not 1.68's: 1.67 fails it alike under the containers' 1,024-process limit (zombies from the approved known limitation "A Ctrl-C inside Popen during a serial start ..." reach the limit; 15 of 15 pass at 4,096, the limit the 1.66 and 1.67 suites used), so the 3.14 and 3.11 suites were rerun at 4,096 for that cause. Built (branch feat/168 at 61cedd0, worktree multicmd-168; scratchpad notes b168m). 1cfe272: a 0 column or row count is unknown (COLUMNS or LINES, else 240x50, multiSSH3's form; not shutil, which returns zeros before 3.11), every frame fits in one column less than the terminal counted in terminal columns (a prefix gives way from its start), `_min` regenerated (v1.67's source regenerates v1.67's bytes); 61cedd0 docs and lessons. Suites: 3.14 993 and 3.11 991 passed (at 4,096 processes), 3.9 and 3.6 first runs; macOS at a 0x0 pty 235 passed; 24 of the 25 new tests fail on 1.67. Next: the fix round above, a re-read, then release. Where: docs/limitations.md "At a terminal of no size, a progress bar of a known total draws over several lines". Then hpcp, statblk and statbtrfs take the new embed in their next versions. From: hpcp 9.95's build (2026-11-16), the main session's call (a deferral).

### multiCMD 1.69 (no written plan yet)
- **The stop-probe test reaps its own stray children,** so `test_stops_in_serial_commands_never_jam_thread_starts` passes under a process limit (it fails on 1.67 and 1.68 alike at 1,024: the zombies of the approved limitation "A Ctrl-C inside Popen during a serial start ..." reach it; suites run at 4,096 meanwhile). State: not started. From: the 1.68 build and review (2026-11-16), a deferral (test only).

### Standing rows
- **Deferred work** is in `docs/limitations.md` (entries of kind "deferred"); each names when it is revisited.
- **The embedded copies.** hpcp, statblk and statbtrfs paste `multiCMD_min_zlib_a85.py` from a release tag; each tool's own progress file has the row for taking a new version.

## Standing decisions
The user's rulings on how the cas repos are worked, one line each; a line changes only on the user's word. The cas monorepo's CLAUDE.md holds the same list, with how to reach the test machines.
- Deferrals (2026-11-05): "please do not ask me about features you think should defer to next version anymore. instead, note them down and after getting the current version done, start the next version right away. only ask me to approve things that are going into limitations that we do not have plans to fix it any time soon."
- The release gate (2026-11-09): "releasing a new version gate is only if it does not make an existing issue worse. even if the fix does not completely resolve the problem, if it makes the result more tolerable, we shall not gate it for release because there are still issues."
- Review before upload (2026-10-18): "commit and push is fine but when packaging, please follow the rule that need a code review before uvpack / upload to pypi".
- A test that passed only on a rerun (2026-11-06, "Green with a warning (Recommended)"): CI stays green with a warning, and "Before any upload, every such test still needs a written cause either way."
- Release pushes (2026-11-10, "Release OK covers it (Recommended)"): an OK for a release covers its push of main, the version tag and the GitHub release.
- Tests (2026-11-12, "Core per release, full at roll-up (Recommended)"): CI's own command on Python 3.14 and 3.11 in Docker before each release; the full gate (every Python in CI, macOS, EL7 on Python 3.11) before a Monday roll-up pin moves.
- CentOS 7 (2026-11-04): "for centos 7, which is already EOL, we only target all the last packages released for it. this also includes EPEL which is also in archive right now."
- EL8 (2026-11-05): "yes rocky 8 it is. let's also only test on them when you think test on real kernel is needed."
- macOS (2026-11-11): tests run on the user's own always-on Mac, as a standard user made for them, never with sudo.

## Questions waiting for the user
- **Bars on a terminal that draws East Asian "ambiguous" characters two columns wide (a known limitation; needs your OK).** What you see: on a terminal set to draw ambiguous-width characters wide (an option some CJK setups turn on; most terminals draw them narrow by default), every progress bar of a known total is about twice as wide as counted and wraps, a row per update. The bar's blocks (U+2588 and the eighths) are ambiguous-width, and multiCMD counts them as one column, as 1.67 did (not worse in 1.68; found by 1.68's second opinions, from reading, not seen on such a terminal). Why not fix: a program cannot ask the terminal how it draws them; guessing from a CJK locale would be wrong for most CJK terminals, which draw them narrow, and an ASCII bar would change every bar for everyone. Revisit when: someone sees it on a real terminal. Options: **(1) known limitation, with one README line (recommended)** / (2) draw the bar in ASCII (`#`) under a CJK locale. Your words it touches: none. Waits on it: only that entry's text; 1.68 ships without it, as 1.67 did.

## Closed
- 1.67 (tag v1.67, the merge of feat/167, released 2026-11-14): `format_bytes()` shows 1000 Y and more in Y and returns for `inf`; a size string is counted in exact whole bytes (`'4.1G'` is 4100000000; 1.66 gave 4099999999); setup.py's conditional license metadata (`license_expression` on setuptools 77 and later). Built in 20ece2b and 1c45a6b; reviewed with no code change.
- 1.66 (tag v1.66, a31bc26, released 2026-11-14): the countdown returns by its timeout at a terminal whatever was typed, reads on past its end to the end of a line of up to 4,096 bytes, and reads any other stdin as 1.65 did; `format_bytes()` takes a string only when all of it is a size. Built on feat/166 (a9b7ae8 to b276820), after two design checks.
- Python 3.11 in the GitHub CI matrix (3.9, 3.11, 3.12, 3.14 and a 3.6 container): done in 1.62, 53ff0ba. Asked 2026-11-12: "Add 3.11 to CI (Recommended)".
