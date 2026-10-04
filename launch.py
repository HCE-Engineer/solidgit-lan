"""Entry point for the packaged application.

A separate script rather than `python -m solidgit_lan.ui`: PyInstaller freezes a *file*, and
a module run as a script loses its package context, so the relative imports inside
`ui/__main__.py` would fail in the built executable.
"""

from solidgit_lan.ui import run

raise SystemExit(run())
