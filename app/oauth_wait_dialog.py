"""Small modal dialog shown while the user completes sign-in in their real
system browser. Polls the LoopbackAuthServer for the captured redirect
instead of embedding any browser UI in-process.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import QDialog, QLabel, QPushButton, QVBoxLayout

from .oauth_local_server import LoopbackAuthServer

_POLL_INTERVAL_MS = 200


class BrowserSignInDialog(QDialog):
    code_received = Signal(str)
    auth_failed = Signal(str)
    cancelled = Signal()

    def __init__(self, provider_label: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Sign in to {provider_label}")
        self.resize(420, 160)
        self._done = False
        self._server: Optional[LoopbackAuthServer] = None

        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                f"A browser window opened for you to sign in to {provider_label}.\n\n"
                "Complete sign-in there — this continues automatically once you do."
            )
        )
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        layout.addWidget(cancel_btn)

        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(_POLL_INTERVAL_MS)
        self._poll_timer.timeout.connect(self._poll)

    def start(self, server: LoopbackAuthServer) -> None:
        self._server = server
        self._poll_timer.start()

    def _poll(self) -> None:
        if self._done or not self._server:
            return
        result = self._server.result
        if result is None:
            return
        code, error = result
        self._finish(code=code, error=error)

    def _finish(self, code: Optional[str] = None, error: Optional[str] = None) -> None:
        if self._done:
            return
        self._done = True
        self._poll_timer.stop()
        if self._server:
            self._server.shutdown()
        if code:
            self.code_received.emit(code)
        else:
            self.auth_failed.emit(error or "Authorization was denied or cancelled.")
        self.accept()

    def reject(self) -> None:
        if not self._done:
            self._done = True
            self._poll_timer.stop()
            if self._server:
                self._server.shutdown()
            self.cancelled.emit()
        super().reject()
