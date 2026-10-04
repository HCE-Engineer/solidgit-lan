"""Reconciling two diverged histories.

This runs when two people worked apart — typically before the network existed — and now meet.
There is no automatic merge and there never will be: `.sldprt` files are opaque binaries, so
the only honest resolution is a person looking at both versions and choosing.

Two invariants hold throughout:

* **Nothing is overwritten without an explicit choice**, and both sides must approve the same
  plan before a single byte is written.
* **The losing version is never deleted.** It stays in the object store, reachable from its
  own commit, so a wrong choice is recoverable.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import Enum
from collections.abc import Mapping

from .manifest import FileRef, Manifest


class ReconcileError(Exception):
    """Raised when a plan does not match the divergence it claims to resolve."""


class Side(Enum):
    MINE = "mine"
    THEIRS = "theirs"


class DiffStatus(Enum):
    """How one path differs between two manifests."""

    SAME = "same"

    # Classified without a common ancestor: we can see that they differ, not who changed what.
    ONLY_MINE = "only_mine"
    ONLY_THEIRS = "only_theirs"
    DIFFERENT = "different"

    # Classified against the merge base, which tells us who actually changed what.
    ADDED_BY_MINE = "added_by_mine"
    ADDED_BY_THEIRS = "added_by_theirs"
    MODIFIED_BY_MINE = "modified_by_mine"
    MODIFIED_BY_THEIRS = "modified_by_theirs"
    DELETED_BY_MINE = "deleted_by_mine"
    DELETED_BY_THEIRS = "deleted_by_theirs"
    BOTH_MODIFIED = "both_modified"
    MODIFY_DELETE = "modify_delete"
    """One side edited it, the other deleted it — always a human decision."""


#: Statuses where the two sides genuinely disagree and no default is defensible.
CONFLICTING = frozenset(
    {
        DiffStatus.DIFFERENT,
        DiffStatus.BOTH_MODIFIED,
        DiffStatus.MODIFY_DELETE,
    }
)


@dataclass(frozen=True)
class FileDiff:
    path: str
    status: DiffStatus
    mine: FileRef | None
    theirs: FileRef | None
    base: FileRef | None = None

    @property
    def needs_decision(self) -> bool:
        return self.status in CONFLICTING

    @property
    def default_side(self) -> Side | None:
        """The only non-destructive choice, where one exists.

        Conflicts have no default on purpose: leaving them unanswered is what forces the UI
        to ask rather than quietly picking a winner.
        """
        if self.needs_decision:
            return None
        if self.status in (
            DiffStatus.ONLY_THEIRS,
            DiffStatus.ADDED_BY_THEIRS,
            DiffStatus.MODIFIED_BY_THEIRS,
            DiffStatus.DELETED_BY_THEIRS,
        ):
            return Side.THEIRS
        return Side.MINE

    def ref_for(self, side: Side) -> FileRef | None:
        return self.mine if side is Side.MINE else self.theirs


def diff_manifests(
    mine: Manifest, theirs: Manifest, base: Manifest | None = None
) -> tuple[FileDiff, ...]:
    """Compare two snapshots, using the merge base when we have it.

    Without a base, a path present on only one side is ambiguous — it was either added there
    or deleted here, and those call for opposite actions. With a base we can tell, which is
    the difference between honouring a teammate's deletion and resurrecting it forever.
    """
    diffs: list[FileDiff] = []
    for path in sorted(mine.paths() | theirs.paths()):
        mine_ref = mine.get(path)
        theirs_ref = theirs.get(path)
        base_ref = base.get(path) if base is not None else None
        status = (
            _classify_without_base(mine_ref, theirs_ref)
            if base is None
            else _classify_with_base(mine_ref, theirs_ref, base_ref)
        )
        diffs.append(
            FileDiff(path=path, status=status, mine=mine_ref, theirs=theirs_ref, base=base_ref)
        )
    return tuple(diffs)


def _classify_without_base(mine: FileRef | None, theirs: FileRef | None) -> DiffStatus:
    if mine is not None and theirs is not None:
        return DiffStatus.SAME if mine.sha256 == theirs.sha256 else DiffStatus.DIFFERENT
    return DiffStatus.ONLY_MINE if mine is not None else DiffStatus.ONLY_THEIRS


def _classify_with_base(
    mine: FileRef | None, theirs: FileRef | None, base: FileRef | None
) -> DiffStatus:
    mine_hash = mine.sha256 if mine else None
    theirs_hash = theirs.sha256 if theirs else None
    base_hash = base.sha256 if base else None

    if mine_hash == theirs_hash:
        return DiffStatus.SAME

    mine_changed = mine_hash != base_hash
    theirs_changed = theirs_hash != base_hash

    if base_hash is None:
        # Neither side inherited it, and they disagree, so at least one side added it.
        if mine_hash is None:
            return DiffStatus.ADDED_BY_THEIRS
        if theirs_hash is None:
            return DiffStatus.ADDED_BY_MINE
        return DiffStatus.BOTH_MODIFIED

    if mine_hash is None and theirs_changed:
        return DiffStatus.MODIFY_DELETE
    if theirs_hash is None and mine_changed:
        return DiffStatus.MODIFY_DELETE
    if mine_hash is None:
        return DiffStatus.DELETED_BY_MINE
    if theirs_hash is None:
        return DiffStatus.DELETED_BY_THEIRS
    if mine_changed and theirs_changed:
        return DiffStatus.BOTH_MODIFIED
    return DiffStatus.MODIFIED_BY_MINE if mine_changed else DiffStatus.MODIFIED_BY_THEIRS


@dataclass(frozen=True)
class ReconcilePlan:
    """A concrete, reviewable proposal: for every path, whose version wins.

    The same plan_id must be approved by both sides. Sending the decision set (rather than
    "trust me, I merged it") is what lets the other person see exactly what they are agreeing
    to before it happens.
    """

    plan_id: str
    mine_head: str | None
    theirs_head: str | None
    resolution: Mapping[str, Side]

    def to_json(self) -> dict[str, object]:
        return {
            "plan_id": self.plan_id,
            "mine_head": self.mine_head,
            "theirs_head": self.theirs_head,
            "resolution": {path: side.value for path, side in sorted(self.resolution.items())},
        }

    @staticmethod
    def from_json(data: Mapping[str, object]) -> ReconcilePlan:
        try:
            resolution = {
                str(path): Side(str(value))
                for path, value in dict(data["resolution"]).items()  # type: ignore[arg-type]
            }
        except (KeyError, TypeError, ValueError) as e:
            raise ReconcileError(f"Malformed reconciliation plan: {e}") from e
        return ReconcilePlan(
            plan_id=str(data["plan_id"]),
            mine_head=_optional_str(data.get("mine_head")),
            theirs_head=_optional_str(data.get("theirs_head")),
            resolution=resolution,
        )


def _optional_str(value: object) -> str | None:
    return None if value is None else str(value)


def conflicting_paths(diffs: tuple[FileDiff, ...]) -> tuple[str, ...]:
    """Paths a human must rule on before any plan is valid."""
    return tuple(diff.path for diff in diffs if diff.needs_decision)


def draft_plan(
    diffs: tuple[FileDiff, ...],
    mine_head: str | None,
    theirs_head: str | None,
    choices: Mapping[str, Side] | None = None,
) -> ReconcilePlan:
    """Build a plan with defaults filled in for everything that is not a real conflict.

    Conflicts are left out unless `choices` supplies them, so an incomplete plan fails
    validation instead of silently resolving in favour of whoever proposed it.
    """
    supplied = dict(choices or {})
    resolution: dict[str, Side] = {}
    for diff in diffs:
        if diff.status is DiffStatus.SAME:
            continue
        if diff.path in supplied:
            resolution[diff.path] = supplied[diff.path]
        elif diff.default_side is not None:
            resolution[diff.path] = diff.default_side
    return ReconcilePlan(
        plan_id=f"r-{uuid.uuid4().hex[:8]}",
        mine_head=mine_head,
        theirs_head=theirs_head,
        resolution=resolution,
    )


def validate_plan(plan: ReconcilePlan, diffs: tuple[FileDiff, ...]) -> None:
    """Reject a plan that does not decide exactly the paths in question.

    Raises rather than filling gaps, because a missing decision here means the approving side
    would be agreeing to something the proposing side had not actually shown them.
    """
    decidable = {diff.path for diff in diffs if diff.status is not DiffStatus.SAME}
    decided = set(plan.resolution)

    unknown = decided - decidable
    if unknown:
        raise ReconcileError(
            "Plan decides paths that are not in dispute: " + ", ".join(sorted(unknown))
        )

    missing = decidable - decided
    if missing:
        raise ReconcileError(
            "Plan leaves conflicting paths undecided: " + ", ".join(sorted(missing))
        )


def apply_plan(plan: ReconcilePlan, diffs: tuple[FileDiff, ...]) -> Manifest:
    """Produce the manifest the reconciliation commit will record.

    Pure: it computes the resulting snapshot and touches no files. Materialising it on disk is
    a separate, later step that only runs once both sides have approved.
    """
    validate_plan(plan, diffs)

    entries: dict[str, FileRef] = {}
    for diff in diffs:
        if diff.status is DiffStatus.SAME:
            if diff.mine is not None:
                entries[diff.path] = diff.mine
            continue
        chosen = diff.ref_for(plan.resolution[diff.path])
        if chosen is not None:  # A None ref means the chosen side deleted the file.
            entries[diff.path] = chosen
    return Manifest(entries)


def summarise(diffs: tuple[FileDiff, ...]) -> dict[str, int]:
    """Counts per status, for the summary line above the reconciliation table."""
    counts: dict[str, int] = {}
    for diff in diffs:
        counts[diff.status.value] = counts.get(diff.status.value, 0) + 1
    return counts
