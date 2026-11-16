# multiCMD: progress

Open work lives here and nowhere else (RULES, "Long work: the progress file"). Edit rows in place, in the commit that changes the work.

State at 2026-11-16: v1.67 is the latest release (the merge of feat/167); 1.68 has one deferred entry, not started.

## Goal
- 2026-11-05, for every cas tool: "we improve the cas scripts iteratively. feel free to add scope / tasks if when doing some work, you noticed some other thing that can be improved with cas scripts." (the user; no goal for multiCMD alone is written in the repo)
- Picks quoted in the repo that shape the countdown: 2026-11-13 "Wait out the countdown" (`input_with_timeout_and_countdown()` gives its default when the countdown ends); 2026-11-14 "Terminal only (Recommended)" (it reads the descriptor itself only at a terminal) and "Read on up to a terminal's line length (Recommended)" (docs/lessons-full.md).

## Open work

### multiCMD 1.68 (no written plan: the "deferred (1.68)" entry in docs/limitations.md)
- **A terminal of no size counts as unknown in `get_terminal_size()`,** so a progress bar of a known total stays on one line at a 0-column pty; and a frame wider than the terminal is cut to it. State: not started. Where: docs/limitations.md "At a terminal of no size, a progress bar of a known total draws over several lines". Then hpcp, statblk and statbtrfs take the new embed in their next versions. From: hpcp 9.95's build (2026-11-16), the main session's call (a deferral).

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
- None.

## Closed
- 1.67 (tag v1.67, the merge of feat/167, released 2026-11-14): `format_bytes()` shows 1000 Y and more in Y and returns for `inf`; a size string is counted in exact whole bytes (`'4.1G'` is 4100000000; 1.66 gave 4099999999); setup.py's conditional license metadata (`license_expression` on setuptools 77 and later). Built in 20ece2b and 1c45a6b; reviewed with no code change.
- 1.66 (tag v1.66, a31bc26, released 2026-11-14): the countdown returns by its timeout at a terminal whatever was typed, reads on past its end to the end of a line of up to 4,096 bytes, and reads any other stdin as 1.65 did; `format_bytes()` takes a string only when all of it is a size. Built on feat/166 (a9b7ae8 to b276820), after two design checks.
- Python 3.11 in the GitHub CI matrix (3.9, 3.11, 3.12, 3.14 and a 3.6 container): done in 1.62, 53ff0ba. Asked 2026-11-12: "Add 3.11 to CI (Recommended)".
