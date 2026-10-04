"""Session recording, so friction in the interface is visible instead of remembered.

**Scope is deliberately narrow.** This records interactions *inside this application only*:
the event filter is installed on our own QApplication, so it sees events delivered to our
windows and nothing else. There is no system-wide hook, no keystroke capture, and nothing is
ever sent anywhere — the file stays on this machine until someone chooses to share it.

What goes in:

* which buttons were pressed and which page was open
* what each action did, and how long it took
* refusals with the reason (a refused lock is the single most useful signal there is)
* anything slow enough to feel broken

What stays out: typed text, file contents, and absolute paths outside the project.
"""

from __future__ import annotations

import json
import os
import platform
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, UTC
from pathlib import Path
from typing import Any
from collections.abc import Iterator

from . import __version__
from .appdata import app_data_dir

#: An action slower than this is worth knowing about; it is long enough to read as a freeze.
SLOW_ACTION_MILLISECONDS = 250

MAX_SESSION_FILES = 40


def sessions_dir() -> Path:
    return app_data_dir() / "sessions"


def settings_path() -> Path:
    return app_data_dir() / "settings.json"


def recording_enabled() -> bool:
    """On by default; the user can switch it off from the interface."""
    try:
        data = json.loads(settings_path().read_text(encoding="utf-8"))
        return bool(data.get("record_sessions", True))
    except (OSError, json.JSONDecodeError):
        return True


def set_recording_enabled(enabled: bool) -> None:
    data: dict[str, Any] = {}
    try:
        data = json.loads(settings_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    data["record_sessions"] = bool(enabled)
    settings_path().parent.mkdir(parents=True, exist_ok=True)
    settings_path().write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


@dataclass
class SessionRecorder:
    """Appends one JSON object per line. Flushed on every write.

    Line-per-event and flush-per-line so that a crash — precisely the case worth
    investigating — still leaves everything that happened up to it on disk.
    """

    path: Path
    enabled: bool = True
    started_at: float = field(default_factory=time.monotonic)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _handle: Any = field(default=None, repr=False)
    _count: int = 0

    @staticmethod
    def create(enabled: bool | None = None) -> SessionRecorder:
        directory = sessions_dir()
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        recorder = SessionRecorder(
            path=directory / f"{stamp}-{uuid.uuid4().hex[:6]}.jsonl",
            enabled=recording_enabled() if enabled is None else enabled,
        )
        if recorder.enabled:
            recorder._open()
            recorder.event(
                "session_start",
                version=__version__,
                os=platform.platform(),
                python=platform.python_version(),
            )
            recorder.prune()
        return recorder

    def _open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = open(self.path, "a", encoding="utf-8")

    # -- writing --------------------------------------------------------------------------

    def event(self, kind: str, **fields: Any) -> None:
        if not self.enabled or self._handle is None:
            return
        record = {
            "t": round(time.monotonic() - self.started_at, 3),
            "at": datetime.now(UTC).astimezone().isoformat(timespec="seconds"),
            "kind": kind,
            **fields,
        }
        line = json.dumps(record, ensure_ascii=False, default=str)
        with self._lock:
            try:
                self._handle.write(line + "\n")
                self._handle.flush()
                self._count += 1
            except OSError:
                # Recording must never be the reason the app stops working.
                self.enabled = False

    @contextmanager
    def timed(self, kind: str, **fields: Any) -> Iterator[dict[str, Any]]:
        """Time a block and record how long it took, whether or not it succeeded."""
        extra: dict[str, Any] = {}
        started = time.perf_counter()
        try:
            yield extra
        except Exception as e:  # noqa: BLE001 - recorded, then re-raised untouched
            self.event(
                kind,
                ms=round((time.perf_counter() - started) * 1000, 1),
                failed=True,
                error=f"{type(e).__name__}: {e}",
                **fields,
                **extra,
            )
            raise
        else:
            elapsed = round((time.perf_counter() - started) * 1000, 1)
            self.event(
                kind,
                ms=elapsed,
                slow=elapsed >= SLOW_ACTION_MILLISECONDS,
                **fields,
                **extra,
            )

    def set_enabled(self, enabled: bool) -> None:
        """Switch recording on or off straight away, not on the next launch.

        Turning it on has to open the file too: a recorder that started disabled has no
        handle, so without this the setting would look applied while nothing was written.
        """
        if enabled and self._handle is None:
            self.enabled = True
            self._open()
            self.event("recording_enabled")
        elif not enabled and self._handle is not None:
            self.close("recording_disabled")
        self.enabled = enabled

    def close(self, reason: str = "closed") -> None:
        if not self.enabled or self._handle is None:
            return
        self.event("session_end", reason=reason, events=self._count)
        with self._lock:
            try:
                self._handle.close()
            except OSError:
                pass
            self._handle = None

    # -- housekeeping ---------------------------------------------------------------------

    def prune(self, keep: int = MAX_SESSION_FILES) -> int:
        """Keep the most recent sessions only; this should never grow without bound."""
        files = sorted(sessions_dir().glob("*.jsonl"))
        removed = 0
        for stale in files[:-keep] if len(files) > keep else []:
            if stale == self.path:
                continue
            try:
                os.remove(stale)
                removed += 1
            except OSError:
                pass
        return removed


def summarise_sessions(limit: int = 20) -> str:
    """A short plain-text digest of recent sessions, for pasting into a conversation."""
    files = sorted(sessions_dir().glob("*.jsonl"))[-limit:]
    if not files:
        return "Henüz kayıt yok."

    lines: list[str] = []
    for file in files:
        events: list[dict[str, Any]] = []
        try:
            for raw in file.read_text(encoding="utf-8").splitlines():
                if raw.strip():
                    events.append(json.loads(raw))
        except (OSError, json.JSONDecodeError):
            continue
        if not events:
            continue

        clicks = sum(1 for e in events if e["kind"] == "click")
        slow = [e for e in events if e.get("slow")]
        failed = [e for e in events if e.get("failed") or e["kind"] == "error"]
        refused = [e for e in events if e["kind"] == "lock_refused"]
        duration = events[-1].get("t", 0)

        lines.append(
            f"{file.stem}  ·  {duration:.0f} sn  ·  {clicks} tık  ·  "
            f"{len(slow)} yavaş  ·  {len(refused)} kilit reddi  ·  {len(failed)} hata"
        )
        for event in slow[:3]:
            lines.append(f"    yavaş: {event['kind']} {event.get('ms')} ms")
        for event in failed[:3]:
            lines.append(f"    hata: {event.get('error') or event.get('message')}")
    return "\n".join(lines)
