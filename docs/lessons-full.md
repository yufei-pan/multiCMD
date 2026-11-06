# Lessons, in full

## 2026-11-06 multiCMD: a command that differs from another only in case is a script file, not an entry point
- Changed: `multiCMD` is no longer a console entry point: `setup.py` installs it as the script file `scripts/multiCMD` (`scripts=`), and `mcmd` and `multicmd` stay entry points. All three are still commands and print the same version.
- Why we had to change: pip 9.0.3, the pip of EL7 and EL8, reads `entry_points.txt` with a case-folding config parser. With `multiCMD` and `multicmd` in it, `pip3 install -U multiCMD` and `pip3 uninstall multiCMD` stopped with `configparser.DuplicateOptionError`.
- Original design was wrong because: it took a command name for a file name only, where a case-sensitive file system keeps `multiCMD` and `multicmd` apart, and not for the key of a config file that a different tool reads on uninstall.
- Lesson: no two commands in one `entry_points.txt` may differ only in case. Install, upgrade and uninstall with the stock pip of the oldest platform the package supports before a release.
