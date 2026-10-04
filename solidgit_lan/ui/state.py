"""Application state, sitting between the widgets and the core/net layers.

Everything the UI shows is derived here and read by a single refresh timer. Polling rather
than cross-thread signals is deliberate: the coordinator runs on a uvicorn thread, and a
timer on the GUI thread cannot race with it the way a callback firing from the server thread
could.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

import httpx
from enum import Enum
from pathlib import Path

from .. import __version__
from ..appdata import DeviceIdentity, load_device_identity, remember_project
from ..core.commits import Author, Commit, Relation
from ..core.reconcile import Side
from ..core.locks import DEFAULT_LEASE_SECONDS, AcquireResult, LockTable

#: How often a joined peer refreshes its mirror and renews its own leases.
LOCK_SYNC_SECONDS = 2.0

#: What a dropped connection actually raises. httpx's transport errors are *not* OSError
#: subclasses, so catching OSError alone let a host walking out of Wi-Fi range kill a worker
#: thread silently — and the progress bar then spun forever with no way to retry.
NETWORK_ERRORS = (OSError, httpx.HTTPError)


def describe_network_error(error: Exception) -> str:
    if isinstance(error, httpx.TimeoutException):
        return "karşı taraf yanıt vermedi (zaman aşımı)"
    if isinstance(error, httpx.ConnectError):
        return "karşı tarafa ulaşılamadı — aynı ağda mısınız?"
    if isinstance(error, httpx.HTTPError):
        return "bağlantı yarıda kesildi"
    return str(error)
from ..core.repo import Repository, RepositoryError
from ..net.client import PeerClient
from ..net.discovery import MdnsResponder, PeerRegistry, UdpBeacon
from ..net.protocol import (
    DEFAULT_HTTPS_PORT,
    Announcement,
    JoinRequest,
    JoinStatus,
    Peer,
    ProtocolError,
    Role,
)
from ..net.server import CoordinatorServer
from ..net.sync import (
    Divergence,
    SyncProgress,
    check_version,
    clone,
    inspect_divergence,
    plan_pull,
    plan_push,
    pull,
    push,
    rebase_onto_theirs,
    reconcile,
)


class FileStatus(Enum):
    CLEAN = "clean"
    MODIFIED = "modified"
    ADDED = "added"
    REMOVED = "removed"


class LockState(Enum):
    FREE = "free"
    MINE = "mine"
    THEIRS = "theirs"


@dataclass(frozen=True)
class FileRow:
    """One line in the file list, with everything the row needs already resolved."""

    path: str
    size: int
    status: FileStatus
    lock_state: LockState
    lock_owner: str = ""
    soft_claim_by: str = ""
    """Someone has this open in SolidWorks but has not edited it yet."""

    @property
    def name(self) -> str:
        return self.path.rsplit("/", 1)[-1]

    @property
    def folder(self) -> str:
        return self.path.rsplit("/", 1)[0] if "/" in self.path else ""


@dataclass(frozen=True)
class TransferState:
    """What a running (or finished) transfer looks like to the interface."""

    stage: str
    message: str
    fraction: float = 0.0
    detail: str = ""
    running: bool = True
    ok: bool | None = None

    @property
    def percent(self) -> int:
        return int(round(self.fraction * 100))


@dataclass(frozen=True)
class JoinAttempt:
    """Progress of joining someone else's session, polled by the network page."""

    label: str
    state: str
    """running · waiting · joined · failed"""
    message: str = ""

    @property
    def finished(self) -> bool:
        return self.state in ("joined", "failed")


