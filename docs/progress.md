# multiCMD: progress

Open work lives here and nowhere else (RULES, "Long work: the progress file"). Edit rows in place, in the commit that changes the work.

State at 2026-11-14: v1.67 is the latest release (the merge of feat/167).

## Goal
- 2026-11-05, for every cas tool: "we improve the cas scripts iteratively. feel free to add scope / tasks if when doing some work, you noticed some other thing that can be improved with cas scripts." (the user; no goal for multiCMD alone is written in the repo)
- Picks quoted in the repo that shape the countdown: 2026-11-13 "Wait out the countdown" (`input_with_timeout_and_countdown()` gives its default when the countdown ends); 2026-11-14 "Terminal only (Recommended)" (it reads the descriptor itself only at a terminal) and "Read on up to a terminal's line length (Recommended)" (docs/lessons-full.md).

## Open work

### Standing rows
- **Deferred work** is in `docs/limitations.md` (entries of kind "deferred"); each names when it is revisited.
- **The embedded copies.** hpcp, statblk and statbtrfs paste `multiCMD_min_zlib_a85.py` from a release tag; each tool's own progress file has the row for taking a new version.

## Questions waiting for the user
- None.

## Closed
- 1.67 (tag v1.67, the merge of feat/167, released 2026-11-14): `format_bytes()` shows 1000 Y and more in Y and returns for `inf`; a size string is counted in exact whole bytes (`'4.1G'` is 4100000000; 1.66 gave 4099999999); setup.py's conditional license metadata (`license_expression` on setuptools 77 and later). Built in 20ece2b and 1c45a6b; reviewed with no code change.
- 1.66 (tag v1.66, a31bc26, released 2026-11-14): the countdown returns by its timeout at a terminal whatever was typed, reads on past its end to the end of a line of up to 4,096 bytes, and reads any other stdin as 1.65 did; `format_bytes()` takes a string only when all of it is a size. Built on feat/166 (a9b7ae8 to b276820), after two design checks.
- Python 3.11 in the GitHub CI matrix (3.9, 3.11, 3.12, 3.14 and a 3.6 container): done in 1.62, 53ff0ba. Asked 2026-11-12: "Add 3.11 to CI (Recommended)".
