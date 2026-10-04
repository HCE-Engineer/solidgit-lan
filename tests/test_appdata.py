from __future__ import annotations

import json
import platform

import pytest

from solidgit_lan import appdata


@pytest.fixture(autouse=True)
def no_override(monkeypatch):
    monkeypatch.delenv("SOLIDGIT_APPDATA", raising=False)
    monkeypatch.setattr(appdata, "_resolved", {})


def test_the_location_is_worked_out_once(monkeypatch, tmp_path):
    """Probing the disk on every call meant a file created and deleted several times a
    second inside a OneDrive folder."""
    monkeypatch.setattr(appdata, "app_root", lambda: tmp_path)
    probes: list[object] = []
    real_probe = appdata._is_writable
    monkeypatch.setattr(appdata, "_is_writable", lambda d: probes.append(d) or real_probe(d))

    for _ in range(50):
        appdata.app_data_dir()

    assert len(probes) == 1


def test_data_lives_beside_the_app(monkeypatch, tmp_path):
    """A folder next to the app is one people can actually find."""
    monkeypatch.setattr(appdata, "app_root", lambda: tmp_path)
    assert appdata.app_data_dir() == tmp_path / appdata.DATA_FOLDER_NAME


def test_falls_back_when_the_app_folder_is_read_only(monkeypatch, tmp_path):
    """Installed under Program Files, writing beside the exe is not an option."""
    monkeypatch.setattr(appdata, "app_root", lambda: tmp_path / "app")
    monkeypatch.setattr(appdata, "_is_writable", lambda _: False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))

    assert appdata.app_data_dir() == tmp_path / "local" / "SolidGitLAN"


def test_device_identity_is_stable_across_runs(monkeypatch, tmp_path):
    monkeypatch.setattr(appdata, "app_root", lambda: tmp_path)
    first = appdata.load_device_identity()
    second = appdata.load_device_identity()
    assert first.device_id == second.device_id


def test_copying_the_app_folder_to_another_machine_mints_a_new_device_id(
    monkeypatch, tmp_path
):
    """Two peers claiming the same device id would break locking in a baffling way."""
    monkeypatch.setattr(appdata, "app_root", lambda: tmp_path)
    original = appdata.load_device_identity()

    stored = json.loads((appdata.app_data_dir() / "device.json").read_text(encoding="utf-8"))
    assert stored["machine"] == platform.node()

    # Simulate the file arriving from someone else's computer.
    stored["machine"] = "SOMEONE-ELSES-PC"
    (appdata.app_data_dir() / "device.json").write_text(
        json.dumps(stored), encoding="utf-8"
    )

    assert appdata.load_device_identity().device_id != original.device_id


def test_settings_are_migrated_from_the_old_location(monkeypatch, tmp_path):
    """Otherwise the recent-projects list silently empties after an update."""
    legacy = tmp_path / "local" / "SolidGitLAN"
    (legacy / "devices").mkdir(parents=True)
    (legacy / "device.json").write_text('{"device_id": "abc123"}', encoding="utf-8")
    (legacy / "recent.json").write_text(
        '{"projects": [{"path": "C:/x", "name": "Eski", "opened_at": "2026-01-01"}]}',
        encoding="utf-8",
    )
    (legacy / "devices" / "repo1.json").write_text('{"devices": []}', encoding="utf-8")

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setattr(appdata, "app_root", lambda: tmp_path / "app")

    assert appdata.migrate_from_legacy_location() is True

    target = appdata.app_data_dir()
    assert json.loads((target / "device.json").read_text())["device_id"] == "abc123"
    assert (target / "devices" / "repo1.json").exists()


def test_migration_does_not_overwrite_existing_settings(monkeypatch, tmp_path):
    legacy = tmp_path / "local" / "SolidGitLAN"
    legacy.mkdir(parents=True)
    (legacy / "device.json").write_text('{"device_id": "old"}', encoding="utf-8")

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setattr(appdata, "app_root", lambda: tmp_path / "app")
    appdata.app_data_dir().mkdir(parents=True, exist_ok=True)
    (appdata.app_data_dir() / "device.json").write_text('{"device_id": "new"}', encoding="utf-8")

    assert appdata.migrate_from_legacy_location() is False
    assert json.loads((appdata.app_data_dir() / "device.json").read_text())["device_id"] == "new"
