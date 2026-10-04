"""Wire types and constants — the contract between two app versions.

Written before the code that uses it, and versioned, because a teammate will eventually be
running an older build. Seeing a version it does not understand must make the app say
"update", not behave subtly wrong. See docs/02-PROTOKOL.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from collections.abc import Mapping

from .. import PROTOCOL_VERSION

#: mDNS service type.
SERVICE_TYPE = "_solidgit._tcp.local."

#: UDP broadcast fallback. Some hotspot drivers smother multicast, so both run at once.
DISCOVERY_PORT = 7800
DEFAULT_HTTPS_PORT = 7801

#: How often a node announces itself, and how long silence is tolerated before it is
#: considered gone. Three missed beacons is deliberate slack for a busy Wi-Fi radio.
BEACON_INTERVAL_SECONDS = 2.0
PEER_TIMEOUT_SECONDS = 7.0

HEADER_PROTOCOL = "X-SG-Protocol"
HEADER_REPO = "X-SG-Repo"


class ErrorCode(str, Enum):
    PROTOCOL_MISMATCH = "PROTOCOL_MISMATCH"
    REPO_MISMATCH = "REPO_MISMATCH"
    NOT_APPROVED = "NOT_APPROVED"
    REJECTED = "REJECTED"
    BAD_JOIN_CODE = "BAD_JOIN_CODE"
    UNAUTHORIZED = "UNAUTHORIZED"
    LOCK_HELD = "LOCK_HELD"
    LOCK_STALE = "LOCK_STALE"
    LOCK_RESERVED = "LOCK_RESERVED"
    LOCK_NOT_OWNED = "LOCK_NOT_OWNED"
    DIVERGED = "DIVERGED"
    OBJECT_MISSING = "OBJECT_MISSING"
    NOT_COORDINATOR = "NOT_COORDINATOR"


class ProtocolError(Exception):
    """A peer answered, but not in a way we can work with."""

    def __init__(self, code: ErrorCode, message: str, detail: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = dict(detail or {})

    def to_json(self) -> dict[str, Any]:
        return {"error": self.code.value, "message": self.message, "detail": self.detail}


class Role(str, Enum):
    HOST = "host"
    PEER = "peer"


class Permission(str, Enum):
    ADMIN = "admin"
    CONTRIBUTOR = "contributor"
    VIEWER = "viewer"


def check_protocol(their_version: int) -> None:
    """Refuse to talk across a version gap instead of guessing what they meant."""
    if their_version != PROTOCOL_VERSION:
        raise ProtocolError(
            ErrorCode.PROTOCOL_MISMATCH,
            f"That machine speaks protocol {their_version}; this build speaks "
            f"{PROTOCOL_VERSION}. One of you needs to update.",
            {"server_protocol": PROTOCOL_VERSION, "their_protocol": their_version},
        )


@dataclass(frozen=True)
class Announcement:
    """What a node broadcasts about itself, over both mDNS TXT records and UDP.

    Deliberately small and free of secrets: anyone on the Wi-Fi can read it. It carries just
    enough for the UI to show a peer list and for a client to know where to knock.
    """

    repo_id: str
    repo_name: str
    device_id: str
    device_name: str
    user_name: str
    role: Role
    port: int
    protocol: int = PROTOCOL_VERSION
    head: str | None = None
    fingerprint: str = ""
    """First bytes of the TLS certificate fingerprint, for a quick visual sanity check."""

    def to_json(self) -> dict[str, Any]:
        return {
            "proto": self.protocol,
            "repo": self.repo_id,
            "name": self.repo_name,
            "dev_id": self.device_id,
            "dev": self.device_name,
            "user": self.user_name,
            "role": self.role.value,
            "port": self.port,
            "head": self.head,
            "fp": self.fingerprint,
        }

    @staticmethod
    def from_json(data: Mapping[str, Any]) -> Announcement:
        try:
            return Announcement(
                protocol=int(data["proto"]),
                repo_id=str(data["repo"]),
                repo_name=str(data["name"]),
                device_id=str(data["dev_id"]),
                device_name=str(data["dev"]),
                user_name=str(data["user"]),
                role=Role(str(data["role"])),
                port=int(data["port"]),
                head=None if data.get("head") is None else str(data["head"]),
                fingerprint=str(data.get("fp", "")),
            )
        except (KeyError, TypeError, ValueError) as e:
            raise ProtocolError(
                ErrorCode.PROTOCOL_MISMATCH, f"Unreadable announcement: {e}"
            ) from e


@dataclass(frozen=True)
class Peer:
    """A node we have heard from, plus when we last heard it."""

    announcement: Announcement
    address: str
    last_seen: float

    @property
    def device_id(self) -> str:
        return self.announcement.device_id

    @property
    def url(self) -> str:
        return f"https://{self.address}:{self.announcement.port}"

    def is_stale(self, now: float, timeout: float = PEER_TIMEOUT_SECONDS) -> bool:
        return now - self.last_seen > timeout


@dataclass(frozen=True)
class InfoResponse:
    """Answer to the unauthenticated `GET /v1/info` knock."""

    protocol: int
    repo_id: str
    repo_name: str
    device_id: str
    device_name: str
    user_name: str
    role: Role
    head: str | None
    lamport: int
    peer_count: int
    app_version: str = ""
    """Build version. The protocol number gates compatibility; this is for telling a person
    which of the two machines needs updating."""

    def to_json(self) -> dict[str, Any]:
        return {
            "app_version": self.app_version,
            "protocol": self.protocol,
            "repo_id": self.repo_id,
            "repo_name": self.repo_name,
            "device_id": self.device_id,
            "device_name": self.device_name,
            "user_name": self.user_name,
            "role": self.role.value,
            "head": self.head,
            "lamport": self.lamport,
            "peer_count": self.peer_count,
        }

    @staticmethod
    def from_json(data: Mapping[str, Any]) -> InfoResponse:
        try:
            return InfoResponse(
                protocol=int(data["protocol"]),
                repo_id=str(data["repo_id"]),
                repo_name=str(data["repo_name"]),
                device_id=str(data["device_id"]),
                device_name=str(data["device_name"]),
                user_name=str(data["user_name"]),
                role=Role(str(data["role"])),
                head=None if data.get("head") is None else str(data["head"]),
                lamport=int(data.get("lamport", 0)),
                peer_count=int(data.get("peer_count", 0)),
                app_version=str(data.get("app_version", "")),
            )
        except (KeyError, TypeError, ValueError) as e:
            raise ProtocolError(ErrorCode.PROTOCOL_MISMATCH, f"Unreadable info response: {e}") from e


@dataclass(frozen=True)
class JoinRequest:
    """Step one of the handshake: who I am, and a nonce for the host to sign."""

    protocol: int
    repo_id: str
    device_id: str
    device_name: str
    user_name: str
    client_nonce: str

    def to_json(self) -> dict[str, Any]:
        return {
            "protocol": self.protocol,
            "repo_id": self.repo_id,
            "device_id": self.device_id,
            "device_name": self.device_name,
            "user_name": self.user_name,
            "client_nonce": self.client_nonce,
        }

    @staticmethod
    def from_json(data: Mapping[str, Any]) -> JoinRequest:
        try:
            return JoinRequest(
                protocol=int(data["protocol"]),
                repo_id=str(data["repo_id"]),
                device_id=str(data["device_id"]),
                device_name=str(data["device_name"]),
                user_name=str(data["user_name"]),
                client_nonce=str(data["client_nonce"]),
            )
        except (KeyError, TypeError, ValueError) as e:
            raise ProtocolError(ErrorCode.BAD_JOIN_CODE, f"Unreadable join request: {e}") from e


@dataclass(frozen=True)
class JoinChallenge:
    """Step two: the host proves it knows the join code before we prove anything to it.

    The host answers first on purpose. A fake host that lured us onto the wrong machine
    cannot produce this, so we find out before sending it anything of our own.
    """

    server_nonce: str
    server_proof: str
    fingerprint: str

    def to_json(self) -> dict[str, Any]:
        return {
            "server_nonce": self.server_nonce,
            "server_proof": self.server_proof,
            "fingerprint": self.fingerprint,
        }

    @staticmethod
    def from_json(data: Mapping[str, Any]) -> JoinChallenge:
        try:
            return JoinChallenge(
                server_nonce=str(data["server_nonce"]),
                server_proof=str(data["server_proof"]),
                fingerprint=str(data["fingerprint"]),
            )
        except (KeyError, TypeError) as e:
            raise ProtocolError(ErrorCode.BAD_JOIN_CODE, f"Unreadable challenge: {e}") from e


@dataclass(frozen=True)
class JoinCompletion:
    """Step three: the client's answering proof, which the host checks before approving."""

    device_id: str
    client_proof: str

    def to_json(self) -> dict[str, Any]:
        return {"device_id": self.device_id, "client_proof": self.client_proof}

    @staticmethod
    def from_json(data: Mapping[str, Any]) -> JoinCompletion:
        try:
            return JoinCompletion(
                device_id=str(data["device_id"]), client_proof=str(data["client_proof"])
            )
        except (KeyError, TypeError) as e:
            raise ProtocolError(ErrorCode.BAD_JOIN_CODE, f"Unreadable completion: {e}") from e


