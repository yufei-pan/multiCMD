import shutil

import pytest

from conftest import loopback_ping_works
from multiCMD import ping

pytestmark = [
	pytest.mark.ping,
	pytest.mark.skipif(shutil.which("ping") is None, reason="ping not installed"),
	pytest.mark.skipif(not loopback_ping_works(), reason="loopback ping failed"),
]


def test_ping_single_true_false():
	assert ping("127.0.0.1", timeout=2, quiet=True) is True


def test_ping_list_true_false():
	assert ping(["127.0.0.1", "127.0.0.1"], timeout=2, quiet=True) == [True, True]


def test_ping_raw_return_codes():
	rcs = ping(
		["127.0.0.1"],
		timeout=2,
		quiet=True,
		return_true_false=False,
		return_code_only=True,
	)
	assert rcs == [0]
