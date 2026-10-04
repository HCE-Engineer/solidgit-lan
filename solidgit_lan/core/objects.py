"""Content-addressed blob store.

A blob's name is the sha256 of its uncompressed content. That buys three things for free:
file-level deduplication across the whole history (an unchanged part is stored once, no matter
how many commits mention it), integrity checking on every read, and a trivial sync protocol
("here are my digests, which ones are you missing?").

Every write lands in `tmp/` first and is then renamed into place, so a crash or a yanked
network cable can never leave a half-written blob that later reads as valid.
"""

from __future__ import annotations

import hashlib
import io
import os
import shutil
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Iterator

from .hashing import READ_CHUNK, is_valid_digest, sha256_bytes, sha256_file

try:
    import zstandard
except ImportError:  # pragma: no cover - exercised only on installs without the extra
    zstandard = None  # type: ignore[assignment]

#: Stored-object header: magic + codec byte. The digest names the *uncompressed* content, so
#: the codec has to be recorded alongside the payload rather than inferred from the name.
_MAGIC = b"SG1"
_CODEC_RAW = 0
_CODEC_ZSTD = 1
_HEADER_SIZE = len(_MAGIC) + 1

#: CAD files compress well and the cost is dwarfed by Wi-Fi transfer time, but level 3 is
#: still fast enough not to bottleneck a local commit.
_ZSTD_LEVEL = 3


class ObjectStoreError(Exception):
    """Raised when a blob is missing, malformed, or fails its integrity check."""


@dataclass(frozen=True)
class StoredObject:
    digest: str
    size: int
    """Size of the original, uncompressed content."""


def clear_readonly(path: str | Path) -> None:
    """Drop the read-only bit so the file can be replaced.

    Files that nobody holds a lock on are deliberately kept read-only on disk, which is what
    makes SolidWorks open them read-only. `os.replace` refuses to overwrite such a file on
    Windows, so every write path has to clear the bit first.
    """
    target = Path(path)
    if not target.exists():
        return
    mode = target.stat().st_mode
    if not mode & stat.S_IWRITE:
        target.chmod(mode | stat.S_IWRITE)


