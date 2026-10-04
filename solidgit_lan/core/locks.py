"""The lock table.

Binary CAD files cannot be merged, so conflicts are prevented rather than resolved: one
person holds a file at a time. Locking is therefore the one place in the system that needs a
single arbiter, which is why the coordinator exists at all.

Three rules carry most of the design:

* **Multi-path acquisition is all-or-nothing.** Granting 9 of 12 requested locks produces the
  classic deadlock — I hold 9, you hold 3, neither of us can proceed and neither of us can
  see why. Partial success is not a possible outcome here.
* **You cannot lock a file you are behind on.** Otherwise, under the pull-based propagation
  model, you would edit a stale part and silently overwrite the newer version on commit.
* **Locks are leases.** A closed laptop must not hold a part hostage forever.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from collections.abc import Iterable, Mapping

#: A lock survives a working session but not an abandoned machine.
DEFAULT_LEASE_SECONDS = 4 * 60 * 60

#: Renewal heartbeat; a lease outlives several missed beats before expiring.
RENEW_INTERVAL_SECONDS = 60

#: After a release, the first person queued gets an exclusive window to take the lock, so it
#: is not a race won by whoever happens to click fastest.
PRIORITY_WINDOW_SECONDS = 30


class LockError(Exception):
    """Raised on an operation the caller had no right to attempt."""


class ReleaseReason(Enum):
    COMMITTED = "committed"
    CLOSED_WITHOUT_SAVING = "closed_without_saving"
    MANUAL = "manual"
    EXPIRED = "expired"
    FORCED = "forced"


@dataclass(frozen=True)
class Lock:
    path: str
    device: str
    user_name: str
    acquired_at: float
    expires_at: float

    def is_expired(self, now: float) -> bool:
        return now >= self.expires_at

    def to_json(self) -> dict[str, object]:
        return {
            "path": self.path,
            "device": self.device,
            "user_name": self.user_name,
            "acquired_at": self.acquired_at,
            "expires_at": self.expires_at,
        }

    @staticmethod
    def from_json(data: Mapping[str, object]) -> Lock:
        return Lock(
            path=str(data["path"]),
            device=str(data["device"]),
            user_name=str(data["user_name"]),
            acquired_at=float(data["acquired_at"]),  # type: ignore[arg-type]
            expires_at=float(data["expires_at"]),  # type: ignore[arg-type]
        )


@dataclass(frozen=True)
class SoftClaim:
    """Announced the moment a file is opened in SolidWorks, before any edit.

    This blocks nobody. It exists so that two people who open the same part both find out
    within seconds, instead of each working for twenty minutes and one of them losing all of
    it at lock time.
    """

    path: str
    device: str
    user_name: str
    claimed_at: float


@dataclass(frozen=True)
class Reservation:
    """A short exclusive window handed to the next person in a file's queue."""

    path: str
    device: str
    user_name: str
    until: float

    def is_active(self, now: float) -> bool:
        return now < self.until


@dataclass(frozen=True)
class HeldConflict:
    path: str
    device: str
    user_name: str
    since: float

    def to_json(self) -> dict[str, object]:
        return {
            "path": self.path,
            "device": self.device,
            "user_name": self.user_name,
            "since": self.since,
        }

    @staticmethod
    def from_json(data: Mapping[str, object]) -> HeldConflict:
        return HeldConflict(
            path=str(data["path"]),
            device=str(data["device"]),
            user_name=str(data["user_name"]),
            since=float(data.get("since", 0.0)),  # type: ignore[arg-type]
        )


@dataclass(frozen=True)
class StaleConflict:
    path: str
    your_hash: str | None
    latest_hash: str
    latest_by: str

    def to_json(self) -> dict[str, object]:
        return {
            "path": self.path,
            "your_hash": self.your_hash,
            "latest_hash": self.latest_hash,
            "latest_by": self.latest_by,
        }

    @staticmethod
    def from_json(data: Mapping[str, object]) -> StaleConflict:
        raw = data.get("your_hash")
        return StaleConflict(
            path=str(data["path"]),
            your_hash=None if raw is None else str(raw),
            latest_hash=str(data["latest_hash"]),
            latest_by=str(data.get("latest_by", "")),
        )


