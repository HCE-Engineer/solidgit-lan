"""Dark theme.

One stylesheet applied to the whole application rather than per-widget styling, so a colour
only ever has to change in one place. Status colours are named by meaning, not by hue — the
file list, the history and the peer list all have to agree on what "locked by someone else"
looks like.
"""

from __future__ import annotations

BACKGROUND = "#17181c"
SURFACE = "#1f2126"
SURFACE_RAISED = "#262931"
BORDER = "#32353f"
BORDER_STRONG = "#3d414d"

TEXT = "#e6e8ee"
TEXT_MUTED = "#8b8f9c"
TEXT_FAINT = "#6a6e7a"

ACCENT = "#5b8def"
ACCENT_HOVER = "#6f9bf2"
ACCENT_PRESSED = "#4a7ade"

OK = "#4ade80"
CHANGED = "#fbbf24"
ADDED = "#60a5fa"
REMOVED = "#f87171"
LOCKED_BY_ME = "#a78bfa"
LOCKED_BY_OTHER = "#f87171"
SOFT_CLAIM = "#fbbf24"

STYLESHEET = f"""
QWidget {{
    background-color: {BACKGROUND};
    color: {TEXT};
    font-family: "Segoe UI", "Inter", sans-serif;
    font-size: 13px;
}}

/* Labels inherit the window background from the QWidget rule above, which reads as a dark
   box wherever they sit on a lighter panel. They should always take their parent's colour. */
QLabel {{ background: transparent; }}

QLabel[role="title"]    {{ font-size: 20px; font-weight: 600; }}
QLabel[role="subtitle"] {{ font-size: 13px; color: {TEXT_MUTED}; }}
QLabel[role="section"]  {{ font-size: 11px; font-weight: 600; color: {TEXT_FAINT};
                           letter-spacing: 1px; }}
QLabel[role="mono"]     {{ font-family: "Cascadia Mono", "Consolas", monospace; }}
QLabel[role="hint"]     {{ font-size: 14px; color: {TEXT}; padding: 2px 0; }}
QLabel[role="code"]     {{ font-family: "Cascadia Mono", "Consolas", monospace;
                           font-size: 30px; font-weight: 600; color: {ACCENT};
                           letter-spacing: 3px; }}

#Sidebar {{
    background-color: {SURFACE};
    border-right: 1px solid {BORDER};
}}
#Sidebar QPushButton {{
    text-align: left;
    padding: 10px 14px;
    border: none;
    border-radius: 8px;
    color: {TEXT_MUTED};
    font-size: 14px;
    background: transparent;
}}
#Sidebar QPushButton:hover  {{ background-color: {SURFACE_RAISED}; color: {TEXT}; }}
/* With no project open only the network page is reachable; the others must look it. */
#Sidebar QPushButton:disabled {{ color: #4a4e59; }}
#Sidebar QPushButton:checked {{ background-color: {SURFACE_RAISED}; color: {TEXT};
                                font-weight: 600; }}

#Header {{
    background-color: {SURFACE};
    border-bottom: 1px solid {BORDER};
}}

#Card {{
    background-color: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 10px;
}}

QPushButton {{
    background-color: {SURFACE_RAISED};
    border: 1px solid {BORDER_STRONG};
    border-radius: 7px;
    padding: 7px 14px;
    color: {TEXT};
}}
QPushButton:hover    {{ background-color: #2e323b; }}
QPushButton:disabled {{ color: {TEXT_FAINT}; background-color: {SURFACE};
                        border-color: {BORDER}; }}
QPushButton[kind="primary"] {{
    background-color: {ACCENT};
    border: 1px solid {ACCENT};
    color: #ffffff;
    font-weight: 600;
}}
QPushButton[kind="primary"]:hover    {{ background-color: {ACCENT_HOVER};
                                        border-color: {ACCENT_HOVER}; }}
QPushButton[kind="primary"]:pressed  {{ background-color: {ACCENT_PRESSED}; }}
QPushButton[kind="primary"]:disabled {{ background-color: {SURFACE_RAISED};
                                        border-color: {BORDER}; color: {TEXT_FAINT}; }}
QPushButton[kind="danger"] {{ color: {REMOVED}; border-color: #5a3438; }}
QPushButton[kind="danger"]:hover {{ background-color: #3a2529; }}

QSpinBox {{
    background-color: {BACKGROUND};
    border: 1px solid {BORDER_STRONG};
    border-radius: 7px;
    padding: 6px 8px;
    min-width: 80px;
}}
/* The spin buttons are left unstyled on purpose: overriding them without supplying arrow
   images renders them blank. */

QLineEdit, QPlainTextEdit {{
    background-color: {BACKGROUND};
    border: 1px solid {BORDER_STRONG};
    border-radius: 7px;
    padding: 8px 10px;
    selection-background-color: {ACCENT};
}}
QLineEdit:focus, QPlainTextEdit:focus {{ border-color: {ACCENT}; }}

QTreeWidget, QListWidget {{
    background-color: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 10px;
    outline: none;
    alternate-background-color: {SURFACE};
}}
QTreeWidget::item, QListWidget::item {{
    padding: 7px 4px;
    border-bottom: 1px solid #24262c;
}}
QTreeWidget::item:selected, QListWidget::item:selected {{
    background-color: #2d3446;
    color: {TEXT};
}}
QHeaderView::section {{
    background-color: {SURFACE};
    color: {TEXT_FAINT};
    border: none;
    border-bottom: 1px solid {BORDER};
    padding: 8px 6px;
    font-size: 11px;
    font-weight: 600;
}}

QScrollBar:vertical {{ background: transparent; width: 10px; margin: 4px; }}
QScrollBar::handle:vertical {{ background: {BORDER_STRONG}; border-radius: 5px;
                               min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: #4b505e; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 4px; }}
QScrollBar::handle:horizontal {{ background: {BORDER_STRONG}; border-radius: 5px;
                                 min-width: 30px; }}

QToolTip {{
    background-color: {SURFACE_RAISED};
    color: {TEXT};
    border: 1px solid {BORDER_STRONG};
    padding: 6px;
    border-radius: 6px;
}}

QSplitter::handle {{ background-color: {BORDER}; }}
QMessageBox {{ background-color: {SURFACE}; }}
"""
