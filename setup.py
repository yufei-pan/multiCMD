from setuptools import setup
from multiCMD import __version__

setup(
    name='multiCMD',
    version=__version__,
    description='Run commands simultaneously',
    long_description=open('README.md').read(),
    long_description_content_type='text/markdown',
    author='Yufei Pan',
    author_email='pan@zopyr.us',
    url='https://github.com/yufei-pan/multiCMD',
    py_modules=['multiCMD'],
    entry_points={
        'console_scripts': [
            'mcmd=multiCMD:main',
            'multiCMD=multiCMD:main',
            'multicmd=multiCMD:main',
        ],
    },
    install_requires=[
        'argparse',
    ],
    classifiers=[
        'Programming Language :: Python :: 3',
        'License :: OSI Approved :: GNU General Public License v3 or later (GPLv3+)',
        'Operating System :: POSIX :: Linux',
        'Operating System :: Microsoft :: Windows',
    ],
    python_requires='>=3.6',
	license='GPLv3+',
)
