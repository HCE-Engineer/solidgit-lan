"""Locks across two machines.

A lock that only exists on one computer prevents nothing. These check the case the whole
project is built around: two people reaching for the same part at the same time.
"""

from __future__ import annotations

import socket

import pytest

from solidgit_lan.core.commits import Author
from solidgit_lan.core.repo import Repository
from solidgit_lan.net import sync
from solidgit_lan.net.client import PeerClient
from solidgit_lan.net.protocol import ErrorCode, JoinStatus, ProtocolError
from solidgit_lan.net.server import CoordinatorServer

HOST_CODE = "SG-4F2A-9C1B"
HUSEYIN = Author(name="Hüseyin", device="HUSEYIN-PC")
AHMET = Author(name="Ahmet", device="AHMET-LAPTOP")


def free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def server(tmp_path):
    root = tmp_path / "host" / "Montaj1"
    (root / "Montaj1").mkdir(parents=True)
    (root / "Montaj1" / "govde.sldprt").write_bytes(b"GOVDE v1" * 500)
    (root / "Montaj1" / "kapak.sldprt").write_bytes(b"KAPAK v1" * 500)
    repo = Repository.initialise(root, name="Montaj1")
    repo.commit("ilk montaj", HUSEYIN)

    coordinator = CoordinatorServer(
        repo=repo,
        user_name="Hüseyin",
        device_name="HUSEYIN-PC",
        device_id="host-device",
        port=free_tcp_port(),
        join_code=HOST_CODE,
        auto_approve=True,
        devices_path=tmp_path / "appdata" / "devices.json",
    )
    coordinator.start(host="127.0.0.1", discovery=False)
    try:
        yield coordinator
    finally:
        coordinator.stop()


def join(server, device_id: str, user: str):
    client = PeerClient(device_id=device_id, device_name=device_id.upper(), user_name=user)
    connection = client.open("127.0.0.1", server.port)
    result = client.join(connection, HOST_CODE, server.repo.info.repo_id)
    assert result.status is JoinStatus.APPROVED
    return client, connection, result.device_token


@pytest.fixture
def ahmet(server, tmp_path):
    client, connection, token = join(server, "ahmet-device", "Ahmet")
    repo, result = sync.clone(
        tmp_path / "ahmet", server.repo.info.repo_id, "Montaj1", client, connection, token
    )
    assert result.ok
    try:
        yield repo, client, connection, token
    finally:
        connection.close()


def test_a_peers_lock_is_visible_to_the_host(ahmet, server):
    repo, client, connection, token = ahmet

    result = client.acquire_locks(
        connection, token, ["Montaj1/govde.sldprt"], repo.local_hashes()
    )

    assert result.ok
    holder = server.locks.holder("Montaj1/govde.sldprt", now=result.granted[0].acquired_at)
    assert holder is not None and holder.user_name == "Ahmet"


def test_two_people_cannot_hold_the_same_part(ahmet, server, tmp_path):
    """The moment this project exists for."""
    repo, client, connection, token = ahmet
    assert client.acquire_locks(
        connection, token, ["Montaj1/govde.sldprt"], repo.local_hashes()
    ).ok

    mehmet, mehmet_connection, mehmet_token = join(server, "mehmet-device", "Mehmet")
    try:
        second = mehmet.acquire_locks(
            mehmet_connection, mehmet_token, ["Montaj1/govde.sldprt"], repo.local_hashes()
        )
    finally:
        mehmet_connection.close()

    assert not second.ok
    assert second.error_code == "LOCK_HELD"
    assert second.held[0].user_name == "Ahmet"


def test_the_host_and_a_peer_share_one_table(ahmet, server):
    repo, client, connection, token = ahmet
    server.acquire_locks("host-device", "Hüseyin", ["Montaj1/kapak.sldprt"], repo.local_hashes())

    mirror = client.fetch_locks(connection, token)

    assert mirror.holder("Montaj1/kapak.sldprt", now=0.0) is not None
    result = client.acquire_locks(
        connection, token, ["Montaj1/kapak.sldprt"], repo.local_hashes()
    )
    assert not result.ok and result.held[0].user_name == "Hüseyin"


def test_multi_file_requests_stay_all_or_nothing_across_the_network(ahmet, server):
    repo, client, connection, token = ahmet
    server.acquire_locks("host-device", "Hüseyin", ["Montaj1/kapak.sldprt"], repo.local_hashes())

    result = client.acquire_locks(
        connection,
        token,
        ["Montaj1/govde.sldprt", "Montaj1/kapak.sldprt"],
        repo.local_hashes(),
    )

    assert not result.ok
    # The free one must not have been taken either — otherwise both sides sit half-blocked.
    assert server.locks.holder("Montaj1/govde.sldprt", now=0.0) is None


def test_committing_a_file_someone_else_holds_is_refused(ahmet, server):
    """Without this the lock is only a suggestion: the second push would still land."""
    repo, client, connection, token = ahmet
    server.acquire_locks("host-device", "Hüseyin", ["Montaj1/govde.sldprt"], repo.local_hashes())

    (repo.root / "Montaj1" / "govde.sldprt").write_bytes(b"AHMET govde")
    repo.commit("govde", AHMET)

    plan = sync.plan_push(repo, client, connection, token)
    result = sync.push(repo, plan, client, connection, token)

    assert not result.ok
    assert "kilitli" in result.message
    assert server.repo.commits.tip() != repo.commits.head()


