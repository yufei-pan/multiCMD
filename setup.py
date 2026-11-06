from setuptools import setup
from multiCMD import __version__

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
    classifiers=[
        'Programming Language :: Python :: 3',
        'License :: OSI Approved :: GNU General Public License v3 or later (GPLv3+)',
        'Operating System :: POSIX :: Linux',
        'Operating System :: Microsoft :: Windows',
    ],
    python_requires='>=3.6',
	license='GPLv3+',
)
