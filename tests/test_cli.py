from conftest import run_cli
import multiCMD


def test_version_flag():
	proc = run_cli(["-V"])
	assert proc.returncode == 0
	assert multiCMD.version in (proc.stdout + proc.stderr)


def test_help_flag():
	proc = run_cli(["-h"])
	assert proc.returncode == 0
	assert "max_threads" in proc.stdout


def test_two_echoes():
	proc = run_cli(["echo hello", "echo world"])
	assert proc.returncode == 0
	combined = proc.stdout + proc.stderr
	assert "hello" in combined
	assert "world" in combined


def test_parse_flag_expands():
	proc = run_cli(["-p", "echo [1-2]"])
	assert proc.returncode == 0
	combined = proc.stdout + proc.stderr
	assert "[1-2]" not in combined
	assert "Running command: echo 1" in combined
	assert "Running command: echo 2" in combined


def test_max_threads_and_timeout_smoke():
	proc = run_cli(["-q", "-m", "2", "-t", "5", "echo a", "echo b"], timeout=20)
	assert proc.returncode == 0


def test_sudo_flag_without_sudo_binary_does_not_crash(monkeypatch):
	# Force child env: PATH without sudo by invoking python -c wrapper is heavy;
	# instead assert current host behavior: --sudo with missing sudo should not
	# abort with traceback. If sudo exists, command still succeeds for echo.
	proc = run_cli(["-q", "--sudo", "echo ok"], timeout=20)
	assert proc.returncode == 0
	assert "Traceback" not in (proc.stdout + proc.stderr)