class JoinStatus(str, Enum):
    PENDING = "pending"
    """Cryptography checked out; now a human on the host has to say yes."""
    APPROVED = "approved"
    REJECTED = "rejected"


@dataclass(frozen=True)
class JoinResult:
    status: JoinStatus
    device_token: str = ""
    permission: Permission = Permission.CONTRIBUTOR
    message: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "device_token": self.device_token,
            "permission": self.permission.value,
            "message": self.message,
        }

    @staticmethod
    def from_json(data: Mapping[str, Any]) -> JoinResult:
        return JoinResult(
            status=JoinStatus(str(data["status"])),
            device_token=str(data.get("device_token", "")),
            permission=Permission(str(data.get("permission", Permission.CONTRIBUTOR.value))),
            message=str(data.get("message", "")),
        )


@dataclass
class KnownDevice:
    """A device the host has met before, as persisted in peers.json."""

    device_id: str
    device_name: str
    user_name: str
    permission: Permission = Permission.CONTRIBUTOR
    token: str = ""
    always_allow: bool = False
    first_seen: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "device_name": self.device_name,
            "user_name": self.user_name,
            "permission": self.permission.value,
            "token": self.token,
            "always_allow": self.always_allow,
            "first_seen": self.first_seen,
            "metadata": self.metadata,
        }

    @staticmethod
    def from_json(data: Mapping[str, Any]) -> KnownDevice:
        return KnownDevice(
            device_id=str(data["device_id"]),
            device_name=str(data.get("device_name", "")),
            user_name=str(data.get("user_name", "")),
            permission=Permission(str(data.get("permission", Permission.CONTRIBUTOR.value))),
            token=str(data.get("token", "")),
            always_allow=bool(data.get("always_allow", False)),
            first_seen=str(data.get("first_seen", "")),
            metadata=dict(data.get("metadata", {})),
        )
