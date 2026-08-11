import multiCMD
from multiCMD import _expand_ranges, _expand_ranges_fast


def test_alias_is_fast_implementation():
	assert _expand_ranges is _expand_ranges_fast


def test_numeric_alpha_and_comma():
	assert _expand_ranges("file[1-3].txt") == ["file1.txt", "file2.txt", "file3.txt"]
	assert _expand_ranges("file[a-c].txt") == ["filea.txt", "fileb.txt", "filec.txt"]
	assert _expand_ranges("file[1-2,a-b].txt") == [
		"file1.txt", "file2.txt", "filea.txt", "fileb.txt"
	]
	assert _expand_ranges("file[1-3,5].txt") == [
		"file1.txt", "file2.txt", "file3.txt", "file5.txt"
	]


def test_zero_padding_shorter_endpoint_rule():
	assert _expand_ranges("file[01-03].txt") == [
		"file01.txt", "file02.txt", "file03.txt"
	]
	assert _expand_ranges("[0-10]") == [str(i) for i in range(11)]
	assert _expand_ranges("[01-10]") == [f"{i:02d}" for i in range(1, 11)]


def test_hex_style_alpha_range():
	assert _expand_ranges("v[a-f]") == [f"v{c}" for c in "abcdef"]


def test_multi_bracket_cartesian():
	assert _expand_ranges("[1-2]-[a-b]") == ["1-a", "1-b", "2-a", "2-b"]


def test_variable_assignment_and_reference():
	multiCMD.__variables.clear()
	assert _expand_ranges("[n:3]host[1-n]") == ["host1", "host2", "host3"]


def test_brace_expression_eval():
	multiCMD.__variables.clear()
	assert _expand_ranges("host[{2+3}]") == ["host5"]


def test_passthrough_without_brackets():
	assert _expand_ranges("plainhost") == ["plainhost"]
	assert _expand_ranges("") == [""]
