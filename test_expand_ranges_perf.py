import string
import re
import time
import itertools
from collections import deque
__variables = {}

def _expand_ranges(inStr):
	'''
	Expand ranges in a string

	@params:
		inStr: The string to expand

	@returns:
		list[str]: The expanded string
	'''
	global __variables
	expandingStr = [inStr]
	expandedList = []
	# all valid alphanumeric characters
	alphanumeric = string.digits + string.ascii_letters
	while len(expandingStr) > 0:
		currentStr = expandingStr.pop()
		match = re.search(r'\[(.*?)]', currentStr)
		if not match:
			expandedList.append(currentStr)
			continue
		group = match.group(1)
		parts = group.split(',')
		for part in parts:
			part = part.strip()
			if ':' in part:
				variableName, _, part = part.partition(':')
				__variables[variableName] = part
				expandingStr.append(currentStr.replace(match.group(0), '', 1))
			elif '-' in part:
				try:
					range_start,_, range_end = part.partition('-')
				except ValueError:
					expandedList.append(currentStr)
					continue
				range_start = range_start.strip()
				if range_start in __variables:
					range_start = __variables[range_start]
				range_end = range_end.strip()
				if range_end in __variables:
					range_end = __variables[range_end]
				if range_start.isdigit() and range_end.isdigit():
					padding_length = min(len(range_start), len(range_end))
					format_str = "{:0" + str(padding_length) + "d}"
					for i in range(int(range_start), int(range_end) + 1):
						formatted_i = format_str.format(i)
						expandingStr.append(currentStr.replace(match.group(0), formatted_i, 1))
				elif all(c in string.hexdigits for c in range_start + range_end):
					for i in range(int(range_start, 16), int(range_end, 16) + 1):
						expandingStr.append(currentStr.replace(match.group(0), format(i, 'x'), 1))
				else:
					try:
						start_index = alphanumeric.index(range_start)
						end_index = alphanumeric.index(range_end)
						for i in range(start_index, end_index + 1):
							expandingStr.append(currentStr.replace(match.group(0), alphanumeric[i], 1))
					except ValueError:
						expandedList.append(currentStr)
			else:
				expandingStr.append(currentStr.replace(match.group(0), part, 1))
	expandedList.reverse()
	return expandedList



# immutable helpers compiled once at import time
_BRACKET_RX   = re.compile(r'\[([^\]]+)\]')
_ALPHANUM     = string.digits + string.ascii_letters
_ALPHA_IDX    = {c: i for i, c in enumerate(_ALPHANUM)}

def _expand_piece(piece: str, vars_: dict[str, str]) -> list[str]:
	"""Turn one comma-separated element from inside [...] into a list of strings."""
	piece = piece.strip()

	# variable assignment  foo:BAR
	if ':' in piece:
		var, _, value = piece.partition(':')
		vars_[var] = value
		return ['']                           # bracket disappears

	# explicit range  start-end
	if '-' in piece:
		start, _, end = (p.strip() for p in piece.partition('-'))

		start = vars_.get(start, start)
		end   = vars_.get(end,   end)

		if start.isdigit() and end.isdigit():               # decimal range
			pad = max(len(start), len(end))
			return [f"{i:0{pad}d}" for i in range(int(start), int(end) + 1)]

		if all(c in string.hexdigits for c in start + end): # hex range
			return [format(i, 'x') for i in range(int(start, 16),
												  int(end,   16) + 1)]

		# alphanumeric range (0-9a-zA-Z)
		try:
			return [_ALPHANUM[i]
					for i in range(_ALPHA_IDX[start], _ALPHA_IDX[end] + 1)]
		except KeyError:
			pass                                            # fall through

	# plain token or ${var}
	return [vars_.get(piece, piece)]

def expand_ranges_fast(template: str) -> list[str]:
	"""~5-30x faster drop-in replacement for _expand_ranges."""
	vars_: dict[str, str] = {}
	segments: list[list[str]] = []
	pos = 0

	# split the template into literal pieces + expandable pieces
	for m in _BRACKET_RX.finditer(template):
		if m.start() > pos:
			segments.append([template[pos:m.start()]])          # literal
		choices: list[str] = []
		for sub in m.group(1).split(','):
			choices.extend(_expand_piece(sub, vars_))
		segments.append(choices or [''])                        # keep length
		pos = m.end()
	segments.append([template[pos:]])                           # tail

	# cartesian product of all segments
	return [''.join(parts) for parts in itertools.product(*segments)]



_range_pattern = re.compile(r'\[(.*?)]')
_alphanumeric = string.digits + string.ascii_letters


