"""Interface smoke tests, run against Qt's offscreen platform.

These do not check how anything looks. They check that the window actually wires up to the
real core — that opening a workspace populates the file list, that a lock refusal reaches the
banner instead of vanishing, and that committing from the button produces a commit.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytest.importorskip("PySide6", reason="PySide6 is only needed for the desktop interface")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from solidgit_lan.ui.main_window import MainWindow  # noqa: E402
from solidgit_lan.ui.state import AppState, FileStatus, LockState  # noqa: E402


@pytest.fixture(scope="session")
def qt_app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("SOLIDGIT_APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("SOLIDGIT_USER", "Hüseyin")
    monkeypatch.setenv("SOLIDGIT_DEVICE", "HUSEYIN-PC")

    root = tmp_path / "Montaj1"
    (root / "Montaj1").mkdir(parents=True)
    for name, content in (
        ("govde.sldprt", b"GOVDE v1"),
        ("kapak.sldprt", b"KAPAK v1"),
        ("montaj.sldasm", b"MONTAJ v1"),
    ):
        (root / "Montaj1" / name).write_bytes(content)
    return root


def test_window_opens_a_workspace_and_lists_its_files(qt_app, workspace):
    window = MainWindow()
    try:
        window.state.initialise_workspace(workspace)
        window._after_open()

        assert window.project_label.text() == "Montaj1"
        assert window.files_page.tree.topLevelItemCount() == 3
        # Nothing is committed yet, so every file reads as new.
        assert window.files_page.tree.topLevelItem(0).text(0) == "Yeni"
    finally:
        window.state.close_workspace()


def test_commit_button_records_a_commit(qt_app, workspace):
    window = MainWindow()
    try:
        window.state.initialise_workspace(workspace)
        window._after_open()

        window.files_page.message.setPlainText("ilk montaj")
        window.files_page._commit()
        window.refresh()

        assert len(window.state.history()) == 1
        assert window.state.history()[0].message == "ilk montaj"
        # After committing, the working folder matches HEAD, so nothing is left to save.
        assert not window.files_page.commit_button.isEnabled()
    finally:
        window.state.close_workspace()


def test_locking_a_file_makes_the_others_read_only(qt_app, workspace):
    state = AppState()
    state.initialise_workspace(workspace)
    try:
        state.commit("ilk montaj")
        result = state.acquire(["Montaj1/govde.sldprt"])
        assert result.ok

        rows = {row.path: row for row in state.file_rows()}
        assert rows["Montaj1/govde.sldprt"].lock_state is LockState.MINE
        assert rows["Montaj1/kapak.sldprt"].lock_state is LockState.FREE

        import stat

        mode = (workspace / "Montaj1" / "kapak.sldprt").stat().st_mode
        assert not mode & stat.S_IWRITE
        assert (workspace / "Montaj1" / "govde.sldprt").stat().st_mode & stat.S_IWRITE
    finally:
        state.close_workspace()


def test_closing_a_project_leaves_no_read_only_files(qt_app, workspace):
    """Otherwise the user is stranded in SolidWorks with a folder they cannot edit."""
    import stat

    state = AppState()
    state.initialise_workspace(workspace)
    state.commit("ilk montaj")
    state.acquire(["Montaj1/govde.sldprt"])
    state.close_workspace()

    for name in ("govde.sldprt", "kapak.sldprt", "montaj.sldasm"):
        assert (workspace / "Montaj1" / name).stat().st_mode & stat.S_IWRITE


def test_refused_lock_is_explained_in_the_banner(qt_app, workspace):
    """A refusal that only appears in a log would look like the button did nothing."""
    window = MainWindow()
    try:
        window.state.initialise_workspace(workspace)
        window._after_open()
        window.files_page.message.setPlainText("ilk montaj")
        window.files_page._commit()

        # Someone else holds one of the two files we are about to ask for.
        import time

        window.state.locks.acquire(
            paths=["Montaj1/kapak.sldprt"],
            device="ahmet-device",
            user_name="Ahmet",
            base_hashes=window.state.require_repo().local_hashes(),
            latest=window.state.require_repo().latest_hashes(),
            now=time.time(),
        )

        window.refresh()
        tree = window.files_page.tree
        tree.clearSelection()
        for index in range(tree.topLevelItemCount()):
            item = tree.topLevelItem(index)
            if item.data(0, Qt.UserRole) in ("Montaj1/govde.sldprt", "Montaj1/kapak.sldprt"):
                item.setSelected(True)
        assert len(window.files_page.selected_paths()) == 2

        window.files_page._lock_selected()

        message = window.files_page.banner._label.text()
        assert "Ahmet" in message
        assert "ya hep ya hiç" in message
        # All-or-nothing: the file that was free must not have been taken either.
        assert window.state.my_locks() == ()
    finally:
        window.state.close_workspace()


def test_next_step_tells_the_user_what_to_do_at_each_stage(qt_app, workspace):
    """Someone new to version control cannot tell whether they are finished."""
    state = AppState()
    assert "klasörü seç" in state.next_step()

    state.initialise_workspace(workspace)
    try:
        assert "Kaydet" in state.next_step()

        state.commit("ilk montaj")
        assert "oturum başlat" in state.next_step()

        state.acquire(["Montaj1/govde.sldprt"])
        assert "salt-okunur" in state.next_step()

        (workspace / "Montaj1" / "govde.sldprt").write_bytes(b"GOVDE v2")
        assert "kaydedilmedi" in state.next_step()
    finally:
        state.close_workspace()


def test_opening_a_project_records_it_as_recent(qt_app, workspace):
    from solidgit_lan.appdata import recent_projects

    state = AppState()
    state.initialise_workspace(workspace, name="Sasi")
    state.close_workspace()

    recent = recent_projects()
    assert [p.name for p in recent] == ["Sasi"]
    assert Path(recent[0].path) == workspace.resolve()


def test_network_page_hides_developer_details_until_asked(qt_app, workspace):
    """Ports and fingerprints make a healthy screen look like an error report."""
    window = MainWindow()
    try:
        window.state.initialise_workspace(workspace)
        window._after_open()
        page = window.network_page
        page.refresh()

        # isHidden rather than isVisible: the window is never shown in tests, so isVisible
        # is False for everything regardless of what the page decided.
        assert page.advanced_body.isHidden()
        assert page.code_value.isHidden()  # nothing to show before hosting

        page.advanced_toggle.setChecked(True)
        page._toggle_advanced()
        assert not page.advanced_body.isHidden()
    finally:
        window.state.close_workspace()


def test_join_button_is_dead_until_a_session_is_selected(qt_app, workspace):
    window = MainWindow()
    try:
        window.state.initialise_workspace(workspace)
        window._after_open()
        page = window.network_page
        page.refresh()

        assert not page.join_button.isEnabled()
        # The empty row is a placeholder, not something you can join.
        assert page.sessions.topLevelItemCount() == 1
        page.sessions.topLevelItem(0).setSelected(True)
        assert page._selected_session() is None
        assert not page.join_button.isEnabled()
    finally:
        window.state.close_workspace()


def test_session_recording_captures_actions_and_refusals(qt_app, workspace, monkeypatch):
    """The whole point of recording is that a refusal leaves evidence behind."""
    import json
    import time

    from solidgit_lan.telemetry import SessionRecorder

    recorder = SessionRecorder.create()
    window = MainWindow(recorder=recorder)
    try:
        window.state.initialise_workspace(workspace)
        window._after_open()
        window.files_page.message.setPlainText("ilk montaj")
        window.files_page._commit()

        window.state.locks.acquire(
            paths=["Montaj1/kapak.sldprt"],
            device="ahmet-device",
            user_name="Ahmet",
            base_hashes=window.state.require_repo().local_hashes(),
            latest=window.state.require_repo().latest_hashes(),
            now=time.time(),
        )
        window.refresh()
        for index in range(window.files_page.tree.topLevelItemCount()):
            window.files_page.tree.topLevelItem(index).setSelected(True)
        window.files_page._lock_selected()
    finally:
        window.state.close_workspace()
        recorder.close()

    events = [
        json.loads(line)
        for line in recorder.path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    kinds = [e["kind"] for e in events]

    assert "workspace_opened" in kinds
    assert "commit" in kinds
    assert "lock_refused" in kinds

    refusal = next(e for e in events if e["kind"] == "lock_refused")
    assert refusal["reason"] == "LOCK_HELD"
    assert "Montaj1/kapak.sldprt" in refusal["held"]

    commit = next(e for e in events if e["kind"] == "commit")
    assert commit["files"] == 3 and commit["ms"] >= 0


def test_recording_never_captures_what_was_typed(qt_app, workspace):
    """Commit messages are the user's words; only their length is of any use here."""
    import json

    from solidgit_lan.telemetry import SessionRecorder

    secret = "gizli proje kod adi ATMACA"
    recorder = SessionRecorder.create()
    window = MainWindow(recorder=recorder)
    try:
        window.state.initialise_workspace(workspace)
        window._after_open()
        window.files_page.message.setPlainText(secret)
        window.files_page._commit()
    finally:
        window.state.close_workspace()
        recorder.close()

    text = recorder.path.read_text(encoding="utf-8")
    assert secret not in text
    commit = next(
        json.loads(line)
        for line in text.splitlines()
        if line.strip() and json.loads(line)["kind"] == "commit"
    )
    assert commit["message_length"] == len(secret)


