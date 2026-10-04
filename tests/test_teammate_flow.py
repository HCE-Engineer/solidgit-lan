"""The teammate's journey, through the window rather than around it.

Every other interface test opens a project first. That hid the most important path in the
whole application: someone who does *not* have the project yet — the exact person a session
exists for — has to be able to find it, join it and download it from a blank window.
"""

from __future__ import annotations

import os
import socket
import time

import pytest

pytest.importorskip("PySide6", reason="PySide6 is only needed for the desktop interface")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from solidgit_lan.ui.main_window import MainWindow  # noqa: E402
from solidgit_lan.ui.state import AppState  # noqa: E402

NETWORK_PAGE = 3


@pytest.fixture(scope="module")
def qt_app():
    return QApplication.instance() or QApplication([])


def free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def wait_for(condition, seconds: float = 20.0, app=None) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if app is not None:
            app.processEvents()
        if condition():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def host(tmp_path, monkeypatch):
    """Hüseyin, with a project, hosting a session."""
    monkeypatch.setenv("SOLIDGIT_APPDATA", str(tmp_path / "host_appdata"))
    monkeypatch.setenv("SOLIDGIT_USER", "Hüseyin")
    monkeypatch.setenv("SOLIDGIT_DEVICE", "HUSEYIN-PC")

    project = tmp_path / "huseyin" / "Sasi Montaji"
    (project / "Montaj1").mkdir(parents=True)
    (project / "Montaj1" / "govde.sldprt").write_bytes(b"GOVDE v1" * 4000)
    (project / "Montaj1" / "kapak.sldprt").write_bytes(b"KAPAK v1" * 2000)

    state = AppState()
    state.initialise_workspace(project, name="Sasi Montaji")
    state.commit("ilk montaj")
    state.start_hosting(free_tcp_port())
    state.server.auto_approve = True
    try:
        yield state
    finally:
        state.shutdown()


@pytest.fixture
def teammate_window(qt_app, tmp_path, monkeypatch, host):
    """Ahmet: a fresh install, no project, nothing open."""
    monkeypatch.setenv("SOLIDGIT_APPDATA", str(tmp_path / "ahmet_appdata"))
    monkeypatch.setenv("SOLIDGIT_USER", "Ahmet")
    monkeypatch.setenv("SOLIDGIT_DEVICE", "AHMET-LAPTOP")

    window = MainWindow()
    try:
        yield window
    finally:
        window.close()


def announce(host: AppState, window: MainWindow) -> None:
    """Deliver the host's beacon by hand: these tests are about the window, not UDP."""
    window.state.registry.observe(host.server.announcement(), "127.0.0.1", time.time())


def test_the_network_page_is_reachable_without_a_project(teammate_window):
    """The bug this file exists for: with nothing open, every page was locked out."""
    window = teammate_window
    assert not window.state.is_open

    network_button = window.nav.button(NETWORK_PAGE)
    assert network_button.isEnabled()

    network_button.click()
    window.refresh()
    assert window.pages.currentIndex() == NETWORK_PAGE, "pencere karşılama ekranına geri itti"


def test_a_window_with_no_project_is_already_listening(teammate_window):
    """Otherwise the session list is empty until you open a project you do not have yet."""
    assert teammate_window.state.beacon is not None


def test_sharing_needs_a_project_and_says_so(teammate_window):
    window = teammate_window
    window.nav.button(NETWORK_PAGE).click()
    window.refresh()

    page = window.network_page
    assert not page.host_button.isEnabled()
    assert "proje" in page.host_explanation.text().lower()


def test_the_welcome_screen_offers_joining(teammate_window):
    window = teammate_window
    window.refresh()
    assert window.pages.currentWidget() is window.welcome

    window.join_button.click()
    window.refresh()
    assert window.pages.currentIndex() == NETWORK_PAGE


def test_a_teammate_can_find_join_and_download_from_a_blank_window(
    teammate_window, host, tmp_path, qt_app
):
    window = teammate_window
    window.nav.button(NETWORK_PAGE).click()
    announce(host, window)
    window.refresh()

    # The session shows up in the list.
    sessions = window.network_page.sessions
    names = [sessions.topLevelItem(i).text(0) for i in range(sessions.topLevelItemCount())]
    assert "Hüseyin" in names

    peer = window.state.nearby_sessions()[0]
    window.state.join(peer, host.server.join_code)
    assert wait_for(lambda: window.state.session is not None, app=qt_app), "katılamadı"

    destination = tmp_path / "ahmet" / "Sasi Montaji"
    window.state.start_download(destination)
    assert wait_for(
        lambda: window.state.transfer is not None and not window.state.transfer.running,
        app=qt_app,
    )
    assert window.state.transfer.ok, window.state.transfer.message

    # The project is now open, and the window has noticed: pages unlocked, files listed.
    window.refresh()
    assert window.state.is_open
    assert all(button.isEnabled() for button in window.nav.buttons())
    assert window.files_page.tree.topLevelItemCount() == 2
    assert (destination / "Montaj1" / "govde.sldprt").read_bytes() == b"GOVDE v1" * 4000

    # Locks keep mirroring after the download replaced "no project" with a real one.
    assert window.state.lock_sync_running


