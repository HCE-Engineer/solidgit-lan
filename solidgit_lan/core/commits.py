"""The commit chain.

Each commit is a full manifest snapshot plus its parents. There is no branching model and no
merge algorithm, because binary CAD files cannot be merged — divergence is resolved by people
choosing files, not by an algorithm (see reconcile.py).

Commit ids are content hashes rather than sequence numbers. Two teammates working offline
would both mint "commit 343"; identical ids for different content is exactly the kind of
corruption that is impossible to recover from, so ids are derived from content instead.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from collections.abc import Callable, Iterator, Mapping

from .hashing import is_valid_digest, sha256_bytes
from .manifest import Manifest

SHORT_ID_LENGTH = 12


class CommitError(Exception):
    """Raised when a commit is missing, malformed, or fails validation."""


@dataclass(frozen=True)
class Author:
    name: str
    device: str

    def to_json(self) -> dict[str, str]:
        return {"name": self.name, "device": self.device}

    @staticmethod
    def from_json(data: Mapping[str, object]) -> Author:
        return Author(name=str(data["name"]), device=str(data["device"]))


@dataclass(frozen=True)
class Commit:
    """One snapshot of the whole workspace."""

    parents: tuple[str, ...]
    lamport: int
    author: Author
    wall_clock: str
    """Display only. Machine clocks disagree; ordering decisions use `lamport`."""
    message: str
    manifest: Manifest
    id: str = ""

    def with_computed_id(self) -> Commit:
        return replace(self, id=sha256_bytes(self.canonical_bytes()))

    def canonical_bytes(self) -> bytes:
        """Deterministic serialisation used to derive the id — sorted keys, no id field."""
        payload = {
            "parents": list(self.parents),
            "lamport": self.lamport,
            "author": self.author.to_json(),
            "wall_clock": self.wall_clock,
            "message": self.message,
            "files": self.manifest.to_json(),
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
            "utf-8"
        )

    @property
    def short_id(self) -> str:
        return self.id[:SHORT_ID_LENGTH]

    @property
    def is_root(self) -> bool:
        return not self.parents

    @property
    def is_reconciliation(self) -> bool:
        """A commit with two parents is the result of an approved reconciliation plan."""
        return len(self.parents) > 1

    def to_json(self) -> dict[str, object]:
        return {
            "id": self.id,
            "parents": list(self.parents),
            "lamport": self.lamport,
            "author": self.author.to_json(),
            "wall_clock": self.wall_clock,
            "message": self.message,
            "files": self.manifest.to_json(),
        }

    @staticmethod
    def from_json(data: Mapping[str, object]) -> Commit:
        try:
            commit = Commit(
                parents=tuple(str(p) for p in data["parents"]),  # type: ignore[union-attr]
                lamport=int(data["lamport"]),  # type: ignore[arg-type]
                author=Author.from_json(data["author"]),  # type: ignore[arg-type]
                wall_clock=str(data["wall_clock"]),
                message=str(data["message"]),
                manifest=Manifest.from_json(data["files"]),  # type: ignore[arg-type]
                id=str(data["id"]),
            )
        except (KeyError, TypeError, ValueError) as e:
            raise CommitError(f"Malformed commit object: {e}") from e

        expected = sha256_bytes(commit.canonical_bytes())
        if commit.id != expected:
            raise CommitError(
                f"Commit {commit.id} does not match its own content (expected id {expected}). "
                "It was either corrupted or tampered with."
            )
        return commit


def build_commit(
    parents: tuple[str, ...],
    lamport: int,
    author: Author,
    wall_clock: str,
    message: str,
    manifest: Manifest,
) -> Commit:
    """Create a commit with its id already derived from its content."""
    return Commit(
        parents=parents,
        lamport=lamport,
        author=author,
        wall_clock=wall_clock,
        message=message,
        manifest=manifest,
    ).with_computed_id()


class CommitStore:
    """Commits on disk under `<repo_dir>/commits`, plus the HEAD pointer."""

    def __init__(self, repo_dir: str | Path) -> None:
        self.repo_dir = Path(repo_dir)
        self.commits_dir = self.repo_dir / "commits"
        self.head_path = self.repo_dir / "HEAD"
        self.tip_path = self.repo_dir / "TIP"
        self.tmp_dir = self.repo_dir / "tmp"
        self.commits_dir.mkdir(parents=True, exist_ok=True)
        self.tmp_dir.mkdir(parents=True, exist_ok=True)

    def path_for(self, commit_id: str) -> Path:
        if not is_valid_digest(commit_id):
            raise CommitError(f"Not a valid commit id: {commit_id!r}")
        return self.commits_dir / f"{commit_id}.json"

    def has(self, commit_id: str) -> bool:
        return self.path_for(commit_id).exists()

    def write(self, commit: Commit) -> None:
        if not commit.id:
            raise CommitError("Refusing to store a commit with no id; call with_computed_id().")
        target = self.path_for(commit.id)
        if target.exists():
            return  # Content-addressed: same id means identical content.
        self._atomic_write_json(target, commit.to_json())

    def read(self, commit_id: str) -> Commit:
        target = self.path_for(commit_id)
        if not target.exists():
            raise CommitError(f"Commit {commit_id} is not in this repository.")
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            raise CommitError(f"Could not read commit {commit_id}: {e}") from e
        return Commit.from_json(data)

    def iter_ids(self) -> Iterator[str]:
        for path in sorted(self.commits_dir.glob("*.json")):
            commit_id = path.stem
            if is_valid_digest(commit_id):
                yield commit_id

    def all_ids(self) -> frozenset[str]:
        return frozenset(self.iter_ids())

    # -- HEAD -----------------------------------------------------------------------------

    def head(self) -> str | None:
        if not self.head_path.exists():
            return None
        value = self.head_path.read_text(encoding="utf-8").strip()
        return value or None

    def set_head(self, commit_id: str, advance_tip: bool = True) -> None:
        """Point this workspace at a commit.

        `advance_tip` is off when taking someone else's work that is *behind* the shared
        front — checking out an older version must not rewind everyone else's notion of what
        the newest work is.
        """
        if not self.has(commit_id):
            raise CommitError(f"Refusing to point HEAD at unknown commit {commit_id}.")
        self._atomic_write_text(self.head_path, commit_id + "\n")
        if advance_tip:
            current_tip = self.tip()
            if current_tip is None or commit_id not in self.ancestors(current_tip):
                self._atomic_write_text(self.tip_path, commit_id + "\n")

    def head_commit(self) -> Commit | None:
        current = self.head()
        return self.read(current) if current else None

    # -- TIP ------------------------------------------------------------------------------

    def tip(self) -> str | None:
        """The newest commit anyone has contributed, which is not always what is checked out.

        HEAD is what this workspace currently has on disk; TIP is the front of the shared
        history. They differ whenever a teammate's work has arrived but the person here has
        not chosen to take it yet — which is the whole point of propagating by pull: files
        must never change under someone with the assembly open.
        """
        if not self.tip_path.exists():
            return self.head()
        value = self.tip_path.read_text(encoding="utf-8").strip()
        return value or self.head()

    def set_tip(self, commit_id: str) -> None:
        if not self.has(commit_id):
            raise CommitError(f"Refusing to point TIP at unknown commit {commit_id}.")
        self._atomic_write_text(self.tip_path, commit_id + "\n")

    def tip_commit(self) -> Commit | None:
        current = self.tip()
        return self.read(current) if current else None

    def behind_tip(self) -> int:
        """How many commits this workspace has not taken yet."""
        head, tip = self.head(), self.tip()
        if tip is None or head == tip:
            return 0
        known = self.ancestors(head) if head else frozenset()
        return len(self.ancestors(tip) - known)

    # -- history walking ------------------------------------------------------------------

    def ancestors(self, commit_id: str, include_self: bool = True) -> frozenset[str]:
        """Every commit reachable from `commit_id` by following parents."""
        seen: set[str] = set()
        stack = [commit_id]
        while stack:
            current = stack.pop()
            if current in seen or not self.has(current):
                continue
            seen.add(current)
            stack.extend(self.read(current).parents)
        if not include_self:
            seen.discard(commit_id)
        return frozenset(seen)

    def history(self, commit_id: str | None = None, limit: int | None = None) -> list[Commit]:
        """Commits reachable from `commit_id` (default HEAD), newest first."""
        start = commit_id if commit_id is not None else self.head()
        if start is None:
            return []
        commits = [self.read(cid) for cid in self.ancestors(start)]
        commits.sort(key=lambda c: (-c.lamport, c.id))
        return commits[:limit] if limit is not None else commits

    def merge_base(self, mine: str, theirs: str) -> str | None:
        """Best common ancestor of two heads we both have locally."""
        return best_common_ancestor(
            self.ancestors(mine) & self.ancestors(theirs),
            lambda cid: self.read(cid).lamport,
        )

    def next_lamport(self) -> int:
        """One past the highest logical clock we have seen — never derived from wall time."""
        highest = 0
        for commit_id in self.iter_ids():
            highest = max(highest, self.read(commit_id).lamport)
        return highest + 1

    # -- atomic writes --------------------------------------------------------------------

    def _atomic_write_text(self, target: Path, text: str) -> None:
        tmp_path = self.tmp_dir / f"{uuid.uuid4().hex}.part"
        try:
            with open(tmp_path, "w", encoding="utf-8") as out:
                out.write(text)
                out.flush()
                os.fsync(out.fileno())
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(tmp_path, target)
        finally:
            tmp_path.unlink(missing_ok=True)

    def _atomic_write_json(self, target: Path, payload: object) -> None:
        self._atomic_write_text(target, json.dumps(payload, indent=2, ensure_ascii=False))


# -- divergence detection ------------------------------------------------------------------


class Relation(Enum):
    """How my history relates to a peer's."""

    EQUAL = "equal"
    AHEAD = "ahead"
    """I have everything they have, plus more. They should fast-forward."""
    BEHIND = "behind"
    """They have everything I have, plus more. I should fast-forward."""
    DIVERGED = "diverged"
    """We each have commits the other lacks. Needs a reconciliation plan."""
    UNRELATED = "unrelated"
    """No shared history at all — almost certainly two different projects."""


