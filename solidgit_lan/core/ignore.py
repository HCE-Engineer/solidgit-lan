"""Which files in a workspace are ours to track.

SolidWorks litters a working folder with lock files, autosaves and backups. Syncing those
would be pure noise at best, and at worst would hand a teammate a half-written file.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field

#: Our own metadata directory, never tracked as content.
REPO_DIR = ".solidgit"

DEFAULT_IGNORE_PATTERNS: tuple[str, ...] = (
    # SolidWorks in-use lock files, written next to the document while it is open.
    "~$*",
    # SolidWorks autosave / backup output.
    "Backup of *",
    "*.bak",
    "*.sldbk",
    "AutoRecover of *",
    # Editor / OS clutter.
    "*.tmp",
    "*.swp",
    "Thumbs.db",
    "desktop.ini",
    ".DS_Store",
)

#: Directory names skipped wholesale during a scan.
DEFAULT_IGNORE_DIRS: tuple[str, ...] = (
    REPO_DIR,
    "SOLIDWORKS Backup",
    "__pycache__",
    ".git",
)


@dataclass(frozen=True)
class IgnoreRules:
    """Glob patterns applied to file names, plus directory names skipped entirely."""

    patterns: tuple[str, ...] = DEFAULT_IGNORE_PATTERNS
    directories: tuple[str, ...] = DEFAULT_IGNORE_DIRS
    _lowered_dirs: frozenset[str] = field(init=False, repr=False, compare=False, default=frozenset())

    def __post_init__(self) -> None:
        object.__setattr__(self, "_lowered_dirs", frozenset(d.lower() for d in self.directories))

    def ignores_file(self, name: str) -> bool:
        lowered = name.lower()
        return any(fnmatch.fnmatch(lowered, pattern.lower()) for pattern in self.patterns)

    def ignores_dir(self, name: str) -> bool:
        return name.lower() in self._lowered_dirs

    def with_extra_patterns(self, extra: tuple[str, ...]) -> IgnoreRules:
        return IgnoreRules(patterns=self.patterns + extra, directories=self.directories)


DEFAULT_RULES = IgnoreRules()
