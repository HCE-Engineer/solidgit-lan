"""End-to-end join handshake over real TLS on the loopback interface.

These run a genuine uvicorn server and a genuine httpx client, because the parts most likely
to be wrong — certificate pinning, the proof binding, HTTP status mapping — are exactly the
parts a mocked transport would paper over.
"""

from __future__ import annotations

import socket

import pytest

from solidgit_lan.core.commits import Author
from solidgit_lan.core.repo import Repository
from solidgit_lan.net.client import PeerClient
from solidgit_lan.net.protocol import ErrorCode, JoinStatus, ProtocolError, Role
from solidgit_lan.net.server import CoordinatorServer

HOST_CODE = "SG-4F2A-9C1B"


def free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def repo(tmp_path):
    workspace = tmp_path / "Montaj1"
    (workspace / "Montaj1").mkdir(parents=True)
    (workspace / "Montaj1" / "govde.sldprt").write_bytes(b"GOVDE v1")
    repository = Repository.initialise(workspace, name="Montaj1")
    repository.commit("ilk montaj", Author(name="Hüseyin", device="HUSEYIN-PC"))
    return repository


@pytest.fixture
def devices_path(tmp_path):
    """Approved-device records live outside the workspace, so tests point them at tmp_path."""
    return tmp_path / "appdata" / "devices.json"


@pytest.fixture
def server(repo, devices_path):
    coordinator = CoordinatorServer(
        repo=repo,
        user_name="Hüseyin",
        device_name="HUSEYIN-PC",
        device_id="host-device",
        port=free_tcp_port(),
        join_code=HOST_CODE,
        devices_path=devices_path,
    )
    # Discovery beacons are exercised in test_discovery; leaving them off keeps these tests
    # from broadcasting on the real network of whoever runs them.
    coordinator.start(host="127.0.0.1", discovery=False)
    try:
        yield coordinator
    finally:
        coordinator.stop()


@pytest.fixture
def client():
    return PeerClient(device_id="ahmet-device", device_name="AHMET-LAPTOP", user_name="Ahmet")


def test_server_starts_with_no_console_attached(repo, devices_path, monkeypatch):
    """The launcher runs pythonw.exe, where sys.stdout is None.

    uvicorn's default logging config builds a colour-aware formatter that calls
    sys.stdout.isatty(), so with the default config, starting a session raised
    "Unable to configure formatter 'default'" — but only in the windowed app, never from a
    console, which is why it survived every test until a real session log caught it.
    """
    monkeypatch.setattr("sys.stdout", None)
    monkeypatch.setattr("sys.stderr", None)

    coordinator = CoordinatorServer(
        repo=repo,
        user_name="Hüseyin",
        device_name="HUSEYIN-PC",
        device_id="host-device",
        port=free_tcp_port(),
        join_code=HOST_CODE,
        devices_path=devices_path,
    )
    try:
        coordinator.start(host="127.0.0.1", discovery=False)
        assert coordinator.identity.fingerprint
    finally:
        coordinator.stop()


def test_the_private_key_never_lands_in_the_project(server, repo):
    """The project folder gets copied around; the session's TLS key must not go with it."""
    assert not any(path.suffix == ".key" for path in repo.root.rglob("*"))

    key_dir = server._tls_dir
    assert key_dir is not None and (key_dir / "server.key").exists()
    server.stop()
    assert not key_dir.exists(), "oturum bitince anahtar silinmedi"


def test_info_is_readable_without_joining(server, client):
    """A peer has to be able to knock before it has any credentials."""
    with client.open("127.0.0.1", server.port) as connection:
        info = client.info(connection)

    assert info.repo_name == "Montaj1"
    assert info.role is Role.HOST
    assert info.head == server.repo.commits.head()


def test_certificate_is_pinned_to_the_host(server, client):
    with client.open("127.0.0.1", server.port) as connection:
        assert connection.fingerprint == server.identity.fingerprint
        assert connection.short_fingerprint == server.identity.short_fingerprint


