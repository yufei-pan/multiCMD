from multiCMD import Task, run_command


def test_task_defaults_and_iter():
	t = Task(["echo", "x"])
	assert t.returncode is None
	assert t.stdout == []
	assert t.stderr == []
	assert t.stop is False
	assert dict(t) == {
		"command": ["echo", "x"],
		"returncode": None,
		"stdout": [],
		"stderr": [],
	}


def test_task_repr_str_and_alive():
	t = run_command(["echo", "hi"], return_object=True, quiet=True)
	assert "hi" in str(t)
	assert "Task(" in repr(t)
	assert t.is_alive() is False