def test_reconcile_dialog_blocks_until_every_conflict_is_answered(qt_app):
    """An unanswered conflict would be one resolved by whoever opened the dialog."""
    from solidgit_lan.core.manifest import FileRef, Manifest
    from solidgit_lan.core.reconcile import Side, conflicting_paths, diff_manifests
    from solidgit_lan.net.sync import Divergence
    from solidgit_lan.ui.reconcile_dialog import ReconcileDialog

    def manifest(**files):
        return Manifest({p: FileRef(sha256=d, size=len(d)) for p, d in files.items()})

    base = manifest(govde="v1", kapak="v1")
    mine = manifest(govde="benim", kapak="v1", braket="yeni")
    theirs = manifest(govde="onun", kapak="v2")
    diffs = diff_manifests(mine, theirs, base)

    dialog = ReconcileDialog(
        Divergence("h1", "h2", "b", diffs, conflicting_paths(diffs)), their_name="Hüseyin"
    )
    try:
        dialog.show()
        # Only govde is genuinely contested; the rest resolve on their own.
        assert list(dialog._selectors) == ["govde"]
        assert not dialog.apply_button.isEnabled()

        dialog._selectors["govde"].setCurrentIndex(2)  # "Hüseyin kalsın"
        assert dialog.apply_button.isEnabled()

        dialog._accept()
        assert dialog.choices == {"govde": Side.THEIRS}
    finally:
        dialog.close()


