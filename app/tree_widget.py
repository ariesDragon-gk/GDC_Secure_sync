"""Tree view of files needing upload, mirroring the local folder structure.
Checking/unchecking a folder propagates to its descendants; checking or
unchecking a file rolls back up to give ancestor folders a partial state.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QTreeWidget, QTreeWidgetItem

from .comparator import NEEDS_UPLOAD, FileDiff
from .file_tree import file_type_label

PATH_ROLE = Qt.UserRole + 1


def human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


class MissingFilesTree(QTreeWidget):
    file_activated = Signal(str)  # emitted with rel_path on double-clicking a file row

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setHeaderLabels(["Name", "Size", "Type", "Status"])
        self.setColumnWidth(0, 420)
        self._diff_by_path: Dict[str, FileDiff] = {}
        self._updating = False
        self.itemChanged.connect(self._on_item_changed)
        self.itemDoubleClicked.connect(self._on_item_double_clicked)

    def _on_item_double_clicked(self, item: QTreeWidgetItem, column: int) -> None:
        rel_path = item.data(0, PATH_ROLE)
        if rel_path is not None:
            self.file_activated.emit(rel_path)

    def populate(self, diffs: List[FileDiff]) -> None:
        self.blockSignals(True)
        try:
            self.clear()
            self._diff_by_path = {}
            folder_items: Dict[str, QTreeWidgetItem] = {}

            needing = sorted(
                (d for d in diffs if d.status in NEEDS_UPLOAD), key=lambda d: d.rel_path.lower()
            )

            for diff in needing:
                parts = diff.rel_path.split("/")
                parent_item: Optional[QTreeWidgetItem] = None
                current_path = ""
                for depth, part in enumerate(parts):
                    current_path = f"{current_path}/{part}" if current_path else part
                    is_file = depth == len(parts) - 1

                    is_new = False
                    if is_file:
                        item = QTreeWidgetItem([part, human_size(diff.local_size), file_type_label(part), diff.status.value])
                        item.setData(0, PATH_ROLE, diff.rel_path)
                        self._diff_by_path[diff.rel_path] = diff
                        is_new = True
                    else:
                        item = folder_items.get(current_path)
                        if item is None:
                            item = QTreeWidgetItem([part, "", "", ""])
                            folder_items[current_path] = item
                            is_new = True

                    if is_new:
                        item.setCheckState(0, Qt.Checked)
                        if parent_item is None:
                            self.addTopLevelItem(item)
                        else:
                            parent_item.addChild(item)

                    parent_item = item

            self.expandAll()
        finally:
            self.blockSignals(False)

    def set_all_checked(self, checked: bool) -> None:
        state = Qt.Checked if checked else Qt.Unchecked
        for i in range(self.topLevelItemCount()):
            self.topLevelItem(i).setCheckState(0, state)

    def selected_diffs(self) -> List[FileDiff]:
        result: List[FileDiff] = []

        def walk(item: QTreeWidgetItem):
            for i in range(item.childCount()):
                child = item.child(i)
                path = child.data(0, PATH_ROLE)
                if path is not None:
                    if child.checkState(0) == Qt.Checked:
                        result.append(self._diff_by_path[path])
                else:
                    walk(child)

        for i in range(self.topLevelItemCount()):
            top = self.topLevelItem(i)
            path = top.data(0, PATH_ROLE)
            if path is not None:
                if top.checkState(0) == Qt.Checked:
                    result.append(self._diff_by_path[path])
            else:
                walk(top)

        return result

    def file_count(self) -> int:
        return len(self._diff_by_path)

    # -------------------------------------------------------- propagation

    def _on_item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if column != 0 or self._updating:
            return
        self._updating = True
        try:
            state = item.checkState(0)
            if state != Qt.PartiallyChecked:
                self._set_children_check_state(item, state)
            self._update_ancestors_check_state(item.parent())
        finally:
            self._updating = False

    def _set_children_check_state(self, item: QTreeWidgetItem, state) -> None:
        for i in range(item.childCount()):
            child = item.child(i)
            child.setCheckState(0, state)
            self._set_children_check_state(child, state)

    def _update_ancestors_check_state(self, item: Optional[QTreeWidgetItem]) -> None:
        while item is not None:
            states = {item.child(i).checkState(0) for i in range(item.childCount())}
            if states == {Qt.Checked}:
                item.setCheckState(0, Qt.Checked)
            elif states == {Qt.Unchecked}:
                item.setCheckState(0, Qt.Unchecked)
            else:
                item.setCheckState(0, Qt.PartiallyChecked)
            item = item.parent()
