"""Lets the user browse their actual Dropbox/OneDrive account and pick a
destination folder, instead of typing a path by hand — which is how a user
can end up typing a web-UI label like "My files" that isn't a real path
segment at all. Folders are loaded lazily, one level at a time: an account
with tens of thousands of files must never trigger a full recursive listing
just to populate a picker.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from .cloud_base import CloudAuthError, CloudClient, CloudSyncError

PATH_ROLE = Qt.UserRole + 1
LOADED_ROLE = Qt.UserRole + 2

ROOT_LABEL = "(account root)"


class RemoteFolderPickerDialog(QDialog):
    def __init__(self, client: CloudClient, provider_label: str, parent=None):
        super().__init__(parent)
        self.client = client
        self.selected_path: Optional[str] = None

        self.setWindowTitle(f"Choose a {provider_label} folder")
        self.resize(480, 560)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"Browse your {provider_label} account and pick a destination folder:"))

        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.itemExpanded.connect(self._on_item_expanded)
        self.tree.itemSelectionChanged.connect(self._on_selection_changed)
        layout.addWidget(self.tree, 1)

        self.path_label = QLabel(f"Selected: {ROOT_LABEL}")
        layout.addWidget(self.path_label)

        btn_row = QHBoxLayout()
        self.select_btn = QPushButton("Select this folder")
        self.select_btn.clicked.connect(self.accept)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(self.select_btn)
        btn_row.addWidget(cancel_btn)
        layout.addLayout(btn_row)

        root_item = QTreeWidgetItem([ROOT_LABEL])
        root_item.setData(0, PATH_ROLE, "/")
        root_item.setData(0, LOADED_ROLE, False)
        self.tree.addTopLevelItem(root_item)
        self.tree.setCurrentItem(root_item)
        self.selected_path = "/"
        self._load_children(root_item)
        root_item.setExpanded(True)

    def _on_item_expanded(self, item: QTreeWidgetItem) -> None:
        if not item.data(0, LOADED_ROLE):
            self._load_children(item)

    def _load_children(self, item: QTreeWidgetItem) -> None:
        path = item.data(0, PATH_ROLE)
        item.takeChildren()
        try:
            folders = self.client.list_child_folders(path)
        except CloudAuthError as exc:
            QMessageBox.critical(self, "Connection lost", f"Your session ended: {exc}\n\nPlease reconnect and try again.")
            self.reject()
            return
        except CloudSyncError as exc:
            QMessageBox.warning(self, "Could not list folder", str(exc))
            item.setData(0, LOADED_ROLE, True)
            return
        except Exception as exc:
            # Last resort: an unanticipated error must still show a message
            # and leave the item loadable again, instead of leaving the
            # "Loading…" placeholder stuck forever with no explanation.
            QMessageBox.warning(self, "Could not list folder", f"Unexpected error: {exc}")
            item.setData(0, LOADED_ROLE, True)
            return

        for folder in folders:
            child_item = QTreeWidgetItem([folder.name])
            child_item.setData(0, PATH_ROLE, folder.path)
            child_item.setData(0, LOADED_ROLE, False)
            child_item.addChild(QTreeWidgetItem(["Loading…"]))  # placeholder gives it an expand arrow
            item.addChild(child_item)
        item.setData(0, LOADED_ROLE, True)

    def _on_selection_changed(self) -> None:
        items = self.tree.selectedItems()
        if not items:
            return
        path = items[0].data(0, PATH_ROLE)
        self.selected_path = path
        self.path_label.setText(f"Selected: {path if path != '/' else ROOT_LABEL}")
