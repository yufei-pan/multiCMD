import subprocess
import sys

from conftest import MULTICMD_PY, run_cli


def test_command_file_skips_blank_lines_and_comments(tmp_path):
	jobs = tmp_path / 'jobs.txt'
	jobs.write_text('echo one\n\n# a comment\n   # indented comment\necho two\n')
	assert run_cli(['-n', '-f', str(jobs)]).stdout.splitlines() == ['echo one', 'echo two']


def test_command_file_from_stdin():
	proc = subprocess.run([sys.executable, str(MULTICMD_PY), '-n', '-f', '-'],
		input='echo a\necho b\n', capture_output=True, text=True, timeout=30)
	assert proc.stdout.splitlines() == ['echo a', 'echo b']


def test_positional_commands_come_before_file_commands(tmp_path):
	jobs = tmp_path / 'jobs.txt'
	jobs.write_text('echo file\n')
	assert run_cli(['-n', 'echo arg', '-f', str(jobs)]).stdout.splitlines() == ['echo arg', 'echo file']


def test_command_files_run(tmp_path):
	jobs = tmp_path / 'jobs.txt'
	jobs.write_text('echo one\necho two\n')
	proc = run_cli(['-f', str(jobs)])
	assert proc.returncode == 0
	assert 'one' in proc.stdout and 'two' in proc.stdout


def test_arg_file_fills_templates_one_argument_per_line(tmp_path):
	hosts = tmp_path / 'hosts.txt'
	hosts.write_text('web 1\nweb2\n\n')
	assert run_cli(['-n', '-a', str(hosts), 'ping -c1 {}']).stdout.splitlines() == ["ping -c1 'web 1'", 'ping -c1 web2']


def test_arg_file_without_placeholder_appends_the_line(tmp_path):
	hosts = tmp_path / 'hosts.txt'
	hosts.write_text('web1\nweb2\n')
	assert run_cli(['-n', '-a', str(hosts), 'echo']).stdout.splitlines() == ['echo web1', 'echo web2']


def test_arg_file_lines_are_not_range_expanded(tmp_path):
	values = tmp_path / 'values.txt'
	values.write_text('[1-2]\n')
	assert run_cli(['-n', '-p', '-a', str(values), 'echo x[1-2] {}']).stdout.splitlines() == \
		["echo x1 '[1-2]'", "echo x2 '[1-2]'"]


def test_arg_file_in_shell_mode_quotes_each_line(tmp_path):
	marker = tmp_path / 'pwned'
	values = tmp_path / 'values.txt'
	values.write_text(f'a; touch {marker}\n')
	proc = run_cli(['--shell', '-a', str(values), 'echo {}'])
	assert proc.returncode == 0
	assert not marker.exists()
	assert f'a; touch {marker}' in proc.stdout


def test_input_files_with_crlf_line_endings(tmp_path):
	hosts = tmp_path / 'hosts.txt'
	hosts.write_bytes(b'web1\r\nweb2\r\n')
	jobs = tmp_path / 'jobs.txt'
	jobs.write_bytes(b'echo a\r\n')
	assert run_cli(['-n', '-a', str(hosts), 'echo {}']).stdout.splitlines() == ['echo web1', 'echo web2']
	assert run_cli(['-n', '-f', str(jobs)]).stdout.splitlines() == ['echo a']


def test_both_inputs_from_stdin_is_a_usage_error():
	assert run_cli(['-f', '-', '-a', '-', 'echo {}']).returncode == 2


def test_arg_file_needs_a_template(tmp_path):
	hosts = tmp_path / 'hosts.txt'
	hosts.write_text('web1\n')
	assert run_cli(['-a', str(hosts)]).returncode == 2


def test_no_commands_is_a_usage_error():
	assert run_cli([]).returncode == 2


def test_empty_command_file_is_a_usage_error(tmp_path):
	jobs = tmp_path / 'jobs.txt'
	jobs.write_text('# nothing here\n\n')
	proc = run_cli(['-f', str(jobs)])
	assert proc.returncode == 2
	assert 'no commands to run' in proc.stderr


def test_missing_input_file_is_an_error(tmp_path):
	proc = run_cli(['-f', str(tmp_path / 'nope.txt')])
	assert proc.returncode == 2
	assert 'nope.txt' in proc.stderr


def test_arg_file_in_shell_mode_is_safe_when_the_template_quotes_the_placeholder(tmp_path):
	# shell-quoting the line only protects an unquoted {}; inside "..." or '...' it must still be data
	marker = tmp_path / 'pwned'
	values = tmp_path / 'values.txt'
	values.write_text(f"$(touch {marker})\nx'; touch {marker}; '\n")
	for template in ('echo "{}"', "echo '{}'", 'echo {}'):
		proc = run_cli(['--shell', '-a', str(values), template])
		assert proc.returncode == 0, template
		assert not marker.exists(), template
		assert f'$(touch {marker})' in proc.stdout, template
		assert f"x'; touch {marker}; '" in proc.stdout, template
