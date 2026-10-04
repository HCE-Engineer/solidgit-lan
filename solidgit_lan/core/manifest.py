"""Workspace snapshots.

A manifest maps every tracked file's workspace-relative path to its content hash. Comparing
two manifests is how we answer every interesting question: what changed since my last commit,
what is a teammate missing, and which files two diverged histories disagree about.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Iterable, Iterator, Mapping

from .hashing import sha256_file
from .ignore import DEFAULT_RULES, IgnoreRules


class WorkspaceError(Exception):
    """Raised when a workspace cannot be scanned into a usable snapshot."""


@dataclass(frozen=True)
class FileRef:
    """What a commit records about one file."""

    sha256: str
    size: int

    def to_json(self) -> dict[str, object]:
        return {"sha256": self.sha256, "size": self.size}

    @staticmethod
    def from_json(data: Mapping[str, object]) -> FileRef:
        return FileRef(sha256=str(data["sha256"]), size=int(data["size"]))  # type: ignore[arg-type]


@dataclass(frozen=True)
class Manifest:
    """An immutable path -> FileRef snapshot.

    Paths are workspace-relative and always use forward slashes, so the same manifest means
    the same thing regardless of which machine produced it.
    """

    entries: Mapping[str, FileRef]

    def __iter__(self) -> Iterator[str]:
        return iter(self.entries)

    def __len__(self) -> int:
        return len(self.entries)

    def __contains__(self, path: str) -> bool:
        return path in self.entries

    def get(self, path: str) -> FileRef | None:
        return self.entries.get(path)

    def paths(self) -> frozenset[str]:
        return frozenset(self.entries)

    def digests(self) -> frozenset[str]:
        return frozenset(ref.sha256 for ref in self.entries.values())

    def total_size(self) -> int:
        return sum(ref.size for ref in self.entries.values())

    def to_json(self) -> dict[str, dict[str, object]]:
        return {path: ref.to_json() for path, ref in sorted(self.entries.items())}

    @staticmethod
    def from_json(data: Mapping[str, Mapping[str, object]]) -> Manifest:
        return Manifest({path: FileRef.from_json(ref) for path, ref in data.items()})

    @staticmethod
    def empty() -> Manifest:
        return Manifest({})


def relative_path(root: Path, target: Path) -> str:
    """Workspace-relative, forward-slash path — the canonical key used everywhere."""
    return target.relative_to(root).as_posix()


def iter_workspace_files(
    root: str | Path, rules: IgnoreRules = DEFAULT_RULES
) -> Iterator[Path]:
    """Walk the workspace, skipping ignored directories and files."""
    root_path = Path(root)
    stack = [root_path]
    while stack:
        directory = stack.pop()
        try:
            children = sorted(directory.iterdir())
        except OSError as e:
            raise WorkspaceError(f"Could not read directory '{directory}': {e}") from e
        for child in children:
            if child.is_dir():
                if not rules.ignores_dir(child.name):
                    stack.append(child)
            elif child.is_file() and not rules.ignores_file(child.name):
                yield child


#: A file whose timestamp is this fresh is re-hashed even on a cache hit. Filesystem
#: timestamps have limited resolution, so a file written twice within the same tick at the
#: same size would otherwise look unchanged.
RACY_WINDOW_SECONDS = 2.0


@dataclass
class StatCache:
    """Remembers which content hash a file had at a given size and timestamp.

    The interface rescans the workspace roughly once a second to keep the file list honest.
    Hashing every part on every pass would mean re-reading hundreds of megabytes per second
    on a real assembly, so a file is re-hashed only once its size or timestamp moves.
    """

    entries: dict[str, tuple[int, int, str]] = field(default_factory=dict)
    hits: int = 0
    misses: int = 0

    def lookup(self, path: str, size: int, mtime_ns: int, now: float) -> str | None:
        cached = self.entries.get(path)
        if cached is None or cached[0] != size or cached[1] != mtime_ns:
            return None
        if now - (mtime_ns / 1_000_000_000) < RACY_WINDOW_SECONDS:
            return None
        return cached[2]

    def remember(self, path: str, size: int, mtime_ns: int, digest: str) -> None:
        self.entries[path] = (size, mtime_ns, digest)

    def forget_missing(self, present: frozenset[str]) -> None:
        for path in set(self.entries) - present:
            del self.entries[path]


def scan_workspace(
    root: str | Path,
    rules: IgnoreRules = DEFAULT_RULES,
    cache: StatCache | None = None,
) -> Manifest:
    """Hash every tracked file under `root` into a manifest.

    Rejects two files whose paths differ only in case: Windows treats them as one file, so a
    manifest containing both would resolve differently on the machine that created it than on
    a teammate's — a silent, extremely confusing divergence.
    """
    root_path = Path(root)
    if not root_path.is_dir():
        raise WorkspaceError(f"Workspace '{root_path}' is not a directory.")

    entries: dict[str, FileRef] = {}
    seen_lowercase: dict[str, str] = {}
    now = time.time()

    for file_path in iter_workspace_files(root_path, rules):
        key = relative_path(root_path, file_path)
        lowered = key.lower()
        if lowered in seen_lowercase:
            raise WorkspaceError(
                "Two files differ only by capitalisation, which Windows cannot tell apart:\n"
                f"  {seen_lowercase[lowered]}\n  {key}\n"
                "Rename one of them before adding this folder."
            )
        seen_lowercase[lowered] = key

        if cache is None:
            digest, size = sha256_file(file_path)
        else:
            stat_result = file_path.stat()
            size, mtime_ns = stat_result.st_size, stat_result.st_mtime_ns
            digest = cache.lookup(key, size, mtime_ns, now)
            if digest is None:
                cache.misses += 1
                digest, size = sha256_file(file_path)
                cache.remember(key, size, mtime_ns, digest)
            else:
                cache.hits += 1
        entries[key] = FileRef(sha256=digest, size=size)

    if cache is not None:
        cache.forget_missing(frozenset(entries))
    return Manifest(entries)


def changed_paths(before: Manifest, after: Manifest) -> tuple[frozenset[str], frozenset[str], frozenset[str]]:
    """Split two manifests into (added, modified, removed) path sets."""
    before_paths = before.paths()
    after_paths = after.paths()
    added = after_paths - before_paths
    removed = before_paths - after_paths
    modified = frozenset(
        path
        for path in before_paths & after_paths
        if before.entries[path].sha256 != after.entries[path].sha256
    )
    return added, modified, removed


def is_dirty(before: Manifest, after: Manifest) -> bool:
    added, modified, removed = changed_paths(before, after)
    return bool(added or modified or removed)


def manifest_from_refs(refs: Iterable[tuple[str, FileRef]]) -> Manifest:
    return Manifest(dict(refs))
