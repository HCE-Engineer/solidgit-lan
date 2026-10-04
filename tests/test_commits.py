from __future__ import annotations

import pytest

from solidgit_lan.core.commits import (
    Author,
    Commit,
    CommitError,
    CommitStore,
    Relation,
    build_commit,
    compare_heads,
)
from solidgit_lan.core.manifest import FileRef, Manifest

AUTHOR = Author(name="Hüseyin", device="HUSEYIN-PC")


def manifest(**files: str) -> Manifest:
    return Manifest({path: FileRef(sha256=digest, size=len(digest)) for path, digest in files.items()})


def make(store: CommitStore, message: str, parents=(), **files: str) -> Commit:
    commit = build_commit(
        parents=parents,
        lamport=store.next_lamport(),
        author=AUTHOR,
        wall_clock="2026-08-08T20:00:00+03:00",
        message=message,
        manifest=manifest(**files),
    )
    store.write(commit)
    store.set_head(commit.id)
    return commit


def test_commit_id_is_derived_from_content(tmp_path):
    """Sequence numbers would collide across peers working offline; content hashes cannot."""
    first = build_commit((), 1, AUTHOR, "t", "same", manifest(a="aa" * 32))
    second = build_commit((), 1, AUTHOR, "t", "same", manifest(a="aa" * 32))
    different = build_commit((), 1, AUTHOR, "t", "different", manifest(a="aa" * 32))

    assert first.id == second.id
    assert first.id != different.id


def test_tampered_commit_is_rejected_on_read(tmp_path):
    store = CommitStore(tmp_path)
    commit = make(store, "original", a="aa" * 32)

    path = store.path_for(commit.id)
    path.write_text(path.read_text(encoding="utf-8").replace("original", "tampered"), encoding="utf-8")

    with pytest.raises(CommitError, match="does not match its own content"):
        store.read(commit.id)


def test_head_must_point_at_a_known_commit(tmp_path):
    store = CommitStore(tmp_path)
    with pytest.raises(CommitError):
        store.set_head("aa" * 32)


def test_history_is_newest_first(tmp_path):
    store = CommitStore(tmp_path)
    first = make(store, "first", a="aa" * 32)
    second = make(store, "second", parents=(first.id,), a="bb" * 32)

    assert [c.id for c in store.history()] == [second.id, first.id]


def test_compare_heads_detects_fast_forward_direction():
    assert compare_heads("x", frozenset({"x", "y"}), "y", frozenset({"y"})) is Relation.AHEAD
    assert compare_heads("y", frozenset({"y"}), "x", frozenset({"x", "y"})) is Relation.BEHIND
    assert compare_heads("x", frozenset({"x"}), "x", frozenset({"x"})) is Relation.EQUAL


def test_compare_heads_detects_divergence():
    mine = frozenset({"base", "mine"})
    theirs = frozenset({"base", "theirs"})
    assert compare_heads("mine", mine, "theirs", theirs) is Relation.DIVERGED


def test_unrelated_histories_are_not_reported_as_diverged():
    """Two different projects must be refused outright, not offered for reconciliation."""
    assert (
        compare_heads("a", frozenset({"a"}), "b", frozenset({"b"})) is Relation.UNRELATED
    )


def test_empty_repository_is_behind_a_populated_one():
    assert compare_heads(None, frozenset(), "x", frozenset({"x"})) is Relation.BEHIND
    assert compare_heads("x", frozenset({"x"}), None, frozenset()) is Relation.AHEAD


def test_merge_base_finds_the_shared_ancestor(tmp_path):
    store = CommitStore(tmp_path)
    base = make(store, "base", a="aa" * 32)
    mine = make(store, "mine", parents=(base.id,), a="bb" * 32)
    theirs = make(store, "theirs", parents=(base.id,), a="cc" * 32)

    assert store.merge_base(mine.id, theirs.id) == base.id


def test_merge_base_is_none_for_unrelated_histories(tmp_path):
    store = CommitStore(tmp_path)
    mine = make(store, "mine", a="aa" * 32)
    theirs = make(store, "theirs", a="bb" * 32)
    assert store.merge_base(mine.id, theirs.id) is None


def test_reconciliation_commit_is_flagged(tmp_path):
    store = CommitStore(tmp_path)
    base = make(store, "base", a="aa" * 32)
    mine = make(store, "mine", parents=(base.id,), a="bb" * 32)
    theirs = make(store, "theirs", parents=(base.id,), a="cc" * 32)
    merged = make(store, "reconciled", parents=(mine.id, theirs.id), a="cc" * 32)

    assert merged.is_reconciliation
    assert not mine.is_reconciliation
