# multiCMD: progress

Open work lives here and nowhere else (RULES, "Long work: the progress file"). Edit rows in place, in the commit that changes the work.

State at 2026-11-14: v1.66 is the latest release (main a31bc26).

## Goal
- 2026-11-05, for every cas tool: "we improve the cas scripts iteratively. feel free to add scope / tasks if when doing some work, you noticed some other thing that can be improved with cas scripts." (the user; no goal for multiCMD alone is written in the repo)
- Picks quoted in the repo that shape the countdown: 2026-11-13 "Wait out the countdown" (`input_with_timeout_and_countdown()` gives its default when the countdown ends); 2026-11-14 "Terminal only (Recommended)" (it reads the descriptor itself only at a terminal) and "Read on up to a terminal's line length (Recommended)" (docs/lessons-full.md).

## Open work

### multiCMD 1.67 (no written plan)
- **Paused 2026-11-14 at the user's ask, in review:** branch feat/167 (worktree multicmd-167), 20ece2b (code, tests, `_min`, setup.py) and 1c45a6b (docs) do all three rows below, plus a size string counted in whole bytes (`'4.1G'` is 4100000000). Suites: 968/14 on 3.14, 966/16 on 3.11, 841/141 on 3.6. The final review so far says release with no code change. Left when work resumes: the second opinion's leads, the reviewer's own 3.6 suite, four wording edits to the release notes, then the release.
- **`format_bytes()` with `to_str` raises `KeyError` for 1000 Y or more.** State: not started. Where: no branch; the deferred entry is in docs/limitations.md ("Revisit when: multiCMD 1.67: stop at the last unit"). Fix: stop the unit loop at the last unit (Y), with a test. From: a deferral, not asked; found by 1.66's second-opinion review.
- **setup.py: multiSSH3's conditional `license_expression`.** State: not started. Where: setup.py still has the `License :: ...` classifier and `license='GPLv3+'`, so setuptools 77 and later warn "License classifiers are deprecated". Copy multiSSH3/setup.py's pattern (setuptools 77 and later: `license_expression` and `license_files`; older: the free-text `license` and the classifier). There is no MANIFEST.in, so check that the sdist still carries LICENSE on old setuptools. From: the 2026-11-07 license survey (the main session's plan); hpcp and TSVZ already have it.
- **Release 1.67.** State: not started. Next: tests per release (serial `python -m pytest -q` on Python 3.14 and 3.11 in Docker), a review before any upload, `uvpack`, push main and the tag, `gh release create` with notes. Then hpcp, statblk and statbtrfs embed it in their next versions (their rows). From: the release rules (cas CLAUDE.md, "Releasing a tool").

### Standing rows
- **Deferred work** is in `docs/limitations.md` (entries of kind "deferred"); each names when it is revisited.
- **The embedded copies.** hpcp, statblk and statbtrfs paste `multiCMD_min_zlib_a85.py` from a release tag; each tool's own progress file has the row for taking a new version.

## Questions waiting for the user
- None.

## Closed
- 1.66 (tag v1.66, a31bc26, released 2026-11-14): the countdown returns by its timeout at a terminal whatever was typed, reads on past its end to the end of a line of up to 4,096 bytes, and reads any other stdin as 1.65 did; `format_bytes()` takes a string only when all of it is a size. Built on feat/166 (a9b7ae8 to b276820), after two design checks.
- Python 3.11 in the GitHub CI matrix (3.9, 3.11, 3.12, 3.14 and a 3.6 container): done in 1.62, 53ff0ba. Asked 2026-11-12: "Add 3.11 to CI (Recommended)".
