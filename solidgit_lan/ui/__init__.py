"""Desktop interface (PySide6).

Reads from `core` and `net`; neither knows this exists. Nothing here holds project rules — a
lock is refused by `core.locks`, and the UI's job is only to explain why.
"""

from __future__ import annotations

import logging
import os
import sys
import traceback
from pathlib import Path

LOG_FILENAME = "solidgit.log"


def log_path() -> Path:
    from ..appdata import app_data_dir

    return app_data_dir() / LOG_FILENAME


def _ensure_standard_streams() -> None:
    """Give pythonw.exe the stdout and stderr that libraries assume exist.

    A windowed process has none, and plenty of third-party code reaches for
    `sys.stdout.isatty()` without checking — which is how starting a session died with an
    unrelated-looking logging error in the packaged app while working fine from a console.
    """
    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))


def _setup_logging() -> Path:
    """Write a log next to the app's settings.

    The launcher runs pythonw.exe, which has no console: without this, anything that goes
    wrong produces exactly no output anywhere, and "nothing happens" becomes impossible to
    tell apart from "it silently crashed".
    """
    target = log_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=str(target),
        filemode="a",
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        force=True,
    )
    logging.info("--- SolidGit LAN started ---")
    return target


def _install_error_dialog(target: Path) -> None:
    """Surface an unexpected error instead of letting the window die quietly."""
    from PySide6.QtWidgets import QMessageBox

    def hook(exc_type, exc, tb) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        logging.error("Unhandled exception", exc_info=(exc_type, exc, tb))
        detail = "".join(traceback.format_exception(exc_type, exc, tb))
        box = QMessageBox()
        box.setIcon(QMessageBox.Critical)
        box.setWindowTitle("Beklenmeyen bir hata oldu")
        box.setText(f"{exc_type.__name__}: {exc}")
        box.setInformativeText(
            f"Uygulama açık kalmaya çalışacak. Ayrıntılar şuraya yazıldı:\n{target}"
        )
        box.setDetailedText(detail)
        box.exec()

    sys.excepthook = hook


def run(workspace: str | Path | None = None) -> int:
    from PySide6.QtWidgets import QApplication

    from ..appdata import migrate_from_legacy_location
    from ..telemetry import SessionRecorder
    from . import theme
    from .main_window import MainWindow
    from .recorder import InteractionRecorder

    _ensure_standard_streams()
    migrated = migrate_from_legacy_location()
    target = _setup_logging()
    if migrated:
        logging.info("Settings migrated from the old per-user location.")

    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("SolidGit LAN")
    app.setStyleSheet(theme.STYLESHEET)
    _install_error_dialog(target)

    recorder = SessionRecorder.create()
    window = MainWindow(workspace, recorder=recorder)

    # Installed on our own application object, so it observes this program's windows and
    # nothing else on the machine.
    interactions = InteractionRecorder(recorder, page_of=window.current_page_name)
    app.installEventFilter(interactions)

    window.show()
    try:
        return app.exec()
    finally:
        recorder.close("quit")
