"""Sending work back: a teammate edits a part and the host receives it.

Download alone is only half of "everyone has the same files" — without this, anyone who is
not the host can only ever consume.
"""

from __future__ import annotations

import socket

import pytest

from solidgit_lan.core.commits import Author
from solidgit_lan.core.repo import Repository, RepositoryError
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
def host_repo(tmp_path):
    root = tmp_path / "host" / "Montaj1"
    (root / "Montaj1").mkdir(parents=True)
    (root / "Montaj1" / "govde.sldprt").write_bytes(b"GOVDE v1" * 2000)
    (root / "Montaj1" / "kapak.sldprt").write_bytes(b"KAPAK v1" * 1000)
    repo = Repository.initialise(root, name="Montaj1")
    repo.commit("ilk montaj", HUSEYIN)
    return repo


@pytest.fixture
def server(host_repo, tmp_path):
    coordinator = CoordinatorServer(
        repo=host_repo,
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


@pytest.fixture
def ahmet(server, tmp_path):
    """Ahmet has joined and downloaded the project."""
    client = PeerClient(device_id="ahmet-device", device_name="AHMET-LAPTOP", user_name="Ahmet")
    connection = client.open("127.0.0.1", server.port)
    result = client.join(connection, HOST_CODE, server.repo.info.repo_id)
    assert result.status is JoinStatus.APPROVED
    token = result.device_token

    repo, sync_result = sync.clone(
        tmp_path / "ahmet" / "Montaj1",
        server.repo.info.repo_id,
        "Montaj1",
        client,
        connection,
        token,
    )
    assert sync_result.ok
    try:
        yield repo, client, connection, token
    finally:
        connection.close()


def test_a_teammates_edit_reaches_the_host(ahmet, server):
    repo, client, connection, token = ahmet

    (repo.root / "Montaj1" / "govde.sldprt").write_bytes(b"AHMET govde" * 2000)
    commit = repo.commit("govde deligi buyutuldu", AHMET)

    plan = sync.plan_push(repo, client, connection, token)
    assert plan.has_work
    assert len(plan.commits) == 1

    result = sync.push(repo, plan, client, connection, token)

    assert result.ok, result.message
    assert server.repo.commits.has(commit.id)
    assert server.repo.commits.tip() == commit.id
    assert server.repo.objects.has(commit.manifest.get("Montaj1/govde.sldprt").sha256)


def test_the_hosts_files_are_not_changed_underneath_them(ahmet, server):
    """Propagation is by pull: nobody's files change while they may have the assembly open."""
    repo, client, connection, token = ahmet
    before = (server.repo.root / "Montaj1" / "govde.sldprt").read_bytes()

    (repo.root / "Montaj1" / "govde.sldprt").write_bytes(b"AHMET govde" * 2000)
    repo.commit("govde", AHMET)
    sync.push(repo, sync.plan_push(repo, client, connection, token), client, connection, token)

    # The work arrived, but the host's own checkout is untouched until they ask for it.
    assert (server.repo.root / "Montaj1" / "govde.sldprt").read_bytes() == before
    assert server.repo.commits.head() != server.repo.commits.tip()
    assert server.repo.behind() == 1


def test_the_host_takes_the_update_when_ready(ahmet, server):
    repo, client, connection, token = ahmet
    (repo.root / "Montaj1" / "govde.sldprt").write_bytes(b"AHMET govde" * 2000)
    repo.commit("govde", AHMET)
    sync.push(repo, sync.plan_push(repo, client, connection, token), client, connection, token)

    server.repo.take_updates()

    assert (server.repo.root / "Montaj1" / "govde.sldprt").read_bytes() == b"AHMET govde" * 2000
    assert server.repo.behind() == 0
    assert server.repo.commits.head() == server.repo.commits.tip()
    assert server.repo.status().is_clean


def test_pushing_from_a_stale_base_is_refused(ahmet, server):
    """Accepting it would silently drop whatever landed in between."""
    repo, client, connection, token = ahmet

    # Someone else's work reaches the host first.
    (server.repo.root / "Montaj1" / "kapak.sldprt").write_bytes(b"HUSEYIN kapak")
    server.repo.commit("kapak revizyonu", HUSEYIN)

    (repo.root / "Montaj1" / "govde.sldprt").write_bytes(b"AHMET govde" * 2000)
    repo.commit("govde", AHMET)

    plan = sync.plan_push(repo, client, connection, token)
    result = sync.push(repo, plan, client, connection, token)

    assert not result.ok
    assert "Önce" in result.message and "al" in result.message
    # The host kept its own work; nothing was overwritten.
    assert (server.repo.root / "Montaj1" / "kapak.sldprt").read_bytes() == b"HUSEYIN kapak"


def test_a_commit_naming_files_the_host_lacks_is_refused(ahmet, server):
    """History must never point at content the other side does not have."""
    repo, client, connection, token = ahmet
    (repo.root / "Montaj1" / "govde.sldprt").write_bytes(b"AHMET govde" * 2000)
    commit = repo.commit("govde", AHMET)

    with pytest.raises(ProtocolError) as raised:
        client.push_commit(connection, token, commit)  # no upload first

    assert raised.value.code is ErrorCode.OBJECT_MISSING
    assert not server.repo.commits.has(commit.id)


def test_uploaded_content_is_verified_before_it_is_stored(ahmet, server):
    repo, client, connection, token = ahmet
    forged = repo.objects.tmp_dir / "forged.bin"
    forged.write_bytes(b"SG1\x00bu icerik adiyla uyusmuyor")
    digest = "ab" * 32

    with pytest.raises(ProtocolError):
        client.upload_object(connection, token, digest, forged)

    assert not server.repo.objects.has(digest)


def test_committing_while_behind_is_refused(ahmet, server):
    """Turns a painful reconciliation into a one-click update taken beforehand."""
    repo, client, connection, token = ahmet
    (repo.root / "Montaj1" / "govde.sldprt").write_bytes(b"AHMET govde" * 2000)
    repo.commit("govde", AHMET)
    sync.push(repo, sync.plan_push(repo, client, connection, token), client, connection, token)

    # The host still has its old HEAD and now trails the shared front.
    (server.repo.root / "Montaj1" / "kapak.sldprt").write_bytes(b"yeni kapak")
    with pytest.raises(RepositoryError, match="yeni değişiklik var"):
        server.repo.commit("kapak", HUSEYIN)


def test_round_trip_leaves_both_sides_identical(ahmet, server):
    repo, client, connection, token = ahmet

    (repo.root / "Montaj1" / "govde.sldprt").write_bytes(b"AHMET govde" * 2000)
    (repo.root / "Montaj1" / "yeni.sldprt").write_bytes(b"YENI PARCA" * 500)
    repo.commit("govde + yeni parca", AHMET)
    assert sync.push(
        repo, sync.plan_push(repo, client, connection, token), client, connection, token
    ).ok

    server.repo.take_updates()

    for path, ref in repo.head_manifest().entries.items():
        assert (server.repo.root / path).read_bytes() == (repo.root / path).read_bytes()
        assert server.repo.objects.has(ref.sha256)
    assert server.repo.commits.head() == repo.commits.head()


def test_working_on_different_files_at_once_merges_without_asking(ahmet, server):
    """The everyday case locks make possible. It used to need a reconciliation dialog and
    the host's approval, for a "conflict" with nothing in it."""
    repo, client, connection, token = ahmet

    (server.repo.root / "Montaj1" / "kapak.sldprt").write_bytes(b"HUSEYIN kapak")
    server.repo.commit("kapak revizyonu", HUSEYIN)

    (repo.root / "Montaj1" / "govde.sldprt").write_bytes(b"AHMET govde")
    repo.commit("govde deligi", AHMET)

    first = sync.push(repo, sync.plan_push(repo, client, connection, token), client, connection, token)
    assert not first.ok and first.diverged

    divergence = sync.inspect_divergence(repo, client, connection, token)
    assert divergence is not None and not divergence.needs_decisions

    merged = sync.rebase_onto_theirs(repo, divergence, client, connection, token, AHMET)
    assert merged.ok, merged.message

    # Ahmet now has both changes on disk…
    assert (repo.root / "Montaj1" / "kapak.sldprt").read_bytes() == b"HUSEYIN kapak"
    assert (repo.root / "Montaj1" / "govde.sldprt").read_bytes() == b"AHMET govde"

    # …and the second push is an ordinary fast-forward — no approval involved.
    again = sync.push(repo, sync.plan_push(repo, client, connection, token), client, connection, token)
    assert again.ok, again.message
    tip = server.repo.commits.read(server.repo.commits.tip())
    assert not tip.is_reconciliation, "geçmiş tek çizgi kalmalı"
    assert tip.message == "govde deligi"

    server.repo.take_updates()
    assert (server.repo.root / "Montaj1" / "govde.sldprt").read_bytes() == b"AHMET govde"
    assert (server.repo.root / "Montaj1" / "kapak.sldprt").read_bytes() == b"HUSEYIN kapak"


def test_the_same_file_changed_on_both_sides_still_goes_to_a_person(ahmet, server):
    repo, client, connection, token = ahmet

    (server.repo.root / "Montaj1" / "govde.sldprt").write_bytes(b"HUSEYIN govde")
    server.repo.commit("govde (huseyin)", HUSEYIN)
    (repo.root / "Montaj1" / "govde.sldprt").write_bytes(b"AHMET govde")
    repo.commit("govde (ahmet)", AHMET)

    divergence = sync.inspect_divergence(repo, client, connection, token)
    result = sync.rebase_onto_theirs(repo, divergence, client, connection, token, AHMET)

    assert not result.ok and result.diverged
    assert divergence.conflicts == ("Montaj1/govde.sldprt",)
    assert (repo.root / "Montaj1" / "govde.sldprt").read_bytes() == b"AHMET govde"


def test_nothing_to_push_when_in_step(ahmet, server):
    repo, client, connection, token = ahmet
    plan = sync.plan_push(repo, client, connection, token)
    assert not plan.has_work
    assert sync.push(repo, plan, client, connection, token).ok
