"""Two people worked apart and now have to agree.

Binary CAD files cannot be merged by any algorithm, so the resolution is people choosing —
and both of them have to approve the same set of choices before anything is written.
"""

from __future__ import annotations

import socket
import threading

import pytest

from solidgit_lan.core.commits import Author
from solidgit_lan.core.reconcile import DiffStatus, Side
from solidgit_lan.core.repo import Repository
from solidgit_lan.net import sync
from solidgit_lan.net.client import PeerClient
from solidgit_lan.net.protocol import JoinStatus, ProtocolError
from solidgit_lan.net.server import CoordinatorServer

HOST_CODE = "SG-4F2A-9C1B"
HUSEYIN = Author(name="Hüseyin", device="HUSEYIN-PC")
AHMET = Author(name="Ahmet", device="AHMET-LAPTOP")


def free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def diverged(tmp_path):
    """A host and a peer that each committed different work from the same starting point."""
    root = tmp_path / "host" / "Montaj1"
    (root / "Montaj1").mkdir(parents=True)
    (root / "Montaj1" / "govde.sldprt").write_bytes(b"GOVDE v1" * 500)
    (root / "Montaj1" / "kapak.sldprt").write_bytes(b"KAPAK v1" * 500)
    (root / "Montaj1" / "mil.sldprt").write_bytes(b"MIL v1" * 500)
    host_repo = Repository.initialise(root, name="Montaj1")
    host_repo.commit("ilk montaj", HUSEYIN)

    server = CoordinatorServer(
        repo=host_repo,
        user_name="Hüseyin",
        device_name="HUSEYIN-PC",
        device_id="host-device",
        port=free_tcp_port(),
        join_code=HOST_CODE,
        auto_approve=True,
        devices_path=tmp_path / "appdata" / "devices.json",
    )
    server.start(host="127.0.0.1", discovery=False)

    client = PeerClient(device_id="ahmet-device", device_name="AHMET-LAPTOP", user_name="Ahmet")
    connection = client.open("127.0.0.1", server.port)
    result = client.join(connection, HOST_CODE, host_repo.info.repo_id)
    assert result.status is JoinStatus.APPROVED
    token = result.device_token

    repo, cloned = sync.clone(
        tmp_path / "ahmet", host_repo.info.repo_id, "Montaj1", client, connection, token
    )
    assert cloned.ok

    # Now they work apart. Both touch govde; each also does something of their own.
    (repo.root / "Montaj1" / "govde.sldprt").write_bytes(b"AHMET govde")
    (repo.root / "Montaj1" / "braket.sldprt").write_bytes(b"AHMET braket")
    repo.commit("ahmet: govde + braket", AHMET)

    (host_repo.root / "Montaj1" / "govde.sldprt").write_bytes(b"HUSEYIN govde")
    (host_repo.root / "Montaj1" / "kapak.sldprt").write_bytes(b"HUSEYIN kapak")
    host_repo.commit("huseyin: govde + kapak", HUSEYIN)

    try:
        yield repo, client, connection, token, server
    finally:
        connection.close()
        server.stop()


def test_only_the_real_conflict_needs_a_person(diverged):
    repo, client, connection, token, server = diverged

    divergence = sync.inspect_divergence(repo, client, connection, token)

    assert divergence is not None
    assert divergence.base is not None
    # Both edited govde; everything else was touched by only one of them.
    assert divergence.conflicts == ("Montaj1/govde.sldprt",)
    by_path = {d.path: d.status for d in divergence.diffs}
    assert by_path["Montaj1/braket.sldprt"] is DiffStatus.ADDED_BY_MINE
    assert by_path["Montaj1/kapak.sldprt"] is DiffStatus.MODIFIED_BY_THEIRS
    assert by_path["Montaj1/mil.sldprt"] is DiffStatus.SAME


def test_an_undecided_conflict_is_refused(diverged):
    """No quiet default: an unanswered conflict is one resolved by whoever proposed it."""
    repo, client, connection, token, server = diverged
    divergence = sync.inspect_divergence(repo, client, connection, token)

    result = sync.reconcile(
        repo, divergence, {}, client, connection, token, AHMET, wait_seconds=1
    )

    assert not result.ok
    assert "undecided" in result.message or "karara" in result.message.lower()


def test_nothing_is_written_until_the_other_side_agrees(diverged):
    repo, client, connection, token, server = diverged
    divergence = sync.inspect_divergence(repo, client, connection, token)
    host_tip_before = server.repo.commits.tip()

    # Nobody approves; the proposal simply times out.
    result = sync.reconcile(
        repo,
        divergence,
        {"Montaj1/govde.sldprt": Side.MINE},
        client,
        connection,
        token,
        AHMET,
        wait_seconds=2,
    )

    assert not result.ok
    assert server.repo.commits.tip() == host_tip_before
    assert (repo.root / "Montaj1" / "govde.sldprt").read_bytes() == b"AHMET govde"


