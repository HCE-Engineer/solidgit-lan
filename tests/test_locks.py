from __future__ import annotations

import pytest

from solidgit_lan.core.locks import (
    DEFAULT_LEASE_SECONDS,
    PRIORITY_WINDOW_SECONDS,
    LockError,
    LockTable,
    ReleaseReason,
)

AHMET = ("ahmet-pc", "Ahmet")
HUSEYIN = ("huseyin-pc", "Hüseyin")

LATEST = {"a.sldprt": "aaa", "b.sldprt": "bbb", "c.sldprt": "ccc"}
CURRENT = {"a.sldprt": "aaa", "b.sldprt": "bbb", "c.sldprt": "ccc"}


def acquire(table, who, paths, base=None, now=0.0, latest=None):
    device, name = who
    return table.acquire(
        paths=paths,
        device=device,
        user_name=name,
        base_hashes=base if base is not None else CURRENT,
        latest=latest if latest is not None else LATEST,
        now=now,
    )


def test_acquire_grants_when_free_and_current():
    table = LockTable()
    result = acquire(table, AHMET, ["a.sldprt", "b.sldprt"])
    assert result.ok
    assert {lock.path for lock in result.granted} == {"a.sldprt", "b.sldprt"}


def test_multi_lock_is_all_or_nothing():
    """The core deadlock guard: a partly-satisfiable request grants nothing at all."""
    table = LockTable()
    assert acquire(table, AHMET, ["c.sldprt"]).ok

    result = acquire(table, HUSEYIN, ["a.sldprt", "b.sldprt", "c.sldprt"])

    assert not result.ok
    assert result.granted == ()
    assert [c.path for c in result.held] == ["c.sldprt"]
    # a and b must remain free — no partial state to unwind.
    assert table.locked_paths(now=0.0) == frozenset({"c.sldprt"})
    assert acquire(table, HUSEYIN, ["a.sldprt", "b.sldprt"]).ok


def test_cannot_lock_a_file_you_are_behind_on():
    """Under pull-based propagation this is what stops a stale edit clobbering newer work."""
    table = LockTable()
    stale_base = dict(CURRENT, **{"a.sldprt": "old-hash"})

    result = acquire(table, HUSEYIN, ["a.sldprt"], base=stale_base)

    assert not result.ok
    assert result.error_code == "LOCK_STALE"
    assert result.stale[0].your_hash == "old-hash"
    assert result.stale[0].latest_hash == "aaa"
    assert table.locked_paths(now=0.0) == frozenset()


def test_new_file_not_yet_in_history_is_lockable():
    table = LockTable()
    result = acquire(table, AHMET, ["brand-new.sldprt"], base={"brand-new.sldprt": "zzz"})
    assert result.ok


def test_reacquire_by_same_owner_keeps_original_acquired_at():
    table = LockTable()
    first = acquire(table, AHMET, ["a.sldprt"], now=100.0)
    second = acquire(table, AHMET, ["a.sldprt"], now=500.0)
    assert second.ok
    assert second.granted[0].acquired_at == first.granted[0].acquired_at == 100.0
    assert second.granted[0].expires_at == 500.0 + DEFAULT_LEASE_SECONDS


def test_release_by_wrong_owner_is_refused():
    table = LockTable()
    acquire(table, AHMET, ["a.sldprt"])
    with pytest.raises(LockError, match="held by Ahmet"):
        table.release(["a.sldprt"], device=HUSEYIN[0], now=0.0)


def test_lease_expires_so_a_dead_machine_cannot_hold_a_part_hostage():
    table = LockTable()
    acquire(table, AHMET, ["a.sldprt"], now=0.0)

    assert table.holder("a.sldprt", now=DEFAULT_LEASE_SECONDS - 1) is not None
    expired = table.expire(now=DEFAULT_LEASE_SECONDS + 1)

    assert [lock.path for lock in expired] == ["a.sldprt"]
    assert acquire(table, HUSEYIN, ["a.sldprt"], now=DEFAULT_LEASE_SECONDS + 1).ok


def test_renew_extends_the_lease():
    table = LockTable()
    acquire(table, AHMET, ["a.sldprt"], now=0.0)
    table.renew(device=AHMET[0], now=1000.0)
    assert table.holder("a.sldprt", now=DEFAULT_LEASE_SECONDS + 500) is not None


def test_queued_peer_gets_an_exclusive_window_after_release():
    """Otherwise "it's free now" is a race won by whoever polls fastest."""
    table = LockTable()
    acquire(table, AHMET, ["a.sldprt"], now=0.0)
    assert table.enqueue("a.sldprt", *HUSEYIN) == 1

    table.release(["a.sldprt"], device=AHMET[0], now=100.0, reason=ReleaseReason.COMMITTED)

    third = ("mehmet-pc", "Mehmet")
    blocked = acquire(table, third, ["a.sldprt"], now=110.0)
    assert not blocked.ok
    assert blocked.error_code == "LOCK_RESERVED"
    assert blocked.reserved[0].reserved_for_name == "Hüseyin"

    assert acquire(table, HUSEYIN, ["a.sldprt"], now=110.0).ok


def test_priority_window_lapses_if_unused():
    table = LockTable()
    acquire(table, AHMET, ["a.sldprt"], now=0.0)
    table.enqueue("a.sldprt", *HUSEYIN)
    table.release(["a.sldprt"], device=AHMET[0], now=100.0)

    later = 100.0 + PRIORITY_WINDOW_SECONDS + 1
    assert acquire(table, ("mehmet-pc", "Mehmet"), ["a.sldprt"], now=later).ok


def test_soft_claim_announces_without_blocking():
    """Opening a file warns others immediately; it must not stop anyone locking it."""
    table = LockTable()
    table.soft_claim("a.sldprt", *AHMET, now=0.0)

    assert table.soft_claim_for("a.sldprt").user_name == "Ahmet"
    assert acquire(table, HUSEYIN, ["a.sldprt"]).ok


def test_force_release_hands_the_lock_over():
    table = LockTable()
    acquire(table, AHMET, ["a.sldprt"], now=0.0)
    removed = table.force_release("a.sldprt", now=10.0)
    assert removed.user_name == "Ahmet"
    assert acquire(table, HUSEYIN, ["a.sldprt"], now=10.0).ok


def test_table_survives_a_round_trip_through_json():
    """Peers mirror the table, and a new coordinator rebuilds it from this after failover."""
    table = LockTable()
    acquire(table, AHMET, ["a.sldprt"], now=0.0)
    table.enqueue("a.sldprt", *HUSEYIN)

    restored = LockTable.from_json(table.to_json())

    assert restored.holder("a.sldprt", now=0.0).user_name == "Ahmet"
    assert restored.queue_for("a.sldprt") == (HUSEYIN,)
