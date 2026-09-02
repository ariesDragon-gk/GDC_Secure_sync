"""Dark tactical/federal-ops visual theme + hacker-terminal log styling.

Tokens adapted from the ui-ux-pro-max skill's design catalog (installed at
.claude/skills/ui-ux-pro-max/data/styles.csv): the "Terminal CLI" style
(Matrix Green #33FF00 on OLED Black #050505, blinking cursor, typewriter
reveal, monospace, 0px border-radius) for the log, and "HUD / Sci-Fi FUI"
(dark background, technical blue accent, sharp borders) for the rest of
the app chrome — toned down from neon to read as "federal ops" rather
than arcade cyberpunk.

Per the skill's accessibility priority (#1: contrast 4.5:1 minimum) —
every foreground/background pair below is a high-contrast pair (mostly
near-white or saturated color on near-black), and every color-coded
status is paired with a symbol (checkmark/tick/cross), not color alone.
"""
from __future__ import annotations

BG_VOID = "#050505"  # OLED black — main window / log background
BG_PANEL = "#0d1117"  # slightly lifted panel background
BORDER_DIM = "#26314a"
TEXT_PRIMARY = "#e6edf3"
TEXT_MUTED = "#8b96a5"
ACCENT_BLUE = "#1f6feb"
TERMINAL_GREEN = "#33ff00"
ALERT_RED = "#ff3333"

MONO_FONT = "Consolas"
MONO_FONT_FALLBACK = "'Consolas', 'JetBrains Mono', 'Courier New', monospace"

STYLESHEET = f"""
QWidget {{
    background-color: {BG_VOID};
    color: {TEXT_PRIMARY};
    font-family: {MONO_FONT_FALLBACK};
    font-size: 10pt;
}}
QMainWindow {{
    background-color: {BG_VOID};
}}
QGroupBox {{
    background-color: {BG_PANEL};
    border: 1px solid {BORDER_DIM};
    border-radius: 0px;
    margin-top: 14px;
    padding: 10px 8px 8px 8px;
    font-weight: bold;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 6px;
    color: {ACCENT_BLUE};
}}
QPushButton {{
    background-color: #0f1620;
    border: 1px solid {ACCENT_BLUE};
    border-radius: 0px;
    color: {ACCENT_BLUE};
    padding: 6px 14px;
    font-weight: bold;
}}
QPushButton:hover {{
    background-color: {ACCENT_BLUE};
    color: #000000;
}}
QPushButton:disabled {{
    color: {TEXT_MUTED};
    border-color: {BORDER_DIM};
}}
QPushButton:pressed {{
    background-color: #0a3d8f;
}}
QLineEdit {{
    background-color: #0a0e14;
    border: 1px solid {BORDER_DIM};
    border-radius: 0px;
    color: {TEXT_PRIMARY};
    padding: 4px;
    selection-background-color: {ACCENT_BLUE};
}}
QLineEdit:focus {{
    border: 1px solid {ACCENT_BLUE};
}}
QTreeWidget, QPlainTextEdit {{
    background-color: #0a0e14;
    border: 1px solid {BORDER_DIM};
    border-radius: 0px;
    color: {TEXT_PRIMARY};
    selection-background-color: {ACCENT_BLUE};
}}
QTreeWidget::item:selected {{
    background-color: {ACCENT_BLUE};
    color: #000000;
}}
QHeaderView::section {{
    background-color: {BG_PANEL};
    color: {ACCENT_BLUE};
    border: 1px solid {BORDER_DIM};
    padding: 4px;
    font-weight: bold;
}}
QTabWidget::pane {{
    border: 1px solid {BORDER_DIM};
    border-radius: 0px;
}}
QTabBar::tab {{
    background-color: {BG_PANEL};
    color: {TEXT_MUTED};
    border: 1px solid {BORDER_DIM};
    padding: 8px 22px;
    font-weight: bold;
}}
QTabBar::tab:selected {{
    color: {ACCENT_BLUE};
    border-bottom: 2px solid {ACCENT_BLUE};
}}
QProgressBar {{
    background-color: #0a0e14;
    border: 1px solid {BORDER_DIM};
    border-radius: 0px;
    color: {TEXT_PRIMARY};
    text-align: center;
}}
QProgressBar::chunk {{
    background-color: {ACCENT_BLUE};
}}
QSplitter::handle {{
    background-color: {BORDER_DIM};
}}
QLabel {{
    background: transparent;
}}
QMessageBox QLabel {{
    color: {TEXT_PRIMARY};
}}
QScrollBar:vertical {{
    background: {BG_VOID};
    width: 12px;
    border: none;
}}
QScrollBar::handle:vertical {{
    background: {BORDER_DIM};
    min-height: 20px;
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0px;
}}
"""