def test_a_commit_releases_the_locks_it_used(ahmet, server):
    repo, client, connection, token = ahmet
    assert client.acquire_locks(
        connection, token, ["Montaj1/govde.sldprt"], repo.local_hashes()
    ).ok

    (repo.root / "Montaj1" / "govde.sldprt").write_bytes(b"AHMET govde")
    repo.commit("govde", AHMET)
    assert sync.push(
        repo, sync.plan_push(repo, client, connection, token), client, connection, token
    ).ok

    assert server.locks.holder("Montaj1/govde.sldprt", now=0.0) is None


def test_you_cannot_lock_a_file_you_are_behind_on(ahmet, server):
    """Staleness is measured against the shared front, not against the host's own checkout."""
    repo, client, connection, token = ahmet

    (server.repo.root / "Montaj1" / "kapak.sldprt").write_bytes(b"YENI kapak")
    server.repo.commit("kapak revizyonu", HUSEYIN)

    result = client.acquire_locks(
        connection, token, ["Montaj1/kapak.sldprt"], repo.local_hashes()
    )

    assert not result.ok
    assert result.error_code == "LOCK_STALE"
    assert result.stale[0].path == "Montaj1/kapak.sldprt"


def test_releasing_someone_elses_lock_is_refused(ahmet, server):
    repo, client, connection, token = ahmet
    server.acquire_locks("host-device", "Hüseyin", ["Montaj1/govde.sldprt"], repo.local_hashes())

    with pytest.raises(ProtocolError) as raised:
        client.release_locks(connection, token, ["Montaj1/govde.sldprt"])

    assert raised.value.code is ErrorCode.LOCK_NOT_OWNED
    assert server.locks.holder("Montaj1/govde.sldprt", now=0.0) is not None


def test_a_soft_claim_warns_without_blocking(ahmet, server):
    """Two people opening the same part find out in seconds, not after twenty minutes."""
    repo, client, connection, token = ahmet
    client.soft_claim(connection, token, "Montaj1/govde.sldprt")

    mirror = client.fetch_locks(connection, token)
    assert mirror.soft_claim_for("Montaj1/govde.sldprt").user_name == "Ahmet"

    # It blocks nobody — the host can still take the real lock.
    assert server.acquire_locks(
        "host-device", "Hüseyin", ["Montaj1/govde.sldprt"], repo.local_hashes()
    ).ok


def test_queueing_reserves_the_lock_for_the_next_person(ahmet, server):
    repo, client, connection, token = ahmet
    server.acquire_locks("host-device", "Hüseyin", ["Montaj1/govde.sldprt"], repo.local_hashes())

    assert client.queue_for_lock(connection, token, "Montaj1/govde.sldprt") == 1
    server.release_locks("host-device", ["Montaj1/govde.sldprt"])

    mehmet, mehmet_connection, mehmet_token = join(server, "mehmet-device", "Mehmet")
    try:
        blocked = mehmet.acquire_locks(
            mehmet_connection, mehmet_token, ["Montaj1/govde.sldprt"], repo.local_hashes()
        )
        assert not blocked.ok
        assert blocked.error_code == "LOCK_RESERVED"
        assert blocked.reserved[0].reserved_for_name == "Ahmet"
    finally:
        mehmet_connection.close()

    assert client.acquire_locks(
        connection, token, ["Montaj1/govde.sldprt"], repo.local_hashes()
    ).ok


def test_the_host_can_break_a_lock_and_it_is_recorded(ahmet, server):
    """The escape hatch for a teammate who went home holding a part."""
    repo, client, connection, token = ahmet
    assert client.acquire_locks(
        connection, token, ["Montaj1/govde.sldprt"], repo.local_hashes()
    ).ok

    broken = server.locks.force_release("Montaj1/govde.sldprt", now=0.0)
    server.broadcast_forced_unlock(broken)

    assert broken.user_name == "Ahmet"
    assert server.locks.holder("Montaj1/govde.sldprt", now=0.0) is None

    # Never silent: the owner hears about it on their next mirror poll — exactly once.
    _, forced = client.fetch_lock_state(connection, token)
    assert [entry["path"] for entry in forced] == ["Montaj1/govde.sldprt"]
    _, again = client.fetch_lock_state(connection, token)
    assert again == []
    # And the file is free again.
    assert server.acquire_locks(
        "host-device", "Hüseyin", ["Montaj1/govde.sldprt"], repo.local_hashes()
    ).ok


def test_the_host_knows_who_is_connected(ahmet, server):
    """Beacons cannot say this — a joined peer announces nothing — so it used to read 0."""
    repo, client, connection, token = ahmet
    client.fetch_locks(connection, token)  # what a joined peer does every two seconds

    assert server.connected_users() == ["Ahmet"]
    assert server.connected_users(within=-1.0) == []  # and it lapses when they go quiet


def test_renewing_keeps_a_lock_alive(ahmet, server):
    repo, client, connection, token = ahmet
    assert client.acquire_locks(
        connection, token, ["Montaj1/govde.sldprt"], repo.local_hashes()
    ).ok

    renewed = client.renew_locks(connection, token)

    assert renewed == ["Montaj1/govde.sldprt"]