class AppState:
    """Holds the open workspace, the hosted session, and the lock table."""

    def __init__(self) -> None:
        self.identity: DeviceIdentity = load_device_identity()
        self.repo: Repository | None = None
        self.server: CoordinatorServer | None = None
        self._locks = LockTable()
        """Used when working alone, and as a mirror of the host's table when joined."""
        self._connection = None
        # Lock traffic comes from two threads — the window (acquire, release) and the
        # background mirror. One lock around the shared connection stops one of them
        # closing it while the other is halfway through a request.
        self._net_lock = threading.RLock()
        self._lock_thread: threading.Thread | None = None
        self._stop_lock_sync = threading.Event()
        self.lock_sync_error: str = ""
        self.last_error: str = ""
        #: Messages for the person at this machine that arrived from elsewhere (a broken
        #: lock, for instance). The files page shows and clears them.
        self.notices: list[str] = []
        self.join_attempt: JoinAttempt | None = None

        #: Set once a join is approved: which host we belong to, and the token it issued.
        self.session: tuple[Peer, str] | None = None
        self.transfer: TransferState | None = None
        self.divergence: Divergence | None = None
        """Set when the two sides turn out to have worked apart; cleared once merged."""
        self.version_warning: str = ""
        self._cancel_transfer = threading.Event()

        # One beacon for the whole app, always listening. It only announces while we are
        # hosting — someone who has not joined a project yet has nothing truthful to say.
        self.registry = PeerRegistry(self_device_id=self.identity.device_id)
        self.beacon: UdpBeacon | None = None
        self.mdns: MdnsResponder | None = None
        self.discovery_error: str = ""

    # -- workspace ------------------------------------------------------------------------

    @property
    def is_open(self) -> bool:
        return self.repo is not None

    @property
    def is_hosting(self) -> bool:
        return self.server is not None

    # Three lifetimes, deliberately kept apart:
    #   listening — the whole time the window is open, project or not;
    #   hosting   — tied to the open project, because that is what is being shared;
    #   a session — joined to someone else's project, which may not exist here yet.
    # They used to all start when a project opened, which left the one person a session
    # is for — a teammate who does not have the files yet — unable to even see it.

    def open_workspace(self, path: str | Path) -> None:
        self.close_workspace(keep_session=True)
        self.repo = Repository(path)
        remember_project(self.repo.root, self.repo.info.name)
        self._drop_session_if_unrelated()

    def initialise_workspace(self, path: str | Path, name: str | None = None) -> None:
        self.close_workspace(keep_session=True)
        self.repo = Repository.initialise(path, name=name)
        remember_project(self.repo.root, self.repo.info.name)
        self._drop_session_if_unrelated()

    def close_workspace(self, keep_session: bool = False) -> None:
        self.stop_hosting()
        if not keep_session:
            self.leave_session()
        if self.repo is not None:
            # Leaving files read-only would strand the user in SolidWorks with no way to edit
            # anything after closing the project.
            try:
                self.repo.make_all_writable()
            except OSError:
                pass
        self.repo = None
        if self.session is None:
            self._locks = LockTable()

    def leave_session(self) -> None:
        self.stop_lock_sync()
        self.session = None
        self.divergence = None
        self.version_warning = ""

    def shutdown(self) -> None:
        """Everything off — the window is closing."""
        self.close_workspace()
        self.stop_listening()

    def _drop_session_if_unrelated(self) -> None:
        """A session belongs to one project; opening a different one ends it."""
        if self.session is None or self.repo is None:
            return
        peer, _ = self.session
        if peer.announcement.repo_id != self.repo.info.repo_id:
            self.leave_session()

    def require_repo(self) -> Repository:
        if self.repo is None:
            raise RepositoryError("No workspace is open.")
        return self.repo

    # -- files ----------------------------------------------------------------------------

    def file_rows(self) -> list[FileRow]:
        repo = self.require_repo()
        head = repo.head_manifest()
        current = repo.scan()
        now = time.time()

        locks = self.locks
        rows: list[FileRow] = []
        for path in sorted(head.paths() | current.paths()):
            in_head, on_disk = head.get(path), current.get(path)
            if on_disk is None:
                status, size = FileStatus.REMOVED, in_head.size if in_head else 0
            elif in_head is None:
                status, size = FileStatus.ADDED, on_disk.size
            elif in_head.sha256 != on_disk.sha256:
                status, size = FileStatus.MODIFIED, on_disk.size
            else:
                status, size = FileStatus.CLEAN, on_disk.size

            lock = locks.holder(path, now)
            if lock is None:
                lock_state, owner = LockState.FREE, ""
            elif lock.device == self.identity.device_id:
                lock_state, owner = LockState.MINE, lock.user_name
            else:
                lock_state, owner = LockState.THEIRS, lock.user_name

            claim = locks.soft_claim_for(path)
            claimed_by = (
                claim.user_name
                if claim is not None and claim.device != self.identity.device_id
                else ""
            )

            rows.append(
                FileRow(
                    path=path,
                    size=size,
                    status=status,
                    lock_state=lock_state,
                    lock_owner=owner,
                    soft_claim_by=claimed_by,
                )
            )
        return rows

    def has_changes(self) -> bool:
        return not self.require_repo().status().is_clean

    def commit(self, message: str) -> Commit:
        repo = self.require_repo()

        if self.server is not None:
            # The host writes straight into the shared history, so the rule a teammate's push
            # is checked against has to be checked here too: a file may only be changed by
            # whoever holds it. Read-only files usually stop it earlier, but not always —
            # a file can be made writable by hand, and the history must not depend on that.
            now = time.time()
            taken = sorted(
                (path, lock.user_name)
                for path in repo.status().changed
                if (lock := self.server.locks.holder(path, now)) is not None
                and lock.device != self.identity.device_id
            )
            if taken:
                names = ", ".join(f"{p.rsplit('/', 1)[-1]} ({who})" for p, who in taken[:4])
                raise RepositoryError(
                    f"Bu dosyalar başkasında kilitli, kaydedemezsin: {names}. "
                    "Değişikliğini geri al ya da kilidin bırakılmasını bekle."
                )

        commit = repo.commit(
            message,
            Author(name=self.identity.user_name, device=self.identity.device_name),
        )
        # Committing ends the edit; the lock has served its purpose.
        held = self.locks.paths_held_by(self.identity.device_id, time.time())
        if held:
            try:
                self.release(list(held))
            except Exception:  # noqa: BLE001 - the commit stands even if the release fails
                pass
        return commit

    def history(self, limit: int = 100) -> list[Commit]:
        return self.require_repo().commits.history(limit=limit)

    def restore_file(self, commit_id: str, path: str) -> str:
        """Put one file back the way it was in an earlier commit.

        History is never rewritten: the old content lands in the working folder as an
        ordinary change, and saving it makes a new commit. Undoing the restore is just
        restoring again. In a shared session it goes through the same lock as any other
        edit — restoring someone else's part while they work on it is still overwriting it.
        """
        repo = self.require_repo()
        commit = repo.commits.read(commit_id)
        ref = commit.manifest.get(path)
        if ref is None:
            raise RepositoryError(f"{path} bu kayıtta yok.")
        if not repo.objects.has(ref.sha256):
            raise RepositoryError(
                "Bu sürümün içeriği bu bilgisayarda yok — başka birinin kaydı olabilir. "
                "Önce oturumdan güncel hâli indir."
            )

        if self.locks_are_shared and path not in self.my_locks():
            result = self.acquire([path])
            if not result.ok:
                if result.held:
                    raise RepositoryError(
                        f"{path.rsplit('/', 1)[-1]} şu an {result.held[0].user_name} adlı "
                        "kişide kilitli; geri yükleyemezsin."
                    )
                raise RepositoryError(
                    "Önce son değişiklikleri al — bu dosyanın daha yeni bir sürümü var."
                )

        repo.objects.extract_to(ref.sha256, repo.root / path)
        if self.locks_are_shared:
            self.apply_readonly()
        return (
            f"{path.rsplit('/', 1)[-1]} #{commit.lamport} kaydındaki hâline döndü. "
            "Dosyalar sayfasından kaydedince yeni bir kayıt olarak geçmişe eklenir."
        )

    # -- locks ----------------------------------------------------------------------------

    @property
    def locks(self) -> LockTable:
        """Whoever is authoritative right now.

        Hosting means our own table *is* the authority. Joined means we hold a mirror and
        every decision is made on the other machine. Alone, it is simply local.
        """
        return self.server.locks if self.server is not None else self._locks

    @property
    def locks_are_shared(self) -> bool:
        return self.server is not None or self.session is not None

    def acquire(self, paths: list[str]) -> AcquireResult:
        repo = self.require_repo()
        base = repo.local_hashes()

        if self.server is not None:
            result = self.server.acquire_locks(
                self.identity.device_id, self.identity.user_name, paths, base
            )
        elif self.session is not None:
            result = self._remote(
                lambda client, connection, token: client.acquire_locks(
                    connection, token, paths, base
                )
            )
        else:
            result = self._locks.acquire(
                paths=paths,
                device=self.identity.device_id,
                user_name=self.identity.user_name,
                base_hashes=base,
                latest=repo.latest_hashes(),
                now=time.time(),
                lease_seconds=DEFAULT_LEASE_SECONDS,
            )
            if result.ok:
                self.apply_readonly()
            return result

        if result.ok:
            self._merge_granted(result)
            self.apply_readonly()
        return result

    def release(self, paths: list[str]) -> tuple[str, ...]:
        if self.server is not None:
            released = self.server.release_locks(self.identity.device_id, list(paths))
        elif self.session is not None:
            released = tuple(
                self._remote(
                    lambda client, connection, token: client.release_locks(
                        connection, token, list(paths)
                    )
                )
            )
        else:
            released = self._locks.release(paths, self.identity.device_id, time.time())
        self._forget_released(released)
        self.apply_readonly()
        return tuple(released)

    def force_release(self, paths: list[str]) -> tuple[str, ...]:
        """Break someone else's lock. Only the host may, and it is never silent.

        The escape hatch for a teammate who went home with a part held. Deliberately not
        available to peers: an authority everyone can override is not an authority.
        """
        if self.server is None:
            raise RepositoryError("Kilidi sadece oturumu açan kişi kırabilir.")
        broken: list[str] = []
        for path in paths:
            lock = self.server.locks.force_release(path, time.time())
            if lock is not None:
                broken.append(path)
                self.server.broadcast_forced_unlock(lock)
        self._forget_released(broken)
        self.apply_readonly()
        return tuple(broken)

    def _merge_granted(self, result: AcquireResult) -> None:
        """Reflect a just-granted lock locally so the list updates without waiting a tick."""
        for lock in result.granted:
            self._locks.locks[lock.path] = lock

    def _forget_released(self, paths) -> None:
        for path in paths:
            self._locks.locks.pop(path, None)

    def _lock_client(self) -> PeerClient:
        # A short timeout on purpose: these run on the interface thread, and a host that has
        # walked out of Wi-Fi range must not freeze the window for ten seconds.
        return PeerClient(
            device_id=self.identity.device_id,
            device_name=self.identity.device_name,
            user_name=self.identity.user_name,
            timeout=4.0,
        )

    def _lock_connection(self):
        """Reuse one TLS connection for lock traffic; a fresh handshake each time is slow.

        Callers must hold `_net_lock`.
        """
        if self.session is None:
            raise RepositoryError("Bir oturuma katılmadın.")
        peer, token = self.session
        if self._connection is None:
            self._connection = self._lock_client().open(peer.address, peer.announcement.port)
        return self._connection, token

    def _drop_connection(self) -> None:
        with self._net_lock:
            if self._connection is not None:
                try:
                    self._connection.close()
                except Exception:  # noqa: BLE001 - closing a dead socket is not interesting
                    pass
                self._connection = None

    def _remote(self, call):
        """Run one request against the host, turning a dead network into a plain sentence.

        Without this, a host that walked out of Wi-Fi range surfaced as a raw httpx
        traceback in an error dialog — accurate, and useless to the person reading it.
        """
        with self._net_lock:
            try:
                connection, token = self._lock_connection()
                return call(self._lock_client(), connection, token)
            except ProtocolError:
                raise
            except (OSError, httpx.HTTPError) as e:
                self._drop_connection()
                raise RepositoryError(
                    "Oturuma ulaşılamıyor — bağlantı kopmuş olabilir. "
                    f"Aynı ağda olduğunuzu kontrol edin. ({type(e).__name__})"
                ) from e

    @property
    def lock_sync_running(self) -> bool:
        return self._lock_thread is not None and self._lock_thread.is_alive()

    def start_lock_sync(self) -> None:
        """Keep the mirror fresh and our own leases alive while we are in a session."""
        if self.lock_sync_running or self.session is None:
            return
        stop = threading.Event()
        self._stop_lock_sync = stop
        # The loop is handed *its own* stop event. Reading `self._stop_lock_sync` instead
        # meant a restart swapped in a fresh, unset event — and the old loop, now watching
        # the new one, never stopped. Every download left another loop running.
        self._lock_thread = threading.Thread(
            target=self._lock_sync_loop, args=(stop,), daemon=True, name="lock-sync"
        )
        self._lock_thread.start()

    def stop_lock_sync(self) -> None:
        self._stop_lock_sync.set()
        self._lock_thread = None
        self._drop_connection()

    def _lock_sync_loop(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                with self._net_lock:
                    connection, token = self._lock_connection()
                    client = self._lock_client()
                    mirror, forced = client.fetch_lock_state(connection, token)
                    host_name = self.session[0].announcement.user_name if self.session else ""
                    for entry in forced:
                        self.notices.append(
                            f"{host_name} şu dosyadaki kilidini kaldırdı: "
                            f"{str(entry.get('path', '')).rsplit('/', 1)[-1]}. Kaydetmeden "
                            "önce tekrar kilitlemen gerekecek."
                        )
                    # Renewing is what stops a lock expiring under someone who is still
                    # working; the lease exists for the machine that never comes back.
                    client.renew_locks(connection, token)
                if not stop.is_set():
                    self._locks = mirror
                self.lock_sync_error = ""
            except Exception as e:  # noqa: BLE001 - a dropped Wi-Fi must not kill the thread
                self.lock_sync_error = f"Kilitler güncellenemiyor: {e}"
                self._drop_connection()
            stop.wait(LOCK_SYNC_SECONDS)

    def apply_readonly(self) -> None:
        """Keep the on-disk read-only bits in step with who holds what.

        This is what makes the lock real: SolidWorks opens a read-only file read-only, so an
        unlocked part cannot be edited by accident in the first place.
        """
        repo = self.require_repo()
        repo.apply_readonly(self.locks.paths_held_by(self.identity.device_id, time.time()))

    def my_locks(self) -> tuple[str, ...]:
        return self.locks.paths_held_by(self.identity.device_id, time.time())

    def expire_locks(self) -> None:
        # Only the authority may expire anything. A mirror doing it locally would make a
        # teammate's lock look free here while it is still very much held there.
        if self.session is None or self.server is not None:
            self.locks.expire(time.time())

    # -- hosting --------------------------------------------------------------------------

    def start_listening(self) -> None:
        """Listen for other sessions from the moment a project is open.

        The network page can then show what is nearby straight away, instead of making the
        user press a "search" button and wait — the commonest reason people conclude the app
        is broken is an empty list they were never told to refresh.
        """
        if self.beacon is not None:
            return
        self.beacon = UdpBeacon(self._announcement_or_none, self.registry)
        try:
            self.beacon.start()
        except OSError as e:
            self.discovery_error = (
                f"Ağ dinlenemedi ({e}). Başka bir SolidGit penceresi açık olabilir."
            )
            self.beacon = None

    def stop_listening(self) -> None:
        if self.beacon is not None:
            self.beacon.stop()
            self.beacon = None

    def _announcement_or_none(self) -> Announcement | None:
        return self.server.announcement() if self.server else None

    def start_hosting(self, port: int = DEFAULT_HTTPS_PORT) -> CoordinatorServer:
        repo = self.require_repo()
        if self.server is not None:
            return self.server
        server = CoordinatorServer(
            repo=repo,
            user_name=self.identity.user_name,
            device_name=self.identity.device_name,
            device_id=self.identity.device_id,
            port=port,
        )
        # The app already runs one beacon; a second would bind the same port for no gain.
        server.start(discovery=False)
        server.registry = self.registry
        self.server = server

        # mDNS is best-effort and its startup can take a second on its own. Hosting does not
        # depend on it — the UDP beacon already carries discovery — so it must not sit in
        # front of the button coming back to life.
        self.mdns = MdnsResponder(server.announcement, self.registry)
        self._mdns_thread = threading.Thread(
            target=self.mdns.start, daemon=True, name="mdns-start"
        )
        self._mdns_thread.start()
        return server

    def stop_hosting(self) -> None:
        if self.mdns is not None:
            # A session closed straight after opening would otherwise stop mDNS halfway
            # through registering, leaving its asyncio tasks orphaned.
            thread = getattr(self, "_mdns_thread", None)
            if thread is not None:
                thread.join(timeout=5)
            self.mdns.stop()
            self.mdns = None
        if self.server is not None:
            self.server.stop()
            self.server = None

    def peers(self) -> tuple[Peer, ...]:
        now = time.time()
        self.registry.prune(now)
        return self.registry.peers(now)

    def nearby_sessions(self) -> tuple[Peer, ...]:
        """Hosts we could join — other people's sessions, never our own."""
        return tuple(p for p in self.peers() if p.announcement.role is Role.HOST)

    # -- joining --------------------------------------------------------------------------

    def join(self, peer: Peer, join_code: str) -> None:
        """Join someone else's session on a worker thread.

        The handshake makes several network round trips. Doing it inline would freeze the
        window for seconds, which reads as a crash.
        """
        label = f"{peer.announcement.user_name} ({peer.announcement.repo_name})"
        self.join_attempt = JoinAttempt(label=label, state="running", message="Bağlanılıyor…")
        threading.Thread(
            target=self._join_worker, args=(peer, join_code, label), daemon=True
        ).start()

    def _join_worker(self, peer: Peer, join_code: str, label: str) -> None:
        client = PeerClient(
            device_id=self.identity.device_id,
            device_name=self.identity.device_name,
            user_name=self.identity.user_name,
        )
        try:
            with client.open(peer.address, peer.announcement.port) as connection:
                info = client.info(connection)
                self.join_attempt = JoinAttempt(
                    label, "waiting", f"{info.user_name} onayı bekleniyor…"
                )
                result = client.join(connection, join_code, info.repo_id, wait_for_approval=60)
        except ProtocolError as e:
            self.join_attempt = JoinAttempt(label, "failed", e.message)
            return
        except NETWORK_ERRORS as e:
            self.join_attempt = JoinAttempt(
                label, "failed", f"Bağlanılamadı: {describe_network_error(e)}"
            )
            return
        except Exception as e:  # noqa: BLE001 - otherwise "Bağlanılıyor…" never ends
            self.join_attempt = JoinAttempt(label, "failed", f"Beklenmeyen hata: {e}")
            return

        if result.status is JoinStatus.APPROVED:
            self.session = (peer, result.device_token)
            self.start_lock_sync()
            self.join_attempt = JoinAttempt(label, "joined", "Katıldın.")
        elif result.status is JoinStatus.REJECTED:
            self.join_attempt = JoinAttempt(label, "failed", "Karşı taraf izin vermedi.")
        else:
            self.join_attempt = JoinAttempt(
                label, "failed", "Onay gelmedi. Arkadaşın 'İzin ver'e basmamış olabilir."
            )

    def clear_join_attempt(self) -> None:
        self.join_attempt = None

    # -- transfer -------------------------------------------------------------------------

    @property
    def can_transfer(self) -> bool:
        return self.session is not None and not (self.transfer and self.transfer.running)

    def start_download(self, destination: Path | None = None) -> None:
        """Fetch the project from the host we joined.

        `destination` is given when we do not have the project yet: the files have to land
        somewhere, and only the person knows where.
        """
        if self.session is None:
            return
        self._cancel_transfer = threading.Event()
        self.transfer = TransferState("hazirlik", "Bağlanılıyor…", running=True)
        threading.Thread(
            target=self._download_worker, args=(destination,), daemon=True, name="sync"
        ).start()

    def start_upload(self) -> None:
        """Send our commits to the host we joined."""
        if self.session is None or not self.is_open:
            return
        self._cancel_transfer = threading.Event()
        self.transfer = TransferState("hazirlik", "Bağlanılıyor…", running=True)
        threading.Thread(target=self._upload_worker, daemon=True, name="push").start()

    def _upload_worker(self) -> None:
        assert self.session is not None
        peer, token = self.session
        repo = self.require_repo()
        client = PeerClient(
            device_id=self.identity.device_id,
            device_name=self.identity.device_name,
            user_name=self.identity.user_name,
        )

        def report(progress: SyncProgress) -> None:
            done = progress.bytes_done / 1024 / 1024
            total = progress.bytes_total / 1024 / 1024
            self.transfer = TransferState(
                stage=progress.stage,
                message=(
                    "Kayıtlar gönderiliyor"
                    if progress.stage == "kayit"
                    else f"Gönderiliyor — {progress.objects_done}/{progress.objects_total} dosya"
                ),
                fraction=progress.fraction,
                detail=f"{done:.1f} / {total:.1f} MB",
                running=True,
            )

        try:
            with client.open(peer.address, peer.announcement.port) as connection:
                plan = plan_push(repo, client, connection, token)
                if not plan.has_work:
                    self.transfer = TransferState(
                        "bitti", plan.describe(), fraction=1.0, running=False, ok=True
                    )
                    return
                result = push(
                    repo, plan, client, connection, token, report, self._cancel_transfer
                )
                if result.diverged:
                    # They moved on while we were working. Usually on other files — then
                    # our work goes on top of theirs and is sent again, nobody asked.
                    merged = self._merge_or_ask(client, connection, token, report)
                    if merged is not None and merged.ok:
                        result = push(
                            repo,
                            plan_push(repo, client, connection, token),
                            client,
                            connection,
                            token,
                            report,
                            self._cancel_transfer,
                        )
                        if result.ok:
                            result.message = (
                                "Arada gelen değişikliklerle birleştirildi ve gönderildi."
                            )
                    elif merged is not None:
                        result = merged
        except ProtocolError as e:
            self.transfer = TransferState("bitti", e.message, running=False, ok=False)
            return
        except NETWORK_ERRORS as e:
            self.transfer = TransferState(
                "bitti", f"Bağlantı koptu: {describe_network_error(e)}", running=False, ok=False
            )
            return
        except Exception as e:  # noqa: BLE001 - a dead worker would leave the bar spinning forever
            self.transfer = TransferState(
                "bitti", f"Beklenmeyen hata: {e}", running=False, ok=False
            )
            return

        self.transfer = TransferState(
            stage="bitti",
            message=result.message,
            fraction=1.0 if result.ok else 0.0,
            detail=f"{result.bytes_transferred / 1024 / 1024:.1f} MB" if result.ok else "",
            running=False,
            ok=result.ok,
        )

    # -- reconciliation -------------------------------------------------------------------

    def _note_divergence(self, client: PeerClient, connection, token: str) -> None:
        """Work out exactly what the two sides disagree about, so it can be shown."""
        try:
            self.divergence = inspect_divergence(self.require_repo(), client, connection, token)
        except Exception:  # noqa: BLE001 - the transfer's own message is the useful one
            self.divergence = None

    def _merge_or_ask(self, client: PeerClient, connection, token: str, report):
        """Resolve a split on our own when nothing is contested; otherwise ask the people.

        Returns the outcome of the automatic merge, or None if a person has to decide — in
        which case `self.divergence` is set and the "Ayrılığı çöz" button appears.
        """
        self._note_divergence(client, connection, token)
        divergence = self.divergence
        if divergence is None or divergence.needs_decisions:
            return None
        self.divergence = None
        return rebase_onto_theirs(
            self.require_repo(),
            divergence,
            client,
            connection,
            token,
            Author(name=self.identity.user_name, device=self.identity.device_name),
            on_progress=report,
            cancel=self._cancel_transfer,
        )

    def start_reconcile(self, choices: dict[str, Side]) -> None:
        if self.session is None or self.divergence is None:
            return
        self._cancel_transfer = threading.Event()
        self.transfer = TransferState("onay", "Karşı tarafın onayı bekleniyor…", running=True)
        threading.Thread(
            target=self._reconcile_worker, args=(choices,), daemon=True, name="reconcile"
        ).start()

    def _reconcile_worker(self, choices: dict[str, Side]) -> None:
        assert self.session is not None and self.divergence is not None
        peer, token = self.session
        repo = self.require_repo()
        client = PeerClient(
            device_id=self.identity.device_id,
            device_name=self.identity.device_name,
            user_name=self.identity.user_name,
        )

        def report(progress: SyncProgress) -> None:
            stage = {
                "onay": "Onay bekleniyor",
                "indirme": "Karşı tarafın dosyaları alınıyor",
                "gonderme": "Kendi dosyaların gönderiliyor",
                "dosyalar": "Dosyalar yerleştiriliyor",
            }.get(progress.stage, progress.stage)
            self.transfer = TransferState(
                stage=progress.stage, message=stage, fraction=progress.fraction, running=True
            )

        try:
            with client.open(peer.address, peer.announcement.port) as connection:
                result = reconcile(
                    repo,
                    self.divergence,
                    choices,
                    client,
                    connection,
                    token,
                    Author(name=self.identity.user_name, device=self.identity.device_name),
                    on_progress=report,
                    cancel=self._cancel_transfer,
                )
        except ProtocolError as e:
            self.transfer = TransferState("bitti", e.message, running=False, ok=False)
            return
        except NETWORK_ERRORS as e:
            self.transfer = TransferState(
                "bitti", f"Bağlantı koptu: {describe_network_error(e)}", running=False, ok=False
            )
            return
        except Exception as e:  # noqa: BLE001 - a dead worker would leave the bar spinning forever
            self.transfer = TransferState("bitti", f"Beklenmeyen hata: {e}", running=False, ok=False)
            return

        if result.ok:
            self.divergence = None
            self.apply_readonly()
        self.transfer = TransferState(
            "bitti", result.message, fraction=1.0 if result.ok else 0.0, running=False, ok=result.ok
        )

    def pending_reconciles(self):
        """Merge proposals waiting on this machine (host side)."""
        return self.server.pending_reconcile_requests() if self.server else ()

    def approve_reconcile(self, plan_id: str) -> None:
        if self.server is not None:
            self.server.approve_reconcile(plan_id)

    def reject_reconcile(self, plan_id: str) -> None:
        if self.server is not None:
            self.server.reject_reconcile(plan_id)

    def behind_count(self) -> int:
        """Commits waiting on this machine that have not been taken onto disk."""
        return self.repo.behind() if self.repo is not None else 0

    def take_updates(self) -> str:
        """Apply already-present commits to the working folder (the host's 'pull')."""
        repo = self.require_repo()
        status = repo.take_updates()
        self.apply_readonly()
        return f"{len(status.added | status.modified)} dosya güncellendi."

    def cancel_download(self) -> None:
        self._cancel_transfer.set()

    def _download_worker(self, destination: Path | None) -> None:
        assert self.session is not None
        peer, token = self.session
        client = PeerClient(
            device_id=self.identity.device_id,
            device_name=self.identity.device_name,
            user_name=self.identity.user_name,
        )

        def report(progress: SyncProgress) -> None:
            done = progress.bytes_done / 1024 / 1024
            total = progress.bytes_total / 1024 / 1024
            stage = {
                "indirme": "İndiriliyor",
                "kayit": "Geçmiş yazılıyor",
                "dosyalar": "Dosyalar yerleştiriliyor",
            }.get(progress.stage, progress.stage)
            self.transfer = TransferState(
                stage=progress.stage,
                message=f"{stage} — {progress.objects_done}/{progress.objects_total} dosya",
                fraction=progress.fraction,
                detail=f"{done:.1f} / {total:.1f} MB",
                running=True,
            )

        started = time.time()

        try:
            with client.open(peer.address, peer.announcement.port) as connection:
                info = client.info(connection)
                self.version_warning = check_version(info.app_version, __version__)

                if destination is not None:
                    repo, result = clone(
                        destination,
                        repo_id=info.repo_id,
                        name=info.repo_name,
                        client=client,
                        connection=connection,
                        token=token,
                        on_progress=report,
                        cancel=self._cancel_transfer,
                    )
                    if repo is not None and result.ok:
                        # Downloading is only offered with nothing open, so there is no
                        # project to close first — closing one used to end the lock
                        # mirroring, and nothing started it again.
                        self.repo = repo
                        remember_project(repo.root, repo.info.name)
                        self.start_lock_sync()
                        # In a session you edit what you lock. Fresh files are not open in
                        # SolidWorks yet, so this is the one safe moment to apply that.
                        self.apply_readonly()
                else:
                    repo = self.require_repo()
                    if repo.info.repo_id != info.repo_id:
                        self.transfer = TransferState(
                            "bitti", "Bu oturum başka bir projeye ait.", running=False, ok=False
                        )
                        return
                    plan = plan_pull(repo, client, connection, token)
                    if plan.relation is Relation.DIVERGED:
                        # We have unsent commits and so do they. On different files that
                        # is not a conflict: take theirs and keep ours on top.
                        merged = self._merge_or_ask(client, connection, token, report)
                        if merged is not None:
                            self.transfer = TransferState(
                                "bitti",
                                merged.message
                                + (" Göndermeyi unutma." if merged.ok else ""),
                                fraction=1.0 if merged.ok else 0.0,
                                running=False,
                                ok=merged.ok,
                            )
                            if merged.ok:
                                self.apply_readonly()
                            return
                    if plan.is_up_to_date:
                        self.transfer = TransferState(
                            "bitti", plan.describe(), fraction=1.0, running=False, ok=True
                        )
                        return
                    result = pull(
                        repo, plan, client, connection, token, report, self._cancel_transfer
                    )
        except ProtocolError as e:
            self.transfer = TransferState("bitti", e.message, running=False, ok=False)
            return
        except NETWORK_ERRORS as e:
            self.transfer = TransferState(
                "bitti", f"Bağlantı koptu: {describe_network_error(e)}", running=False, ok=False
            )
            return
        except Exception as e:  # noqa: BLE001 - a dead worker would leave the bar spinning forever
            self.transfer = TransferState(
                "bitti", f"Beklenmeyen hata: {e}", running=False, ok=False
            )
            return

        self.transfer = TransferState(
            stage="bitti",
            message=result.message,
            fraction=1.0 if result.ok else 0.0,
            detail=(
                f"ağdan {result.bytes_transferred / 1024 / 1024:.1f} MB, "
                f"{max(result.seconds, time.time() - started):.0f} sn"
                if result.ok
                else ""
            ),
            running=False,
            ok=result.ok,
        )

    # -- guidance -------------------------------------------------------------------------

    def next_step(self) -> str:
        """One sentence telling the user what to do now.

        Someone who has never used version control does not know whether they are finished,
        and an interface that never says so leaves them guessing.
        """
        if not self.is_open:
            return "Başlamak için bir montaj klasörü seç."

        repo = self.require_repo()
        behind = self.behind_count()
        if behind:
            return (
                f"Takımdan {behind} yeni değişiklik geldi. Kendi işine devam etmeden önce "
                "“⬇ al” butonuna bas — yoksa iki ayrı sürüm oluşur."
            )
        if repo.commits.head() is None:
            return (
                "Klasördeki dosyalar henüz kaydedilmedi. Aşağıya kısa bir isim yaz "
                "(örn. “başlangıç”) ve Kaydet'e bas."
            )

        held = self.my_locks()
        if held and self.has_changes():
            return (
                f"{len(held)} dosya sende ve değişiklikler kaydedilmedi. İşin bitince "
                "aşağıya ne yaptığını yaz ve kaydet — kilit kendiliğinden kalkar."
            )
        if held:
            return (
                f"{len(held)} dosya sende kilitli. Düzenlemeye başlayabilirsin; "
                "diğer dosyalar kazara değişmesin diye salt-okunur."
            )
        if self.has_changes():
            return "Kaydedilmemiş değişiklikler var. Aşağıya ne yaptığını yaz ve kaydet."
        if not self.is_hosting:
            return (
                "Her şey kayıtlı. Arkadaşlarınla çalışmak için Ağ sekmesinden "
                "oturum başlat."
            )
        return "Her şey kayıtlı. Bir dosya üzerinde çalışmak için seçip Kilitle'ye bas."

    def connected_users(self) -> list[str]:
        """Teammates currently in our session (host side)."""
        return self.server.connected_users() if self.server else []

    def pending_join_requests(self) -> tuple[JoinRequest, ...]:
        return self.server.pending_requests() if self.server else ()

    def approve(self, device_id: str, always_allow: bool = False) -> None:
        if self.server is not None:
            self.server.approve(device_id, always_allow=always_allow)

    def reject(self, device_id: str) -> None:
        if self.server is not None:
            self.server.reject(device_id)
