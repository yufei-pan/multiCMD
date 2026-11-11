'''
The embeddable copy ( multiCMD_min_zlib_a85.py ) that hpcp, statblk and statbtrfs paste: it runs on a Python without lzma,
pasted as it is or indented inside a try, and it is the current source's
'''
import ast
import base64
import re
import subprocess
import sys
import zlib

import pytest

from conftest import PACKAGE_ROOT

EMBED = PACKAGE_ROOT / 'multiCMD_min_zlib_a85.py'


def _version(text):
	return re.search(r"^version\s*=\s*['\"]([\d.]+)['\"]", text, re.M).group(1)


def _decoded(embed):
	'''The source the embed's _SRC_B85 literal holds, as Python reads the literal'''
	literal, = re.findall(r"_SRC_B85=(r'''.*?''')", embed, re.S)
	return zlib.decompress(base64.a85decode(ast.literal_eval(literal))).decode('utf-8')


@pytest.mark.parametrize('form', ['as is', 'indented in a try'])
def test_the_embed_runs_on_a_python_without_lzma(tmp_path, form):
	# ( the 1.60 plan, G1 ) up to 1.59 the embed decoded with lzma, an optional module ( a Python built without the xz
	# headers lacks it ): a program that pasted it failed to import there unless its installed multiCMD was new enough.
	# hpcp 9.88 pastes it indented one tab inside a try, which ascii85 decoding reads with the tabs ignored ( G2 )
	block = EMBED.read_text(encoding='utf-8')
	if form == 'indented in a try':
		block = 'try:\n' + ''.join('\t' + line for line in block.splitlines(True)) + 'finally:\n\tpass\n'
	# an installed multiCMD older than the embedded one: the embedded copy runs
	(tmp_path / 'multiCMD.py').write_text("version = '1.0'\n")
	probe = tmp_path / 'probe.py'
	probe.write_text("import sys\nsys.modules['lzma'] = sys.modules['_lzma'] = None\n" + block +
		"print(multiCMD.version, multiCMD.run_command(['true'], quiet=True, return_code_only=True))\n", encoding='utf-8')
	out = subprocess.run([sys.executable, '-I', str(probe)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
		universal_newlines=True, timeout=60)
	assert out.returncode == 0, out.stderr
	assert out.stdout.split() == [_version((PACKAGE_ROOT / 'multiCMD.py').read_text(encoding='utf-8')), '0']


def test_the_generated_files_are_the_current_sources():
	# regenerated after the last version bump ( compressedEmbeddedCodeGenerator.py, see its docstring ): the embed holds
	# multiCMD_min.py, both carry multiCMD.py's version, and the embed's gate is that version
	version = _version((PACKAGE_ROOT / 'multiCMD.py').read_text(encoding='utf-8'))
	minified = (PACKAGE_ROOT / 'multiCMD_min.py').read_text(encoding='utf-8')
	embed = EMBED.read_text(encoding='utf-8')
	assert _decoded(embed) == minified
	assert _version(minified) == version
	assert '.version.split(\'.\')) < (%s):' % ', '.join(version.split('.')) in embed
	assert '\timport sys,types,base64,zlib ' in embed
