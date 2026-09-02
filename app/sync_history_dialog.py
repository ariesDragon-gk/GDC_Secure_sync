"""Viewer for the persistent sync-history log: how many syncs happened per
date, plus every individual run with its speed/duration, across both
providers and across app restarts.
"""
from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from .file_tree import human_size
from .sync_history import load_history, summarize_by_date


def _format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


class SyncHistoryDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Sync History")
        self.resize(950, 650)
        layout = QVBoxLayout(self)

        layout.addWidget(QLabel("Syncs by date"))
        self.by_date_table = QTableWidget(0, 6)
        self.by_date_table.setHorizontalHeaderLabels(
            ["Date", "Syncs", "Succeeded", "Failed", "Data uploaded", "Avg speed"]
        )
        self.by_date_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.by_date_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        layout.addWidget(self.by_date_table, 1)

        layout.addWidget(QLabel("All sync runs"))
        self.runs_table = QTableWidget(0, 7)
        self.runs_table.setHorizontalHeaderLabels(
            ["When", "Provider", "Succeeded", "Failed", "Cancelled", "Data uploaded", "Duration"]
        )
        self.runs_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.runs_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        layout.addWidget(self.runs_table, 2)

        btn_row = QHBoxLayout()
        refresh_btn = QPushButton("⟳ Refresh")
        refresh_btn.clicked.connect(self.refresh)
        btn_row.addWidget(refresh_btn)
        btn_row.addStretch(1)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.close)
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

        self.refresh()

    def refresh(self) -> None:
        records = load_history()

        self.by_date_table.setRowCount(0)
        for date, count, succeeded, failed, total_bytes, elapsed in summarize_by_date(records):
            row = self.by_date_table.rowCount()
            self.by_date_table.insertRow(row)
            speed = total_bytes / elapsed if elapsed > 0 else 0
            values = [
                date,
                str(count),
                str(succeeded),
                str(failed),
                human_size(total_bytes),
                f"{human_size(int(speed))}/s",
            ]
            for col, val in enumerate(values):
                self.by_date_table.setItem(row, col, QTableWidgetItem(val))

        self.runs_table.setRowCount(0)
        for record in sorted(records, key=lambda r: r.timestamp, reverse=True):
            row = self.runs_table.rowCount()
            self.runs_table.insertRow(row)
            values = [
                record.timestamp.replace("T", " "),
                record.provider,
                str(record.succeeded),
                str(record.failed),
                "Yes" if record.cancelled else "No",
                human_size(record.total_bytes),
                _format_duration(record.elapsed_seconds),
            ]
            for col, val in enumerate(values):
                self.runs_table.setItem(row, col, QTableWidgetItem(val))
