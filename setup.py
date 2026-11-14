import setuptools
from setuptools import setup
from multiCMD import __version__

classifiers = [
    'Programming Language :: Python :: 3',
    'Operating System :: POSIX :: Linux',
    'Operating System :: Microsoft :: Windows',
]
# setuptools 77+ (PEP 639) takes an SPDX license expression and warns about license classifiers (an error next to an
# expression); older ones (the newest for Python 3.8 and earlier, so all a Python 3.6 install has) know only the
# free-text license and the classifier. Their sdist and wheel take LICENSE by its name (setuptools 56+, wheel 0.32+).
if int(setuptools.__version__.split('.')[0]) >= 77:
    license_metadata = {'license_expression': 'GPL-3.0-or-later', 'license_files': ['LICENSE']}
else:
    license_metadata = {'license': 'GPL-3.0-or-later'}
    classifiers.append('License :: OSI Approved :: GNU General Public License v3 or later (GPLv3+)')

setup(
    name='multiCMD',
    version=__version__,
    description='Run commands simultaneously',
    long_description=open('README.md', encoding='utf-8').read(),
    long_description_content_type='text/markdown',
    author='Yufei Pan',
    author_email='pan@zopyr.us',
    url='https://github.com/yufei-pan/multiCMD',
    py_modules=['multiCMD'],
    # The mixed-case command is a script file and not an entry point: pip 9 (stock on EL7 and EL8) reads entry_points.txt with a
    # parser that folds case, so two commands that differ only in case make it fail to upgrade or uninstall the package.
    scripts=['scripts/multiCMD'],
    entry_points={
        'console_scripts': [
            'mcmd=multiCMD:main',
            'multicmd=multiCMD:main',
        ],
    },
    classifiers=classifiers,
    python_requires='>=3.6',
    **license_metadata
)
