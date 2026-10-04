"""Small shared pieces: cards, labelled text, status dots."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from . import theme


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def label(text: str, role: str = "") -> QLabel:
    widget = QLabel(text)
    if role:
        widget.setProperty("role", role)
    return widget


def dot_icon(colour: str, size: int = 10) -> QIcon:
    """A filled circle, used as a status marker in list rows."""
    pixmap = QPixmap(size + 6, size + 6)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setBrush(QColor(colour))
    painter.setPen(Qt.NoPen)
    painter.drawEllipse(3, 3, size, size)
    painter.end()
    return QIcon(pixmap)


class Card(QFrame):
    """A titled panel. Everything on the network page is one of these."""

    def __init__(self, title: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(18, 16, 18, 16)
        self._layout.setSpacing(12)
        if title:
            self._layout.addWidget(label(title.upper(), "section"))

    def body(self) -> QVBoxLayout:
        return self._layout

    def add(self, widget: QWidget) -> QWidget:
        self._layout.addWidget(widget)
        return widget

    def add_row(self, *widgets: QWidget, stretch_last: bool = False) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(10)
        for widget in widgets:
            row.addWidget(widget)
        if not stretch_last:
            row.addStretch(1)
        self._layout.addLayout(row)
        return row


class KeyValue(QWidget):
    """A muted caption above a value — used for join code, fingerprint, addresses."""

    def __init__(self, caption: str, value: str = "", mono: bool = False) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        layout.addWidget(label(caption, "section"))
        self.value = label(value, "mono" if mono else "")
        self.value.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.value)

    def set_value(self, text: str) -> None:
        self.value.setText(text)


class Banner(QFrame):
    """An inline message strip. Used for errors and for what is not built yet."""

    def __init__(self, text: str = "", tone: str = "info") -> None:
        super().__init__()
        self.setObjectName("Banner")
        self._label = QLabel(text)
        self._label.setWordWrap(True)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.addWidget(self._label)
        self.set_tone(tone)
        self.setVisible(bool(text))

    def set_tone(self, tone: str) -> None:
        colour = {
            "info": theme.ACCENT,
            "warn": theme.CHANGED,
            "error": theme.REMOVED,
        }.get(tone, theme.ACCENT)
        # Square left corners: a 3px accent bar curving around a rounded corner renders as a
        # stray bracket glyph. Flat on that edge reads as deliberate.
        #
        # Scoped to #Banner on purpose. A bare `QFrame` selector also matches the QLabel
        # inside (QLabel *is* a QFrame), which drew a second, inner border tight around the
        # text — a box within the box.
        self.setStyleSheet(
            f"#Banner {{ background-color: {theme.SURFACE}; border: 1px solid {colour};"
            f" border-left: 3px solid {colour};"
            f" border-top-right-radius: 8px; border-bottom-right-radius: 8px;"
            f" border-top-left-radius: 0px; border-bottom-left-radius: 0px; }}"
            f" QLabel {{ color: {theme.TEXT}; background: transparent; border: none; }}"
        )

    def show_message(self, text: str, tone: str = "info") -> None:
        self._label.setText(text)
        self.set_tone(tone)
        self.setVisible(bool(text))

    def clear(self) -> None:
        self._label.setText("")
        self.setVisible(False)
