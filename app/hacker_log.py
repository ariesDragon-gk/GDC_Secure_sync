"""A hacker/terminal-styled log console: matrix-green-on-black monospace
text, new lines revealed with a typewriter effect, and a blinking block
cursor while idle — modeled on the ui-ux-pro-max skill's "Terminal CLI"
style tokens (blinking cursor ~500ms, typewriter text reveal, monospace,
0px border-radius).

Lines are queued and normally typed out one character at a time. On a large
sync (tens of thousands of per-file confirmation lines), that queue can grow
far faster than the typewriter can drain it, so the log falls further and
further behind real time — new activity queues up behind a backlog that can
take hours to animate through, and what's on screen keeps showing stale,
old-timestamped lines. Once the backlog crosses a threshold, remaining
queued lines are flushed instantly (no per-character delay) so the display
catches back up to the present; the typewriter effect resumes for the next
line once the queue drains back down.
"""
from __future__ import annotations

import time
from typing import List, Tuple

from PySide6.QtCore import QTimer
from PySide6.QtGui import QColor, QFont, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import QPlainTextEdit

from .theme import ALERT_RED, BG_VOID, MONO_FONT, TERMINAL_GREEN

_CHAR_INTERVAL_MS = 8
_BLINK_INTERVAL_MS = 500
_MAX_BLOCK_COUNT = 2000
_CURSOR_GLYPH = "█"
_BACKLOG_INSTANT_THRESHOLD = 15

COLOR_MAP = {
    "green": TERMINAL_GREEN,
    "red": ALERT_RED,
    "": TERMINAL_GREEN,
}


class HackerLogWidget(QPlainTextEdit):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("hackerLog")
        self.setReadOnly(True)
        self.setMaximumBlockCount(_MAX_BLOCK_COUNT)
        self.setFont(QFont(MONO_FONT, 10))
        self.setStyleSheet(f"QPlainTextEdit#hackerLog {{ background-color: {BG_VOID}; color: {TERMINAL_GREEN}; border: 1px solid #26314a; }}")

        self._queue: List[Tuple[str, str]] = []
        self._typing = False
        self._current_text = ""
        self._current_color = TERMINAL_GREEN
        self._current_index = 0
        self._cursor_glyph_present = False
        self._blink_on = False

        self._type_timer = QTimer(self)
        self._type_timer.setInterval(_CHAR_INTERVAL_MS)
        self._type_timer.timeout.connect(self._type_next_char)

        self._blink_timer = QTimer(self)
        self._blink_timer.setInterval(_BLINK_INTERVAL_MS)
        self._blink_timer.timeout.connect(self._toggle_cursor_glyph)
        self._blink_timer.start()

    def append_line(self, text: str, color: str = "") -> None:
        timestamp = time.strftime("[%H:%M:%S] ")
        self._queue.append((timestamp + text, COLOR_MAP.get(color, TERMINAL_GREEN)))
        if not self._typing:
            self._start_next_line()

    def flush_now(self) -> None:
        """Manual "Refresh" control: instantly render anything still queued
        or mid-typewriter and jump to the latest line, instead of waiting for
        the backlog threshold to trip on its own."""
        self._type_timer.stop()
        if self._typing and self._current_index < len(self._current_text):
            remaining = self._current_text[self._current_index:]
            cursor = self.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.End)
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(self._current_color))
            cursor.setCharFormat(fmt)
            cursor.insertText(remaining)
            self.setTextCursor(cursor)
        self._flush_backlog_instantly()
        self._typing = False
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.setTextCursor(cursor)
        self.ensureCursorVisible()

    # -------------------------------------------------------- typewriter

    def _start_next_line(self) -> None:
        if not self._queue:
            self._typing = False
            return
        if len(self._queue) > _BACKLOG_INSTANT_THRESHOLD:
            self._flush_backlog_instantly()
            self._typing = False
            return
        self._typing = True
        self._remove_cursor_glyph()
        self._current_text, self._current_color = self._queue.pop(0)
        self._current_index = 0
        self._begin_new_block()
        self._type_timer.start()

    def _flush_backlog_instantly(self) -> None:
        self._remove_cursor_glyph()
        # Anything past the widget's own block cap would be scrolled off the
        # instant it's inserted anyway (see setMaximumBlockCount), and the
        # point of catching up is to show CURRENT activity, not grind through
        # a stale backlog — so drop everything except the most recent lines.
        if len(self._queue) > _MAX_BLOCK_COUNT:
            self._queue = self._queue[-_MAX_BLOCK_COUNT:]
        while self._queue:
            text, color = self._queue.pop(0)
            self._begin_new_block()
            cursor = self.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.End)
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(color))
            cursor.setCharFormat(fmt)
            cursor.insertText(text)
            self.setTextCursor(cursor)
        self.ensureCursorVisible()

    def _begin_new_block(self) -> None:
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        if self.document().characterCount() > 1:
            cursor.insertBlock()
        self.setTextCursor(cursor)

    def _type_next_char(self) -> None:
        if self._current_index >= len(self._current_text):
            self._type_timer.stop()
            self._start_next_line()
            return
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(self._current_color))
        cursor.setCharFormat(fmt)
        cursor.insertText(self._current_text[self._current_index])
        self.setTextCursor(cursor)
        self.ensureCursorVisible()
        self._current_index += 1

    # ------------------------------------------------------ idle cursor

    def _toggle_cursor_glyph(self) -> None:
        if self._typing:
            return
        if self._cursor_glyph_present:
            self._remove_cursor_glyph()
        else:
            self._add_cursor_glyph()

    def _add_cursor_glyph(self) -> None:
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(TERMINAL_GREEN))
        cursor.setCharFormat(fmt)
        cursor.insertText(_CURSOR_GLYPH)
        self._cursor_glyph_present = True

    def _remove_cursor_glyph(self) -> None:
        if not self._cursor_glyph_present:
            return
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.movePosition(QTextCursor.MoveOperation.PreviousCharacter, QTextCursor.MoveMode.KeepAnchor)
        if cursor.selectedText() == _CURSOR_GLYPH:
            cursor.removeSelectedText()
        self._cursor_glyph_present = False
