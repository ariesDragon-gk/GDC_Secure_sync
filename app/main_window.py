from __future__ import annotations

import time

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .config import load_settings
from .dropbox_panel import DropboxPanel
from .hacker_log import HackerLogWidget
from .onedrive_panel import OneDrivePanel
from .sync_history_dialog import SyncHistoryDialog
from .theme import ACCENT_BLUE


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("SECURE CLOUD SYNC TERMINAL")
        self.resize(1360, 940)

        self.settings = load_settings()

        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        banner = QLabel("/ /  S E C U R E   C L O U D   S Y N C   O P E R A T I O N S   T E R M I N A L   —   R E S T R I C T E D   A C C E S S  / /")
        banner.setAlignment(Qt.AlignmentFlag.AlignCenter)
        banner.setStyleSheet(f"color: {ACCENT_BLUE}; font-weight: bold; padding: 4px;")
        layout.addWidget(banner)

        self.tabs = QTabWidget()
        self.dropbox_panel = DropboxPanel(self.settings)
        self.onedrive_panel = OneDrivePanel(self.settings)
        self.tabs.addTab(self.dropbox_panel, "DROPBOX")
        self.tabs.addTab(self.onedrive_panel, "ONEDRIVE")
        layout.addWidget(self.tabs, 3)

        self.dropbox_panel.log_message.connect(self.log)
        self.onedrive_panel.log_message.connect(self.log)
        self.dropbox_panel.log_message_colored.connect(self.log_colored)
        self.onedrive_panel.log_message_colored.connect(self.log_colored)

        log_header = QHBoxLayout()
        log_header.addWidget(QLabel("Live Log"))
        log_header.addStretch(1)
        self.refresh_log_btn = QPushButton("⟳ Refresh")
        self.refresh_log_btn.setToolTip("Jump to the latest log entries immediately")
        self.refresh_log_btn.clicked.connect(self.on_refresh_log_clicked)
        log_header.addWidget(self.refresh_log_btn)
        self.export_log_btn = QPushButton("⇩ Export Logs")
        self.export_log_btn.setToolTip("Save the current log output to a file")
        self.export_log_btn.clicked.connect(self.on_export_log_clicked)
        log_header.addWidget(self.export_log_btn)
        self.history_btn = QPushButton("📅 Sync History")
        self.history_btn.setToolTip("How many syncs happened, per date, with speed and duration")
        self.history_btn.clicked.connect(self.on_history_clicked)
        log_header.addWidget(self.history_btn)
        layout.addLayout(log_header)

        self.log_view = HackerLogWidget()
        layout.addWidget(self.log_view, 1)

    def log(self, message: str) -> None:
        self.log_view.append_line(message)

    def log_colored(self, message: str, color: str) -> None:
        self.log_view.append_line(message, color)

    def on_refresh_log_clicked(self) -> None:
        self.log_view.flush_now()

    def on_history_clicked(self) -> None:
        dialog = SyncHistoryDialog(self)
        dialog.exec()

    def on_export_log_clicked(self) -> None:
        default_name = f"sync_log_{time.strftime('%Y%m%d_%H%M%S')}.txt"
        path, _ = QFileDialog.getSaveFileName(
            self, "Export logs", default_name, "Text files (*.txt);;Log files (*.log);;All files (*)"
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.log_view.toPlainText())
        except OSError as exc:
            QMessageBox.critical(self, "Export failed", f"Could not write log file:\n{exc}")
            return
        self.log_view.append_line(f"Logs exported to {path}", "green")

    def closeEvent(self, event) -> None:
        self.dropbox_panel.shutdown()
        self.onedrive_panel.shutdown()
        super().closeEvent(event)
