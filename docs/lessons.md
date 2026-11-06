# Lessons

One line per change to an API, format or agreed behaviour; the full story of each is in `docs/lessons-full.md`.

- **2026-11-06 multiCMD: a command that differs from another only in case is a script file, not an entry point**: a command name is also a config key to the tools that install it. pip 9 (stock on EL7 and EL8) reads `entry_points.txt` case-folded and fails on upgrade or uninstall, so keep one spelling as the entry point and ship the other as a script file.