def best_common_ancestor(
    shared: frozenset[str], lamport_of: Callable[[str], int]
) -> str | None:
    """Pick the merge base out of a set of common ancestors.

    Split out from CommitStore because the two histories being compared normally live in two
    different stores — a peer's commits arrive over the wire, and the CLI's `compare` reads a
    second workspace off disk. Neither side can answer this alone.

    Knowing the base is what lets reconciliation tell "they added this file" apart from
    "I deleted it": identical from the manifests alone, opposite in what they call for.
    """
    if not shared:
        return None
    return max(shared, key=lambda cid: (lamport_of(cid), cid))


def compare_heads(
    mine_head: str | None,
    mine_ids: frozenset[str],
    theirs_head: str | None,
    theirs_ids: frozenset[str],
) -> Relation:
    """Decide who is ahead using only commit ids — never wall-clock timestamps.

    A teammate's clock can be hours off, so "newer" is answered by reachability: if their head
    is somewhere in my history, I am the one who is ahead.
    """
    if mine_head is None and theirs_head is None:
        return Relation.EQUAL
    if mine_head is None:
        return Relation.BEHIND
    if theirs_head is None:
        return Relation.AHEAD
    if mine_head == theirs_head:
        return Relation.EQUAL
    if not (mine_ids & theirs_ids):
        return Relation.UNRELATED
    if theirs_head in mine_ids:
        return Relation.AHEAD
    if mine_head in theirs_ids:
        return Relation.BEHIND
    return Relation.DIVERGED