class ObjectStore:
    """Blob storage under `<repo_dir>/objects`, with scratch space in `<repo_dir>/tmp`."""

    def __init__(self, repo_dir: str | Path) -> None:
        self.repo_dir = Path(repo_dir)
        self.objects_dir = self.repo_dir / "objects"
        self.tmp_dir = self.repo_dir / "tmp"
        self.objects_dir.mkdir(parents=True, exist_ok=True)
        self.tmp_dir.mkdir(parents=True, exist_ok=True)

    # -- layout ---------------------------------------------------------------------------

    def path_for(self, digest: str) -> Path:
        """Fan out over 256 subdirectories; a flat directory of 50k blobs is slow on NTFS."""
        if not is_valid_digest(digest):
            raise ObjectStoreError(f"Not a valid sha256 digest: {digest!r}")
        return self.objects_dir / digest[:2] / digest[2:]

    def has(self, digest: str) -> bool:
        return self.path_for(digest).exists()

    def iter_digests(self) -> Iterator[str]:
        for prefix_dir in sorted(self.objects_dir.iterdir()):
            if not prefix_dir.is_dir() or len(prefix_dir.name) != 2:
                continue
            for blob in sorted(prefix_dir.iterdir()):
                digest = prefix_dir.name + blob.name
                if is_valid_digest(digest):
                    yield digest

    def missing_of(self, digests: list[str]) -> list[str]:
        """Which of these do we not have? This is the whole sync negotiation."""
        return [d for d in digests if not self.has(d)]

    # -- writing --------------------------------------------------------------------------

    def _new_tmp_path(self) -> Path:
        return self.tmp_dir / f"{uuid.uuid4().hex}.part"

    def _commit_tmp(self, tmp_path: Path, digest: str) -> None:
        """Move a fully written temp file into place, or discard it if we already have it."""
        final_path = self.path_for(digest)
        if final_path.exists():
            tmp_path.unlink(missing_ok=True)
            return
        final_path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(tmp_path, final_path)

    def put_bytes(self, data: bytes) -> StoredObject:
        digest = sha256_bytes(data)
        if self.has(digest):
            return StoredObject(digest=digest, size=len(data))

        tmp_path = self._new_tmp_path()
        try:
            with open(tmp_path, "wb") as out:
                _write_header(out)
                if zstandard is None:
                    out.write(data)
                else:
                    out.write(zstandard.ZstdCompressor(level=_ZSTD_LEVEL).compress(data))
                out.flush()
                os.fsync(out.fileno())
            self._commit_tmp(tmp_path, digest)
        finally:
            tmp_path.unlink(missing_ok=True)
        return StoredObject(digest=digest, size=len(data))

    def put_file(self, source: str | Path) -> StoredObject:
        """Ingest a workspace file in a single read pass, hashing and compressing together."""
        source_path = Path(source)
        tmp_path = self._new_tmp_path()
        try:
            digest_state = hashlib.sha256()
            total = 0
            with open(source_path, "rb") as src, open(tmp_path, "wb") as out:
                _write_header(out)
                # closefd=False: closing the compressor must flush its final frame without
                # closing `out` underneath us — we still have to fsync it.
                sink = (
                    out
                    if zstandard is None
                    else zstandard.ZstdCompressor(level=_ZSTD_LEVEL).stream_writer(
                        out, closefd=False
                    )
                )
                try:
                    while chunk := src.read(READ_CHUNK):
                        digest_state.update(chunk)
                        total += len(chunk)
                        sink.write(chunk)
                finally:
                    if sink is not out:
                        sink.close()
                out.flush()
                os.fsync(out.fileno())

            digest = digest_state.hexdigest()
            self._commit_tmp(tmp_path, digest)
        finally:
            tmp_path.unlink(missing_ok=True)
        return StoredObject(digest=digest, size=total)

    # -- reading --------------------------------------------------------------------------

    def get_bytes(self, digest: str) -> bytes:
        blob_path = self.path_for(digest)
        if not blob_path.exists():
            raise ObjectStoreError(f"Object {digest} is not in the store.")

        with open(blob_path, "rb") as handle:
            codec = _read_header(handle, digest)
            payload = handle.read()

        data = payload if codec == _CODEC_RAW else _decompress(payload, digest)
        actual = sha256_bytes(data)
        if actual != digest:
            raise ObjectStoreError(
                f"Object {digest} is corrupt: content hashes to {actual}."
            )
        return data

    def extract_to(self, digest: str, destination: str | Path) -> None:
        """Materialise a blob as a workspace file, atomically and verified.

        The destination is replaced only once the full content is on disk and its hash checks
        out, so an interrupted update leaves the previous version intact rather than a
        truncated file that SolidWorks would refuse to open.
        """
        blob_path = self.path_for(digest)
        if not blob_path.exists():
            raise ObjectStoreError(f"Object {digest} is not in the store.")

        dest_path = Path(destination)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._new_tmp_path()
        try:
            with open(blob_path, "rb") as src, open(tmp_path, "wb") as out:
                codec = _read_header(src, digest)
                if codec == _CODEC_RAW:
                    shutil.copyfileobj(src, out, READ_CHUNK)
                else:
                    _stream_decompress(src, out)
                out.flush()
                os.fsync(out.fileno())

            actual, _ = sha256_file(tmp_path)
            if actual != digest:
                raise ObjectStoreError(
                    f"Object {digest} is corrupt: extracted content hashes to {actual}."
                )

            clear_readonly(dest_path)
            os.replace(tmp_path, dest_path)
        finally:
            tmp_path.unlink(missing_ok=True)

    # -- transfer -------------------------------------------------------------------------

    def stored_size(self, digest: str) -> int:
        """Size of the blob *as stored* — what a transfer actually moves over the wire."""
        return self.path_for(digest).stat().st_size

    def partial_path(self, digest: str) -> Path:
        """Where a half-received blob waits.

        Named after the digest rather than randomly, so an interrupted download can be
        resumed on the next attempt instead of starting the whole file again.
        """
        return self.tmp_dir / f"{digest}.part"

    def adopt_partial(self, digest: str) -> None:
        """Verify a fully downloaded blob and move it into the store.

        The bytes arrive in their stored form, compression and all, so they are checked by
        decompressing and hashing rather than trusting the sender. A peer that sends
        something wrong — or a transfer that silently truncated — is caught here, before the
        content can ever be written into someone's workspace.
        """
        partial = self.partial_path(digest)
        if not partial.exists():
            raise ObjectStoreError(f"No partial download for {digest}.")

        try:
            with open(partial, "rb") as handle:
                codec = _read_header(handle, digest)
                payload = handle.read()
            data = payload if codec == _CODEC_RAW else _decompress(payload, digest)
            actual = sha256_bytes(data)
        except ObjectStoreError:
            partial.unlink(missing_ok=True)
            raise
        except OSError as e:
            raise ObjectStoreError(f"Could not read the download of {digest}: {e}") from e

        if actual != digest:
            partial.unlink(missing_ok=True)
            raise ObjectStoreError(
                f"Downloaded object does not match its name: expected {digest}, got {actual}."
            )
        self._commit_tmp(partial, digest)

    def partial_size(self, digest: str) -> int:
        """How much of an interrupted download we already have. Zero if none."""
        partial = self.partial_path(digest)
        return partial.stat().st_size if partial.exists() else 0

    def discard_partial(self, digest: str) -> None:
        self.partial_path(digest).unlink(missing_ok=True)

    # -- maintenance ----------------------------------------------------------------------

    def delete(self, digest: str) -> bool:
        """Remove one blob. Only ever called by garbage collection against unreachable ones."""
        blob_path = self.path_for(digest)
        if not blob_path.exists():
            return False
        blob_path.unlink()
        return True

    def clear_tmp(self) -> int:
        """Drop leftover scratch files from an interrupted run. Returns how many were removed."""
        removed = 0
        for leftover in self.tmp_dir.glob("*.part"):
            leftover.unlink(missing_ok=True)
            removed += 1
        return removed

    def disk_usage(self) -> int:
        return sum(self.path_for(d).stat().st_size for d in self.iter_digests())