@dataclass(frozen=True)
class ReservedConflict:
    path: str
    reserved_for: str
    reserved_for_name: str
    until: float

    def to_json(self) -> dict[str, object]:
        return {
            "path": self.path,
            "reserved_for": self.reserved_for,
            "reserved_for_name": self.reserved_for_name,
            "until": self.until,
        }

    @staticmethod
    def from_json(data: Mapping[str, object]) -> ReservedConflict:
        return ReservedConflict(
            path=str(data["path"]),
            reserved_for=str(data["reserved_for"]),
            reserved_for_name=str(data["reserved_for_name"]),
            until=float(data.get("until", 0.0)),  # type: ignore[arg-type]
        )


@dataclass(frozen=True)
class AcquireResult:
    """Outcome of one all-or-nothing acquisition attempt."""

    ok: bool
    granted: tuple[Lock, ...] = ()
    held: tuple[HeldConflict, ...] = ()
    stale: tuple[StaleConflict, ...] = ()
    reserved: tuple[ReservedConflict, ...] = ()

    @property
    def error_code(self) -> str | None:
        """The single code reported to the client, most actionable conflict first."""
        if self.ok:
            return None
        if self.stale:
            return "LOCK_STALE"
        if self.held:
            return "LOCK_HELD"
        return "LOCK_RESERVED"

    def to_json(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "granted": [lock.to_json() for lock in self.granted],
            "held": [c.to_json() for c in self.held],
            "stale": [c.to_json() for c in self.stale],
            "reserved": [c.to_json() for c in self.reserved],
        }

    @staticmethod
    def from_json(data: Mapping[str, object]) -> AcquireResult:
        return AcquireResult(
            ok=bool(data.get("ok", False)),
            granted=tuple(Lock.from_json(raw) for raw in data.get("granted", [])),  # type: ignore[union-attr]
            held=tuple(HeldConflict.from_json(raw) for raw in data.get("held", [])),  # type: ignore[union-attr]
            stale=tuple(StaleConflict.from_json(raw) for raw in data.get("stale", [])),  # type: ignore[union-attr]
            reserved=tuple(
                ReservedConflict.from_json(raw) for raw in data.get("reserved", [])  # type: ignore[union-attr]
            ),
        )


