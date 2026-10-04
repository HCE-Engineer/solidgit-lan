"""The coordinator's HTTP server.

Milestone 2 scope: answer "who are you" and run the join handshake. Object transfer, locks
and reconciliation endpoints arrive in later milestones and hang off the same app.

Request bodies are parsed with the dataclasses in `protocol.py` rather than a second set of
models here, so there is exactly one definition of what goes over the wire.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, UTC
from pathlib import Path
from typing import Any
from collections.abc import Callable, Mapping

import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from .. import PROTOCOL_VERSION, __version__
from ..appdata import app_data_dir
from ..core.commits import Commit
from ..core.locks import AcquireResult, LockError, LockTable, ReleaseReason
from ..core.manifest import changed_paths
from ..core.objects import ObjectStoreError
from ..core.reconcile import ReconcilePlan
from ..core.repo import Repository
from . import security
from .discovery import MdnsResponder, PeerRegistry, UdpBeacon
from .protocol import (
    DEFAULT_HTTPS_PORT,
    Announcement,
    ErrorCode,
    InfoResponse,
    JoinChallenge,
    JoinCompletion,
    JoinRequest,
    JoinResult,
    JoinStatus,
    KnownDevice,
    Permission,
    ProtocolError,
    Role,
    check_protocol,
)

#: Streamed in pieces rather than read whole: a 200 MB assembly must not sit in the host's
#: memory just because someone asked for it.
TRANSFER_CHUNK = 512 * 1024


def _ranged_file_response(path: Path, request: Request) -> Response:
    """Serve a stored blob, honouring `Range` so an interrupted download can resume.

    Resume is not a nicety here. On a laptop hotspot, a 200 MB transfer that has to restart
    from zero every time someone walks out of range never finishes at all.
    """
    size = path.stat().st_size
    start, end = 0, size - 1
    status = 200

    raw_range = request.headers.get("range", "")
    if raw_range.startswith("bytes="):
        first, _, last = raw_range[6:].partition("-")
        try:
            start = int(first) if first else 0
            end = int(last) if last else size - 1
        except ValueError:
            start, end = 0, size - 1
        if start >= size:
            return Response(
                status_code=416, headers={"Content-Range": f"bytes */{size}"}
            )
        end = min(end, size - 1)
        status = 206

    def stream():
        remaining = end - start + 1
        with open(path, "rb") as handle:
            handle.seek(start)
            while remaining > 0:
                chunk = handle.read(min(TRANSFER_CHUNK, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    headers = {
        "Content-Length": str(end - start + 1),
        "Accept-Ranges": "bytes",
    }
    if status == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    return StreamingResponse(
        stream(), status_code=status, headers=headers, media_type="application/octet-stream"
    )


_STATUS_FOR_CODE = {
    ErrorCode.PROTOCOL_MISMATCH: 426,
    ErrorCode.REPO_MISMATCH: 409,
    ErrorCode.NOT_APPROVED: 202,
    ErrorCode.REJECTED: 403,
    ErrorCode.BAD_JOIN_CODE: 401,
    ErrorCode.UNAUTHORIZED: 401,
}

#: A half-finished handshake is abandoned rather than kept alive indefinitely.
HANDSHAKE_TTL_SECONDS = 120

#: A joined peer that has not been heard from for this long is no longer shown as connected.
#: Peers poll every two seconds, so this survives a few dropped requests on a busy hotspot.
PRESENCE_SECONDS = 10.0


@dataclass
class PendingReconcile:
    """A merge proposal waiting on a person here to agree to it."""

    plan: ReconcilePlan
    device: str
    user_name: str
    status: str = "pending"
    proposed_at: float = field(default_factory=time.time)


@dataclass
class PendingJoin:
    """A device that has proved it knows the join code and is now waiting on a human."""

    request: JoinRequest
    client_nonce: str
    server_nonce: str
    started_at: float
    proved: bool = False
    status: JoinStatus = JoinStatus.PENDING
    token: str = ""


class DeviceDirectory:
    """Devices the host has approved, one file per project under the app data directory.

    Deliberately **not** inside the workspace. These records hold bearer tokens, and a
    workspace folder is a thing people copy to a USB stick and hand around — trust decisions
    belong to this machine, not to the project.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.devices: dict[str, KnownDevice] = {}
        self._lock = threading.Lock()
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        self.devices = {
            str(entry["device_id"]): KnownDevice.from_json(entry)
            for entry in data.get("devices", [])
        }

    def save(self) -> None:
        payload = {"devices": [d.to_json() for d in self.devices.values()]}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    def remember(self, device: KnownDevice) -> KnownDevice:
        with self._lock:
            self.devices[device.device_id] = device
            self.save()
        return device

    def get(self, device_id: str) -> KnownDevice | None:
        return self.devices.get(device_id)

    def by_token(self, token: str) -> KnownDevice | None:
        if not token:
            return None
        return next((d for d in self.devices.values() if d.token == token), None)

    def is_always_allowed(self, device_id: str) -> bool:
        device = self.devices.get(device_id)
        return bool(device and device.always_allow)


