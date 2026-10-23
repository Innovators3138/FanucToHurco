"""
version.py  —  which build this is.

The build workflows overwrite VERSION before PyInstaller runs: a release with
its tag ('v1.0'), a build of main with `git describe` against the last version
tag ('v1.0-3-gabc1234').  Committed, it stays 'dev'.
"""

import os
import subprocess

VERSION = 'dev'


def app_version() -> str:
    """VERSION, or for a run from source, 'dev' plus where the checkout is."""
    if VERSION != 'dev':
        return VERSION
    try:
        out = subprocess.run(
            # Only version tags: the old 'Release' tag (GitHub release v0.1)
            # would otherwise name every build after itself.
            ['git', 'describe', '--tags', '--match', 'v[0-9]*', '--always',
             '--dirty'],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            capture_output=True, text=True, timeout=2,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        described = out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        described = ''
    return f'dev ({described})' if described else 'dev'