@dataclass
class LockTable:
    """In-memory authority for who holds what. The coordinator owns the only real one."""

    locks: dict[str, Lock] = field(default_factory=dict)
    soft_claims: dict[str, SoftClaim] = field(default_factory=dict)
    queues: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    """path -> ordered list of (device, user_name) waiting for it."""
    reservations: dict[str, Reservation] = field(default_factory=dict)

    # -- queries --------------------------------------------------------------------------

    def holder(self, path: str, now: float) -> Lock | None:
        lock = self.locks.get(path)
        if lock is None or lock.is_expired(now):
            return None
        return lock

    def is_locked_by(self, path: str, device: str, now: float) -> bool:
        lock = self.holder(path, now)
        return lock is not None and lock.device == device

    def locked_paths(self, now: float) -> frozenset[str]:
        return frozenset(p for p, lock in self.locks.items() if not lock.is_expired(now))

    def paths_held_by(self, device: str, now: float) -> tuple[str, ...]:
        return tuple(
            sorted(
                path
                for path, lock in self.locks.items()
                if lock.device == device and not lock.is_expired(now)
            )
        )

    # -- acquisition ----------------------------------------------------------------------

    def acquire(
        self,
        paths: Iterable[str],
        device: str,
        user_name: str,
        base_hashes: Mapping[str, str | None],
        latest: Mapping[str, str],
        now: float,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        latest_authors: Mapping[str, str] | None = None,
    ) -> AcquireResult:
        """Take every requested lock, or none of them.

        `base_hashes` is what the requester has on disk; `latest` is what the newest commit
        says each path should be. A mismatch means they are behind and must update first.

        Nothing is mutated until every path has passed every check, so a rejected request
        leaves the table exactly as it was — there is no partial state to unwind.
        """
        requested = list(dict.fromkeys(paths))  # de-duplicate, preserve order
        if not requested:
            return AcquireResult(ok=True)

        authors = latest_authors or {}
        held: list[HeldConflict] = []
        stale: list[StaleConflict] = []
        reserved: list[ReservedConflict] = []

        for path in requested:
            current = self.holder(path, now)
            if current is not None and current.device != device:
                held.append(
                    HeldConflict(
                        path=path,
                        device=current.device,
                        user_name=current.user_name,
                        since=current.acquired_at,
                    )
                )
                continue

            reservation = self.reservations.get(path)
            if (
                reservation is not None
                and reservation.is_active(now)
                and reservation.device != device
            ):
                reserved.append(
                    ReservedConflict(
                        path=path,
                        reserved_for=reservation.device,
                        reserved_for_name=reservation.user_name,
                        until=reservation.until,
                    )
                )
                continue

            latest_hash = latest.get(path)
            if latest_hash is not None and base_hashes.get(path) != latest_hash:
                stale.append(
                    StaleConflict(
                        path=path,
                        your_hash=base_hashes.get(path),
                        latest_hash=latest_hash,
                        latest_by=authors.get(path, ""),
                    )
                )

        if held or stale or reserved:
            return AcquireResult(
                ok=False, held=tuple(held), stale=tuple(stale), reserved=tuple(reserved)
            )

        granted: list[Lock] = []
        for path in requested:
            lock = Lock(
                path=path,
                device=device,
                user_name=user_name,
                acquired_at=self.locks[path].acquired_at
                if path in self.locks and self.locks[path].device == device
                else now,
                expires_at=now + lease_seconds,
            )
            self.locks[path] = lock
            granted.append(lock)
            self._consume_reservation(path, device)
            self._dequeue(path, device)

        return AcquireResult(ok=True, granted=tuple(granted))

    # -- release / renewal ----------------------------------------------------------------

    def release(
        self,
        paths: Iterable[str],
        device: str,
        now: float,
        reason: ReleaseReason = ReleaseReason.MANUAL,
    ) -> tuple[str, ...]:
        """Give up locks held by `device`. Returns the paths actually released.

        Releasing a path someone else holds is refused outright — silently doing nothing
        would hide a real bug in the caller.
        """
        released: list[str] = []
        for path in dict.fromkeys(paths):
            lock = self.locks.get(path)
            if lock is None:
                continue
            if lock.device != device and reason is not ReleaseReason.FORCED:
                raise LockError(
                    f"'{path}' is held by {lock.user_name}, not by this device."
                )
            del self.locks[path]
            released.append(path)
            self._promote_next_in_queue(path, now)
        return tuple(released)

    def force_release(self, path: str, now: float) -> Lock | None:
        """Administrator override. The owner is always notified; this is never silent."""
        lock = self.locks.pop(path, None)
        if lock is not None:
            self._promote_next_in_queue(path, now)
        return lock

    def renew(
        self,
        device: str,
        now: float,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        paths: Iterable[str] | None = None,
    ) -> tuple[str, ...]:
        """Extend the lease on this device's locks; called on a heartbeat while the app runs."""
        targets = list(paths) if paths is not None else list(self.paths_held_by(device, now))
        renewed: list[str] = []
        for path in targets:
            lock = self.locks.get(path)
            if lock is None or lock.device != device or lock.is_expired(now):
                continue
            self.locks[path] = Lock(
                path=lock.path,
                device=lock.device,
                user_name=lock.user_name,
                acquired_at=lock.acquired_at,
                expires_at=now + lease_seconds,
            )
            renewed.append(path)
        return tuple(renewed)

    def expire(self, now: float) -> tuple[Lock, ...]:
        """Drop leases whose time has run out. This is what unsticks a dead machine."""
        expired = [lock for lock in self.locks.values() if lock.is_expired(now)]
        for lock in expired:
            del self.locks[lock.path]
            self._promote_next_in_queue(lock.path, now)
        for path, reservation in list(self.reservations.items()):
            if not reservation.is_active(now):
                del self.reservations[path]
        return tuple(expired)

    # -- soft claims ----------------------------------------------------------------------

    def soft_claim(self, path: str, device: str, user_name: str, now: float) -> SoftClaim:
        claim = SoftClaim(path=path, device=device, user_name=user_name, claimed_at=now)
        self.soft_claims[path] = claim
        return claim

    def clear_soft_claim(self, path: str, device: str) -> bool:
        claim = self.soft_claims.get(path)
        if claim is None or claim.device != device:
            return False
        del self.soft_claims[path]
        return True

    def soft_claim_for(self, path: str) -> SoftClaim | None:
        return self.soft_claims.get(path)

    # -- queueing -------------------------------------------------------------------------

    def enqueue(self, path: str, device: str, user_name: str) -> int:
        """Ask to be told when `path` frees up. Returns the caller's 1-based position."""
        waiting = self.queues.setdefault(path, [])
        for index, (waiting_device, _) in enumerate(waiting):
            if waiting_device == device:
                return index + 1
        waiting.append((device, user_name))
        return len(waiting)

    def queue_for(self, path: str) -> tuple[tuple[str, str], ...]:
        return tuple(self.queues.get(path, ()))

    def _dequeue(self, path: str, device: str) -> None:
        waiting = self.queues.get(path)
        if not waiting:
            return
        self.queues[path] = [entry for entry in waiting if entry[0] != device]
        if not self.queues[path]:
            del self.queues[path]

    def _promote_next_in_queue(self, path: str, now: float) -> Reservation | None:
        waiting = self.queues.get(path)
        if not waiting:
            self.reservations.pop(path, None)
            return None
        device, user_name = waiting[0]
        reservation = Reservation(
            path=path,
            device=device,
            user_name=user_name,
            until=now + PRIORITY_WINDOW_SECONDS,
        )
        self.reservations[path] = reservation
        return reservation

    def _consume_reservation(self, path: str, device: str) -> None:
        reservation = self.reservations.get(path)
        if reservation is not None and reservation.device == device:
            del self.reservations[path]

    # -- serialisation --------------------------------------------------------------------

    def to_json(self) -> dict[str, object]:
        return {
            "locks": [lock.to_json() for lock in sorted(self.locks.values(), key=lambda l: l.path)],
            "queues": {path: list(waiting) for path, waiting in sorted(self.queues.items())},
            "soft_claims": [
                {
                    "path": claim.path,
                    "device": claim.device,
                    "user_name": claim.user_name,
                    "claimed_at": claim.claimed_at,
                }
                for claim in sorted(self.soft_claims.values(), key=lambda c: c.path)
            ],
        }

    @staticmethod
    def from_json(data: Mapping[str, object]) -> LockTable:
        """Rebuild a table from the wire — used by peers and after coordinator failover.

        Reservations are deliberately not restored: a priority window is seconds-scale live
        state that a new coordinator has no business asserting. Soft claims do come across,
        because a mirrored table is what tells everyone else "Ahmet has this open".
        """
        table = LockTable()
        for raw in data.get("locks", []):  # type: ignore[union-attr]
            lock = Lock.from_json(raw)
            table.locks[lock.path] = lock
        for path, waiting in dict(data.get("queues", {})).items():  # type: ignore[arg-type]
            table.queues[str(path)] = [(str(d), str(n)) for d, n in waiting]
        for raw in data.get("soft_claims", []):  # type: ignore[union-attr]
            claim = SoftClaim(
                path=str(raw["path"]),
                device=str(raw["device"]),
                user_name=str(raw["user_name"]),
                claimed_at=float(raw.get("claimed_at", 0.0)),
            )
            table.soft_claims[claim.path] = claim
        return table
