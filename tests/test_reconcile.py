from __future__ import annotations

import pytest

from solidgit_lan.core.manifest import FileRef, Manifest
from solidgit_lan.core.reconcile import (
    DiffStatus,
    ReconcileError,
    Side,
    apply_plan,
    conflicting_paths,
    diff_manifests,
    draft_plan,
    validate_plan,
)


def manifest(**files: str) -> Manifest:
    return Manifest({path: FileRef(sha256=d, size=len(d)) for path, d in files.items()})


def status_of(diffs, path):
    return next(d.status for d in diffs if d.path == path)


def test_without_a_base_a_changed_file_is_a_conflict():
    diffs = diff_manifests(manifest(a="mine"), manifest(a="theirs"))
    assert status_of(diffs, "a") is DiffStatus.DIFFERENT
    assert conflicting_paths(diffs) == ("a",)


def test_base_distinguishes_one_sided_edits_from_real_conflicts():
    base = manifest(a="v1", b="v1", c="v1")
    mine = manifest(a="v2", b="v1", c="v2")
    theirs = manifest(a="v1", b="v2", c="v3")

    diffs = diff_manifests(mine, theirs, base)

    assert status_of(diffs, "a") is DiffStatus.MODIFIED_BY_MINE
    assert status_of(diffs, "b") is DiffStatus.MODIFIED_BY_THEIRS
    assert status_of(diffs, "c") is DiffStatus.BOTH_MODIFIED
    assert conflicting_paths(diffs) == ("c",)


def test_base_tells_a_deletion_apart_from_an_addition():
    """Without the base these look identical, and they call for opposite actions."""
    base = manifest(kept="v1", deleted="v1")
    mine = manifest(kept="v1", deleted="v1", added="new")
    theirs = manifest(kept="v1")

    diffs = diff_manifests(mine, theirs, base)

    assert status_of(diffs, "added") is DiffStatus.ADDED_BY_MINE
    assert status_of(diffs, "deleted") is DiffStatus.DELETED_BY_THEIRS


def test_edit_versus_delete_always_needs_a_person():
    base = manifest(part="v1")
    mine = manifest(part="v2")
    theirs = manifest()

    diffs = diff_manifests(mine, theirs, base)
    assert status_of(diffs, "part") is DiffStatus.MODIFY_DELETE
    assert conflicting_paths(diffs) == ("part",)


def test_draft_plan_leaves_conflicts_undecided():
    """A conflict with a quiet default is a conflict resolved by whoever proposed the plan."""
    diffs = diff_manifests(manifest(a="mine", b="same"), manifest(a="theirs", b="same"))
    plan = draft_plan(diffs, mine_head="m", theirs_head="t")

    assert "a" not in plan.resolution
    with pytest.raises(ReconcileError, match="undecided"):
        validate_plan(plan, diffs)


def test_plan_with_explicit_choices_validates_and_applies():
    diffs = diff_manifests(manifest(a="mine", b="only-mine"), manifest(a="theirs", c="only-theirs"))
    plan = draft_plan(diffs, "m", "t", choices={"a": Side.THEIRS})

    validate_plan(plan, diffs)
    result = apply_plan(plan, diffs)

    assert result.get("a").sha256 == "theirs"
    # Nothing is lost by default: files unique to either side are kept.
    assert result.get("b").sha256 == "only-mine"
    assert result.get("c").sha256 == "only-theirs"


def test_choosing_the_deleting_side_drops_the_file():
    base = manifest(part="v1")
    diffs = diff_manifests(manifest(part="v2"), manifest(), base)
    plan = draft_plan(diffs, "m", "t", choices={"part": Side.THEIRS})

    assert apply_plan(plan, diffs).get("part") is None


def test_plan_deciding_an_undisputed_path_is_rejected():
    """Plans arrive over the network, so validation cannot assume a well-behaved proposer."""
    from solidgit_lan.core.reconcile import ReconcilePlan

    diffs = diff_manifests(manifest(a="same"), manifest(a="same"))
    plan = ReconcilePlan(plan_id="r-forged", mine_head="m", theirs_head="t",
                         resolution={"a": Side.MINE})

    with pytest.raises(ReconcileError, match="not in dispute"):
        validate_plan(plan, diffs)


def test_plan_survives_a_round_trip_through_json():
    from solidgit_lan.core.reconcile import ReconcilePlan

    diffs = diff_manifests(manifest(a="mine"), manifest(a="theirs"))
    plan = draft_plan(diffs, "m", "t", choices={"a": Side.THEIRS})

    restored = ReconcilePlan.from_json(plan.to_json())

    assert restored.plan_id == plan.plan_id
    assert restored.resolution == plan.resolution
