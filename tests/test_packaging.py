"""The packaging: the commands setup.py installs."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def test_no_two_commands_differ_only_in_case(tmp_path):
	pytest.importorskip("setuptools", reason="needs setuptools to run setup.py")
	# pip 9 (stock on EL7 and EL8) reads entry_points.txt with a parser that folds case, so two commands that differ only
	# in case make it fail to upgrade or uninstall the package. The mixed-case command is a script file (scripts=) instead.
	result = subprocess.run([sys.executable, "setup.py", "-q", "egg_info", "--egg-base", str(tmp_path)],
		cwd=str(PACKAGE_ROOT), env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
		universal_newlines=True, timeout=120)
	assert result.returncode == 0, result.stderr
	egg_info = tmp_path / "multiCMD.egg-info"
	entry_points = (egg_info / "entry_points.txt").read_text(encoding="utf-8")
	names = [line.split("=")[0].strip() for line in entry_points.splitlines() if "=" in line]
	assert sorted(names) == ["mcmd", "multicmd"]
	# the mixed-case command ships in the sdist as a script, so the three spellings stay three commands
	assert "scripts/multiCMD" in (egg_info / "SOURCES.txt").read_text(encoding="utf-8").splitlines()
	assert (PACKAGE_ROOT / "scripts" / "multiCMD").read_text(encoding="utf-8").startswith("#!")
