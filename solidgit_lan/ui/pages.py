"""The three main pages: files, history, network.

Each page exposes a `refresh()` that reads from AppState and rebuilds what it shows. The
window drives them all from one timer, so nothing on screen can drift out of step with
anything else.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from PySide6.QtCore import QUrl, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..core.locks import LockError
from ..core.repo import RepositoryError
from ..net.protocol import DEFAULT_HTTPS_PORT
from ..telemetry import SLOW_ACTION_MILLISECONDS, SessionRecorder
from . import theme
from .state import AppState, FileStatus, LockState
from .widgets import Banner, Card, KeyValue, dot_icon, human_size, label

LOGGER = logging.getLogger(__name__)

_STATUS_TEXT = {
    FileStatus.CLEAN: ("Güncel", theme.OK),
    FileStatus.MODIFIED: ("Değişti", theme.CHANGED),
    FileStatus.ADDED: ("Yeni", theme.ADDED),
    FileStatus.REMOVED: ("Silindi", theme.REMOVED),
}


def _transparent_row() -> QWidget:
    """A plain container that takes its card's colour.

    A bare QWidget picks up the window background from the global stylesheet and shows as
    a dark band across the card. The object-name selector keeps the rule off the buttons
    inside, which a plain `QWidget {}` rule would also have reached.
    """
    row = QWidget()
    row.setObjectName("CardRow")
    row.setStyleSheet("#CardRow { background: transparent; }")
    return row


def _primary(text: str) -> QPushButton:
    button = QPushButton(text)
    button.setProperty("kind", "primary")
    return button


class FilesPage(QWidget):
    """The working folder: what changed, who holds what, and the commit box."""

    changed = Signal()

    def __init__(self, state: AppState, recorder: SessionRecorder | None = None) -> None:
        super().__init__()
        self.state = state
        self.recorder = recorder or SessionRecorder(path=Path(), enabled=False)

        self.banner = Banner()
        self.tree = QTreeWidget()
        self.tree.setColumnCount(4)
        self.tree.setHeaderLabels(["DURUM", "DOSYA", "BOYUT", "KİLİT"])
        self.tree.setRootIsDecorated(False)
        self.tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tree.setUniformRowHeights(True)
        self.tree.setObjectName("dosya-listesi")
        header = self.tree.header()
        header.setSectionResizeMode(0, QHeaderView.Fixed)
        header.resizeSection(0, 110)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.Fixed)
        header.resizeSection(2, 90)
        header.setSectionResizeMode(3, QHeaderView.Fixed)
        header.resizeSection(3, 190)
        self.tree.itemSelectionChanged.connect(self._update_buttons)

        self.hint = label("", "hint")
        self.hint.setWordWrap(True)

        self.lock_button = QPushButton("Kilitle")
        self.lock_button.setToolTip(
            "Seçili dosyaları sana ayırır. Aynı anda başkası düzenleyemez.\n"
            "Kilitli olmayan dosyalar salt-okunur olur, böylece kazara değiştirilemez."
        )
        self.unlock_button = QPushButton("Kilidi bırak")
        self.unlock_button.setToolTip("Dosyayı serbest bırakır, başkası çalışabilir.")
        self.folder_button = QPushButton("Klasörü aç")
        self.folder_button.setToolTip("Proje klasörünü Dosya Gezgini'nde açar.")
        self.lock_button.clicked.connect(self._lock_selected)
        self.unlock_button.clicked.connect(self._unlock_selected)
        self.folder_button.clicked.connect(self._open_folder)

        self.summary = label("", "subtitle")

        self.force_button = QPushButton("Kilidi kır")
        self.force_button.setProperty("kind", "danger")
        self.force_button.setVisible(False)
        self.force_button.setToolTip(
            "Başkasının kilidini kaldırır — arkadaşın eve gitmiş ve dosya üstünde kalmışsa.\n"
            "Sahibine bildirilir; sessizce yapılmaz."
        )
        self.force_button.clicked.connect(self._force_release)

        self.take_button = _primary("")
        self.take_button.setVisible(False)
        self.take_button.clicked.connect(self._take_updates)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)
        toolbar.addWidget(self.lock_button)
        toolbar.addWidget(self.unlock_button)
        toolbar.addWidget(self.folder_button)
        toolbar.addWidget(self.force_button)
        toolbar.addWidget(self.take_button)
        toolbar.addStretch(1)
        toolbar.addWidget(self.summary)

        self.message = QPlainTextEdit()
        self.message.setFixedHeight(70)
        self.commit_button = _primary("Değişiklikleri kaydet")
        self.commit_button.setToolTip(
            "Klasörün şu anki halini geçmişe kaydeder.\n"
            "SolidWorks'ün Kaydet'inden farklı: Geçmiş sayfasından herhangi bir dosyanın\n"
            "bu hâline sonradan geri dönebilirsin."
        )
        self.commit_button.clicked.connect(self._commit)

        commit_row = QHBoxLayout()
        commit_row.setSpacing(12)
        commit_row.addWidget(self.message, 1)
        commit_row.addWidget(self.commit_button, 0, Qt.AlignBottom)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(12)
        layout.addWidget(self.hint)
        layout.addWidget(self.banner)
        layout.addLayout(toolbar)
        layout.addWidget(self.tree, 1)
        layout.addLayout(commit_row)

    def _open_folder(self) -> None:
        if self.state.is_open:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.state.require_repo().root)))

    def _force_release(self) -> None:
        paths = [p for p in self.selected_paths() if p in self._others_locks()]
        if not paths:
            return
        names = ", ".join(p.rsplit("/", 1)[-1] for p in paths[:4])
        answer = QMessageBox.question(
            self,
            "Kilidi kır",
            f"<b>{names}</b> için başkasının kilidi kaldırılacak.<br><br>"
            "O kişi şu anda dosya üzerinde çalışıyor olabilir; işini kaybedebilir. "
            "Kendisine bildirilecek.<br><br>Devam edilsin mi?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        try:
            broken = self.state.force_release(paths)
        except RepositoryError as e:
            self.banner.show_message(str(e), "error")
            return
        self.recorder.event("lock_forced", count=len(broken))
        self.banner.show_message(f"{len(broken)} kilit kırıldı.", "warn")
        self.changed.emit()

    def _others_locks(self) -> set[str]:
        # Read straight from the lock table. Going through file_rows() rescanned the whole
        # project on every click in the list just to decide whether a button is enabled.
        now = time.time()
        me = self.state.identity.device_id
        return {
            path
            for path, lock in self.state.locks.locks.items()
            if lock.device != me and not lock.is_expired(now)
        }

    def _take_updates(self) -> None:
        """Apply work that has already arrived but is not on disk yet."""
        if self.state.has_changes():
            self.banner.show_message(
                "Önce kendi değişikliklerini kaydet — üzerine yazmayalım.", "warn"
            )
            return
        try:
            with self.recorder.timed("take_updates"):
                message = self.state.take_updates()
        except RepositoryError as e:
            self.banner.show_message(str(e), "error")
            return
        self.banner.show_message(message, "info")
        self.changed.emit()

    # -- actions --------------------------------------------------------------------------

    def selected_paths(self) -> list[str]:
        return [item.data(0, Qt.UserRole) for item in self.tree.selectedItems()]

    def _lock_selected(self) -> None:
        paths = self.selected_paths()
        if not paths:
            return
        with self.recorder.timed("lock", requested=len(paths)) as extra:
            result = self.state.acquire(paths)
            extra["granted"] = len(result.granted)
            extra["outcome"] = "ok" if result.ok else result.error_code
        if not result.ok:
            # A refusal is the most informative thing that happens in this interface: it is
            # where the model's rules meet an expectation the person actually had.
            self.recorder.event(
                "lock_refused",
                reason=result.error_code,
                stale=[c.path for c in result.stale],
                held=[c.path for c in result.held],
                reserved=[c.path for c in result.reserved],
            )
        if result.ok:
            self.banner.show_message(
                f"{len(result.granted)} dosya kilitlendi. Diğer dosyalar diskte salt-okunur "
                "yapıldı — SolidWorks onları salt-okunur açacak.",
                "info",
            )
        else:
            self.banner.show_message(self._explain(result), "warn")
        self.changed.emit()

    def _explain(self, result) -> str:
        """Turn a refused acquisition into the reason, and what to do about it."""
        parts: list[str] = []
        if result.stale:
            names = ", ".join(conflict.path.rsplit("/", 1)[-1] for conflict in result.stale)
            parts.append(
                f"{names} dosyasının daha yeni sürümü var. Kilitlemeden önce güncellemelisin."
            )
        if result.held:
            names = ", ".join(
                f"{c.path.rsplit('/', 1)[-1]} ({c.user_name})" for c in result.held
            )
            parts.append(f"Şu an başkasında: {names}.")
        if result.reserved:
            names = ", ".join(
                f"{c.path.rsplit('/', 1)[-1]} ({c.reserved_for_name})" for c in result.reserved
            )
            parts.append(f"Sırada bekleyen var: {names}.")
        parts.append("Hiçbiri kilitlenmedi — çoklu kilit ya hep ya hiç.")
        return " ".join(parts)

    def _unlock_selected(self) -> None:
        paths = self.selected_paths()
        if not paths:
            return
        try:
            with self.recorder.timed("unlock", requested=len(paths)):
                released = self.state.release(paths)
        except LockError as e:
            self.recorder.event("error", where="unlock", message=str(e))
            self.banner.show_message(str(e), "error")
            return
        self.banner.show_message(f"{len(released)} kilit bırakıldı.", "info")
        self.changed.emit()

    def _commit(self) -> None:
        message = self.message.toPlainText().strip()
        if not message:
            self.recorder.event("commit_rejected", reason="empty_message")
            self.banner.show_message("Bir kayıt mesajı yaz.", "warn")
            return

        QGuiApplication.setOverrideCursor(Qt.WaitCursor)
        QApplication.processEvents()
        try:
            # Hashing a real assembly takes seconds; timing it is how a freeze that people
            # experience but cannot describe becomes a number.
            with self.recorder.timed("commit", message_length=len(message)) as extra:
                commit = self.state.commit(message)
                extra["files"] = len(commit.manifest)
                extra["bytes"] = commit.manifest.total_size()
        except RepositoryError as e:
            self.recorder.event("error", where="commit", message=str(e))
            self.banner.show_message(str(e), "error")
            return
        finally:
            QGuiApplication.restoreOverrideCursor()
        self.message.clear()
        self.banner.show_message(
            f"Kaydedildi: {commit.short_id} — {len(commit.manifest)} dosya.", "info"
        )
        self.changed.emit()

    # -- refresh --------------------------------------------------------------------------

    def refresh(self) -> None:
        if not self.state.is_open:
            return
        started = time.perf_counter()
        self.state.expire_locks()
        if self.state.notices:
            # Something happened to this person's work elsewhere — show it where they look.
            self.banner.show_message("  ".join(self.state.notices), "warn")
            self.recorder.event("notice_shown", count=len(self.state.notices))
            self.state.notices.clear()
        selected = set(self.selected_paths())
        scroll = self.tree.verticalScrollBar().value()

        self.tree.setUpdatesEnabled(False)
        self.tree.clear()
        changed = 0
        for row in self.state.file_rows():
            status_text, status_colour = _STATUS_TEXT[row.status]
            if row.status is not FileStatus.CLEAN:
                changed += 1

            if row.lock_state is LockState.MINE:
                lock_text, lock_colour = "Sende kilitli", theme.LOCKED_BY_ME
            elif row.lock_state is LockState.THEIRS:
                lock_text, lock_colour = f"{row.lock_owner} kilitledi", theme.LOCKED_BY_OTHER
            elif row.soft_claim_by:
                # Blocks nobody — it exists so two people who open the same part find out in
                # seconds instead of each losing twenty minutes of work.
                lock_text, lock_colour = f"{row.soft_claim_by} açtı", theme.SOFT_CLAIM
            else:
                lock_text, lock_colour = "", ""

            item = QTreeWidgetItem([status_text, row.path, human_size(row.size), lock_text])
            item.setData(0, Qt.UserRole, row.path)
            item.setIcon(0, dot_icon(status_colour))
            item.setForeground(0, _brush(status_colour))
            if lock_text:
                item.setIcon(3, dot_icon(lock_colour))
                item.setForeground(3, _brush(lock_colour))
            self.tree.addTopLevelItem(item)
            if row.path in selected:
                item.setSelected(True)

        self.tree.setUpdatesEnabled(True)
        self.tree.verticalScrollBar().setValue(scroll)

        held = len(self.state.my_locks())
        self.summary.setText(
            f"{changed} değişiklik · {self.tree.topLevelItemCount()} dosya · {held} kilit sende"
        )
        behind = self.state.behind_count()
        self.take_button.setVisible(behind > 0)
        if behind:
            self.take_button.setText(f"⬇  {behind} yeni değişiklik — al")

        self.commit_button.setEnabled(changed > 0 and behind == 0)
        self.hint.setText(self.state.next_step())
        self.message.setPlaceholderText(
            "Bu ilk kayıt — kısa bir isim yeter (örn. başlangıç)"
            if self.state.require_repo().commits.head() is None
            else "Ne değiştirdin? (örn. gövde kalınlığı 3 mm'ye çıkarıldı)"
        )
        self._update_buttons()

        # The list redraws roughly once a second. If that ever stops being cheap, the whole
        # window feels sluggish for reasons nobody can point at, so it reports on itself.
        elapsed = round((time.perf_counter() - started) * 1000, 1)
        if elapsed >= SLOW_ACTION_MILLISECONDS:
            self.recorder.event(
                "slow_refresh", ms=elapsed, slow=True, files=self.tree.topLevelItemCount()
            )

    def _update_buttons(self) -> None:
        """Enable each action only for a selection it can actually apply to.

        A button that is clickable but always answers "you can't do that" teaches people to
        distrust the whole toolbar.
        """
        selected = set(self.selected_paths())
        self.folder_button.setEnabled(self.state.is_open)
        if not selected:
            self.lock_button.setEnabled(False)
            self.unlock_button.setEnabled(False)
            return
        mine = set(self.state.my_locks())
        self.lock_button.setEnabled(bool(selected - mine))
        self.unlock_button.setEnabled(bool(selected & mine))

        # Only the host may break a lock, and only on files somebody else is holding.
        others = self._others_locks() if self.state.is_hosting else set()
        self.force_button.setVisible(bool(others))
        self.force_button.setEnabled(bool(selected & others))


def _brush(colour: str) -> QBrush:
    return QBrush(QColor(colour))


class HistoryPage(QWidget):
    """Every commit, newest first, what each one contained — and a way back to any of it."""

    changed = Signal()

    def __init__(self, state: AppState, recorder: SessionRecorder | None = None) -> None:
        super().__init__()
        self.state = state
        self.recorder = recorder or SessionRecorder(path=Path(), enabled=False)
        self._signature: tuple | None = None

        self.banner = Banner()
        self.tree = QTreeWidget()
        self.tree.setColumnCount(4)
        self.tree.setHeaderLabels(["SIRA", "NE YAPILDI", "KİM", "NE ZAMAN"])
        self.tree.setRootIsDecorated(False)
        header = self.tree.header()
        header.setSectionResizeMode(0, QHeaderView.Fixed)
        header.resizeSection(0, 70)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.Fixed)
        header.resizeSection(2, 170)
        header.setSectionResizeMode(3, QHeaderView.Fixed)
        header.resizeSection(3, 180)
        self.tree.currentItemChanged.connect(self._show_files)

        self.files = QTreeWidget()
        self.files.setColumnCount(2)
        self.files.setHeaderLabels(["DOSYA", "BOYUT"])
        self.files.setRootIsDecorated(False)
        self.files.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.files.header().setSectionResizeMode(1, QHeaderView.Fixed)
        self.files.header().resizeSection(1, 90)
        self.files.setMaximumHeight(220)
        self.files.itemSelectionChanged.connect(self._update_restore_button)

        self.restore_button = QPushButton("Bu sürümü geri yükle")
        self.restore_button.setToolTip(
            "Seçili dosyayı bu kayıttaki hâline döndürür.\n"
            "Geçmiş silinmez: geri yükleme, kaydettiğinde yeni bir kayıt olur."
        )
        self.restore_button.setEnabled(False)
        self.restore_button.clicked.connect(self._restore_selected)

        files_header = QHBoxLayout()
        files_header.addWidget(label("SEÇİLİ KAYITTAKİ DOSYALAR", "section"))
        files_header.addStretch(1)
        files_header.addWidget(self.restore_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(12)
        layout.addWidget(self.banner)
        layout.addWidget(self.tree, 1)
        layout.addLayout(files_header)
        layout.addWidget(self.files)

    def refresh(self) -> None:
        if not self.state.is_open:
            return
        history = self.state.history()
        # Runs every second. Rebuilding an unchanged list re-selected the commit, which
        # reloaded the file list underneath and threw away whatever file the user had
        # just clicked — so only rebuild when the history actually changed.
        signature = tuple(commit.id for commit in history)
        if signature == self._signature:
            return
        self._signature = signature

        current = self.tree.currentItem()
        keep = current.data(0, Qt.UserRole) if current else None

        self.tree.clear()
        for commit in history:
            marker = " ⑂" if commit.is_reconciliation else ""
            when = commit.wall_clock.replace("T", " ")[:16]
            # A sequence number is what people can hold in their head; the content hash is
            # kept on the tooltip for when it is actually needed.
            item = QTreeWidgetItem(
                [f"#{commit.lamport}{marker}", commit.message, commit.author.name, when]
            )
            item.setData(0, Qt.UserRole, commit.id)
            item.setToolTip(0, commit.id)
            self.tree.addTopLevelItem(item)
            if commit.id == keep:
                self.tree.setCurrentItem(item)

        if self.tree.currentItem() is None and self.tree.topLevelItemCount():
            self.tree.setCurrentItem(self.tree.topLevelItem(0))

    def _show_files(self) -> None:
        self.files.clear()
        self._update_restore_button()
        item = self.tree.currentItem()
        if item is None or not self.state.is_open:
            return
        commit = self.state.require_repo().commits.read(item.data(0, Qt.UserRole))
        for path, ref in sorted(commit.manifest.entries.items()):
            row = QTreeWidgetItem([path, human_size(ref.size)])
            row.setData(0, Qt.UserRole, path)
            self.files.addTopLevelItem(row)

    def _update_restore_button(self) -> None:
        self.restore_button.setEnabled(
            self.tree.currentItem() is not None and bool(self.files.selectedItems())
        )

    def _restore_selected(self) -> None:
        commit_item = self.tree.currentItem()
        file_items = self.files.selectedItems()
        if commit_item is None or not file_items:
            return
        commit_id = commit_item.data(0, Qt.UserRole)
        path = file_items[0].data(0, Qt.UserRole)
        try:
            with self.recorder.timed("restore_file"):
                message = self.state.restore_file(commit_id, path)
        except (RepositoryError, LockError) as e:
            self.banner.show_message(str(e), "warn")
            return
        self.banner.show_message(message, "info")
        self.changed.emit()


HOST_IDLE_TEXT = (
    "Bu bilgisayardaki projeyi arkadaşlarına açarsın. Bilgisayarının hotspot'unu aç ya da "
    "hepiniz aynı Wi-Fi ağına bağlanın."
)


class NetworkPage(QWidget):
    """Two things a person can do here: share their project, or join someone else's.

    Everything meaningful only to a developer — port numbers, certificate fingerprints,
    which discovery mechanism won — lives behind "Gelişmiş". Shown by default, it makes a
    perfectly healthy screen look like an error report.
    """

    changed = Signal()

    def __init__(self, state: AppState, recorder: SessionRecorder | None = None) -> None:
        super().__init__()
        self.state = state
        self.recorder = recorder or SessionRecorder(path=Path(), enabled=False)
        self._busy = False
        self._last_join_state: str | None = None
        self._requests_signature: tuple | None = None

        self.banner = Banner()

        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(14)
        layout.addWidget(self.banner)
        layout.addWidget(self._build_host_card())
        layout.addWidget(self._build_join_card())
        layout.addWidget(self._build_transfer_card())
        layout.addWidget(self._build_advanced_card())
        layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidget(content)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)

    # -- cards ----------------------------------------------------------------------------

    def _build_host_card(self) -> Card:
        card = Card("Projeni paylaş")
        self.host_explanation = label(HOST_IDLE_TEXT, "subtitle")
        self.host_explanation.setWordWrap(True)
        card.add(self.host_explanation)

        self.host_button = _primary("Oturumu başlat")
        self.host_button.clicked.connect(self._toggle_hosting)
        card.add_row(self.host_button)

        self.code_caption = label("ARKADAŞLARINA BU KODU SÖYLE", "section")
        self.code_value = label("", "code")
        self.code_value.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.copy_button = QPushButton("Kodu kopyala")
        self.copy_button.clicked.connect(self._copy_code)
        card.add(self.code_caption)

        code_row = QHBoxLayout()
        code_row.addWidget(self.code_value)
        code_row.addSpacing(16)
        code_row.addWidget(self.copy_button, 0, Qt.AlignVCenter)
        code_row.addStretch(1)
        card.body().addLayout(code_row)

        self.host_hint = label(
            "Arkadaşların uygulamayı açıp aşağıdaki listeden senin oturumunu seçsin ve bu "
            "kodu girsin. Sonra burada onay isteği belirecek.",
            "subtitle",
        )
        self.host_hint.setWordWrap(True)
        card.add(self.host_hint)

        self.connected_label = label("", "subtitle")
        self.connected_label.setWordWrap(True)
        card.add(self.connected_label)

        self.requests_body = QVBoxLayout()
        card.body().addLayout(self.requests_body)
        return card

    def _build_join_card(self) -> Card:
        card = Card("Arkadaşına katıl")
        explanation = label(
            "Aynı ağdaki oturumlar burada kendiliğinden görünür — aramana gerek yok.",
            "subtitle",
        )
        explanation.setWordWrap(True)
        card.add(explanation)

        self.sessions = QTreeWidget()
        self.sessions.setColumnCount(3)
        self.sessions.setHeaderLabels(["KİM", "PROJE", "ADRES"])
        self.sessions.setRootIsDecorated(False)
        self.sessions.header().setSectionResizeMode(QHeaderView.Stretch)
        self.sessions.setMinimumHeight(140)
        self.sessions.setObjectName("yakindaki-oturumlar")
        self.sessions.itemSelectionChanged.connect(self._update_join_button)
        self.sessions.itemDoubleClicked.connect(lambda *_: self._join_selected())
        card.add(self.sessions)

        self.join_button = _primary("Seçili oturuma katıl")
        self.join_button.clicked.connect(self._join_selected)
        self.join_status = label("", "subtitle")
        self.join_status.setWordWrap(True)
        card.add_row(self.join_button, self.join_status, stretch_last=True)
        return card

    def _build_transfer_card(self) -> Card:
        card = Card("Dosyaları al")
        self.transfer_explanation = label(
            "Bir oturuma katıldığında projenin dosyalarını buradan indirirsin.",
            "subtitle",
        )
        self.transfer_explanation.setWordWrap(True)
        card.add(self.transfer_explanation)

        self.transfer_progress = QProgressBar()
        self.transfer_progress.setRange(0, 100)
        self.transfer_progress.setVisible(False)
        card.add(self.transfer_progress)

        self.transfer_status = label("", "subtitle")
        self.transfer_status.setWordWrap(True)
        card.add(self.transfer_status)

        self.download_button = _primary("Dosyaları indir")
        self.download_button.clicked.connect(self._download)
        self.upload_button = QPushButton("Kendi değişikliklerimi gönder")
        self.upload_button.clicked.connect(self._upload)
        self.reconcile_button = _primary("Ayrılığı çöz…")
        self.reconcile_button.setVisible(False)
        self.reconcile_button.clicked.connect(self._reconcile)
        self.stop_transfer_button = QPushButton("Durdur")
        self.stop_transfer_button.setProperty("kind", "danger")
        self.stop_transfer_button.clicked.connect(self.state.cancel_download)
        card.add_row(
            self.download_button,
            self.upload_button,
            self.reconcile_button,
            self.stop_transfer_button,
        )

        self.transfer_card = card
        return card

    def _download(self) -> None:
        if self.state.session is None:
            return
        destination = None
        if not self.state.is_open:
            # Nothing open means we do not have this project yet, so it needs somewhere to
            # land. Asking for a parent folder and naming the child ourselves keeps the repo
            # id in the folder name, which is what stops SolidWorks resolving an old copy.
            parent = QFileDialog.getExistingDirectory(
                self, "Proje nereye indirilsin? (içine yeni bir klasör açılacak)"
            )
            if not parent:
                return
            peer, _ = self.state.session
            announcement = peer.announcement
            destination = Path(parent) / (
                f"{announcement.repo_name} [{announcement.repo_id[:6]}]"
            )
            if destination.exists() and any(destination.iterdir()):
                self.banner.show_message(
                    f"'{destination.name}' klasörü zaten dolu. Başka bir yer seç.", "warn"
                )
                return
        self.recorder.event("download_started", clone=destination is not None)
        self.state.start_download(destination)
        self.refresh()

    def _reconcile(self) -> None:
        divergence = self.state.divergence
        if divergence is None or self.state.session is None:
            return
        from .reconcile_dialog import ReconcileDialog

        peer, _ = self.state.session
        dialog = ReconcileDialog(divergence, peer.announcement.user_name, self)
        self.recorder.event("reconcile_opened", conflicts=len(divergence.conflicts))
        if dialog.exec() != dialog.DialogCode.Accepted:
            self.recorder.event("reconcile_cancelled")
            return
        self.recorder.event("reconcile_proposed", decisions=len(dialog.choices))
        self.state.start_reconcile(dialog.choices)
        self.refresh()

    def _upload(self) -> None:
        if self.state.session is None or not self.state.is_open:
            return
        if self.state.has_changes():
            self.banner.show_message(
                "Kaydedilmemiş değişikliklerin var. Önce Dosyalar sekmesinden kaydet — "
                "sadece kaydedilmiş işler gönderilebilir.",
                "warn",
            )
            return
        self.recorder.event("upload_started")
        self.state.start_upload()
        self.refresh()

    def _build_advanced_card(self) -> Card:
        card = Card()
        self.advanced_toggle = QPushButton("Gelişmiş ayarlar")
        self.advanced_toggle.setCheckable(True)
        self.advanced_toggle.clicked.connect(self._toggle_advanced)
        card.add_row(self.advanced_toggle)

        self.advanced_body = QWidget()
        body = QVBoxLayout(self.advanced_body)
        body.setContentsMargins(0, 8, 0, 0)
        body.setSpacing(10)

        self.port = QSpinBox()
        self.port.setRange(1024, 65535)
        self.port.setValue(DEFAULT_HTTPS_PORT)
        self.port.setFixedWidth(110)
        # Named so the session log says "port" instead of the enclosing card. Without this,
        # a run where someone repeatedly nudged the port recorded only "Card, Card, Card".
        self.port.setObjectName("port")
        port_row = QHBoxLayout()
        port_row.addWidget(label("Port"))
        port_row.addWidget(self.port)
        port_row.addStretch(1)
        body.addLayout(port_row)

        self.fingerprint = KeyValue("GÜVENLİK KİMLİĞİ", mono=True)
        self.fingerprint.setToolTip(
            "Bu oturumun benzersiz kimliği. Arkadaşının ekranında aynı değeri görmesi, "
            "gerçekten senin bilgisayarına bağlandığı anlamına gelir."
        )
        self.addresses = KeyValue("BU BİLGİSAYARIN ADRESLERİ", mono=True)
        self.discovery = KeyValue("AĞDA GÖRÜNÜRLÜK")
        for widget in (self.fingerprint, self.addresses, self.discovery):
            body.addWidget(widget)

        self.advanced_body.setVisible(False)
        card.add(self.advanced_body)
        return card

    # -- actions --------------------------------------------------------------------------

    def _toggle_advanced(self) -> None:
        self.advanced_body.setVisible(self.advanced_toggle.isChecked())

    def _copy_code(self) -> None:
        if self.state.server is not None:
            QGuiApplication.clipboard().setText(self.state.server.join_code)
            self.banner.show_message("Kod kopyalandı.", "info")

    def _toggle_hosting(self) -> None:
        """Start or stop hosting, with the button showing that something is happening.

        Bringing the server up blocks for a second or two. Without an interim state the
        window simply freezes, which reads as a dead button — and a second, impatient click
        lands after startup finishes and immediately shuts the session back down, so the net
        effect really is "nothing happened". The re-entry guard is what prevents that.
        """
        if self._busy:
            return
        self._busy = True
        was_hosting = self.state.is_hosting
        self.host_button.setEnabled(False)
        self.host_button.setText("Kapatılıyor…" if was_hosting else "Başlatılıyor…")
        QGuiApplication.setOverrideCursor(Qt.WaitCursor)
        QApplication.processEvents()  # Let that state actually reach the screen.

        try:
            if was_hosting:
                with self.recorder.timed("hosting_stop"):
                    self.state.stop_hosting()
                self.banner.show_message("Oturum kapatıldı.", "info")
            else:
                with self.recorder.timed("hosting_start", port=self.port.value()):
                    self.state.start_hosting(self.port.value())
                server = self.state.server
                assert server is not None
                self.banner.show_message(
                    f"Oturum açık. Arkadaşlarına bu kodu ver: {server.join_code}", "info"
                )
        except Exception as e:  # noqa: BLE001 - a silent failure here is the worst outcome
            LOGGER.exception("Hosting could not be toggled")
            self.recorder.event("error", where="hosting", message=f"{type(e).__name__}: {e}")
            # Only name the port when the port is genuinely the problem. The earlier version
            # guessed, and the guess sent someone chasing port numbers for two minutes while
            # the real fault was somewhere else entirely.
            looks_like_a_port_clash = isinstance(e, OSError) and getattr(e, "errno", None) in (
                10048,  # WSAEADDRINUSE
                98,  # EADDRINUSE
                13,  # EACCES
            )
            advice = (
                f"\n{self.port.value()} portu başka bir program tarafından kullanılıyor — "
                "Gelişmiş ayarlardan başka bir port dene."
                if looks_like_a_port_clash
                else "\nAyrıntılar 'Kullanım kaydı' klasöründeki solidgit.log dosyasında."
            )
            self.banner.show_message(f"Oturum başlatılamadı: {e}{advice}", "error")
        finally:
            QGuiApplication.restoreOverrideCursor()
            self.host_button.setEnabled(True)
            self._busy = False

        self.changed.emit()
        self.refresh()

    def _selected_session(self):
        items = self.sessions.selectedItems()
        if not items or items[0].data(0, Qt.UserRole) is None:
            return None
        return self.state.registry.get(items[0].data(0, Qt.UserRole))

    def _join_selected(self) -> None:
        peer = self._selected_session()
        if peer is None:
            return
        code, accepted = QInputDialog.getText(
            self,
            "Katılım kodu",
            f"{peer.announcement.user_name} sana bir kod verdi. Onu buraya yaz:",
            text="SG-",
        )
        if not accepted or not code.strip():
            self.recorder.event("join_cancelled")
            return
        self.recorder.event("join_started", host=peer.announcement.user_name)
        self._last_join_state = None
        self.state.join(peer, code.strip())
        self.refresh()

    # -- refresh --------------------------------------------------------------------------

    def refresh(self) -> None:
        hosting = self.state.is_hosting
        self.host_button.setText("Oturumu kapat" if hosting else "Oturumu başlat")
        self.host_button.setProperty("kind", "" if hosting else "primary")
        self.host_button.style().polish(self.host_button)
        self.port.setEnabled(not hosting)

        for widget in (self.code_caption, self.code_value, self.copy_button, self.host_hint):
            widget.setVisible(hosting)

        connected = self.state.connected_users() if hosting else []
        self.connected_label.setVisible(hosting)
        self.connected_label.setStyleSheet(
            f"color: {theme.OK if connected else theme.TEXT_MUTED};"
        )
        self.connected_label.setText(
            "Bağlı: " + ", ".join(connected) if connected else "Henüz kimse bağlanmadı."
        )

        if hosting:
            server = self.state.server
            assert server is not None
            from ..net.security import local_ip_addresses

            self.code_value.setText(server.join_code)
            self.fingerprint.set_value(server.identity.short_fingerprint)
            self.addresses.set_value(", ".join(local_ip_addresses()))
            self.discovery.set_value(
                "Görünüyorsun (mDNS + yayın)"
                if self.state.mdns and self.state.mdns.available
                else "Görünüyorsun (yayın)"
            )
            self.host_explanation.setText(
                "Oturum açık. Arkadaşların aynı ağa bağlanıp bu kodu girebilir."
            )
        elif not self.state.is_open:
            # Sharing needs something to share. Saying why beats a button that does nothing.
            self.host_explanation.setText(
                "Paylaşmak için önce bir proje aç. Arkadaşının projesini almak "
                "istiyorsan aşağıdaki listeden onun oturumuna katıl."
            )
        else:
            self.host_explanation.setText(HOST_IDLE_TEXT)
        self.host_button.setEnabled(hosting or self.state.is_open)

        if self.state.discovery_error:
            self.banner.show_message(self.state.discovery_error, "error")

        self._refresh_requests()
        self._refresh_sessions()
        self._refresh_join_status()
        self._refresh_transfer()

    def _refresh_transfer(self) -> None:
        session = self.state.session
        transfer = self.state.transfer
        running = bool(transfer and transfer.running)

        self.transfer_card.setVisible(session is not None or running)
        if session is None and not running:
            return

        if session is not None:
            peer, _ = session
            self.transfer_explanation.setText(
                f"{peer.announcement.user_name} adlı kişinin oturumundasın "
                f"({peer.announcement.repo_name})."
                + ("" if self.state.is_open else " Proje henüz bu bilgisayarda yok.")
            )

        self.download_button.setText(
            "Dosyaları indir" if not self.state.is_open else "Yeni değişiklikleri al"
        )
        self.download_button.setEnabled(self.state.can_transfer)
        self.upload_button.setEnabled(self.state.can_transfer and self.state.is_open)
        self.reconcile_button.setVisible(self.state.divergence is not None)
        self.reconcile_button.setEnabled(self.state.can_transfer)
        self.stop_transfer_button.setEnabled(running)
        self.transfer_progress.setVisible(running)

        if transfer is None:
            self.transfer_status.setText("")
            return

        self.transfer_progress.setValue(transfer.percent)
        colour = (
            theme.TEXT_MUTED
            if transfer.running
            else (theme.OK if transfer.ok else theme.REMOVED)
        )
        self.transfer_status.setStyleSheet(f"color: {colour};")
        detail = f"  ·  {transfer.detail}" if transfer.detail else ""
        self.transfer_status.setText(f"{transfer.message}{detail}")

        if self.state.version_warning:
            self.banner.show_message(self.state.version_warning, "warn")

    def _refresh_requests(self) -> None:
        # Only rebuild when the set of requests actually changes. Rebuilding every second
        # meant "İzin ver" was destroyed and recreated under the user's cursor — a click
        # landing on that boundary simply vanished — and the half-deleted old row was
        # still painted for a moment, drawing every request twice.
        signature = (
            tuple(p.plan.plan_id for p in self.state.pending_reconciles()),
            tuple(r.device_id for r in self.state.pending_join_requests()),
        )
        if signature == self._requests_signature:
            return
        self._requests_signature = signature

        while self.requests_body.count():
            item = self.requests_body.takeAt(0)
            if item.widget():
                item.widget().hide()  # gone now, not whenever deleteLater gets to it
                item.widget().deleteLater()

        for pending in self.state.pending_reconciles():
            # Approving a merge is a bigger decision than letting a laptop in, so it says
            # exactly how many files it settles and who proposed it.
            row = _transparent_row()
            box = QHBoxLayout(row)
            box.setContentsMargins(0, 6, 0, 0)
            box.addWidget(
                QLabel(
                    f"<b>{pending.user_name}</b> birleştirme öneriyor — "
                    f"{len(pending.plan.resolution)} dosya karara bağlanacak"
                )
            )
            box.addStretch(1)
            accept = _primary("Onayla")
            refuse = QPushButton("Reddet")
            refuse.setProperty("kind", "danger")
            accept.clicked.connect(
                lambda _, p=pending.plan.plan_id: self._approve_reconcile(p)
            )
            refuse.clicked.connect(lambda _, p=pending.plan.plan_id: self._reject_reconcile(p))
            box.addWidget(accept)
            box.addWidget(refuse)
            self.requests_body.addWidget(row)

        for request in self.state.pending_join_requests():
            row = _transparent_row()
            box = QHBoxLayout(row)
            box.setContentsMargins(0, 6, 0, 0)
            box.addWidget(
                QLabel(f"<b>{request.user_name}</b> ({request.device_name}) katılmak istiyor")
            )
            box.addStretch(1)

            allow = _primary("İzin ver")
            always = QPushButton("Hep izin ver")
            always.setToolTip("Bu cihaz bir daha sorulmadan katılabilir.")
            refuse = QPushButton("Reddet")
            refuse.setProperty("kind", "danger")
            allow.clicked.connect(lambda _, d=request.device_id: self._approve(d, False))
            always.clicked.connect(lambda _, d=request.device_id: self._approve(d, True))
            refuse.clicked.connect(lambda _, d=request.device_id: self._reject(d))
            for button in (allow, always, refuse):
                box.addWidget(button)
            self.requests_body.addWidget(row)

    def _approve_reconcile(self, plan_id: str) -> None:
        self.state.approve_reconcile(plan_id)
        self.recorder.event("reconcile_approved")
        self.banner.show_message("Birleştirme onaylandı.", "info")
        self.refresh()

    def _reject_reconcile(self, plan_id: str) -> None:
        self.state.reject_reconcile(plan_id)
        self.recorder.event("reconcile_rejected")
        self.banner.show_message("Birleştirme reddedildi — hiçbir şey değişmedi.", "warn")
        self.refresh()

    def _approve(self, device_id: str, always: bool) -> None:
        self.state.approve(device_id, always_allow=always)
        self.banner.show_message("Cihaza izin verildi.", "info")
        self.refresh()

    def _reject(self, device_id: str) -> None:
        self.state.reject(device_id)
        self.banner.show_message("Cihaz reddedildi.", "warn")
        self.refresh()

    def _refresh_sessions(self) -> None:
        keep = None
        if self.sessions.selectedItems():
            keep = self.sessions.selectedItems()[0].data(0, Qt.UserRole)

        self.sessions.clear()
        found = self.state.nearby_sessions()
        if not found:
            placeholder = QTreeWidgetItem(
                [
                    "Yakında oturum yok",
                    "Arkadaşın oturumu başlattığı hâlde görünmüyorsa, Windows Güvenlik "
                    "Duvarı izni engelliyor olabilir.",
                    "",
                ]
            )
            for column in (0, 1):
                placeholder.setForeground(column, _brush(theme.TEXT_FAINT))
            self.sessions.addTopLevelItem(placeholder)

        for peer in found:
            a = peer.announcement
            item = QTreeWidgetItem([a.user_name, a.repo_name, peer.url])
            item.setData(0, Qt.UserRole, a.device_id)
            self.sessions.addTopLevelItem(item)
            if a.device_id == keep:
                item.setSelected(True)
        self._update_join_button()

    def _update_join_button(self) -> None:
        attempt = self.state.join_attempt
        busy = attempt is not None and not attempt.finished
        self.join_button.setEnabled(self._selected_session() is not None and not busy)

    def _refresh_join_status(self) -> None:
        attempt = self.state.join_attempt
        if attempt is None:
            self.join_status.setText("")
            self._last_join_state = None
            return

        if attempt.state != self._last_join_state:
            self._last_join_state = attempt.state
            self.recorder.event(
                "join_progress", state=attempt.state, message=attempt.message
            )

        colour = {
            "running": theme.TEXT_MUTED,
            "waiting": theme.CHANGED,
            "joined": theme.OK,
            "failed": theme.REMOVED,
        }[attempt.state]
        self.join_status.setStyleSheet(f"color: {colour};")
        self.join_status.setText(f"{attempt.label} — {attempt.message}")


