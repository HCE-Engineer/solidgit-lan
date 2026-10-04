from __future__ import annotations

import pytest

from solidgit_lan.core.manifest import WorkspaceError, changed_paths, scan_workspace


def write(root, relative, content=b"x"):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_scan_uses_forward_slash_relative_paths(tmp_path):
    write(tmp_path, "Montaj1/govde.sldprt")
    manifest = scan_workspace(tmp_path)
    assert set(manifest.paths()) == {"Montaj1/govde.sldprt"}


def test_solidworks_clutter_is_ignored(tmp_path):
    write(tmp_path, "Montaj1/govde.sldprt")
    write(tmp_path, "Montaj1/~$govde.sldprt")
    write(tmp_path, "Montaj1/Backup of govde.sldprt")
    write(tmp_path, "Montaj1/govde.sldprt.bak")
    write(tmp_path, ".solidgit/objects/aa/bb")

    assert set(scan_workspace(tmp_path).paths()) == {"Montaj1/govde.sldprt"}


def test_case_only_collision_is_refused(tmp_path):
    """Windows cannot tell these apart, so the manifest would mean different things per machine."""
    write(tmp_path, "Montaj1/Govde.sldprt")
    write(tmp_path, "Montaj1/govde.sldprt")

    # On a case-insensitive filesystem the second write just overwrites the first, in which
    # case there is nothing to detect.
    if len(list((tmp_path / "Montaj1").iterdir())) < 2:
        pytest.skip("filesystem is case-insensitive; collision cannot occur")

    with pytest.raises(WorkspaceError, match="capitalisation"):
        scan_workspace(tmp_path)


def test_changed_paths_splits_added_modified_removed(tmp_path):
    write(tmp_path, "a.sldprt", b"one")
    write(tmp_path, "b.sldprt", b"two")
    before = scan_workspace(tmp_path)

    write(tmp_path, "b.sldprt", b"two-changed")
    write(tmp_path, "c.sldprt", b"three")
    (tmp_path / "a.sldprt").unlink()
    after = scan_workspace(tmp_path)

    added, modified, removed = changed_paths(before, after)
    assert added == frozenset({"c.sldprt"})
    assert modified == frozenset({"b.sldprt"})
    assert removed == frozenset({"a.sldprt"})


def test_stat_cache_avoids_rehashing_unchanged_files(tmp_path):
    """The file list rescans every second; re-reading the whole assembly each time is fatal."""
    import os
    import time as time_module

    from solidgit_lan.core.manifest import StatCache

    write(tmp_path, "a.sldprt", b"one")
    write(tmp_path, "b.sldprt", b"two")
    # Backdate the files past the racy window so the cache is allowed to trust them.
    old = time_module.time() - 60
    for name in ("a.sldprt", "b.sldprt"):
        os.utime(tmp_path / name, (old, old))

    cache = StatCache()
    first = scan_workspace(tmp_path, cache=cache)
    assert cache.misses == 2 and cache.hits == 0

    second = scan_workspace(tmp_path, cache=cache)
    assert second.entries == first.entries
    assert cache.hits == 2 and cache.misses == 2  # nothing re-hashed


def test_stat_cache_notices_a_real_edit(tmp_path):
    import os
    import time as time_module

    from solidgit_lan.core.manifest import StatCache

    write(tmp_path, "a.sldprt", b"one")
    old = time_module.time() - 60
    os.utime(tmp_path / "a.sldprt", (old, old))

    cache = StatCache()
    before = scan_workspace(tmp_path, cache=cache)

    write(tmp_path, "a.sldprt", b"one-changed")
    os.utime(tmp_path / "a.sldprt", (old + 5, old + 5))
    after = scan_workspace(tmp_path, cache=cache)

    assert after.entries["a.sldprt"].sha256 != before.entries["a.sldprt"].sha256


def test_stat_cache_distrusts_a_just_written_file(tmp_path):
    """Timestamps have limited resolution: a same-size rewrite within one tick is invisible."""
    from solidgit_lan.core.manifest import StatCache

    write(tmp_path, "a.sldprt", b"one")
    cache = StatCache()
    scan_workspace(tmp_path, cache=cache)

    write(tmp_path, "a.sldprt", b"two")  # same size, written immediately
    result = scan_workspace(tmp_path, cache=cache)

    from solidgit_lan.core.hashing import sha256_bytes

    assert result.entries["a.sldprt"].sha256 == sha256_bytes(b"two")


def test_stat_cache_forgets_deleted_files(tmp_path):
    from solidgit_lan.core.manifest import StatCache

    write(tmp_path, "a.sldprt", b"one")
    cache = StatCache()
    scan_workspace(tmp_path, cache=cache)

    (tmp_path / "a.sldprt").unlink()
    scan_workspace(tmp_path, cache=cache)
    assert cache.entries == {}


def test_identical_content_shares_one_digest(tmp_path):
    write(tmp_path, "left.sldprt", b"identical")
    write(tmp_path, "right.sldprt", b"identical")
    manifest = scan_workspace(tmp_path)
    assert len(manifest) == 2
    assert len(manifest.digests()) == 1