def _expand_ranges_2(inStr):
	expanding_str = deque([inStr])
	expanded_list = []

	while expanding_str:
		current_str = expanding_str.pop()
		match = _range_pattern.search(current_str)

		if not match:
			expanded_list.append(current_str)
			continue

		group = match.group(1)
		parts = [part.strip() for part in group.split(',')]

		for part in parts:
			if ':' in part:
				variable_name, _, value = part.partition(':')
				__variables[variable_name] = value
				expanding_str.append(current_str.replace(match.group(0), '', 1))
			elif '-' in part:
				range_start, _, range_end = part.partition('-')
				range_start = __variables.get(range_start, range_start).strip()
				range_end = __variables.get(range_end, range_end).strip()

				if range_start.isdigit() and range_end.isdigit():
					padding_length = max(len(range_start), len(range_end))
					format_str = "{:0" + str(padding_length) + "d}"
					for i in range(int(range_start), int(range_end) + 1):
						formatted_i = format_str.format(i)
						expanding_str.append(current_str.replace(match.group(0), formatted_i, 1))
				elif all(c in string.hexdigits for c in range_start + range_end):
					for i in range(int(range_start, 16), int(range_end, 16) + 1):
						formatted_i = format(i, 'x')
						expanding_str.append(current_str.replace(match.group(0), formatted_i, 1))
				else:
					start_index = _alphanumeric.find(range_start)
					end_index = _alphanumeric.find(range_end)
					if start_index >= 0 and end_index >= 0:
						for c in _alphanumeric[start_index:end_index + 1]:
							expanding_str.append(current_str.replace(match.group(0), c, 1))
					else:
						expanded_list.append(current_str)
			else:
				expanding_str.append(current_str.replace(match.group(0), part, 1))
	expanded_list.reverse()
	return expanded_list


def _expand_ranges_3(inStr):
	'''
	Expand ranges in a string

	@params:
		inStr: The string to expand

	@returns:
		list[str]: The expanded string
	'''
	global __variables
	expandingStr = [inStr]
	expandedList = []
	# all valid alphanumeric characters
	alphanumeric = string.digits + string.ascii_letters
	while len(expandingStr) > 0:
		currentStr = expandingStr.pop()
		match = re.search(r'\[(.*?)]', currentStr)
		if not match:
			expandedList.append(currentStr)
			continue
		group = match.group(1)
		parts = group.split(',')
		for part in parts:
			part = part.strip()
			if ':' in part:
				variableName, _, part = part.partition(':')
				__variables[variableName] = part
				expandingStr.append(currentStr.replace(match.group(0), '', 1))
			elif '-' in part:
				try:
					range_start,_, range_end = part.partition('-')
				except ValueError:
					expandedList.append(currentStr)
					continue
				range_start = range_start.strip()
				if range_start in __variables:
					range_start = __variables[range_start]
				range_end = range_end.strip()
				if range_end in __variables:
					range_end = __variables[range_end]
				if range_start.isdigit() and range_end.isdigit():
					padding_length = min(len(range_start), len(range_end))
					format_str = "{:0" + str(padding_length) + "d}"
					for i in range(int(range_start), int(range_end) + 1):
						formatted_i = format_str.format(i)
						replacedStr = currentStr.replace(match.group(0), formatted_i, 1)
						# if there is more groups to expand, add it to expandingStr
						if re.search(r'\[(.*?)]', replacedStr):
							expandingStr.append(replacedStr)
						else:
							expandedList.append(replacedStr)
				elif all(c in string.hexdigits for c in range_start + range_end):
					for i in range(int(range_start, 16), int(range_end, 16) + 1):
						#expandingStr.append(currentStr.replace(match.group(0), format(i, 'x'), 1))
						replacedStr = currentStr.replace(match.group(0), format(i, 'x'), 1)
						# if there is more groups to expand, add it to expandingStr
						if re.search(r'\[(.*?)]', replacedStr):
							expandingStr.append(replacedStr)
						else:
							expandedList.append(replacedStr)
				else:
					try:
						start_index = alphanumeric.index(range_start)
						end_index = alphanumeric.index(range_end)
						for i in range(start_index, end_index + 1):
							#expandingStr.append(currentStr.replace(match.group(0), alphanumeric[i], 1))
							replacedStr = currentStr.replace(match.group(0), alphanumeric[i], 1)
							# if there is more groups to expand, add it to expandingStr
							if re.search(r'\[(.*?)]', replacedStr):
								expandingStr.append(replacedStr)
							else:
								expandedList.append(replacedStr)
					except ValueError:
						expandedList.append(currentStr)
			else:
				#expandingStr.append(currentStr.replace(match.group(0), part, 1))
				replacedStr = currentStr.replace(match.group(0), part, 1)
				# if there is more groups to expand, add it to expandingStr
				if re.search(r'\[(.*?)]', replacedStr):
					expandingStr.append(replacedStr)
				else:
					expandedList.append(replacedStr)
	expandedList.reverse()
	return expandedList

