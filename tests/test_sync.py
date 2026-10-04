"""File transfer end to end: a real server, real TLS, real bytes on disk.

The whole promise of the project is "everyone ends up with the same files", so these tests
check the files, not the messages.
"""

from __future__ import annotations

import socket
import threading

import pytest

from solidgit_lan.core.commits import Author, Relation
from solidgit_lan.core.repo import Repository
from solidgit_lan.net import sync
from solidgit_lan.net.client import PeerClient
from solidgit_lan.net.protocol import ErrorCode, JoinStatus, ProtocolError
from solidgit_lan.net.server import CoordinatorServer

HOST_CODE = "SG-4F2A-9C1B"
AUTHOR = Author(name="Hüseyin", device="HUSEYIN-PC")


def free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def host_repo(tmp_path):
    root = tmp_path / "host" / "Montaj1"
    (root / "Montaj1").mkdir(parents=True)
    (root / "Montaj1" / "govde.sldprt").write_bytes(b"GOVDE v1" * 5000)
    (root / "Montaj1" / "kapak.sldprt").write_bytes(b"KAPAK v1" * 3000)
    (root / "Montaj1" / "montaj.sldasm").write_bytes(b"MONTAJ v1" * 4000)
    repo = Repository.initialise(root, name="Montaj1")
    repo.commit("ilk montaj", AUTHOR)
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
def joined(server):
    """A client that has completed the handshake, plus its connection and token."""
    client = PeerClient(device_id="ahmet-device", device_name="AHMET-LAPTOP", user_name="Ahmet")
    connection = client.open("127.0.0.1", server.port)
    result = client.join(connection, HOST_CODE, server.repo.info.repo_id)
    assert result.status is JoinStatus.APPROVED
    try:
        yield client, connection, result.device_token
    finally:
        connection.close()


def test_transfer_endpoints_reject_a_device_that_never_joined(server):
    """Being on the Wi-Fi is not the same as being allowed to read the project."""
    client = PeerClient(device_id="stranger", device_name="X", user_name="X")
    with client.open("127.0.0.1", server.port) as connection:
        with pytest.raises(ProtocolError) as raised:
            client.commits_since(connection, token="not-a-real-token")
    assert raised.value.code is ErrorCode.UNAUTHORIZED


def test_clone_reproduces_every_file_byte_for_byte(joined, server, tmp_path):
    client, connection, token = joined

    repo, result = sync.clone(
        tmp_path / "ahmet" / "Montaj1",
        repo_id=server.repo.info.repo_id,
        name="Montaj1",
        client=client,
        connection=connection,
        token=token,
    )

    assert result.ok, result.message
    assert repo is not None
    assert repo.commits.head() == server.repo.commits.head()

    for path, ref in server.repo.head_manifest().entries.items():
        copied = repo.root / path
        assert copied.exists(), f"{path} gelmedi"
        assert copied.read_bytes() == (server.repo.root / path).read_bytes()
        assert repo.objects.has(ref.sha256)

    assert repo.status().is_clean


def test_clone_keeps_the_projects_identity(joined, server, tmp_path):
    """A different repo id would make every later handshake report unrelated projects."""
    client, connection, token = joined
    repo, _ = sync.clone(
        tmp_path / "ahmet2", server.repo.info.repo_id, "Montaj1", client, connection, token
    )
    assert repo is not None
    assert repo.info.repo_id == server.repo.info.repo_id


def test_pull_brings_over_only_what_changed(joined, server, tmp_path):
    client, connection, token = joined
    repo, result = sync.clone(
        tmp_path / "ahmet3", server.repo.info.repo_id, "Montaj1", client, connection, token
    )
    assert result.ok

    # The host edits one part out of three and records it.
    (server.repo.root / "Montaj1" / "govde.sldprt").write_bytes(b"GOVDE v2" * 5000)
    server.repo.commit("govde kalinligi", AUTHOR)

    plan = sync.plan_pull(repo, client, connection, token)
    assert plan.relation is Relation.BEHIND
    assert len(plan.missing) == 1  # only the changed part; the other two are already stored

    result = sync.pull(repo, plan, client, connection, token)

    assert result.ok, result.message
    assert (repo.root / "Montaj1" / "govde.sldprt").read_bytes() == b"GOVDE v2" * 5000
    assert (repo.root / "Montaj1" / "kapak.sldprt").read_bytes() == b"KAPAK v1" * 3000
    assert repo.commits.head() == server.repo.commits.head()


