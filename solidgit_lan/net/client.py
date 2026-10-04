"""The peer side of the handshake.

The certificate is fetched and pinned before any request is sent, and the host has to prove
it knows the join code before we send our own proof. Together that means a machine pretending
to be the host on the same hotspot gets nothing out of an attempt.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any
from collections.abc import Callable

import httpx

from .. import PROTOCOL_VERSION
from ..core.commits import Commit
from ..core.locks import AcquireResult, LockTable
from . import security
from .protocol import (
    ErrorCode,
    InfoResponse,
    JoinChallenge,
    JoinCompletion,
    JoinRequest,
    JoinResult,
    JoinStatus,
    ProtocolError,
    check_protocol,
)

DEFAULT_TIMEOUT = 10.0

#: How often to ask whether the host has clicked "allow" yet.
APPROVAL_POLL_SECONDS = 1.0


@dataclass
class Connection:
    """An open, certificate-pinned session with one host."""

    host: str
    port: int
    certificate_pem: bytes
    fingerprint: str
    client: httpx.Client

    @property
    def short_fingerprint(self) -> str:
        return self.fingerprint[:8].upper()

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> Connection:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


class PeerClient:
    """Talks to one coordinator at a time on behalf of this device."""

    def __init__(
        self,
        device_id: str,
        device_name: str,
        user_name: str,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.device_id = device_id
        self.device_name = device_name
        self.user_name = user_name
        self.timeout = timeout

    def open(self, host: str, port: int) -> Connection:
        """Pin the host's certificate, then build a client that trusts only that one."""
        certificate_pem = security.fetch_certificate(host, port, timeout=self.timeout)
        fingerprint = security.fingerprint_of(certificate_pem)
        client = httpx.Client(
            base_url=f"https://{host}:{port}",
            verify=security.pinned_client_context(certificate_pem),
            timeout=self.timeout,
        )
        return Connection(
            host=host,
            port=port,
            certificate_pem=certificate_pem,
            fingerprint=fingerprint,
            client=client,
        )

    # -- requests -------------------------------------------------------------------------

    def info(self, connection: Connection) -> InfoResponse:
        response = _unwrap(connection.client.get("/v1/info"))
        info = InfoResponse.from_json(response)
        check_protocol(info.protocol)
        return info

    def join(
        self,
        connection: Connection,
        join_code: str,
        repo_id: str,
        wait_for_approval: float = 0.0,
    ) -> JoinResult:
        """Run the three-step handshake, optionally waiting for the host to approve.

        Raises before sending our own proof if the host's proof does not check out — that is
        the case where we are talking to the wrong machine.
        """
        key = security.derive_key(join_code, repo_id)
        client_nonce = security.make_nonce()

        request = JoinRequest(
            protocol=PROTOCOL_VERSION,
            repo_id=repo_id,
            device_id=self.device_id,
            device_name=self.device_name,
            user_name=self.user_name,
            client_nonce=client_nonce,
        )
        challenge = JoinChallenge.from_json(
            _unwrap(connection.client.post("/v1/join/begin", json=request.to_json()))
        )

        if challenge.fingerprint != connection.fingerprint:
            raise ProtocolError(
                ErrorCode.UNAUTHORIZED,
                "The host named a different certificate than the one it is using. "
                "Someone may be sitting between you and it.",
                {"expected": connection.fingerprint, "claimed": challenge.fingerprint},
            )

        expected = security.server_proof(
            key, client_nonce, challenge.server_nonce, connection.fingerprint
        )
        if not security.proofs_match(expected, challenge.server_proof):
            raise ProtocolError(
                ErrorCode.BAD_JOIN_CODE,
                "That join code did not match this host — check the code, or check that this "
                "is really your teammate's machine.",
            )

        completion = JoinCompletion(
            device_id=self.device_id,
            client_proof=security.client_proof(
                key, client_nonce, challenge.server_nonce, connection.fingerprint
            ),
        )
        result = JoinResult.from_json(
            _unwrap(connection.client.post("/v1/join/complete", json=completion.to_json()))
        )

        if result.status is JoinStatus.PENDING and wait_for_approval > 0:
            result = self.wait_for_approval(connection, wait_for_approval)
        return result

    def wait_for_approval(self, connection: Connection, seconds: float) -> JoinResult:
        """Poll until the host decides, or we give up waiting."""
        deadline = time.monotonic() + seconds
        result = JoinResult(status=JoinStatus.PENDING)
        while time.monotonic() < deadline:
            result = JoinResult.from_json(
                _unwrap(
                    connection.client.get(
                        "/v1/join/status", params={"device_id": self.device_id}
                    )
                )
            )
            if result.status is not JoinStatus.PENDING:
                return result
            time.sleep(APPROVAL_POLL_SECONDS)
        return result


    # -- transfer -------------------------------------------------------------------------

    def commits_since(
        self, connection: Connection, token: str, since: str | None = None
    ) -> tuple[str | None, list[Commit], bool]:
        """Fetch the host's history.

        Returns (their head, the commits they sent, whether they recognised `since`). That
        last flag matters: a trimmed list means they build on our history, an untrimmed one
        means they have never seen it.
        """
        payload = _unwrap(
            connection.client.get(
                "/v1/commits",
                params={"since": since} if since else None,
                headers=_auth(token),
            )
        )
        commits = [Commit.from_json(raw) for raw in payload.get("commits", [])]
        head = payload.get("head")
        return (str(head) if head else None), commits, bool(payload.get("since_known", False))

    def object_sizes(
        self, connection: Connection, token: str, digests: list[str]
    ) -> dict[str, int]:
        """Ask how many bytes these blobs will actually cost to fetch."""
        if not digests:
            return {}
        payload = _unwrap(
            connection.client.post(
                "/v1/objects/sizes", json={"digests": digests}, headers=_auth(token)
            )
        )
        return {str(k): int(v) for k, v in payload.get("sizes", {}).items()}

    def download_object(
        self,
        connection: Connection,
        token: str,
        digest: str,
        destination: Path,
        already_have: int = 0,
        on_chunk: Callable[[int], None] | None = None,
        cancel: threading.Event | None = None,
    ) -> int:
        """Download one blob, continuing from `already_have` bytes if there are any.

        Returns the total size on disk afterwards. The caller verifies the content; this
        function only moves bytes.
        """
        headers = _auth(token)
        mode = "wb"
        if already_have > 0:
            headers["Range"] = f"bytes={already_have}-"
            mode = "ab"

        written = already_have
        with connection.client.stream("GET", f"/v1/objects/{digest}", headers=headers) as response:
            if response.status_code == 200 and already_have > 0:
                # The host ignored the range and restarted; so must we, or the file would be
                # the tail of one attempt glued onto the head of another.
                mode, written = "wb", 0
            elif not response.is_success:
                response.read()
                raise ProtocolError(
                    ErrorCode.OBJECT_MISSING,
                    f"Object {digest[:12]} could not be fetched (HTTP {response.status_code}).",
                )

            destination.parent.mkdir(parents=True, exist_ok=True)
            with open(destination, mode) as handle:
                for chunk in response.iter_bytes():
                    if cancel is not None and cancel.is_set():
                        raise TransferCancelled()
                    handle.write(chunk)
                    written += len(chunk)
                    if on_chunk is not None:
                        on_chunk(len(chunk))
                handle.flush()
                os.fsync(handle.fileno())
        return written


    def missing_on_host(
        self, connection: Connection, token: str, digests: list[str]
    ) -> list[str]:
        """Which of these blobs does the host not have? Nothing else needs uploading."""
        if not digests:
            return []
        payload = _unwrap(
            connection.client.post(
                "/v1/objects/missing", json={"have": digests}, headers=_auth(token)
            )
        )
        return [str(d) for d in payload.get("missing", [])]

    def upload_object(
        self,
        connection: Connection,
        token: str,
        digest: str,
        source: Path,
        on_chunk: Callable[[int], None] | None = None,
        cancel: threading.Event | None = None,
    ) -> None:
        """Send one stored blob, streamed rather than read into memory."""

        def chunks():
            with open(source, "rb") as handle:
                while block := handle.read(512 * 1024):
                    if cancel is not None and cancel.is_set():
                        raise TransferCancelled()
                    if on_chunk is not None:
                        on_chunk(len(block))
                    yield block

        response = connection.client.put(
            f"/v1/objects/{digest}",
            content=chunks(),
            headers={**_auth(token), "Content-Type": "application/octet-stream"},
        )
        _unwrap(response)

    def push_commit(
        self,
        connection: Connection,
        token: str,
        commit: Commit,
        plan_id: str | None = None,
    ) -> str | None:
        """Offer a commit to the host. Returns the shared history's new front."""
        body: dict[str, Any] = (
            {"commit": commit.to_json(), "plan_id": plan_id} if plan_id else commit.to_json()
        )
        payload = _unwrap(
            connection.client.post("/v1/commits", json=body, headers=_auth(token))
        )
        tip = payload.get("tip")
        return str(tip) if tip else None

    # -- reconciliation -------------------------------------------------------------------

    def propose_reconcile(
        self, connection: Connection, token: str, plan, commits: list[Commit]
    ) -> str:
        """Send a merge proposal. Returns its id, to be polled until a person answers."""
        payload = _unwrap(
            connection.client.post(
                "/v1/reconcile/propose",
                json={"plan": plan.to_json(), "commits": [c.to_json() for c in commits]},
                headers=_auth(token),
            )
        )
        return str(payload["plan_id"])

    def reconcile_status(self, connection: Connection, token: str, plan_id: str) -> str:
        payload = _unwrap(
            connection.client.get(
                "/v1/reconcile/status", params={"plan_id": plan_id}, headers=_auth(token)
            )
        )
        return str(payload.get("status", "pending"))


    # -- locks ----------------------------------------------------------------------------

    def fetch_locks(self, connection: Connection, token: str) -> LockTable:
        """Mirror the coordinator's table. Nobody but the host decides what it says."""
        return self.fetch_lock_state(connection, token)[0]

    def fetch_lock_state(
        self, connection: Connection, token: str
    ) -> tuple[LockTable, list[dict[str, Any]]]:
        """The mirror, plus any of *our* locks the host broke since we last asked."""
        payload = _unwrap(connection.client.get("/v1/locks", headers=_auth(token)))
        return LockTable.from_json(payload), list(payload.get("forced", []))

    def acquire_locks(
        self,
        connection: Connection,
        token: str,
        paths: list[str],
        base_hashes: dict[str, str | None],
    ) -> AcquireResult:
        return AcquireResult.from_json(
            _unwrap(
                connection.client.post(
                    "/v1/locks/acquire",
                    json={"paths": paths, "base": base_hashes},
                    headers=_auth(token),
                )
            )
        )

    def release_locks(
        self, connection: Connection, token: str, paths: list[str], reason: str = "manual"
    ) -> list[str]:
        payload = _unwrap(
            connection.client.post(
                "/v1/locks/release",
                json={"paths": paths, "reason": reason},
                headers=_auth(token),
            )
        )
        return [str(p) for p in payload.get("released", [])]

    def renew_locks(self, connection: Connection, token: str) -> list[str]:
        payload = _unwrap(connection.client.post("/v1/locks/renew", headers=_auth(token)))
        return [str(p) for p in payload.get("renewed", [])]

    def soft_claim(
        self, connection: Connection, token: str, path: str, clear: bool = False
    ) -> None:
        _unwrap(
            connection.client.post(
                "/v1/locks/soft-claim",
                json={"path": path, "clear": clear},
                headers=_auth(token),
            )
        )

    def queue_for_lock(self, connection: Connection, token: str, path: str) -> int:
        payload = _unwrap(
            connection.client.post(
                "/v1/locks/queue", json={"path": path}, headers=_auth(token)
            )
        )
        return int(payload.get("position", 0))


class TransferCancelled(Exception):
    """Raised when the user stops a transfer part-way through."""


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _unwrap(response: httpx.Response) -> dict[str, Any]:
    """Turn an error response back into the ProtocolError the host raised."""
    if response.is_success:
        return response.json()
    try:
        payload = response.json()
        code = ErrorCode(payload["error"])
    except (ValueError, KeyError):
        raise ProtocolError(
            ErrorCode.UNAUTHORIZED,
            f"Unexpected reply from the host (HTTP {response.status_code}).",
        ) from None
    raise ProtocolError(code, str(payload.get("message", "")), payload.get("detail"))