# Precompile regex and prepare alphanumeric lookup
_PATTERN = re.compile(r'\[([^\]]+)\]')
_ALPHANUMERIC = string.digits + string.ascii_letters


def expand_ranges_4(s: str, variables: dict[str, str] = None) -> list[str]:
	"""
	Optimized expansion of bracketed ranges and variables in a string.

	Supports numeric, hexadecimal, and alphanumeric ranges, as well as variable definitions.

	:param s: Input string with bracket expressions
	:param variables: Optional dict to carry over variable definitions
	:return: List of expanded strings
	"""
	if variables is None:
		variables = {}

	output: list[str] = []
	queue: deque[str] = deque([s])

	while queue:
		current = queue.popleft()
		match = _PATTERN.search(current)
		if not match:
			output.append(current)
			continue

		body = match.group(1)
		start, end = match.span()
		tokens = [tok.strip() for tok in body.split(',')]

		for tok in tokens:
			# Variable definition e.g. [var:value]
			if ':' in tok:
				name, val = tok.split(':', 1)
				variables[name] = val
				queue.append(current[:start] + current[end:])

			# Range e.g. [1-5], [a-f], [var1-var2]
			elif '-' in tok:
				left, right = tok.split('-', 1)
				left = variables.get(left, left)
				right = variables.get(right, right)

				# Numeric range
				if left.isdigit() and right.isdigit():
					width = max(len(left), len(right))
					for i in range(int(left), int(right) + 1):
						rep = f"{i:0{width}d}"
						queue.append(current[:start] + rep + current[end:])

				# Hexadecimal range
				elif all(c in string.hexdigits for c in left + right):
					start_i = int(left, 16)
					end_i = int(right, 16)
					for i in range(start_i, end_i + 1):
						rep = format(i, 'x')
						queue.append(current[:start] + rep + current[end:])

				# Alphanumeric range
				else:
					try:
						idx1 = _ALPHANUMERIC.index(left)
						idx2 = _ALPHANUMERIC.index(right)
						for i in range(idx1, idx2 + 1):
							queue.append(current[:start] + _ALPHANUMERIC[i] + current[end:])
					except ValueError:
						# Invalid range, keep original
						output.append(current)

			# Simple token replacement
			else:
				queue.append(current[:start] + tok + current[end:])

	return output


def test_expand_ranges_perf():
	"""Test the performance of the range expansion functions."""
	global __variables
	__variables = {}

	# Test input
	test_input = "item[0001-20000]_[00-100]"

	# Measure performance of _expand_ranges
	start_time = time.time()
	result1 = _expand_ranges(test_input)
	elapsed1 = time.time() - start_time

	# Measure performance of expand_ranges_fast
	start_time = time.time()
	result2 = expand_ranges_fast(test_input)
	elapsed2 = time.time() - start_time
	# Ensure both methods produce the same result
	if result1 != result2:
		print("Results do not match between _expand_ranges and expand_ranges_fast.")
		print(f'result1: {result1[:10]}...')
		print(f'result2: {result2[:10]}...')
	# Measure performance of _expand_ranges_2
	start_time = time.time()
	result3 = _expand_ranges_2(test_input)
	elapsed3 = time.time() - start_time
	# Ensure both methods produce the same result
	if result1 != result3:
		print("Results do not match between _expand_ranges and _expand_ranges_2.")
		print(f'result1: {result1[:10]}...')
		print(f'result3: {result3[:10]}...')
	  
	# Measure performance of _expand_ranges_3
	start_time = time.time()
	result4 = _expand_ranges_3(test_input)
	elapsed4 = time.time() - start_time
	# Ensure both methods produce the same result
	if result1 != result4:
		print("Results do not match between _expand_ranges and _expand_ranges_3.")
		print(f'result1: {result1[:10]}...')
		print(f'result4: {result4[:10]}...')

	start_time = time.time()
	result5 = expand_ranges_4(test_input)
	elapsed5 = time.time() - start_time
	# Ensure both methods produce the same result
	if result1 != result5:
		print("Results do not match between _expand_ranges and expand_ranges_4.")
		print(f'result1: {result1[:10]}...')
		print(f'result5: {result5[:10]}...')
	
	print(f"_expand_ranges took {elapsed1:.6f} seconds and produced {len(result1)} results.")
	print(f"expand_ranges_fast took {elapsed2:.6f} seconds and produced {len(result2)} results.")
	print(f"_expand_ranges_2 took {elapsed3:.6f} seconds and produced {len(result3)} results.")
	print(f"_expand_ranges_3 took {elapsed4:.6f} seconds and produced {len(result4)} results.")
	print(f"expand_ranges_4 took {elapsed5:.6f} seconds and produced {len(result5)} results.")

if __name__ == "__main__":
	test_expand_ranges_perf()