#!/usr/bin/env python3
"""Build the minified and embeddable copies of a single-file module.

	multiCMD.py -> multiCMD_min.py            (python-minifier)
	            -> multiCMD_min_zlib_a85.py   (zlib + ascii85 blob in a drop-in import guard)

The import guard uses an installed module only if its version is not older
than the embedded copy; otherwise it exec()s the embedded copy into a fresh
module. Paste multiCMD_min_zlib_a85.py verbatim into the consuming scripts.
It decodes with zlib, not lzma: lzma is an optional module that a Python built
without the xz headers lacks, and zlib is one pip itself needs. The blob sits
in a raw string that ascii85 decoding reads with whitespace ignored, so the
block may also be pasted indented, inside a try of the consumer's.

python-minifier writes f-strings in the syntax of the Python it runs on, so
run this under Python <= 3.11 to keep the output loadable on Python 3.6+:

	uv run --no-project --python 3.11 --with python-minifier==3.4.0 python compressedEmbeddedCodeGenerator.py multiCMD.py
"""
import argparse
import ast
import base64
import re
import sys
import zlib
from pathlib import Path

# Keep names readable (no renaming) and drop docstrings.
MINIFY_OPTIONS = dict(
	rename_locals=False,
	rename_globals=False,
	remove_literal_statements=True,
	hoist_literals=False,
	remove_annotations=True,
	combine_imports=True,
	remove_pass=False,
)
B85_LINE_WIDTH = 116
VERSION_RE = re.compile(r"""^version\s*=\s*['"]([\d.]+)['"]""", re.MULTILINE)

EMBED_TEMPLATE = '''try:
	import {module}  # type: ignore
	if tuple(int(x) for x in {module}.version.split('.')) < ({version_tuple}):
		raise ImportError('{module} too old')
except Exception:
	import sys,types,base64,zlib   # noqa: E401
	{module} = types.ModuleType("{module}")
	sys.modules["{module}"] = {module}
	_SRC_B85=r\'\'\'
{b85}\'\'\'
	exec(zlib.decompress(base64.a85decode(_SRC_B85)).decode("utf-8"), {module}.__dict__)
'''

def minify(source):
	if sys.version_info >= (3, 12):
		sys.exit("Run under Python <= 3.11: python-minifier would emit 3.12-only f-strings (see module docstring).")
	try:
		import python_minifier
	except ImportError:
		sys.exit("python-minifier is required (see module docstring for the uv command).")
	minified = python_minifier.minify(source, **MINIFY_OPTIONS)
	if sys.version_info >= (3, 8):
		try:
			ast.parse(minified, feature_version=(3, 6))
		except SyntaxError as e:
			sys.exit(f"Minified output is not Python 3.6 syntax: {e}")
	return minified

def b85_is_raw_string_safe(b85):
	# The blob sits in an r''' ''' literal: it can't contain the closing quotes
	# or end in a backslash (which would escape them).
	return "'''" not in b85 and not b85.endswith("\\")

def compress(data):
	"""Return (b85_text, level, mem_level, compressed_size) for the best zlib settings.

	Tries every compression level and memory level. Prefers the smallest output,
	then the lowest level, then the lowest memory level, skipping any whose
	ascii85 text can't be embedded in a raw string. The output depends on the
	zlib library too: the documented command's Python 3.11 has the same one on
	every run, so the files regenerate byte for byte.
	"""
	candidates = []
	for level in range(1, 10):
		for mem_level in range(1, 10):
			packer = zlib.compressobj(level, zlib.DEFLATED, zlib.MAX_WBITS, mem_level)
			blob = packer.compress(data) + packer.flush()
			candidates.append((len(blob), level, mem_level, blob))
	for size, level, mem_level, blob in sorted(candidates, key=lambda c: c[:3]):
		b85 = base64.a85encode(blob).decode('ascii')
		if b85_is_raw_string_safe(b85):
			return b85, level, mem_level, size
	sys.exit("No zlib setting produced an ascii85 blob that fits in a raw string.")

def build_embed(module, version, b85):
	wrapped = "\n".join(b85[i:i + B85_LINE_WIDTH] for i in range(0, len(b85), B85_LINE_WIDTH))
	version_tuple = ", ".join(version.split("."))
	return EMBED_TEMPLATE.format(module=module, version_tuple=version_tuple, b85=wrapped)

def verify_embed(embed, expected_source):
	"""The _SRC_B85 literal, as Python parses it, must decode to expected_source."""
	tree = ast.parse(embed)
	literals = [node.value for node in ast.walk(tree)
		if isinstance(node, ast.Assign) and any(getattr(t, 'id', None) == '_SRC_B85' for t in node.targets)]
	if len(literals) != 1:
		sys.exit("Embed snippet must assign _SRC_B85 exactly once.")
	text = ast.literal_eval(literals[0])
	if zlib.decompress(base64.a85decode(text)).decode("utf-8") != expected_source:
		sys.exit("Embed snippet does not decode back to the minified source.")

def main():
	parser = argparse.ArgumentParser(description="Minify a single-file module and build its embeddable zlib/ascii85 import guard.")
	parser.add_argument('source', nargs='?', help="Module to process (e.g. multiCMD.py). Prompted for if omitted.")
	args = parser.parse_args()
	source_path = Path(args.source or input("Enter the path to the file you want to compress: "))
	source = source_path.read_text(encoding="utf-8")
	module = source_path.stem
	match = VERSION_RE.search(source)
	if not match:
		sys.exit(f"No `version = 'x.y'` line found in {source_path}.")
	version = match.group(1)

	minified = minify(source)
	min_path = source_path.with_name(f"{module}_min.py")
	min_path.write_text(minified, encoding="utf-8")

	b85, level, mem_level, compressed_size = compress(minified.encode("utf-8"))
	embed = build_embed(module, version, b85)
	verify_embed(embed, minified)
	embed_path = source_path.with_name(f"{module}_min_zlib_a85.py")
	embed_path.write_text(embed, encoding="utf-8")

	print(f"{module} {version}")
	print(f"Wrote {min_path} ({len(minified.encode('utf-8'))} bytes, from {len(source.encode('utf-8'))})")
	print(f"Wrote {embed_path} (zlib {zlib.ZLIB_RUNTIME_VERSION} level {level}, memLevel {mem_level}, {compressed_size} bytes compressed)")

if __name__ == "__main__":
	main()
