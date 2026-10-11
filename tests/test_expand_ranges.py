from multiCMD import _expand_ranges, _expand_ranges_fast, run_commands


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
	assert _expand_ranges("[n:3]host[1-n]") == ["host1", "host2", "host3"]


def test_brace_expression_eval():
	assert _expand_ranges("host[{2+3}]") == ["host5"]


def test_passthrough_without_brackets():
	assert _expand_ranges("plainhost") == ["plainhost"]
	assert _expand_ranges("") == [""]


def test_letter_ranges_keep_their_case():
	assert _expand_ranges('host[A-C]') == ['hostA', 'hostB', 'hostC']
	assert _expand_ranges('host[a-c]') == ['hosta', 'hostb', 'hostc']


def test_hex_ranges_keep_padding_and_case():
	assert _expand_ranges('n[08-0b]') == ['n08', 'n09', 'n0a', 'n0b']
	assert _expand_ranges('n[08-0B]') == ['n08', 'n09', 'n0A', 'n0B']
	assert _expand_ranges('[a-10]') == ['a', 'b', 'c', 'd', 'e', 'f', '10']


def test_range_with_an_empty_end_is_a_plain_token():
	assert _expand_ranges('[-5]') == ['-5']


def test_ipv6_addresses_pass_through():
	assert _expand_ranges('ping [fe80::1,fe80::2]') == ['ping fe80::1', 'ping fe80::2']


def test_escaped_colon_is_literal():
	assert _expand_ranges('curl [web1\\:8080,web2\\:8080]') == ['curl web1:8080', 'curl web2:8080']


def test_variables_do_not_leak_between_calls():
	assert _expand_ranges('[n:3]host[1-n]') == ['host1', 'host2', 'host3']
	assert _expand_ranges('disk[n]') == ['diskn']


def test_variables_are_shared_within_one_run_commands_call():
	assert run_commands(['echo [n:2]first', 'echo second[1-n]'], parse=True, quiet=True) == \
		[['first'], ['second1'], ['second2']]


def test_explicit_variables_dict_is_used():
	variables = {}
	_expand_ranges('[n:2]', variables)
	assert variables == {'n': '2'}
