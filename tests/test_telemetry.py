from __future__ import annotations

import json

import pytest

from solidgit_lan.telemetry import (
    SLOW_ACTION_MILLISECONDS,
    SessionRecorder,
    recording_enabled,
    set_recording_enabled,
    summarise_sessions,
)


@pytest.fixture
def appdata(tmp_path, monkeypatch):
    monkeypatch.setenv("SOLIDGIT_APPDATA", str(tmp_path / "appdata"))
    return tmp_path / "appdata"


def read_events(recorder: SessionRecorder) -> list[dict]:
    return [
        json.loads(line)
        for line in recorder.path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_events_land_on_disk_immediately(appdata):
    """Flushed per line so a crash — the case worth investigating — still leaves a trail."""
    recorder = SessionRecorder.create()
    recorder.event("click", control="Kilitle", page="dosyalar")

    kinds = [e["kind"] for e in read_events(recorder)]
    assert kinds == ["session_start", "click"]
    recorder.close()


def test_timed_records_duration_and_flags_slow_work(appdata, monkeypatch):
    recorder = SessionRecorder.create()

    with recorder.timed("commit", files=3) as extra:
        extra["bytes"] = 1234

    event = read_events(recorder)[-1]
    assert event["kind"] == "commit"
    assert event["files"] == 3 and event["bytes"] == 1234
    assert event["ms"] >= 0
    assert event["slow"] is (event["ms"] >= SLOW_ACTION_MILLISECONDS)
    recorder.close()


def test_timed_records_a_failure_and_re_raises(appdata):
    """Recording must observe, never swallow."""
    recorder = SessionRecorder.create()

    with pytest.raises(ValueError):
        with recorder.timed("commit"):
            raise ValueError("disk dolu")

    event = read_events(recorder)[-1]
    assert event["failed"] is True
    assert "disk dolu" in event["error"]
    recorder.close()


def test_recording_can_be_switched_off(appdata):
    assert recording_enabled() is True
    set_recording_enabled(False)
    assert recording_enabled() is False

    recorder = SessionRecorder.create()
    recorder.event("click", control="Kilitle")
    assert not recorder.path.exists()


def test_a_disabled_recorder_is_harmless(tmp_path):
    """The CLI and the tests construct one of these; it must never touch the filesystem."""
    from pathlib import Path

    recorder = SessionRecorder(path=Path(tmp_path / "nope.jsonl"), enabled=False)
    recorder.event("click", control="Kilitle")
    with recorder.timed("commit"):
        pass
    recorder.close()

    assert not (tmp_path / "nope.jsonl").exists()


def test_turning_recording_on_mid_session_starts_writing_immediately(appdata):
    """Switching it on must not quietly wait for the next launch to take effect."""
    set_recording_enabled(False)
    recorder = SessionRecorder.create()
    assert not recorder.enabled

    recorder.set_enabled(True)
    recorder.event("click", control="Kilitle")

    kinds = [e["kind"] for e in read_events(recorder)]
    assert kinds == ["recording_enabled", "click"]
    recorder.close()


def test_turning_recording_off_mid_session_stops_writing(appdata):
    recorder = SessionRecorder.create()
    recorder.event("click", control="Kilitle")
    recorder.set_enabled(False)
    recorder.event("click", control="Kaydet")

    controls = [e.get("control") for e in read_events(recorder)]
    assert "Kilitle" in controls
    assert "Kaydet" not in controls


def test_old_sessions_are_pruned(appdata):
    from solidgit_lan.telemetry import sessions_dir

    sessions_dir().mkdir(parents=True, exist_ok=True)
    for index in range(50):
        (sessions_dir() / f"2026010{index // 10}-0000{index % 10}-aaaaaa.jsonl").write_text(
            "{}\n", encoding="utf-8"
        )

    recorder = SessionRecorder.create()
    recorder.prune(keep=10)

    assert len(list(sessions_dir().glob("*.jsonl"))) <= 11  # 10 kept plus the live one
    recorder.close()


def test_summary_highlights_slowness_and_refusals(appdata):
    recorder = SessionRecorder.create()
    recorder.event("click", control="Kilitle")
    recorder.event("lock_refused", reason="LOCK_HELD", held=["Montaj1/govde.sldprt"])
    recorder.event("commit", ms=1800.0, slow=True)
    recorder.close()

    summary = summarise_sessions()
    assert "1 kilit reddi" in summary
    assert "yavaş: commit 1800.0 ms" in summary
