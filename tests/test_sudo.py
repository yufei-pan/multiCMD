import os

import pytest

import multiCMD
from conftest import has_sudo_binary
from multiCMD import run_command, set_sudo


def test_set_sudo_false():
	multiCMD.USE_SUDO = True
	set_sudo(False)
	assert multiCMD.USE_SUDO is False


def test_set_sudo_true_when_root():
	if os.geteuid() != 0:
		pytest.skip("requires root to exercise root-disables-sudo path")
	set_sudo(True)
	assert multiCMD.USE_SUDO is False


def test_set_sudo_true_without_sudo_binary(capsys):
	multiCMD.SUDO_PATH = None
	if os.geteuid() == 0:
		pytest.skip("root path short-circuits before SUDO_PATH check")
	set_sudo(True)
	assert multiCMD.USE_SUDO is False
	err = capsys.readouterr().err
	assert "sudo not found" in err


@pytest.mark.sudo
def test_set_sudo_true_non_root_with_binary():
	if os.geteuid() == 0 or not has_sudo_binary():
		pytest.skip("needs non-root and sudo in PATH")
	set_sudo(True)
	assert multiCMD.USE_SUDO is True


def test_use_sudo_none_path_does_not_crash():
	multiCMD.SUDO_PATH = None
	# Must not raise TypeError from Popen([None, ...])
	result = run_command(["echo", "ok"], quiet=True, use_sudo=True)
	assert result == ["ok"]