def test_the_approve_button_survives_refreshes(qt_app, host, tmp_path, monkeypatch):
    """It was rebuilt every second, so a click could land on a button being destroyed."""
    from solidgit_lan.net.client import PeerClient

    monkeypatch.setenv("SOLIDGIT_APPDATA", str(tmp_path / "host_window_appdata"))
    window = MainWindow()
    try:
        window.state.shutdown()
        window.state = host  # show the hosting state in a real window
        window.network_page.state = host
        window.nav.button(NETWORK_PAGE).click()

        client = PeerClient(device_id="mehmet", device_name="MEHMET-PC", user_name="Mehmet")
        host.server.auto_approve = False
        with client.open("127.0.0.1", host.server.port) as connection:
            client.join(connection, host.server.join_code, host.require_repo().info.repo_id)

        window.network_page.refresh()
        row = window.network_page.requests_body.itemAt(0).widget()
        for _ in range(5):
            window.network_page.refresh()
        assert window.network_page.requests_body.itemAt(0).widget() is row
    finally:
        window.close()


def test_the_host_cannot_commit_a_file_a_teammate_holds(host, tmp_path):
    """The rule a teammate's push is held to applies to the host's own commits too."""
    from solidgit_lan.core.repo import RepositoryError

    repo = host.require_repo()
    base = repo.local_hashes()
    assert host.server.acquire_locks("ahmet-device", "Ahmet", ["Montaj1/govde.sldprt"], base).ok

    path = repo.root / "Montaj1" / "govde.sldprt"
    path.chmod(0o666)  # made writable by hand — the read-only bit is not the safeguard
    path.write_bytes(b"HUSEYIN yine de degistirdi")

    with pytest.raises(RepositoryError, match="Ahmet"):
        host.commit("izinsiz")
    assert len(host.history()) == 1


def _joined_and_downloaded(window, host, tmp_path, qt_app, folder):
    announce(host, window)
    window.state.join(window.state.nearby_sessions()[0], host.server.join_code)
    assert wait_for(lambda: window.state.session is not None, app=qt_app)
    window.state.start_download(tmp_path / folder / "Sasi Montaji")
    assert wait_for(
        lambda: window.state.transfer is not None and not window.state.transfer.running,
        app=qt_app,
    )
    assert window.state.transfer.ok


def test_sending_after_the_host_moved_on_just_works(teammate_window, host, tmp_path, qt_app):
    """Through the window: Ahmet sends while Hüseyin has saved a different file meanwhile."""
    window = teammate_window
    _joined_and_downloaded(window, host, tmp_path, qt_app, "ahmet-merge")

    assert host.acquire(["Montaj1/kapak.sldprt"]).ok
    (host.require_repo().root / "Montaj1" / "kapak.sldprt").write_bytes(b"HUSEYIN kapak")
    host.commit("kapak revizyonu")

    assert window.state.acquire(["Montaj1/govde.sldprt"]).ok
    (window.state.require_repo().root / "Montaj1" / "govde.sldprt").write_bytes(b"AHMET govde")
    window.state.commit("govde deligi")

    window.state.start_upload()
    assert wait_for(
        lambda: window.state.transfer is not None and not window.state.transfer.running,
        app=qt_app,
    )

    assert window.state.transfer.ok, window.state.transfer.message
    assert "birleştirildi" in window.state.transfer.message
    assert window.state.divergence is None, "boş bir ayrılık için onay ekranı açılmamalı"
    host.take_updates()
    root = host.require_repo().root
    assert (root / "Montaj1" / "govde.sldprt").read_bytes() == b"AHMET govde"
    assert (root / "Montaj1" / "kapak.sldprt").read_bytes() == b"HUSEYIN kapak"


def test_locks_taken_by_the_host_appear_for_the_teammate(
    teammate_window, host, tmp_path, qt_app
):
    window = teammate_window
    announce(host, window)
    peer = window.state.nearby_sessions()[0]
    window.state.join(peer, host.server.join_code)
    assert wait_for(lambda: window.state.session is not None, app=qt_app)
    window.state.start_download(tmp_path / "ahmet2" / "Sasi Montaji")
    assert wait_for(
        lambda: window.state.transfer is not None and not window.state.transfer.running,
        app=qt_app,
    )

    host.acquire(["Montaj1/govde.sldprt"])

    def teammate_sees_it():
        rows = {row.path: row for row in window.state.file_rows()}
        return rows["Montaj1/govde.sldprt"].lock_owner == "Hüseyin"

    assert wait_for(teammate_sees_it, seconds=10, app=qt_app)