def test_a_rejected_proposal_changes_nothing(diverged):
    repo, client, connection, token, server = diverged
    divergence = sync.inspect_divergence(repo, client, connection, token)
    before = (repo.root / "Montaj1" / "govde.sldprt").read_bytes()

    def reject_when_asked(plan, user_name):
        server.reject_reconcile(plan.plan_id)

    server.on_reconcile_request = reject_when_asked

    result = sync.reconcile(
        repo,
        divergence,
        {"Montaj1/govde.sldprt": Side.THEIRS},
        client,
        connection,
        token,
        AHMET,
        wait_seconds=5,
    )

    assert not result.ok
    assert "onaylamadı" in result.message
    assert (repo.root / "Montaj1" / "govde.sldprt").read_bytes() == before


def test_an_approved_merge_keeps_both_sides_work(diverged):
    repo, client, connection, token, server = diverged
    divergence = sync.inspect_divergence(repo, client, connection, token)
    server.on_reconcile_request = lambda plan, user: server.approve_reconcile(plan.plan_id)

    result = sync.reconcile(
        repo,
        divergence,
        {"Montaj1/govde.sldprt": Side.THEIRS},  # Hüseyin's body wins the contested file
        client,
        connection,
        token,
        AHMET,
        wait_seconds=15,
    )

    assert result.ok, result.message
    merged = repo.commits.read(result.head)
    assert merged.is_reconciliation
    assert set(merged.parents) == {divergence.our_head, divergence.their_tip}

    # The contested file went the way it was decided; everything else survived.
    assert (repo.root / "Montaj1" / "govde.sldprt").read_bytes() == b"HUSEYIN govde"
    assert (repo.root / "Montaj1" / "kapak.sldprt").read_bytes() == b"HUSEYIN kapak"
    assert (repo.root / "Montaj1" / "braket.sldprt").read_bytes() == b"AHMET braket"

    assert server.repo.commits.tip() == merged.id
    server.repo.take_updates()
    for path in merged.manifest.entries:
        assert (server.repo.root / path).read_bytes() == (repo.root / path).read_bytes()


def test_the_losing_version_is_still_recoverable(diverged):
    """A wrong choice must be a decision, not a deletion."""
    repo, client, connection, token, server = diverged
    divergence = sync.inspect_divergence(repo, client, connection, token)
    server.on_reconcile_request = lambda plan, user: server.approve_reconcile(plan.plan_id)

    ahmet_version = repo.commits.read(divergence.our_head).manifest.get("Montaj1/govde.sldprt")

    result = sync.reconcile(
        repo,
        divergence,
        {"Montaj1/govde.sldprt": Side.THEIRS},
        client,
        connection,
        token,
        AHMET,
        wait_seconds=15,
    )
    assert result.ok

    # Ahmet's rejected body is still in the store, reachable from his own commit.
    assert repo.objects.has(ahmet_version.sha256)
    assert repo.objects.get_bytes(ahmet_version.sha256) == b"AHMET govde"


def test_a_merge_cannot_be_pushed_without_an_approval(diverged):
    """Otherwise the second signature is decorative."""
    repo, client, connection, token, server = diverged
    divergence = sync.inspect_divergence(repo, client, connection, token)

    from solidgit_lan.core.commits import build_commit
    from solidgit_lan.core.reconcile import apply_plan, draft_plan

    plan = draft_plan(
        divergence.diffs,
        divergence.our_head,
        divergence.their_tip,
        choices={"Montaj1/govde.sldprt": Side.MINE},
    )
    forged = build_commit(
        parents=(divergence.our_head, divergence.their_tip),
        lamport=99,
        author=AHMET,
        wall_clock="2026-01-01T00:00:00+03:00",
        message="onaysiz birlestirme",
        manifest=apply_plan(plan, divergence.diffs),
    )

    with pytest.raises(ProtocolError, match="onaylanmış"):
        client.push_commit(connection, token, forged, plan_id="uydurma-plan")

    assert not server.repo.commits.has(forged.id)


def test_cancelling_while_waiting_changes_nothing(diverged):
    repo, client, connection, token, server = diverged
    divergence = sync.inspect_divergence(repo, client, connection, token)
    cancel = threading.Event()
    cancel.set()

    result = sync.reconcile(
        repo,
        divergence,
        {"Montaj1/govde.sldprt": Side.MINE},
        client,
        connection,
        token,
        AHMET,
        wait_seconds=10,
        cancel=cancel,
    )

    assert not result.ok
    assert repo.commits.head() == divergence.our_head