# -- header / codec helpers ----------------------------------------------------------------


def _write_header(out) -> None:
    out.write(_MAGIC)
    out.write(bytes([_CODEC_RAW if zstandard is None else _CODEC_ZSTD]))


def _read_header(handle, digest: str) -> int:
    header = handle.read(_HEADER_SIZE)
    if len(header) < _HEADER_SIZE or header[: len(_MAGIC)] != _MAGIC:
        raise ObjectStoreError(f"Object {digest} has a malformed header.")
    codec = header[len(_MAGIC)]
    if codec not in (_CODEC_RAW, _CODEC_ZSTD):
        raise ObjectStoreError(f"Object {digest} uses unknown codec {codec}.")
    if codec == _CODEC_ZSTD and zstandard is None:
        raise ObjectStoreError(
            f"Object {digest} is zstd-compressed but the 'zstandard' package is not installed."
        )
    return codec


def _decompress(payload: bytes, digest: str) -> bytes:
    # Streaming rather than one-shot `decompress()`: blobs written by `put_file` use a
    # streamed frame, which carries no declared content size for the one-shot API to use.
    try:
        with io.BytesIO(payload) as source:
            return zstandard.ZstdDecompressor().stream_reader(source).read()
    except zstandard.ZstdError as e:
        raise ObjectStoreError(f"Object {digest} could not be decompressed: {e}") from e


def _stream_decompress(src, out) -> None:
    reader = zstandard.ZstdDecompressor().stream_reader(src)
    shutil.copyfileobj(reader, out, READ_CHUNK)
