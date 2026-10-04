"""Choosing between two versions of the same part.

There is no automatic answer here and there never will be — `.sldprt` files are opaque, so
the only honest resolution is a person looking at both and deciding. The dialog's job is to
make the decision small: show what is genuinely contested, and say plainly that everything
else has already been handled without loss.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from ..core.reconcile import CONFLICTING, DiffStatus, Side
from . import theme
from .widgets import Banner, human_size, label

_STATUS_TEXT = {
    DiffStatus.BOTH_MODIFIED: "ikiniz de değiştirdiniz",
    DiffStatus.MODIFY_DELETE: "biri değiştirdi, diğeri sildi",
    DiffStatus.DIFFERENT: "farklı",
    DiffStatus.ADDED_BY_MINE: "sen ekledin",
    DiffStatus.ADDED_BY_THEIRS: "o ekledi",
    DiffStatus.MODIFIED_BY_MINE: "sen değiştirdin",
    DiffStatus.MODIFIED_BY_THEIRS: "o değiştirdi",
    DiffStatus.DELETED_BY_MINE: "sen sildin",
    DiffStatus.DELETED_BY_THEIRS: "o sildi",
    DiffStatus.ONLY_MINE: "sadece sende",
    DiffStatus.ONLY_THEIRS: "sadece onda",
}


class ReconcileDialog(QDialog):
    """Collects one decision per contested file. Returns them via `choices`."""

    def __init__(self, divergence, their_name: str, parent=None) -> None:
        super().__init__(parent)
        self.divergence = divergence
        self.choices: dict[str, Side] = {}
        self._selectors: dict[str, QComboBox] = {}

        self.setWindowTitle("Ayrı çalışmışsınız — birleştirme")
        self.setMinimumSize(820, 560)
        self.setStyleSheet(theme.STYLESHEET)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)

        layout.addWidget(label("İkiniz de ayrı ayrı çalışmışsınız", "title"))
        conflicts = [d for d in divergence.diffs if d.status in CONFLICTING]
        automatic = [
            d
            for d in divergence.diffs
            if d.status is not DiffStatus.SAME and d.status not in CONFLICTING
        ]

        summary = QLabel(
            f"{len(automatic)} dosya kendiliğinden çözüldü — hiçbiri kaybolmuyor. "
            f"<b>{len(conflicts)} dosyada</b> ikiniz de değişiklik yapmışsınız; "
            "bunlarda hangisinin kalacağına sen karar vereceksin."
            if conflicts
            else f"{len(automatic)} dosya kendiliğinden çözülüyor, karar gerektiren dosya yok."
        )
        summary.setWordWrap(True)
        summary.setStyleSheet(f"color: {theme.TEXT_MUTED};")
        layout.addWidget(summary)

        self.banner = Banner(
            "Seçmediğin sürüm silinmez — geçmişte kalır, sonradan geri alabilirsin.", "info"
        )
        layout.addWidget(self.banner)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(4)
        self.tree.setHeaderLabels(["DOSYA", "DURUM", "BOYUT", "HANGİSİ KALSIN"])
        self.tree.setRootIsDecorated(False)
        header = self.tree.header()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.Fixed)
        header.resizeSection(1, 190)
        header.setSectionResizeMode(2, QHeaderView.Fixed)
        header.resizeSection(2, 110)
        header.setSectionResizeMode(3, QHeaderView.Fixed)
        header.resizeSection(3, 190)
        layout.addWidget(self.tree, 1)

        for diff in conflicts + automatic:
            self._add_row(diff, their_name, decidable=diff.status in CONFLICTING)

        self.apply_button = QPushButton("Birleştirmeyi öner")
        self.apply_button.setProperty("kind", "primary")
        self.apply_button.setToolTip(
            f"{their_name} aynı seçimleri onaylamadan hiçbir dosya değişmez."
        )
        self.apply_button.clicked.connect(self._accept)
        cancel = QPushButton("Vazgeç")
        cancel.clicked.connect(self.reject)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(cancel)
        buttons.addWidget(self.apply_button)
        layout.addLayout(buttons)

    def _add_row(self, diff, their_name: str, decidable: bool) -> None:
        size = (diff.mine or diff.theirs).size if (diff.mine or diff.theirs) else 0
        item = QTreeWidgetItem(
            [diff.path, _STATUS_TEXT.get(diff.status, diff.status.value), human_size(size), ""]
        )
        if decidable:
            item.setForeground(1, _brush(theme.CHANGED))
        else:
            item.setForeground(0, _brush(theme.TEXT_MUTED))
            item.setForeground(1, _brush(theme.TEXT_FAINT))
        self.tree.addTopLevelItem(item)

        if not decidable:
            side = diff.default_side
            item.setText(3, "otomatik")
            item.setForeground(3, _brush(theme.TEXT_FAINT))
            if side is not None:
                self.choices.pop(diff.path, None)
            return

        selector = QComboBox()
        selector.addItem("— seç —", None)
        selector.addItem("Benimki kalsın", Side.MINE)
        selector.addItem(f"{their_name} kalsın", Side.THEIRS)
        selector.currentIndexChanged.connect(lambda _: self._update_button())
        self.tree.setItemWidget(item, 3, selector)
        self._selectors[diff.path] = selector

    def _update_button(self) -> None:
        undecided = [p for p, s in self._selectors.items() if s.currentData() is None]
        self.apply_button.setEnabled(not undecided)
        if undecided:
            self.banner.show_message(
                f"{len(undecided)} dosyada henüz seçim yapmadın.", "warn"
            )
        else:
            self.banner.show_message(
                "Seçmediğin sürüm silinmez — geçmişte kalır, sonradan geri alabilirsin.",
                "info",
            )

    def _accept(self) -> None:
        self.choices = {
            path: selector.currentData()
            for path, selector in self._selectors.items()
            if selector.currentData() is not None
        }
        self.accept()

    def showEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().showEvent(event)
        self._update_button()


def _brush(colour: str):
    from PySide6.QtGui import QBrush, QColor

    return QBrush(QColor(colour))
