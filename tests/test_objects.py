from __future__ import annotations

import stat

import pytest

from solidgit_lan.core.hashing import sha256_bytes
from solidgit_lan.core.objects import ObjectStore, ObjectStoreError


def test_roundtrip_bytes(tmp_path):
    store = ObjectStore(tmp_path / ".solidgit")
    stored = store.put_bytes(b"hello assembly")
    assert stored.digest == sha256_bytes(b"hello assembly")
    assert store.get_bytes(stored.digest) == b"hello assembly"


def test_roundtrip_large_file_via_streaming(tmp_path):
    # Larger than one read chunk, so put_file and extract_to exercise their streaming paths.
    source = tmp_path / "part.sldprt"
    payload = bytes(range(256)) * 12000
    source.write_bytes(payload)

    store = ObjectStore(tmp_path / ".solidgit")
    stored = store.put_file(source)
    assert stored.size == len(payload)

    destination = tmp_path / "out" / "part.sldprt"
    store.extract_to(stored.digest, destination)
    assert destination.read_bytes() == payload
    assert store.get_bytes(stored.digest) == payload


def test_identical_content_is_stored_once(tmp_path):
    store = ObjectStore(tmp_path / ".solidgit")
    first = store.put_bytes(b"same content")
    second = store.put_bytes(b"same content")
    assert first.digest == second.digest
    assert len(list(store.iter_digests())) == 1


def test_corrupt_blob_is_detected_not_returned(tmp_path):
    store = ObjectStore(tmp_path / ".solidgit")
    stored = store.put_bytes(b"original")

    blob = store.path_for(stored.digest)
    data = bytearray(blob.read_bytes())
    data[-1] ^= 0xFF
    blob.write_bytes(bytes(data))

    with pytest.raises(ObjectStoreError):
        store.get_bytes(stored.digest)


def test_missing_of_reports_only_absent_digests(tmp_path):
    store = ObjectStore(tmp_path / ".solidgit")
    present = store.put_bytes(b"present").digest
    absent = sha256_bytes(b"absent")
    assert store.missing_of([present, absent]) == [absent]


def test_extract_over_readonly_file_succeeds(tmp_path):
    """Unlocked files are kept read-only, so updating one must clear the bit first."""
    store = ObjectStore(tmp_path / ".solidgit")
    stored = store.put_bytes(b"new version")

    destination = tmp_path / "govde.sldprt"
    destination.write_bytes(b"old version")
    destination.chmod(destination.stat().st_mode & ~stat.S_IWRITE)

    store.extract_to(stored.digest, destination)
    assert destination.read_bytes() == b"new version"


def test_failed_extract_leaves_previous_version_intact(tmp_path):
    store = ObjectStore(tmp_path / ".solidgit")
    stored = store.put_bytes(b"replacement")

    # Corrupt the blob so verification fails partway through the extract.
    blob = store.path_for(stored.digest)
    data = bytearray(blob.read_bytes())
    data[-1] ^= 0xFF
    blob.write_bytes(bytes(data))

    destination = tmp_path / "govde.sldprt"
    destination.write_bytes(b"still here")

    with pytest.raises(ObjectStoreError):
        store.extract_to(stored.digest, destination)

    assert destination.read_bytes() == b"still here"
    assert list(store.tmp_dir.glob("*.part")) == []


def test_invalid_digest_is_rejected(tmp_path):
    store = ObjectStore(tmp_path / ".solidgit")
    with pytest.raises(ObjectStoreError):
        store.path_for("../../etc/passwd")
