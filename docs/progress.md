# multiCMD: progress

Open work lives here and nowhere else (RULES, "Long work: the progress file"). Edit rows in place, in the commit that changes the work.

State at 2026-11-21: v1.69 is the latest release (the merge of feat/169); 1.70 is next (rows below).

## Goal
- 2026-11-05, for every cas tool: "we improve the cas scripts iteratively. feel free to add scope / tasks if when doing some work, you noticed some other thing that can be improved with cas scripts." (the user; no goal for multiCMD alone is written in the repo)
- Picks quoted in the repo that shape the countdown: 2026-11-13 "Wait out the countdown" (`input_with_timeout_and_countdown()` gives its default when the countdown ends); 2026-11-14 "Terminal only (Recommended)" (it reads the descriptor itself only at a terminal) and "Read on up to a terminal's line length (Recommended)" (docs/lessons-full.md).

## Open work

### multiCMD 1.70 (no written plan yet: the entry of kind "deferred (1.70)" in docs/limitations.md; next, after 1.69's release on 2026-11-21)
- **A bar's percent outlasts the prefix's inner fields,** so hpcp's copy bar at 127 to 134 columns (its rate-limited line at 135 to 142, its `Deleted` bar a column later in each) keeps its percent as 1.68 did, beside a bar of 10 cells: cut the prefix to its first `|` field before the percent goes (`_genrate_progress_bar`'s order and its tests). State: built on feat/170 (601eb62, 42d47be: percent kept from 109 columns on hpcp's copy bar; every CI Python passed at first runs), final review: release after small fixes (the commit date, one test, docs; a second opinion found the completion frame, one cell wider, can drop a part the frames before it showed: the layout is to be decided with the widest percent); fix round f170 running (2026-11-23). From: 1.69's final review (Fable F1) and the main session's triage, 2026-11-20, a deferral, not asked.

### multiCMD 1.71 (no written plan yet; its limitations entry is written when 1.70 merges)
- **`use_sudo` hangs when sudo wants a password:** the sudo multiCMD starts in its own process group, with stdin from /dev/null, stops at the password prompt and the run waits until a Ctrl-C (measured on 1.69 with sudo 1.9.16 by statbtrfs 0.46's build, which fixed its own calls with `sudo -n`). Plan to check: prefix `sudo -n` in `_build_tasks`, keeping the foreground `sudo -v` as the place that asks. State: not started. From: statbtrfs 0.46's build (2026-11-23), a deferral, not asked.

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
- None. (The two on the bars, ambiguous-width terminals and a bar drawn past its total, were answered by chief's decisions C206 and C207 of 2026-11-23: both known limitations, written in docs/limitations.md.)

## Closed
- The bars' two known limitations (chief's decisions C206 and C207, 2026-11-23, not the user's words): ambiguous-width terminals (with one README line) and a bar drawn past its total (the README's 1.69 notes already say only the first frame past the total ends its line); docs/limitations.md.
- 1.69 (tag v1.69, the merge of feat/169 8a42a2c, released 2026-11-21): a progress bar with a total keeps a bar of at least 10 cells and gives way a field at a time (hpcp's copy bar at 80 columns shows its count); the first frame at or past a bar's total ends its line; a control character in a frame's prefix or suffix is drawn as `?`; the first frame of a known total holds ` Calculating... ` or blanks in its bar's place, never `||`; the stop-probe test reaps its strays, so it passes under a process limit of 1,024. Build b169, final review fr169 (Fable high: release; the code second opinion found the first-frame cut), fix round f169, re-review rr169 (Opus xhigh: release), wording rounds f169b and R1 (35ddbdb, the reviewer's text), re-read: release; suites 3.14 1,064, 3.11 1,062 (at 04b7d98), 3.12 1,061, 3.9 939, 3.6 940 (at ec367a4) passed, first runs; PyPI wheel cd96ecbe and sdist 34aae105 match the build; GitHub release v1.69; main's CI green in all five jobs (run 44961128237). hpcp, statblk and statbtrfs take the new embed in their next versions; cas-toolbox pins it after its full gate.
- 1.68 (tag v1.68, the merge of feat/168, released 2026-11-17): a terminal that reports no size counts as 80x24 (COLUMNS / LINES first; no terminal stays 240x50), so a progress bar of a known total stays on one line at a 0-column pty and `mcmd`'s table and pty windows get a size; every progress frame fits in one column less than the terminal, counted in display columns, a frame with no total giving way from its prefix's start, then whole fields from its end (never a number without its unit, or a bare label); the last bar frame is as wide as the others; a frame is measured as stdout writes it. Build b168m, final review fr168m (fix first), fix round f168m, re-review rr168m (release after text), tests-and-docs commit f168m2; suites 3.14 1,030, 3.11 1,028, 3.9 905, 3.6 906 passed; PyPI wheel ceecf4bc and sdist 1afa9c60 match the build; GitHub release v1.68; main's CI green in all five jobs. hpcp, statblk and statbtrfs take the new embed in their next versions; cas-toolbox takes it in the week-48 roll-up.
- 1.67 (tag v1.67, the merge of feat/167, released 2026-11-14): `format_bytes()` shows 1000 Y and more in Y and returns for `inf`; a size string is counted in exact whole bytes (`'4.1G'` is 4100000000; 1.66 gave 4099999999); setup.py's conditional license metadata (`license_expression` on setuptools 77 and later). Built in 20ece2b and 1c45a6b; reviewed with no code change.
- 1.66 (tag v1.66, a31bc26, released 2026-11-14): the countdown returns by its timeout at a terminal whatever was typed, reads on past its end to the end of a line of up to 4,096 bytes, and reads any other stdin as 1.65 did; `format_bytes()` takes a string only when all of it is a size. Built on feat/166 (a9b7ae8 to b276820), after two design checks.
- Python 3.11 in the GitHub CI matrix (3.9, 3.11, 3.12, 3.14 and a 3.6 container): done in 1.62, 53ff0ba. Asked 2026-11-12: "Add 3.11 to CI (Recommended)".