def test_join_with_the_right_code_needs_a_human_then_succeeds(server, client):
    with client.open("127.0.0.1", server.port) as connection:
        result = client.join(connection, HOST_CODE, server.repo.info.repo_id)
        assert result.status is JoinStatus.PENDING

        waiting = server.pending_requests()
        assert [r.user_name for r in waiting] == ["Ahmet"]

        server.approve("ahmet-device")
        approved = client.wait_for_approval(connection, seconds=5)

    assert approved.status is JoinStatus.APPROVED
    assert approved.device_token
    assert server.directory.get("ahmet-device").user_name == "Ahmet"


def test_wrong_join_code_is_refused(server, client):
    with client.open("127.0.0.1", server.port) as connection:
        with pytest.raises(ProtocolError) as raised:
            client.join(connection, "SG-0000-0000", server.repo.info.repo_id)

    assert raised.value.code is ErrorCode.BAD_JOIN_CODE
    assert server.pending_requests() == ()


def test_a_wrong_code_is_caught_before_we_prove_anything(server, client):
    """The host answers the challenge first, so an impostor learns nothing from an attempt."""
    with client.open("127.0.0.1", server.port) as connection:
        with pytest.raises(ProtocolError, match="did not match this host"):
            client.join(connection, "SG-0000-0000", server.repo.info.repo_id)

        # Nothing was recorded against this device: we never sent our proof.
        assert server.directory.get("ahmet-device") is None


def test_joining_a_different_project_is_refused(server, client):
    with client.open("127.0.0.1", server.port) as connection:
        with pytest.raises(ProtocolError) as raised:
            client.join(connection, HOST_CODE, "some-other-repo-id")

    assert raised.value.code is ErrorCode.REPO_MISMATCH


def test_rejected_device_is_told_so(server, client):
    with client.open("127.0.0.1", server.port) as connection:
        client.join(connection, HOST_CODE, server.repo.info.repo_id)
        server.reject("ahmet-device")
        result = client.wait_for_approval(connection, seconds=5)

    assert result.status is JoinStatus.REJECTED
    assert server.directory.get("ahmet-device") is None


def test_always_allow_skips_the_prompt_next_time(server, client):
    with client.open("127.0.0.1", server.port) as connection:
        client.join(connection, HOST_CODE, server.repo.info.repo_id)
        first = server.approve("ahmet-device", always_allow=True)

    with client.open("127.0.0.1", server.port) as connection:
        second = client.join(connection, HOST_CODE, server.repo.info.repo_id)

    assert second.status is JoinStatus.APPROVED
    # The same device keeps its token, so an approval is not silently revoked by a reconnect.
    assert second.device_token == first.device_token


def test_approved_devices_persist_outside_the_workspace(server, client, repo, devices_path):
    """Tokens are bearer credentials; a workspace folder is a thing people copy around."""
    with client.open("127.0.0.1", server.port) as connection:
        client.join(connection, HOST_CODE, server.repo.info.repo_id)
        server.approve("ahmet-device", always_allow=True)

    assert devices_path.exists()
    assert not (repo.repo_dir / "peers.json").exists()

    from solidgit_lan.net.server import DeviceDirectory

    reloaded = DeviceDirectory(devices_path)
    assert reloaded.is_always_allowed("ahmet-device")


def test_approving_a_device_that_never_shook_hands_is_refused(server):
    with pytest.raises(ProtocolError, match="has not completed the handshake"):
        server.approve("a-device-we-never-met")


def test_protocol_mismatch_is_reported_as_upgrade_required(server, client):
    """A teammate on an old build must be told to update, not silently misbehave."""
    from solidgit_lan.net.protocol import JoinRequest

    with client.open("127.0.0.1", server.port) as connection:
        stale = JoinRequest(
            protocol=999,
            repo_id=server.repo.info.repo_id,
            device_id="old-build",
            device_name="OLD-PC",
            user_name="Mehmet",
            client_nonce="cn",
        )
        response = connection.client.post("/v1/join/begin", json=stale.to_json())

    assert response.status_code == 426
    assert response.json()["error"] == ErrorCode.PROTOCOL_MISMATCH.value
