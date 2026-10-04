"""The "make a version I can hand over" dialog.

The build runs on a worker thread with its output streamed into the window. A multi-minute
wait behind a frozen window is the same mistake as the one the hosting button already taught
us, only ten times longer.
"""

from __future__ import annotations

import threading

from PySide6.QtCore import QThread, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
)

from .. import __version__
from ..packaging import BuildResult, build, can_build, output_dir
from ..telemetry import SessionRecorder
from . import theme
from .widgets import Banner, label


class BuildWorker(QThread):
    """Runs the build off the GUI thread and reports progress as it goes."""

    line = Signal(str)
    done = Signal(object)

    def __init__(self, run_tests: bool, one_file: bool) -> None:
        super().__init__()
        self._run_tests = run_tests
        self._one_file = one_file
        self._cancel = threading.Event()

    def run(self) -> None:  # noqa: D102 - QThread entry point
        result = build(
            on_line=self.line.emit,
            run_tests=self._run_tests,
            one_file=self._one_file,
            cancel=self._cancel,
        )
        self.done.emit(result)

    def cancel(self) -> None:
        self._cancel.set()


class BuildDialog(QDialog):
    def __init__(self, parent=None, recorder: SessionRecorder | None = None) -> None:
        super().__init__(parent)
        from pathlib import Path

        self.recorder = recorder or SessionRecorder(path=Path(), enabled=False)
        self.worker: BuildWorker | None = None
        self.result: BuildResult | None = None

        self.setWindowTitle("Sürüm oluştur")
        self.setMinimumSize(760, 560)
        self.setStyleSheet(theme.STYLESHEET)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)

        layout.addWidget(label(f"Sürüm {__version__} paketi", "title"))
        intro = QLabel(
            "Arkadaşlarına verebileceğin tek bir zip dosyası oluşturur. Karşı tarafın "
            "Python, git ya da başka bir şey kurmasına gerek kalmaz — zip'i açıp exe'ye "
            "çift tıklamaları yeter."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet(f"color: {theme.TEXT_MUTED};")
        layout.addWidget(intro)

        self.banner = Banner()
        layout.addWidget(self.banner)

        self.run_tests = QCheckBox("Önce testleri çalıştır (önerilir)")
        self.run_tests.setChecked(True)
        self.run_tests.setToolTip(
            "Testler geçmezse paket oluşturulmaz.\n"
            "Bozuk bir sürümü arkadaşına vermek, hatanı ona ayıklatmak demektir."
        )
        self.one_file = QCheckBox("Tek dosya yap (daha kolay paylaşılır, daha yavaş açılır)")
        self.one_file.setToolTip(
            "İşaretsiz: klasör hâlinde paketlenir — açılışı çok daha hızlıdır.\n"
            "İşaretli: tek exe — her çalıştırmada kendini geçici klasöre açar, 10-20 sn sürer."
        )
        layout.addWidget(self.run_tests)
        layout.addWidget(self.one_file)

        self.progress = QProgressBar()
        self.progress.setRange(0, 0)  # Indeterminate: we cannot know how far along we are.
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setStyleSheet(
            f"font-family: 'Cascadia Mono', Consolas, monospace; font-size: 12px;"
            f" color: {theme.TEXT_MUTED};"
        )
        layout.addWidget(self.log, 1)

        self.start_button = QPushButton("Sürüm oluştur")
        self.start_button.setProperty("kind", "primary")
        self.start_button.clicked.connect(self._start)
        self.cancel_button = QPushButton("Durdur")
        self.cancel_button.setProperty("kind", "danger")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self._cancel)
        self.open_button = QPushButton("Klasörü aç")
        self.open_button.setEnabled(False)
        self.open_button.clicked.connect(self._open_output)
        self.copy_button = QPushButton("Yolu kopyala")
        self.copy_button.setEnabled(False)
        self.copy_button.clicked.connect(self._copy_path)
        close_button = QPushButton("Kapat")
        close_button.clicked.connect(self.reject)

        buttons = QHBoxLayout()
        buttons.addWidget(self.start_button)
        buttons.addWidget(self.cancel_button)
        buttons.addStretch(1)
        buttons.addWidget(self.copy_button)
        buttons.addWidget(self.open_button)
        buttons.addWidget(close_button)
        layout.addLayout(buttons)

        possible, reason = can_build()
        if not possible:
            self.start_button.setEnabled(False)
            self.banner.show_message(reason, "warn")

    # -- actions --------------------------------------------------------------------------

    def _start(self) -> None:
        self.log.clear()
        self.banner.clear()
        self.result = None
        self.start_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.open_button.setEnabled(False)
        self.copy_button.setEnabled(False)
        self.progress.setVisible(True)

        self.recorder.event(
            "build_started",
            run_tests=self.run_tests.isChecked(),
            one_file=self.one_file.isChecked(),
        )

        self.worker = BuildWorker(self.run_tests.isChecked(), self.one_file.isChecked())
        self.worker.line.connect(self._append)
        self.worker.done.connect(self._finished)
        self.worker.start()

    def _cancel(self) -> None:
        if self.worker is not None:
            self.worker.cancel()
            self._append("Durduruluyor…")

    def _append(self, line: str) -> None:
        self.log.appendPlainText(line)
        bar = self.log.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _finished(self, result: BuildResult) -> None:
        self.result = result
        self.progress.setVisible(False)
        self.start_button.setEnabled(True)
        self.cancel_button.setEnabled(False)

        if result.ok and result.package is not None:
            megabytes = result.size_bytes / 1024 / 1024
            self.banner.show_message(
                f"{result.package.name} hazır ({megabytes:.0f} MB, "
                f"{result.seconds:.0f} saniye sürdü).\n"
                "Bu zip dosyasını arkadaşlarına gönder — içinde nasıl çalıştırılacağını "
                "anlatan bir OKU-BENI.txt de var.",
                "info",
            )
            self.open_button.setEnabled(True)
            self.copy_button.setEnabled(True)
        else:
            self.banner.show_message(result.message, "error")

        self.recorder.event(
            "build_finished",
            ok=result.ok,
            seconds=round(result.seconds, 1),
            size_mb=round(result.size_bytes / 1024 / 1024, 1),
            message=result.message,
        )

    def _open_output(self) -> None:
        output_dir().mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(output_dir())))

    def _copy_path(self) -> None:
        if self.result and self.result.package:
            QGuiApplication.clipboard().setText(str(self.result.package))
            self.banner.show_message("Dosya yolu kopyalandı.", "info")

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if self.worker is not None and self.worker.isRunning():
            self.worker.cancel()
            self.worker.wait(5000)
        super().closeEvent(event)
