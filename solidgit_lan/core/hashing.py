"""Content hashing.

Every blob in the object store is named after the sha256 of its *uncompressed* content, so a
file that never changes is stored once no matter how many commits reference it, and any
corruption is caught the moment the blob is read back.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import BinaryIO

READ_CHUNK = 1024 * 1024


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_stream(stream: BinaryIO) -> tuple[str, int]:
    """Hash a stream to EOF. Returns (hex digest, byte count)."""
    digest = hashlib.sha256()
    total = 0
    while chunk := stream.read(READ_CHUNK):
        digest.update(chunk)
        total += len(chunk)
    return digest.hexdigest(), total


def sha256_file(path: str | Path) -> tuple[str, int]:
    """Hash a file's contents. Returns (hex digest, size in bytes)."""
    with open(path, "rb") as handle:
        return sha256_stream(handle)


def is_valid_digest(value: str) -> bool:
    """True for a lowercase 64-char hex string.

    Digests arrive from the network and end up as filesystem paths, so they are validated
    before use rather than trusted.
    """
    if len(value) != 64:
        return False
    return all(c in "0123456789abcdef" for c in value)