def test_taking_updates_never_overwrites_unsaved_work(joined, server, tmp_path):
    """It used to: the update rewrote every file differing from the target, unsaved edits
    included, so someone mid-edit lost their work without a word."""
    client, connection, token = joined
    repo, _ = sync.clone(
        tmp_path / "ahmet-unsaved", server.repo.info.repo_id, "Montaj1", client, connection, token
    )
    (repo.root / "Montaj1" / "govde.sldprt").write_bytes(b"AHMETIN KAYDEDILMEMIS ISI")

    (server.repo.root / "Montaj1" / "govde.sldprt").write_bytes(b"GOVDE v2" * 5000)
    server.repo.commit("yeni govde", AUTHOR)

    plan = sync.plan_pull(repo, client, connection, token)
    result = sync.pull(repo, plan, client, connection, token)

    assert not result.ok
    assert "kaydet" in result.message
    assert (repo.root / "Montaj1" / "govde.sldprt").read_bytes() == b"AHMETIN KAYDEDILMEMIS ISI"


def test_unsaved_work_on_an_unrelated_file_is_left_alone(joined, server, tmp_path):
    client, connection, token = joined
    repo, _ = sync.clone(
        tmp_path / "ahmet-other", server.repo.info.repo_id, "Montaj1", client, connection, token
    )
    (repo.root / "Montaj1" / "kapak.sldprt").write_bytes(b"AHMETIN KAPAK TASLAGI")

    (server.repo.root / "Montaj1" / "govde.sldprt").write_bytes(b"GOVDE v2" * 5000)
    server.repo.commit("yeni govde", AUTHOR)

    result = sync.pull(repo, sync.plan_pull(repo, client, connection, token), client, connection, token)

    assert result.ok, result.message
    assert (repo.root / "Montaj1" / "govde.sldprt").read_bytes() == b"GOVDE v2" * 5000
    assert (repo.root / "Montaj1" / "kapak.sldprt").read_bytes() == b"AHMETIN KAPAK TASLAGI"


def test_a_file_deleted_upstream_is_removed_here_too(joined, server, tmp_path):
    """Leaving it would make it look new and invite resurrecting a deleted part."""
    client, connection, token = joined
    repo, _ = sync.clone(
        tmp_path / "ahmet-del", server.repo.info.repo_id, "Montaj1", client, connection, token
    )
    (server.repo.root / "Montaj1" / "kapak.sldprt").unlink()
    server.repo.commit("kapak artik yok", AUTHOR)

    assert sync.pull(
        repo, sync.plan_pull(repo, client, connection, token), client, connection, token
    ).ok
    assert not (repo.root / "Montaj1" / "kapak.sldprt").exists()
    assert repo.status().is_clean


def test_nothing_to_do_when_already_in_step(joined, server, tmp_path):
    client, connection, token = joined
    repo, _ = sync.clone(
        tmp_path / "ahmet4", server.repo.info.repo_id, "Montaj1", client, connection, token
    )

    plan = sync.plan_pull(repo, client, connection, token)
    assert plan.relation is Relation.EQUAL
    assert sync.pull(repo, plan, client, connection, token).ok


def test_divergence_is_refused_rather_than_overwritten(joined, server, tmp_path):
    """Two people worked apart. Picking a winner without asking is the one unforgivable move."""
    client, connection, token = joined
    repo, _ = sync.clone(
        tmp_path / "ahmet5", server.repo.info.repo_id, "Montaj1", client, connection, token
    )

    (repo.root / "Montaj1" / "kapak.sldprt").write_bytes(b"AHMET kapak")
    repo.commit("ahmet kapagi degistirdi", Author(name="Ahmet", device="AHMET-LAPTOP"))
    (server.repo.root / "Montaj1" / "kapak.sldprt").write_bytes(b"HUSEYIN kapak")
    server.repo.commit("huseyin kapagi degistirdi", AUTHOR)

    plan = sync.plan_pull(repo, client, connection, token)
    assert plan.relation is Relation.DIVERGED

    result = sync.pull(repo, plan, client, connection, token)

    assert not result.ok
    # Our own work is untouched — nothing was written.
    assert (repo.root / "Montaj1" / "kapak.sldprt").read_bytes() == b"AHMET kapak"