def test_locks_come_from_the_host_when_joined(qt_app, workspace):
    """A mirror must never expire a teammate's lock locally — it is not the authority."""
    import time as time_module

    from solidgit_lan.core.locks import Lock

    state = AppState()
    state.initialise_workspace(workspace)
    try:
        state.session = ("peer-placeholder", "token")
        state._locks.locks["Montaj1/govde.sldprt"] = Lock(
            path="Montaj1/govde.sldprt",
            device="ahmet-device",
            user_name="Ahmet",
            acquired_at=0.0,
            expires_at=time_module.time() - 1,  # already stale in our mirror
        )

        state.expire_locks()

        # Still shown as held: only the coordinator gets to decide it has lapsed.
        assert "Montaj1/govde.sldprt" in state.locks.locks
    finally:
        state.session = None
        state.close_workspace()


def test_a_file_can_be_restored_from_history(qt_app, workspace):
    """The commit button promises a way back; this is that way back."""
    state = AppState()
    state.initialise_workspace(workspace)
    try:
        first = state.commit("ilk montaj")
        (workspace / "Montaj1" / "govde.sldprt").write_bytes(b"GOVDE v2 - yanlis")
        state.commit("govde bozuldu")

        message = state.restore_file(first.id, "Montaj1/govde.sldprt")

        assert (workspace / "Montaj1" / "govde.sldprt").read_bytes() == b"GOVDE v1"
        assert "geri" in message or "döndü" in message
        # History is not rewritten: the restore is an ordinary change waiting to be saved.
        assert len(state.history()) == 2
        rows = {row.path: row for row in state.file_rows()}
        assert rows["Montaj1/govde.sldprt"].status is FileStatus.MODIFIED

        state.commit("govde geri alindi")
        assert len(state.history()) == 3
    finally:
        state.close_workspace()


def test_restoring_a_file_someone_else_holds_is_refused(qt_app, workspace):
    import time as time_module

    from solidgit_lan.core.repo import RepositoryError

    state = AppState()
    state.initialise_workspace(workspace)
    try:
        first = state.commit("ilk montaj")
        state.session = ("peer-placeholder", "token")  # behave as a shared session
        from solidgit_lan.core.locks import AcquireResult, HeldConflict

        # The host says no: Ahmet holds it.
        state.acquire = lambda paths: AcquireResult(
            ok=False,
            held=(HeldConflict("Montaj1/govde.sldprt", "ahmet", "Ahmet", time_module.time()),),
        )

        with pytest.raises(RepositoryError, match="Ahmet"):
            state.restore_file(first.id, "Montaj1/govde.sldprt")
        assert (workspace / "Montaj1" / "govde.sldprt").read_bytes() == b"GOVDE v1"
    finally:
        state.session = None
        state.close_workspace()


def test_history_keeps_the_file_selection_between_refreshes(qt_app, workspace):
    """It rebuilt itself every second, discarding whatever file you had just clicked."""
    window = MainWindow()
    try:
        window.state.initialise_workspace(workspace)
        window._after_open()
        window.state.commit("ilk montaj")
        window.pages.setCurrentIndex(2)
        window.refresh()

        page = window.history_page
        page.files.topLevelItem(1).setSelected(True)
        assert page.restore_button.isEnabled()

        for _ in range(3):
            window.refresh()

        assert page.files.selectedItems(), "seçim yenilemede kayboldu"
        assert page.restore_button.isEnabled()
    finally:
        window.state.shutdown()


def test_status_reflects_a_modified_file(qt_app, workspace):
    state = AppState()
    state.initialise_workspace(workspace)
    try:
        state.commit("ilk montaj")
        state.acquire(["Montaj1/govde.sldprt"])
        (workspace / "Montaj1" / "govde.sldprt").write_bytes(b"GOVDE v2")

        rows = {row.path: row for row in state.file_rows()}
        assert rows["Montaj1/govde.sldprt"].status is FileStatus.MODIFIED
        assert rows["Montaj1/kapak.sldprt"].status is FileStatus.CLEAN
    finally:
        state.close_workspace()
