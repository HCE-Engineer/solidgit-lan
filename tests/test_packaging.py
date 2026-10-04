from __future__ import annotations

import threading

import pytest

from solidgit_lan import packaging


def test_a_built_app_refuses_to_rebuild_itself(monkeypatch):
    """The packaged exe has no source tree, so the button must explain rather than fail."""
    monkeypatch.setattr("sys.frozen", True, raising=False)
    possible, reason = packaging.can_build()
    assert not possible
    assert "kaynak koddan" in reason


def test_build_is_refused_without_the_source_tree(monkeypatch, tmp_path):
    monkeypatch.setattr(packaging, "app_root", lambda: tmp_path)
    possible, reason = packaging.can_build()
    assert not possible
    assert "Kaynak kod bulunamadı" in reason


def test_failing_tests_stop_the_build(monkeypatch, tmp_path):
    """Handing a teammate a broken build means they debug your bug instead of you."""
    (tmp_path / "launch.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(packaging, "app_root", lambda: tmp_path)

    calls: list[list[str]] = []

    def fake_stream(command, cwd, on_line, cancel):
        calls.append(command)
        on_line("1 failed")
        return 1

    monkeypatch.setattr(packaging, "_stream", fake_stream)

    result = packaging.build(on_line=lambda _: None, run_tests=True)

    assert not result.ok
    assert "Testler geçmedi" in result.message
    # Only pytest ran; PyInstaller was never reached.
    assert len(calls) == 1 and "pytest" in calls[0]


def test_cancelling_stops_before_packaging(monkeypatch, tmp_path):
    (tmp_path / "launch.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(packaging, "app_root", lambda: tmp_path)

    cancel = threading.Event()

    def fake_stream(command, cwd, on_line, cancel_event):
        cancel_event.set()
        return -1

    monkeypatch.setattr(packaging, "_stream", fake_stream)

    result = packaging.build(on_line=lambda _: None, run_tests=True, cancel=cancel)
    assert not result.ok
    assert "İptal" in result.message


def test_the_readme_warns_about_smartscreen(tmp_path):
    """An unsigned exe trips SmartScreen; unexplained, half the recipients delete it."""
    path = packaging._write_readme(tmp_path, one_file=True)
    text = path.read_text(encoding="utf-8")

    assert "SmartScreen" in text
    assert "Yine de çalıştır" in text
    assert "AYNI sürüm" in text  # protocol mismatch is refused, so say so up front

    # Written with a BOM: the recipient opens this in Notepad, and without one Windows
    # guesses the legacy code page and turns every Turkish character into mojibake.
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")
    assert "Oluşturulma" in path.read_text(encoding="utf-8-sig")


def test_command_collects_the_modules_pyinstaller_cannot_see(monkeypatch, tmp_path):
    """uvicorn and zeroconf resolve themselves at run time; missing them breaks the build."""
    (tmp_path / "launch.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(packaging, "app_root", lambda: tmp_path)
    monkeypatch.setattr(packaging, "pyinstaller_installed", lambda: True)

    recorded: list[list[str]] = []

    def fake_stream(command, cwd, on_line, cancel):
        recorded.append(command)
        return 1  # Stop after PyInstaller so nothing is actually written.

    monkeypatch.setattr(packaging, "_stream", fake_stream)
    packaging.build(on_line=lambda _: None, run_tests=False)

    command = " ".join(recorded[0])
    assert "--collect-all zeroconf" in command
    assert "--collect-submodules uvicorn" in command
    assert "--exclude-module PySide6.QtWebEngineCore" in command
    assert command.endswith("launch.py")


def test_a_failing_progress_callback_does_not_kill_the_build(monkeypatch, tmp_path):
    """A Turkish 'ğ' on a cp1252 console really did take a whole build down once."""
    (tmp_path / "launch.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(packaging, "app_root", lambda: tmp_path)
    monkeypatch.setattr(packaging, "pyinstaller_installed", lambda: True)
    monkeypatch.setattr(packaging, "_stream", lambda c, cwd, on_line, cancel: 1)

    def exploding(_: str) -> None:
        raise UnicodeEncodeError("charmap", "ğ", 0, 1, "boom")

    result = packaging.build(on_line=exploding, run_tests=False)

    # It failed for the real reason (the stubbed PyInstaller), not because of the logger.
    assert not result.ok
    assert "Derleme başarısız" in result.message


@pytest.mark.parametrize("one_file, expected", [(True, "--onefile"), (False, "--onedir")])
def test_packaging_mode_is_passed_through(monkeypatch, tmp_path, one_file, expected):
    (tmp_path / "launch.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(packaging, "app_root", lambda: tmp_path)
    monkeypatch.setattr(packaging, "pyinstaller_installed", lambda: True)

    recorded: list[list[str]] = []
    monkeypatch.setattr(
        packaging, "_stream", lambda c, cwd, on_line, cancel: (recorded.append(c), 1)[1]
    )

    packaging.build(on_line=lambda _: None, run_tests=False, one_file=one_file)
    assert expected in recorded[0]
