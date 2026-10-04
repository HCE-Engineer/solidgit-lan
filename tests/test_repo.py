from __future__ import annotations

import stat

import pytest

from solidgit_lan.core.commits import Author
from solidgit_lan.core.repo import Repository, RepositoryError

AUTHOR = Author(name="Hüseyin", device="HUSEYIN-PC")


def write(root, relative, content=b"x"):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


@pytest.fixture
def repo(tmp_path):
    write(tmp_path, "Montaj1/govde.sldprt", b"govde v1")
    write(tmp_path, "Montaj1/kapak.sldprt", b"kapak v1")
    write(tmp_path, "Montaj1/montaj.sldasm", b"montaj v1")
    return Repository.initialise(tmp_path, name="Montaj1")


def test_initialise_does_not_disturb_existing_files(repo):
    assert (repo.root / "Montaj1/govde.sldprt").read_bytes() == b"govde v1"
    status = repo.status()
    assert len(status.added) == 3
    assert status.head is None


def test_commit_then_clean(repo):
    commit = repo.commit("first drop", AUTHOR)
    assert len(commit.manifest) == 3
    assert repo.status().is_clean


def test_empty_commit_is_refused(repo):
    repo.commit("first", AUTHOR)
    with pytest.raises(RepositoryError, match="Nothing to commit"):
        repo.commit("again", AUTHOR)


def test_unchanged_files_are_not_stored_twice(repo):
    repo.commit("first", AUTHOR)
    blobs_after_first = len(list(repo.objects.iter_digests()))

    write(repo.root, "Montaj1/govde.sldprt", b"govde v2")
    repo.commit("thicker wall", AUTHOR)

    # Only the changed part adds a blob; the other two are already stored under their hash.
    assert len(list(repo.objects.iter_digests())) == blobs_after_first + 1


def test_checkout_restores_an_older_version(repo):
    first = repo.commit("first", AUTHOR)
    write(repo.root, "Montaj1/govde.sldprt", b"govde v2")
    repo.commit("second", AUTHOR)

    repo.checkout(first.id)

    assert (repo.root / "Montaj1/govde.sldprt").read_bytes() == b"govde v1"
    assert repo.commits.head() == first.id


def test_checkout_keeps_untracked_files_unless_asked(repo):
    first = repo.commit("first", AUTHOR)
    write(repo.root, "Montaj1/yeni.sldprt", b"new part")
    repo.commit("added a part", AUTHOR)

    repo.checkout(first.id)
    assert (repo.root / "Montaj1/yeni.sldprt").exists()

    repo.checkout(first.id, remove_extra=True)
    assert not (repo.root / "Montaj1/yeni.sldprt").exists()


def test_readonly_enforcement_matches_held_locks(repo):
    repo.commit("first", AUTHOR)

    repo.apply_readonly(writable_paths={"Montaj1/govde.sldprt"})

    def is_writable(relative):
        return bool((repo.root / relative).stat().st_mode & stat.S_IWRITE)

    assert is_writable("Montaj1/govde.sldprt")
    assert not is_writable("Montaj1/kapak.sldprt")
    assert not is_writable("Montaj1/montaj.sldasm")

    repo.make_all_writable()
    assert is_writable("Montaj1/kapak.sldprt")


def test_commit_can_write_over_readonly_workspace_files(repo):
    """Enforcement must not lock the app out of its own checkout path."""
    first = repo.commit("first", AUTHOR)
    write(repo.root, "Montaj1/govde.sldprt", b"govde v2")
    repo.commit("second", AUTHOR)

    repo.apply_readonly(writable_paths=set())
    repo.checkout(first.id)

    assert (repo.root / "Montaj1/govde.sldprt").read_bytes() == b"govde v1"


def test_latest_hashes_feed_the_lock_staleness_check(repo):
    commit = repo.commit("first", AUTHOR)
    latest = repo.latest_hashes()
    assert latest["Montaj1/govde.sldprt"] == commit.manifest.get("Montaj1/govde.sldprt").sha256
    assert repo.local_hashes() == latest


def test_garbage_collect_only_targets_unreachable_blobs(repo):
    repo.commit("first", AUTHOR)
    orphan = repo.objects.put_bytes(b"never referenced").digest

    assert repo.garbage_collect(dry_run=True) == (orphan,)
    repo.garbage_collect(dry_run=False)

    assert not repo.objects.has(orphan)
    assert repo.status().is_clean


def test_reopening_a_workspace_sees_the_same_history(repo):
    commit = repo.commit("first", AUTHOR)
    reopened = Repository(repo.root)
    assert reopened.commits.head() == commit.id
    assert reopened.info.repo_id == repo.info.repo_id
