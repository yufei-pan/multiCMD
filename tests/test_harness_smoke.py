import multiCMD
from conftest import run_cli

def test_import_version():
	assert multiCMD.version
	assert multiCMD.__version__ == multiCMD.version

def test_run_cli_help():
	proc = run_cli(["-h"])
	assert proc.returncode == 0
	assert "max_threads" in proc.stdout
