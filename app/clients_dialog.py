"""Shows every machine that has synced against each provider's app id —
hostname, OS, public IP/location, first/last sync time — plus the most recent
sync events, and lets you free a slot by removing a machine that is retired.
Data comes from the shared registry in the cloud account, so it includes
machines anywhere in the world, not just this one.
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .client_registry import ClientRegistry, describe_location, sorted_clients
from .file_tree import human_size


def format_utc_as_local(iso_utc: str) -> str:
    if not iso_utc:
        return "never"
    try:
        return datetime.fromisoformat(iso_utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return iso_utc


class _RegistryLoader(QThread):
    loaded = Signal(dict)
    failed = Signal(str)

    def __init__(self, registry: ClientRegistry, remove_id: Optional[str] = None):
        super().__init__()
        self._registry = registry
        self._remove_id = remove_id

    def run(self):
        try:
            if self._remove_id:
                self._registry.remove_client(self._remove_id)
            self.loaded.emit(self._registry.load())
        except Exception as exc:
            self.failed.emit(str(exc))


class _ProviderClientsView(QWidget):
    def __init__(self, registry: ClientRegistry, parent=None):
        super().__init__(parent)
        self._registry = registry
        self._loader: Optional[_RegistryLoader] = None
        self._client_ids: List[str] = []

        layout = QVBoxLayout(self)
        self.header = QLabel("Loading…")
        layout.addWidget(self.header)

        self.clients_table = QTableWidget(0, 8)
        self.clients_table.setHorizontalHeaderLabels(
            ["Machine", "OS", "User", "Public IP", "Location", "Last sync (your time)", "Syncs", "First seen"]
        )
        self.clients_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.clients_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.clients_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        layout.addWidget(self.clients_table, 2)

        layout.addWidget(QLabel("Recent sync events"))
        self.events_table = QTableWidget(0, 6)
        self.events_table.setHorizontalHeaderLabels(
            ["When (your time)", "Machine", "Public IP", "Succeeded", "Failed", "Data uploaded"]
        )
        self.events_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.events_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        layout.addWidget(self.events_table, 3)

        row = QHBoxLayout()
        self.refresh_btn = QPushButton("⟳ Refresh")
        self.refresh_btn.clicked.connect(self.refresh)
        row.addWidget(self.refresh_btn)
        self.remove_btn = QPushButton("Remove selected machine")
        self.remove_btn.setToolTip("Frees a client slot. The machine re-registers itself the next time it syncs.")
        self.remove_btn.clicked.connect(self.on_remove_clicked)
        row.addWidget(self.remove_btn)
        row.addStretch(1)
        layout.addLayout(row)

        self.refresh()

    def _start(self, remove_id: Optional[str] = None) -> None:
        self.refresh_btn.setEnabled(False)
        self.remove_btn.setEnabled(False)
        self.header.setText("Loading…")
        self._loader = _RegistryLoader(self._registry, remove_id)
        self._loader.loaded.connect(self.populate)
        self._loader.failed.connect(self.on_failed)
        self._loader.start()

    def refresh(self) -> None:
        self._start()

    def on_remove_clicked(self) -> None:
        row = self.clients_table.currentRow()
        if row < 0 or row >= len(self._client_ids):
            QMessageBox.information(self, "Select a machine", "Select a machine in the table first.")
            return
        cid = self._client_ids[row]
        name = self.clients_table.item(row, 0).text()
        if QMessageBox.question(
            self, "Remove machine", f"Remove '{name}' from the registry and free its slot?"
        ) != QMessageBox.StandardButton.Yes:
            return
        self._start(remove_id=cid)

    def on_failed(self, message: str) -> None:
        self.header.setText(f"Could not read the client registry: {message}")
        self.refresh_btn.setEnabled(True)
        self.remove_btn.setEnabled(True)

    def populate(self, registry: dict) -> None:
        clients = sorted_clients(registry)
        limit = self._registry.max_clients
        me = self._registry.client_id
        self.header.setText(
            f"{self._registry.provider} — app id {self._registry.app_id} — "
            f"{len(clients)} of {limit} client slots in use"
            + ("  (LIMIT REACHED)" if len(clients) >= limit else "")
        )
        self._client_ids = [cid for cid, _ in clients]
        names = {cid: e.get("hostname", "?") for cid, e in clients}

        self.clients_table.setRowCount(0)
        for cid, e in clients:
            r = self.clients_table.rowCount()
            self.clients_table.insertRow(r)
            values = [
                e.get("hostname", "?") + ("  (this machine)" if cid == me else ""),
                e.get("os", ""),
                e.get("user", ""),
                e.get("public_ip", "") or "unknown",
                describe_location(e),
                format_utc_as_local(e.get("last_sync", "")),
                str(e.get("sync_count", 0)),
                format_utc_as_local(e.get("first_seen", "")),
            ]
            for c, v in enumerate(values):
                self.clients_table.setItem(r, c, QTableWidgetItem(v))

        self.events_table.setRowCount(0)
        for ev in reversed(registry.get("events", [])):
            r = self.events_table.rowCount()
            self.events_table.insertRow(r)
            values = [
                format_utc_as_local(ev.get("time", "")),
                names.get(ev.get("client_id"), ev.get("hostname", "?")),
                ev.get("public_ip", "") or "unknown",
                str(ev.get("succeeded", 0)),
                str(ev.get("failed", 0)),
                human_size(int(ev.get("total_bytes", 0))),
            ]
            for c, v in enumerate(values):
                self.events_table.setItem(r, c, QTableWidgetItem(v))

        self.refresh_btn.setEnabled(True)
        self.remove_btn.setEnabled(True)

    def wait(self) -> None:
        if self._loader and self._loader.isRunning():
            self._loader.wait(3000)


class ClientsDialog(QDialog):
    def __init__(self, registries: List[ClientRegistry], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Connected Clients")
        self.resize(1100, 700)
        layout = QVBoxLayout(self)
        self.views: List[_ProviderClientsView] = []
        if registries:
            tabs = QTabWidget()
            for reg in registries:
                view = _ProviderClientsView(reg)
                self.views.append(view)
                tabs.addTab(view, reg.provider)
            layout.addWidget(tabs)
        else:
            layout.addWidget(QLabel(
                "Connect a provider and enter its app id first — the client list lives in that account."
            ))
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.close)
        layout.addWidget(close_btn)

    def done(self, result: int) -> None:
        for v in self.views:
            v.wait()
        super().done(result)