class CoordinatorServer:
    """HTTPS server plus the discovery beacons, for the node hosting the session."""

    def __init__(
        self,
        repo: Repository,
        user_name: str,
        device_name: str,
        device_id: str,
        port: int = DEFAULT_HTTPS_PORT,
        join_code: str | None = None,
        auto_approve: bool = False,
        on_join_request: Callable[[JoinRequest], None] | None = None,
        devices_path: Path | None = None,
    ) -> None:
        self.repo = repo
        self.user_name = user_name
        self.device_name = device_name
        self.device_id = device_id
        self.port = port
        self.auto_approve = auto_approve
        self.on_join_request = on_join_request

        self.join_code = join_code or security.generate_join_code()
        self.identity = security.generate_identity(f"SolidGit LAN — {repo.info.name}")
        self._key = security.derive_key(self.join_code, repo.info.repo_id)

        #: The one authoritative lock table. Peers mirror this; nobody else decides.
        self.locks = LockTable()
        self.forced_unlocks: list[dict[str, Any]] = []
        self._last_seen: dict[str, tuple[str, float]] = {}

        self.directory = DeviceDirectory(
            devices_path or app_data_dir() / "devices" / f"{repo.info.repo_id}.json"
        )
        self.registry = PeerRegistry(self_device_id=device_id)
        self.pending: dict[str, PendingJoin] = {}
        self.pending_reconciles: dict[str, PendingReconcile] = {}
        self.on_reconcile_request: Callable[[ReconcilePlan, str], None] | None = None
        self._lock = threading.Lock()

        self._app = self._build_app()
        self._tls_dir: Path | None = None
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self._beacon: UdpBeacon | None = None
        self._mdns: MdnsResponder | None = None

    # -- announcements --------------------------------------------------------------------

    def announcement(self) -> Announcement:
        return Announcement(
            repo_id=self.repo.info.repo_id,
            repo_name=self.repo.info.name,
            device_id=self.device_id,
            device_name=self.device_name,
            user_name=self.user_name,
            role=Role.HOST,
            port=self.port,
            head=self.repo.commits.head(),
            fingerprint=self.identity.short_fingerprint,
        )

    def info(self) -> InfoResponse:
        head = self.repo.commits.head_commit()
        return InfoResponse(
            protocol=PROTOCOL_VERSION,
            repo_id=self.repo.info.repo_id,
            repo_name=self.repo.info.name,
            device_id=self.device_id,
            device_name=self.device_name,
            user_name=self.user_name,
            role=Role.HOST,
            head=head.id if head else None,
            lamport=head.lamport if head else 0,
            peer_count=len(self.registry.peers(time.time())),
            app_version=__version__,
        )

    # -- join handshake -------------------------------------------------------------------

    def begin_join(self, request: JoinRequest) -> JoinChallenge:
        """Answer the client's challenge, proving we know the join code first.

        Order matters: the host commits to the shared secret before the client does, so a
        machine impersonating the host learns nothing from an attempt.
        """
        check_protocol(request.protocol)
        if request.repo_id != self.repo.info.repo_id:
            raise ProtocolError(
                ErrorCode.REPO_MISMATCH,
                f"This session is hosting '{self.repo.info.name}', which is a different "
                "project from the one you are trying to join.",
            )

        server_nonce = security.make_nonce()
        with self._lock:
            self._drop_expired_handshakes()
            self.pending[request.device_id] = PendingJoin(
                request=request,
                client_nonce=request.client_nonce,
                server_nonce=server_nonce,
                started_at=time.time(),
            )

        return JoinChallenge(
            server_nonce=server_nonce,
            server_proof=security.server_proof(
                self._key, request.client_nonce, server_nonce, self.identity.fingerprint
            ),
            fingerprint=self.identity.fingerprint,
        )

    def complete_join(self, completion: JoinCompletion) -> JoinResult:
        with self._lock:
            session = self.pending.get(completion.device_id)
        if session is None:
            raise ProtocolError(
                ErrorCode.BAD_JOIN_CODE, "No handshake in progress for this device; start over."
            )
        if time.time() - session.started_at > HANDSHAKE_TTL_SECONDS:
            with self._lock:
                self.pending.pop(completion.device_id, None)
            raise ProtocolError(ErrorCode.BAD_JOIN_CODE, "That handshake timed out; start over.")

        expected = security.client_proof(
            self._key, session.client_nonce, session.server_nonce, self.identity.fingerprint
        )
        if not security.proofs_match(expected, completion.client_proof):
            with self._lock:
                self.pending.pop(completion.device_id, None)
            raise ProtocolError(ErrorCode.BAD_JOIN_CODE, "That join code is not correct.")

        session.proved = True

        # A correct code is necessary but not sufficient: a person on the host still decides,
        # unless they already chose "always allow" for this device.
        if self.auto_approve or self.directory.is_always_allowed(completion.device_id):
            return self.approve(completion.device_id)

        if self.on_join_request is not None:
            self.on_join_request(session.request)
        return JoinResult(
            status=JoinStatus.PENDING,
            message=f"Waiting for {self.user_name} to allow this device.",
        )

    def approve(self, device_id: str, always_allow: bool = False) -> JoinResult:
        with self._lock:
            session = self.pending.get(device_id)
        if session is None or not session.proved:
            raise ProtocolError(
                ErrorCode.BAD_JOIN_CODE, "That device has not completed the handshake."
            )

        existing = self.directory.get(device_id)
        token = existing.token if existing and existing.token else security.new_device_token()
        device = KnownDevice(
            device_id=device_id,
            device_name=session.request.device_name,
            user_name=session.request.user_name,
            permission=Permission.CONTRIBUTOR,
            token=token,
            always_allow=always_allow or bool(existing and existing.always_allow),
            first_seen=existing.first_seen
            if existing
            else datetime.now(UTC).astimezone().isoformat(timespec="seconds"),
        )
        self.directory.remember(device)

        session.status = JoinStatus.APPROVED
        session.token = token
        return JoinResult(
            status=JoinStatus.APPROVED, device_token=token, permission=device.permission
        )

    def reject(self, device_id: str) -> JoinResult:
        with self._lock:
            session = self.pending.get(device_id)
            if session is not None:
                session.status = JoinStatus.REJECTED
        return JoinResult(status=JoinStatus.REJECTED, message="The host declined this device.")

    def join_status(self, device_id: str) -> JoinResult:
        with self._lock:
            session = self.pending.get(device_id)
        if session is None:
            raise ProtocolError(ErrorCode.BAD_JOIN_CODE, "No handshake in progress.")
        if session.status is JoinStatus.APPROVED:
            return JoinResult(status=JoinStatus.APPROVED, device_token=session.token)
        if session.status is JoinStatus.REJECTED:
            return JoinResult(status=JoinStatus.REJECTED, message="The host declined this device.")
        return JoinResult(status=JoinStatus.PENDING)

    def pending_requests(self) -> tuple[JoinRequest, ...]:
        with self._lock:
            return tuple(
                s.request
                for s in self.pending.values()
                if s.proved and s.status is JoinStatus.PENDING
            )

    def _drop_expired_handshakes(self) -> None:
        cutoff = time.time() - HANDSHAKE_TTL_SECONDS
        for device_id, session in list(self.pending.items()):
            if session.started_at < cutoff and session.status is JoinStatus.PENDING:
                del self.pending[device_id]

    # -- HTTP -----------------------------------------------------------------------------

    def _build_app(self) -> FastAPI:
        app = FastAPI(title="SolidGit LAN", docs_url=None, redoc_url=None)

        @app.exception_handler(ProtocolError)
        async def _protocol_error(_: Request, error: ProtocolError) -> JSONResponse:
            return JSONResponse(
                status_code=_STATUS_FOR_CODE.get(error.code, 400), content=error.to_json()
            )

        @app.get("/v1/info")
        async def get_info() -> dict[str, Any]:
            return self.info().to_json()

        @app.post("/v1/join/begin")
        async def post_join_begin(request: Request) -> dict[str, Any]:
            return self.begin_join(JoinRequest.from_json(await request.json())).to_json()

        @app.post("/v1/join/complete")
        async def post_join_complete(request: Request) -> dict[str, Any]:
            return self.complete_join(JoinCompletion.from_json(await request.json())).to_json()

        @app.get("/v1/join/status")
        async def get_join_status(device_id: str) -> dict[str, Any]:
            return self.join_status(device_id).to_json()

        # -- transfer (approved devices only) ---------------------------------------------

        @app.get("/v1/commits")
        async def get_commits(request: Request, since: str | None = None) -> dict[str, Any]:
            self._require_device(request)
            store = self.repo.commits
            # Served from TIP, not HEAD: a teammate's contribution belongs to the shared
            # history the moment it arrives, even though the host has not taken it onto
            # their own disk yet.
            head = store.tip()
            since_known = bool(since and store.has(since))
            known = store.ancestors(since) if since_known else frozenset()
            commits = [
                store.read(cid).to_json()
                for cid in (store.ancestors(head) - known if head else frozenset())
            ]
            commits.sort(key=lambda c: c["lamport"])
            # `since_known` tells the caller whether this list was trimmed. Without it they
            # cannot tell "you already have the rest" from "I have never heard of your
            # history", and those mean fast-forward and divergence respectively.
            return {"head": head, "since_known": since_known, "commits": commits}

        @app.get("/v1/objects/{digest}")
        async def get_object(digest: str, request: Request) -> Response:
            self._require_device(request)
            if not self.repo.objects.has(digest):
                raise ProtocolError(
                    ErrorCode.OBJECT_MISSING, f"This session does not have object {digest}."
                )
            return _ranged_file_response(self.repo.objects.path_for(digest), request)

        @app.post("/v1/objects/missing")
        async def post_objects_missing(request: Request) -> dict[str, Any]:
            self._require_device(request)
            payload = await request.json()
            have = [str(d) for d in payload.get("have", [])]
            return {"missing": self.repo.objects.missing_of(have)}

        @app.put("/v1/objects/{digest}")
        async def put_object(digest: str, request: Request) -> dict[str, Any]:
            """Accept a blob from a teammate.

            Streamed to a temp file and verified against its own name before it is allowed
            into the store — nothing a peer sends is trusted on its word.
            """
            self._require_device(request)
            store = self.repo.objects
            if store.has(digest):
                return {"stored": True, "already_had": True}

            partial = store.partial_path(digest)
            partial.parent.mkdir(parents=True, exist_ok=True)
            try:
                with open(partial, "wb") as handle:
                    async for chunk in request.stream():
                        handle.write(chunk)
                store.adopt_partial(digest)
            except ObjectStoreError as e:
                raise ProtocolError(ErrorCode.OBJECT_MISSING, str(e)) from e
            except OSError as e:
                partial.unlink(missing_ok=True)
                raise ProtocolError(
                    ErrorCode.OBJECT_MISSING, f"Object could not be stored: {e}"
                ) from e
            return {"stored": True, "already_had": False}

        @app.post("/v1/commits")
        async def post_commit(request: Request) -> dict[str, Any]:
            device = self._require_device(request)
            payload = await request.json()
            # Two shapes accepted: a bare commit, or one wrapped with the approval it relies
            # on. Merges need the second; ordinary work needs neither.
            raw = payload.get("commit", payload)
            plan_id = payload.get("plan_id") if isinstance(payload, dict) else None
            return self.accept_commit(
                Commit.from_json(raw), device.device_id, str(plan_id) if plan_id else None
            ).copy()

        # -- reconciliation ---------------------------------------------------------------

        @app.post("/v1/reconcile/propose")
        async def post_reconcile_propose(request: Request) -> dict[str, Any]:
            device = self._require_device(request)
            payload = await request.json()
            return self.propose_reconcile(
                ReconcilePlan.from_json(payload["plan"]),
                device.device_id,
                device.user_name,
                [Commit.from_json(raw) for raw in payload.get("commits", [])],
            )

        @app.get("/v1/reconcile/status")
        async def get_reconcile_status(request: Request, plan_id: str) -> dict[str, Any]:
            self._require_device(request)
            return self.reconcile_status(plan_id)

        # -- locks ------------------------------------------------------------------------

        @app.get("/v1/locks")
        async def get_locks(request: Request) -> dict[str, Any]:
            device = self._require_device(request)
            # Piggybacks on the mirror poll every peer already makes, so a broken lock
            # reaches its owner within seconds without a separate channel.
            return {**self.locks_json(), "forced": self.take_forced_unlocks_for(device.device_id)}

        @app.post("/v1/locks/acquire")
        async def post_lock_acquire(request: Request) -> dict[str, Any]:
            device = self._require_device(request)
            payload = await request.json()
            result = self.acquire_locks(
                device=device.device_id,
                user_name=device.user_name,
                paths=[str(p) for p in payload.get("paths", [])],
                base_hashes={
                    str(k): (None if v is None else str(v))
                    for k, v in dict(payload.get("base", {})).items()
                },
            )
            return result.to_json()

        @app.post("/v1/locks/release")
        async def post_lock_release(request: Request) -> dict[str, Any]:
            device = self._require_device(request)
            payload = await request.json()
            released = self.release_locks(
                device.device_id,
                [str(p) for p in payload.get("paths", [])],
                str(payload.get("reason", "manual")),
            )
            return {"released": list(released)}

        @app.post("/v1/locks/renew")
        async def post_lock_renew(request: Request) -> dict[str, Any]:
            device = self._require_device(request)
            return {"renewed": list(self.locks.renew(device.device_id, time.time()))}

        @app.post("/v1/locks/soft-claim")
        async def post_soft_claim(request: Request) -> dict[str, Any]:
            device = self._require_device(request)
            payload = await request.json()
            path = str(payload.get("path", ""))
            if payload.get("clear"):
                self.locks.clear_soft_claim(path, device.device_id)
            else:
                self.locks.soft_claim(path, device.device_id, device.user_name, time.time())
            return {"ok": True}

        @app.post("/v1/locks/queue")
        async def post_lock_queue(request: Request) -> dict[str, Any]:
            device = self._require_device(request)
            payload = await request.json()
            position = self.locks.enqueue(
                str(payload.get("path", "")), device.device_id, device.user_name
            )
            return {"position": position}

        @app.post("/v1/objects/sizes")
        async def post_objects_sizes(request: Request) -> dict[str, Any]:
            """Transfer sizes for a set of blobs.

            Asked before downloading so the progress bar counts real bytes. Without it the
            only honest display is "file 3 of 12", which on a 200 MB assembly tells someone
            nothing about whether to wait or go and get a coffee.
            """
            self._require_device(request)
            payload = await request.json()
            sizes = {
                digest: self.repo.objects.stored_size(digest)
                for digest in (str(d) for d in payload.get("digests", []))
                if self.repo.objects.has(digest)
            }
            return {"sizes": sizes}

        return app

    # -- reconciliation -------------------------------------------------------------------

    def propose_reconcile(
        self, plan: ReconcilePlan, device: str, user_name: str, commits: list[Commit]
    ) -> dict[str, Any]:
        """Receive a merge proposal and put it in front of a person.

        Their commits are stored so the host can actually look at what is being proposed, but
        TIP is not moved: nothing about the project changes until someone here says yes.
        """
        for commit in sorted(commits, key=lambda c: c.lamport):
            self.repo.commits.write(commit)

        with self._lock:
            self.pending_reconciles[plan.plan_id] = PendingReconcile(
                plan=plan, device=device, user_name=user_name
            )
        if self.on_reconcile_request is not None:
            self.on_reconcile_request(plan, user_name)
        return {"status": "pending", "plan_id": plan.plan_id}

    def approve_reconcile(self, plan_id: str) -> dict[str, Any]:
        with self._lock:
            pending = self.pending_reconciles.get(plan_id)
        if pending is None:
            raise ProtocolError(ErrorCode.DIVERGED, "Böyle bir birleştirme önerisi yok.")
        pending.status = "approved"
        return {"status": "approved", "plan_id": plan_id}

    def reject_reconcile(self, plan_id: str) -> dict[str, Any]:
        with self._lock:
            pending = self.pending_reconciles.get(plan_id)
        if pending is not None:
            pending.status = "rejected"
        return {"status": "rejected", "plan_id": plan_id}

    def reconcile_status(self, plan_id: str) -> dict[str, Any]:
        with self._lock:
            pending = self.pending_reconciles.get(plan_id)
        if pending is None:
            raise ProtocolError(ErrorCode.DIVERGED, "Böyle bir birleştirme önerisi yok.")
        return {"status": pending.status, "plan_id": plan_id}

    def pending_reconcile_requests(self) -> tuple[PendingReconcile, ...]:
        with self._lock:
            return tuple(p for p in self.pending_reconciles.values() if p.status == "pending")

    # -- locks ----------------------------------------------------------------------------

    def acquire_locks(
        self,
        device: str,
        user_name: str,
        paths: list[str],
        base_hashes: Mapping[str, str | None],
    ) -> AcquireResult:
        """Grant every requested lock or none of them, checked against the shared front.

        Staleness is measured against TIP rather than the host's own HEAD: the newest work
        anyone has contributed is what a person must be holding before they may edit it.
        """
        now = time.time()
        self.locks.expire(now)
        tip = self.repo.commits.tip_commit()
        latest = (
            {path: ref.sha256 for path, ref in tip.manifest.entries.items()} if tip else {}
        )
        authors = {path: tip.author.name for path in latest} if tip else {}
        return self.locks.acquire(
            paths=paths,
            device=device,
            user_name=user_name,
            base_hashes=base_hashes,
            latest=latest,
            now=now,
            latest_authors=authors,
        )

    def release_locks(self, device: str, paths: list[str], reason: str = "manual") -> tuple[str, ...]:
        try:
            release_reason = ReleaseReason(reason)
        except ValueError:
            release_reason = ReleaseReason.MANUAL
        try:
            return self.locks.release(paths, device, time.time(), release_reason)
        except LockError as e:
            raise ProtocolError(ErrorCode.LOCK_NOT_OWNED, str(e)) from e

    def broadcast_forced_unlock(self, lock) -> None:
        """Record that a lock was broken, so the owner can be told rather than just find out.

        Kept as an explicit step: breaking someone's lock while they may be mid-edit is the
        one administrative action that must never look like an accident.
        """
        self.forced_unlocks.append(
            {"path": lock.path, "device": lock.device, "user_name": lock.user_name,
             "at": time.time()}
        )
        del self.forced_unlocks[:-50]

    def forced_unlocks_for(self, device: str) -> list[dict[str, Any]]:
        return [entry for entry in self.forced_unlocks if entry["device"] == device]

    def take_forced_unlocks_for(self, device: str) -> list[dict[str, Any]]:
        """Hand over — once — the notices meant for this device."""
        mine = self.forced_unlocks_for(device)
        self.forced_unlocks = [e for e in self.forced_unlocks if e["device"] != device]
        return mine

    def locks_json(self) -> dict[str, Any]:
        self.locks.expire(time.time())
        return self.locks.to_json()

    def _changed_paths(self, commit: Commit) -> frozenset[str]:
        """Which paths this commit alters relative to its parent."""
        parent_manifest = (
            self.repo.commits.read(commit.parents[0]).manifest
            if commit.parents and self.repo.commits.has(commit.parents[0])
            else None
        )
        if parent_manifest is None:
            return commit.manifest.paths()
        added, modified, removed = changed_paths(parent_manifest, commit.manifest)
        return added | modified | removed

    def accept_commit(
        self, commit: Commit, device: str | None = None, plan_id: str | None = None
    ) -> dict[str, Any]:
        """Take a teammate's commit into the shared history.

        Fast-forward only. A commit whose parent is not the current tip means the sender was
        working from an older state, and accepting it would silently drop whatever landed in
        between — the exact failure this whole project exists to prevent. They are told to
        take the newer work first.

        The host's own HEAD is deliberately left alone: their files change only when they
        choose, never while they might have the assembly open.
        """
        store = self.repo.commits
        tip = store.tip()

        if store.has(commit.id):
            return {"accepted": True, "tip": tip, "already_had": True}

        if commit.is_reconciliation:
            # A merge may land on a tip it does not descend from — that is what makes it a
            # merge. What it cannot do is arrive without a person here having agreed to it.
            approved = self.pending_reconciles.get(plan_id or "")
            if approved is None or approved.status != "approved":
                raise ProtocolError(
                    ErrorCode.DIVERGED,
                    "Birleştirme kaydı ancak onaylanmış bir öneriyle gönderilebilir.",
                )
            if tip is not None and tip not in commit.parents:
                raise ProtocolError(
                    ErrorCode.DIVERGED,
                    "Bu birleştirme, aradan gelen yeni bir kaydı atlıyor. Baştan başla.",
                    {"tip": tip},
                )
        else:
            parent = commit.parents[0] if commit.parents else None
            if parent != tip:
                raise ProtocolError(
                    ErrorCode.DIVERGED,
                    "Bu kayıt güncel olmayan bir sürümün üzerine yazılmış. Önce yeni "
                    "değişiklikleri al, sonra tekrar gönder.",
                    {"tip": tip, "your_parent": parent},
                )

        missing = [
            ref.sha256
            for ref in commit.manifest.entries.values()
            if not self.repo.objects.has(ref.sha256)
        ]
        if missing:
            raise ProtocolError(
                ErrorCode.OBJECT_MISSING,
                f"{len(missing)} dosyanın içeriği henüz yüklenmedi.",
                {"missing": missing[:20]},
            )

        if device is not None and not commit.is_reconciliation:
            now = time.time()
            self.locks.expire(now)
            # A file may only be changed by whoever holds it. Without this the lock is a
            # suggestion: two people could edit the same part and the second push would still
            # land, which is precisely the situation this project exists to prevent.
            not_ours = [
                path
                for path in sorted(self._changed_paths(commit))
                if (holder := self.locks.holder(path, now)) is not None
                and holder.device != device
            ]
            if not_ours:
                holders = ", ".join(
                    f"{p.rsplit('/', 1)[-1]} ({self.locks.holder(p, now).user_name})"
                    for p in not_ours[:5]
                )
                raise ProtocolError(
                    ErrorCode.LOCK_NOT_OWNED,
                    f"Bu dosyalar başkasında kilitli: {holders}. Kaydın kabul edilmedi.",
                    {"paths": not_ours},
                )

        store.write(commit)
        store.set_tip(commit.id)

        # The work is in; holding the files no longer serves anyone.
        if device is not None:
            self.locks.release(
                [p for p in self._changed_paths(commit) if self.locks.is_locked_by(p, device, time.time())],
                device,
                time.time(),
                ReleaseReason.COMMITTED,
            )
        return {"accepted": True, "tip": commit.id, "already_had": False}

    def connected_users(self, within: float = PRESENCE_SECONDS) -> list[str]:
        """Who has talked to us recently — the honest answer to "who is here".

        Discovery beacons cannot answer it: someone who joined does not announce anything
        (they are not hosting), so counting beacons showed "0 devices" to a host with two
        teammates connected. Joined peers renew their locks every couple of seconds, so a
        recent authenticated request is a reliable sign of life.
        """
        cutoff = time.time() - within
        return sorted(name for name, seen in self._last_seen.values() if seen >= cutoff)

    def _require_device(self, request: Request) -> KnownDevice:
        """Reject anything without a token this host issued.

        The join handshake is what hands out that token, so this is the line between "on the
        Wi-Fi" and "allowed to read the project".
        """
        header = request.headers.get("authorization", "")
        token = header[7:].strip() if header.lower().startswith("bearer ") else ""
        device = self.directory.by_token(token)
        if device is not None:
            self._last_seen[device.device_id] = (device.user_name, time.time())
        if device is None:
            raise ProtocolError(
                ErrorCode.UNAUTHORIZED,
                "Bu cihaz bu oturuma katılmamış. Önce katılım kodunu gir.",
            )
        return device

    # -- lifecycle ------------------------------------------------------------------------

    def start(self, host: str = "0.0.0.0", discovery: bool = True) -> None:
        # uvicorn wants the key as a file. It used to go into `.solidgit/tls/` inside the
        # project — a folder people copy onto memory sticks — so the session's private key
        # travelled with the parts. A private temp directory, removed when the session
        # ends, keeps it on this machine and only for as long as it is in use.
        self._tls_dir = Path(tempfile.mkdtemp(prefix="solidgit-tls-"))
        certificate_path = self._tls_dir / "server.crt"
        key_path = self._tls_dir / "server.key"
        certificate_path.write_bytes(self.identity.certificate_pem)
        key_path.write_bytes(self.identity.private_key_pem)

        config = uvicorn.Config(
            self._app,
            host=host,
            port=self.port,
            ssl_certfile=str(certificate_path),
            ssl_keyfile=str(key_path),
            log_level="warning",
            # log_config=None leaves logging entirely to us. uvicorn's default config builds
            # a colour-aware formatter that calls sys.stdout.isatty(), and under pythonw.exe
            # there is no stdout at all — so simply starting a session raised
            # "Unable to configure formatter 'default'" in the packaged, windowed app while
            # working perfectly from a console.
            log_config=None,
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True, name="coordinator")
        self._thread.start()

        deadline = time.time() + 15
        while not self._server.started and time.time() < deadline:
            if not self._thread.is_alive():
                self._remove_tls_files()
                raise RuntimeError("The coordinator server thread stopped while starting up.")
            time.sleep(0.05)
        if not self._server.started:
            self.stop()
            raise RuntimeError(f"The coordinator server did not come up on port {self.port}.")

        if discovery:
            self._beacon = UdpBeacon(self.announcement, self.registry)
            self._beacon.start()
            self._mdns = MdnsResponder(self.announcement, self.registry)
            self._mdns.start()

    def stop(self) -> None:
        if self._mdns is not None:
            self._mdns.stop()
            self._mdns = None
        if self._beacon is not None:
            self._beacon.stop()
            self._beacon = None
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=10)
            self._thread = None
        self._server = None
        self._remove_tls_files()

    def _remove_tls_files(self) -> None:
        if self._tls_dir is not None:
            shutil.rmtree(self._tls_dir, ignore_errors=True)
            self._tls_dir = None

    def __enter__(self) -> CoordinatorServer:
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    @property
    def mdns_available(self) -> bool:
        return bool(self._mdns and self._mdns.available)
