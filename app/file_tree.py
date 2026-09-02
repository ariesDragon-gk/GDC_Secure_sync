"""Full-listing tree widgets for the dashboard's Local/Destination panes.
Unlike MissingFilesTree (which only shows files needing upload, for
selection), these show *everything* on each side — including on accounts
with tens of thousands of files, so construction is batched with updates
disabled and nothing is auto-expanded beyond the top level.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QApplication, QTreeWidget, QTreeWidgetItem

from .cloud_base import CloudFileMeta
from .comparator import FileDiff, SyncStatus
from .scanner import LocalFileMeta

_HIGHLIGHT_COLOR = Qt.GlobalColor.red
_PUMP_EVERY = 2000  # keep the window responsive during a multi-second populate at real-world scale

_EXTENSION_TYPES = {
    "pdf": "PDF",
    "xlsx": "Excel", "xls": "Excel", "xlsm": "Excel", "csv": "CSV",
    "doc": "Word", "docx": "Word",
    "ppt": "PowerPoint", "pptx": "PowerPoint",
    "txt": "Text",
    "jpg": "Image", "jpeg": "Image", "png": "Image", "gif": "Image", "bmp": "Image", "tif": "Image", "tiff": "Image",
    "zip": "Archive", "rar": "Archive", "7z": "Archive",
}


def human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def file_type_label(name: str) -> str:
    if "." not in name:
        return "File"
    ext = name.rsplit(".", 1)[-1].lower()
    return _EXTENSION_TYPES.get(ext, ext.upper() if ext else "File")


class FileTree(QTreeWidget):
    file_activated = Signal(str)  # emitted with rel_path on double-clicking a file row

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setHeaderLabels(["Name", "Size", "Type", "Modified", "Status"])
        self.setColumnWidth(0, 320)
        self._file_count = 0
        self.itemDoubleClicked.connect(self._on_item_double_clicked)

    def file_count(self) -> int:
        return self._file_count

    def _on_item_double_clicked(self, item: QTreeWidgetItem, column: int) -> None:
        rel_path = item.data(0, Qt.ItemDataRole.UserRole)
        if rel_path is not None:
            self.file_activated.emit(rel_path)

    def populate(self, rows: List[Tuple[str, str, str, str, str, bool]]) -> None:
        """rows: (rel_path, size_str, type_str, modified_str, status_str, highlight)."""
        self.setUpdatesEnabled(False)
        self.blockSignals(True)
        try:
            self.clear()
            self._file_count = len(rows)
            folder_items: Dict[str, QTreeWidgetItem] = {}

            for row_idx, (rel_path, size_str, type_str, modified_str, status_str, highlight) in enumerate(
                sorted(rows, key=lambda r: r[0].lower())
            ):
                if row_idx and row_idx % _PUMP_EVERY == 0:
                    # A full populate at real-world scale (tens of thousands of
                    # files) can take several seconds; without this, Windows
                    # marks the whole app "Not Responding" partway through.
                    QApplication.processEvents()
                parts = rel_path.split("/")
                parent_item = None
                current_path = ""
                for depth, part in enumerate(parts):
                    current_path = f"{current_path}/{part}" if current_path else part
                    is_file = depth == len(parts) - 1
                    is_new = False

                    if is_file:
                        item = QTreeWidgetItem([part, size_str, type_str, modified_str, status_str])
                        item.setData(0, Qt.ItemDataRole.UserRole, rel_path)
                        is_new = True
                        if highlight:
                            for col in range(5):
                                item.setForeground(col, _HIGHLIGHT_COLOR)
                    else:
                        item = folder_items.get(current_path)
                        if item is None:
                            item = QTreeWidgetItem([part, "", "", "", ""])
                            folder_items[current_path] = item
                            is_new = True

                    if is_new:
                        if parent_item is None:
                            self.addTopLevelItem(item)
                        else:
                            parent_item.addChild(item)
                    parent_item = item

            for i in range(self.topLevelItemCount()):
                self.topLevelItem(i).setExpanded(True)
        finally:
            self.setUpdatesEnabled(True)
            self.blockSignals(False)

    def populate_from_diffs(self, diffs: List[FileDiff], local_map: Optional[Dict[str, LocalFileMeta]] = None) -> None:
        """Shows every local file; anything needing upload is highlighted."""
        local_map = local_map or {}
        rows = []
        for d in diffs:
            local = local_map.get(d.rel_path)
            name = d.rel_path.rsplit("/", 1)[-1]
            modified_str = local.mtime_utc.strftime("%Y-%m-%d %H:%M") if local else ""
            rows.append((d.rel_path, human_size(d.local_size), file_type_label(name), modified_str, d.status.value, d.status != SyncStatus.IN_SYNC))
        self.populate(rows)

    def populate_from_remote(self, remote_map: Dict[str, CloudFileMeta], matched_paths: Optional[set] = None) -> None:
        """Shows every file currently on the destination. matched_paths (rel
        paths that also exist locally) drives the Status column so it's
        immediately visible, per file, which destination files are "extra"
        (no local counterpart) versus genuinely matched."""
        rows = []
        for path, meta in remote_map.items():
            name = path.rsplit("/", 1)[-1]
            if matched_paths is None:
                status = ""
            else:
                status = "Matched to local" if path in matched_paths else "Extra on destination"
            rows.append((path, human_size(meta.size), file_type_label(name), meta.client_modified.strftime("%Y-%m-%d %H:%M"), status, False))
        self.populate(rows)