def test_progress_reaches_the_end(joined, server, tmp_path):
    client, connection, token = joined
    seen: list[sync.SyncProgress] = []

    repo, result = sync.clone(
        tmp_path / "ahmet6",
        server.repo.info.repo_id,
        "Montaj1",
        client,
        connection,
        token,
        on_progress=lambda p: seen.append(
            sync.SyncProgress(
                p.objects_total, p.objects_done, p.bytes_total, p.bytes_done, p.current, p.stage
            )
        ),
    )

    assert result.ok
    assert seen, "hiç ilerleme bildirilmedi"
    last = seen[-1]
    assert last.objects_done == last.objects_total
    assert last.bytes_done >= last.bytes_total
    assert {p.stage for p in seen} >= {"indirme", "dosyalar"}


def test_cancelling_leaves_the_workspace_untouched(joined, server, tmp_path):
    """Stopping halfway must never leave a half-updated assembly on disk."""
    client, connection, token = joined
    repo, result = sync.clone(
        tmp_path / "ahmet7", server.repo.info.repo_id, "Montaj1", client, connection, token
    )
    assert result.ok
    before = (repo.root / "Montaj1" / "govde.sldprt").read_bytes()

    (server.repo.root / "Montaj1" / "govde.sldprt").write_bytes(b"GOVDE v2" * 5000)
    server.repo.commit("yeni govde", AUTHOR)

    plan = sync.plan_pull(repo, client, connection, token)
    cancel = threading.Event()
    cancel.set()  # Already cancelled when the transfer starts.

    result = sync.pull(repo, plan, client, connection, token, cancel=cancel)

    assert not result.ok
    assert (repo.root / "Montaj1" / "govde.sldprt").read_bytes() == before


def test_an_interrupted_download_resumes_instead_of_restarting(joined, server, tmp_path):
    """On a hotspot, a transfer that restarts from zero every time never finishes."""
    client, connection, token = joined
    repo, _ = sync.clone(
        tmp_path / "ahmet8", server.repo.info.repo_id, "Montaj1", client, connection, token
    )

    (server.repo.root / "Montaj1" / "govde.sldprt").write_bytes(b"GOVDE v3" * 5000)
    server.repo.commit("v3", AUTHOR)
    plan = sync.plan_pull(repo, client, connection, token)
    digest = plan.missing[0]
    full = server.repo.objects.stored_size(digest)

    # Pretend an earlier attempt got the first half.
    head_bytes = server.repo.objects.path_for(digest).read_bytes()[: full // 2]
    repo.objects.partial_path(digest).write_bytes(head_bytes)

    received: list[int] = []
    total = client.download_object(
        connection,
        token,
        digest,
        repo.objects.partial_path(digest),
        already_have=len(head_bytes),
        on_chunk=received.append,
    )

    assert total == full
    assert sum(received) == full - len(head_bytes), "tamamı yeniden indirilmiş"
    repo.objects.adopt_partial(digest)
    assert repo.objects.has(digest)


def test_a_tampered_blob_is_rejected(joined, server, tmp_path):
    """Content is verified against its own name before it can reach a workspace."""
    from solidgit_lan.core.objects import ObjectStoreError

    client, connection, token = joined
    repo, _ = sync.clone(
        tmp_path / "ahmet9", server.repo.info.repo_id, "Montaj1", client, connection, token
    )

    digest = next(iter(server.repo.head_manifest().digests()))
    repo.objects.partial_path(digest).write_bytes(b"SG1\x00sahte icerik")

    with pytest.raises(ObjectStoreError):
        repo.objects.adopt_partial(digest)
    assert not repo.objects.partial_path(digest).exists()


def test_version_mismatch_is_reported_to_the_person(joined, server):
    client, connection, token = joined
    info = client.info(connection)
    assert info.app_version

    assert sync.check_version(info.app_version, info.app_version) == ""
    warning = sync.check_version("9.9.9", info.app_version)
    assert "9.9.9" in warning and "aynı sürümü" in warning
