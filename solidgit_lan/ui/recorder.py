"""Turns interactions with *this application's* windows into recorded events.

The filter is installed on our own QApplication, so it only ever sees events Qt delivers to
our widgets. It does not observe other programs, and key events are deliberately ignored so
that nothing anyone types is captured.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject
from PySide6.QtWidgets import QAbstractButton, QWidget

from ..telemetry import SessionRecorder

#: How far up the parent chain to look for something worth naming. Clicks land on internal
#: children (a button's label, a view's viewport) rather than the widget people think of.
_MAX_DEPTH = 6


class InteractionRecorder(QObject):
    """Records which control was pressed, and on which page."""

    def __init__(self, recorder: SessionRecorder, page_of=None) -> None:
        super().__init__()
        self._recorder = recorder
        self._page_of = page_of

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802 - Qt naming
        if event.type() == QEvent.Type.MouseButtonRelease and isinstance(watched, QWidget):
            described = self._describe(watched)
            if described is not None:
                self._recorder.event(
                    "click",
                    control=described,
                    page=self._page_of() if self._page_of else None,
                )
        return False  # Always pass the event on; observing must never change behaviour.

    def _describe(self, widget: QWidget) -> str | None:
        current: QObject | None = widget
        for _ in range(_MAX_DEPTH):
            if current is None:
                return None
            if isinstance(current, QAbstractButton):
                text = current.text().strip()
                return text or current.objectName() or type(current).__name__
            if isinstance(current, QWidget) and current.objectName():
                return current.objectName()
            current = current.parent()
        return type(widget).__name__
