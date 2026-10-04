"""Settings, logs and device identity — kept in the app's own folder.

Everything lives in a `veri` folder beside the application, because a path buried under
`%LOCALAPPDATA%` is one nobody can find when they need it. It falls back to `%LOCALAPPDATA%`
only when the app sits somewhere unwritable, such as Program Files.

Kept out of the *workspace* regardless: this is about the machine, not the project, and a
workspace folder is a thing people copy onto a memory stick and hand around.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from datetime import UTC

DATA_FOLDER_NAME = "veri"


def app_root() -> Path:
    """Where the application itself lives — next to the exe once it is packaged."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _is_writable(directory: Path) -> bool:
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".write-test"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


#: The writability probe touches the disk, and this is asked for many times a second (the
#: welcome screen alone reads the recent-projects list on every refresh). With the project
#: living in a OneDrive folder, each probe was a file created and deleted for OneDrive to
#: chase. The answer does not change while the app runs, so it is worked out once per root.
_resolved: dict[Path, Path] = {}


def app_data_dir() -> Path:
    """The app's own `veri` folder, or a per-user fallback if that is not writable."""
    override = os.environ.get("SOLIDGIT_APPDATA")
    if override:
        return Path(override)

    root = app_root()
    cached = _resolved.get(root)
    if cached is not None:
        return cached

    beside_app = root / DATA_FOLDER_NAME
    if _is_writable(beside_app):
        chosen = beside_app
    elif os.environ.get("LOCALAPPDATA"):
        chosen = Path(os.environ["LOCALAPPDATA"]) / "SolidGitLAN"
    else:
        chosen = Path.home() / ".solidgit-lan"
    _resolved[root] = chosen
    return chosen


def migrate_from_legacy_location() -> bool:
    """Bring across settings written before the move, once.

    Without this the recent-projects list quietly empties on first run after an update,
    which looks exactly like the app having lost the user's work.
    """
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data or os.environ.get("SOLIDGIT_APPDATA"):
        return False

    legacy = Path(local_app_data) / "SolidGitLAN"
    target = app_data_dir()
    if legacy == target or not legacy.is_dir() or (target / "device.json").exists():
        return False

    moved = False
    for name in ("device.json", "recent.json", "settings.json"):
        source = legacy / name
        if source.exists():
            try:
                shutil.copy2(source, target / name)
                moved = True
            except OSError:
                pass
    if (legacy / "devices").is_dir() and not (target / "devices").exists():
        try:
            shutil.copytree(legacy / "devices", target / "devices")
            moved = True
        except OSError:
            pass
    return moved


@dataclass(frozen=True)
class DeviceIdentity:
    device_id: str
    device_name: str
    user_name: str


def load_device_identity() -> DeviceIdentity:
    """Read this machine's identity, creating it on first run."""
    path = app_data_dir() / "device.json"
    data: dict[str, str] = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}

    # Identity now lives beside the app, so copying the whole folder to a teammate's machine
    # would otherwise hand them our device id — and two peers claiming to be the same device
    # break locking and discovery in ways that are very hard to diagnose. Recording the host
    # it was minted on lets us notice and start fresh.
    if data.get("machine") and data["machine"] != platform.node():
        data = {}

    device_id = data.get("device_id") or uuid.uuid4().hex
    device_name = (
        os.environ.get("SOLIDGIT_DEVICE")
        or data.get("device_name")
        or os.environ.get("COMPUTERNAME")
        or "unknown-device"
    )
    user_name = (
        os.environ.get("SOLIDGIT_USER")
        or data.get("user_name")
        or os.environ.get("USERNAME")
        or "unknown"
    )

    identity = DeviceIdentity(device_id=device_id, device_name=device_name, user_name=user_name)
    if (
        data.get("device_id") != device_id
        or data.get("device_name") != device_name
        or data.get("machine") != platform.node()
    ):
        save_device_identity(identity)
    return identity


@dataclass(frozen=True)
class RecentProject:
    path: str
    name: str
    opened_at: str

    @property
    def exists(self) -> bool:
        return Path(self.path).is_dir()


MAX_RECENT = 8


def recent_projects() -> list[RecentProject]:
    """Projects opened before, newest first.

    Without this, every session starts with a file dialog and the question "where did I put
    that folder?" — which is exactly the friction a version control tool should be removing.
    """
    path = app_data_dir() / "recent.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    projects = [
        RecentProject(
            path=str(entry["path"]), name=str(entry["name"]), opened_at=str(entry["opened_at"])
        )
        for entry in data.get("projects", [])
        if isinstance(entry, dict) and "path" in entry
    ]
    return [project for project in projects if project.exists]


def remember_project(project_path: str | Path, name: str) -> None:
    from datetime import datetime

    resolved = str(Path(project_path).resolve())
    kept = [p for p in recent_projects() if p.path != resolved]
    entry = RecentProject(
        path=resolved,
        name=name,
        opened_at=datetime.now(UTC).astimezone().isoformat(timespec="seconds"),
    )
    payload = {
        "projects": [
            {"path": p.path, "name": p.name, "opened_at": p.opened_at}
            for p in ([entry] + kept)[:MAX_RECENT]
        ]
    }
    target = app_data_dir() / "recent.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def forget_project(project_path: str | Path) -> None:
    resolved = str(Path(project_path).resolve())
    payload = {
        "projects": [
            {"path": p.path, "name": p.name, "opened_at": p.opened_at}
            for p in recent_projects()
            if p.path != resolved
        ]
    }
    (app_data_dir() / "recent.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def save_device_identity(identity: DeviceIdentity) -> None:
    path = app_data_dir() / "device.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "device_id": identity.device_id,
                "device_name": identity.device_name,
                "user_name": identity.user_name,
                "machine": platform.node(),
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
