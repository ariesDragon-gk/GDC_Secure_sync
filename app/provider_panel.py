"""Shared dashboard UI for one cloud provider tab: folder pickers, a
Scan & Compare step, a Local/Destination dual-tree browser, an Upload
status panel, and a Sync button. Subclasses only need to supply the
provider-specific connect UI and auth flow.
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)
from PySide6.QtCore import Qt, Signal

from .cloud_base import CloudClient, CloudFileMeta
from .comparator import CONFLICT_STATUSES, NEEDS_UPLOAD, FileDiff, SyncStatus
from .file_tree import FileTree, file_type_label, human_size
from .folder_picker import RemoteFolderPickerDialog
from .theme import ALERT_RED, TERMINAL_GREEN
from .scanner import LocalFileMeta
from .client_registry import ClientRegistry
from .sync_history import record_sync
from .tree_widget import MissingFilesTree
from .workers import ScanCompareWorker, UploadWorker


class _ExpandedTreeDialog(QDialog):
    """Pops a tree widget out into a large, resizable/maximizable window and
    returns it to its original spot in the dashboard when closed. The widget
    itself is reparented, not copied, so it keeps showing live data (no
    re-populating a second copy of a 79k-file tree) and always reflects
    whatever is currently happening in the background scan/upload."""

    def __init__(self, title: str, tree: QWidget, owner_layout: QVBoxLayout, owner_index: int, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setWindowFlags(self.windowFlags() | Qt.WindowMaximizeButtonHint | Qt.WindowMinimizeButtonHint)
        self.resize(1200, 800)
        self._tree = tree
        self._owner_layout = owner_layout
        self._owner_index = owner_index

        layout = QVBoxLayout(self)
        layout.addWidget(tree, 1)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.close)
        layout.addWidget(close_btn)

    def done(self, result: int) -> None:
        self._owner_layout.insertWidget(self._owner_index, self._tree)
        super().done(result)


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


class ProviderPanel(QWidget):
    log_message = Signal(str)
    log_message_colored = Signal(str, str)  # (message, color name — "green" or "red")

    def __init__(self, provider_label: str, remote_placeholder: str, parent=None):
        super().__init__(parent)
        self.provider_label = provider_label
        self.client: Optional[CloudClient] = None
        self.local_map: Dict[str, LocalFileMeta] = {}
        self.remote_map: Dict[str, CloudFileMeta] = {}
        self.diffs: List[FileDiff] = []
        self.scan_worker: Optional[ScanCompareWorker] = None
        self.upload_worker: Optional[UploadWorker] = None
        self._last_upload_bytes_done: float = 0.0
        self._last_upload_elapsed: float = 0.0

        root = QVBoxLayout(self)

        conn_group = QGroupBox(f"{provider_label} connection")
        conn_layout = QHBoxLayout(conn_group)
        for widget in self._build_connect_controls():
            conn_layout.addWidget(widget)
        self.conn_status_label = QLabel("Not connected")
        self.conn_status_label.setStyleSheet(f"color: {ALERT_RED}; font-weight: bold;")
        conn_layout.addWidget(self.conn_status_label)
        conn_layout.addStretch(1)
        root.addWidget(conn_group)

        folders_group = QGroupBox("Folders to compare")
        folders_layout = QVBoxLayout(folders_group)

        local_row = QHBoxLayout()
        local_row.addWidget(QLabel("Local folder:"))
        self.local_folder_edit = QLineEdit()
        local_row.addWidget(self.local_folder_edit, 1)
        browse_btn = QPushButton("Browse…")
        browse_btn.clicked.connect(self.on_browse_local)
        local_row.addWidget(browse_btn)
        folders_layout.addLayout(local_row)

        remote_row = QHBoxLayout()
        remote_row.addWidget(QLabel(f"{provider_label} destination folder:"))
        self.remote_folder_edit = QLineEdit()
        self.remote_folder_edit.setPlaceholderText(remote_placeholder)
        remote_row.addWidget(self.remote_folder_edit, 1)
        self.browse_remote_btn = QPushButton("Browse account…")
        self.browse_remote_btn.setEnabled(False)
        self.browse_remote_btn.setToolTip(f"Browse your {provider_label} account to pick a folder (connect first)")
        self.browse_remote_btn.clicked.connect(self.on_browse_remote)
        remote_row.addWidget(self.browse_remote_btn)
        folders_layout.addLayout(remote_row)

        self.local_folder_edit.editingFinished.connect(self._persist_folders)
        self.remote_folder_edit.editingFinished.connect(self._persist_folders)

        self.scan_btn = QPushButton("Scan && Compare")
        self.scan_btn.setEnabled(False)
        self.scan_btn.clicked.connect(self.on_scan_clicked)
        folders_layout.addWidget(self.scan_btn)
        root.addWidget(folders_group)

        # --- Part 1 & 2: Local files / Destination files, side by side ---
        browse_splitter = QSplitter(Qt.Horizontal)

        local_group = QGroupBox("Local files")
        local_layout = QVBoxLayout(local_group)
        local_header = QHBoxLayout()
        self.local_summary_label = QLabel("Scan to populate")
        local_header.addWidget(self.local_summary_label, 1)
        local_expand_btn = QPushButton("⛶ Expand")
        local_expand_btn.setToolTip("Open the full local file tree in a larger window")
        local_expand_btn.clicked.connect(
            lambda: self._expand_tree("Local files — full tree", self.local_tree, local_layout)
        )
        local_header.addWidget(local_expand_btn)
        local_layout.addLayout(local_header)
        self.local_tree = FileTree()
        local_layout.addWidget(self.local_tree, 1)
        browse_splitter.addWidget(local_group)

        dest_group = QGroupBox(f"{provider_label} files")
        dest_layout = QVBoxLayout(dest_group)
        dest_header = QHBoxLayout()
        self.destination_summary_label = QLabel("Scan to populate")
        dest_header.addWidget(self.destination_summary_label, 1)
        dest_expand_btn = QPushButton("⛶ Expand")
        dest_expand_btn.setToolTip(f"Open the full {provider_label} file tree in a larger window")
        dest_expand_btn.clicked.connect(
            lambda: self._expand_tree(f"{provider_label} files — full tree", self.destination_tree, dest_layout)
        )
        dest_header.addWidget(dest_expand_btn)
        dest_layout.addLayout(dest_header)
        self.destination_tree = FileTree()
        dest_layout.addWidget(self.destination_tree, 1)
        browse_splitter.addWidget(dest_group)

        browse_splitter.setSizes([1, 1])

        # --- Part 3: Upload status ---
        status_group = QGroupBox("Upload status")
        status_layout = QVBoxLayout(status_group)

        select_row = QHBoxLayout()
        select_all_btn = QPushButton("Select all")
        select_all_btn.clicked.connect(lambda: self.tree.set_all_checked(True))
        select_none_btn = QPushButton("Deselect all")
        select_none_btn.clicked.connect(lambda: self.tree.set_all_checked(False))
        select_row.addWidget(select_all_btn)
        select_row.addWidget(select_none_btn)
        select_row.addStretch(1)
        self.summary_label = QLabel("")
        select_row.addWidget(self.summary_label)
        status_expand_btn = QPushButton("⛶ Expand")
        status_expand_btn.setToolTip("Open the full upload-status tree in a larger window")
        status_expand_btn.clicked.connect(
            lambda: self._expand_tree("Upload status — full tree", self.tree, status_layout)
        )
        select_row.addWidget(status_expand_btn)
        status_layout.addLayout(select_row)

        self.tree = MissingFilesTree()
        status_layout.addWidget(self.tree, 1)

        sync_row = QHBoxLayout()
        self.sync_btn = QPushButton(f"Sync missing files to {provider_label}")
        self.sync_btn.setEnabled(False)
        self.sync_btn.clicked.connect(self.on_sync_clicked)
        sync_row.addWidget(self.sync_btn)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self.on_cancel_clicked)
        sync_row.addWidget(self.cancel_btn)
        status_layout.addLayout(sync_row)

        self.progress_bar = QProgressBar()
        self.progress_bar.setFormat("%v / %m files")
        status_layout.addWidget(self.progress_bar)

        self.progress_detail_label = QLabel("")
        status_layout.addWidget(self.progress_detail_label)

        outer_splitter = QSplitter(Qt.Vertical)
        outer_splitter.addWidget(browse_splitter)
        outer_splitter.addWidget(status_group)
        outer_splitter.setSizes([3, 2])
        root.addWidget(outer_splitter, 1)

        self.local_tree.file_activated.connect(self._show_file_details)
        self.destination_tree.file_activated.connect(self._show_file_details)
        self.tree.file_activated.connect(self._show_file_details)

    # -------------------------------------------------- expand-tree popup

    def _expand_tree(self, title: str, tree: QWidget, owner_layout: QVBoxLayout) -> None:
        index = owner_layout.indexOf(tree)
        dialog = _ExpandedTreeDialog(title, tree, owner_layout, index, self)
        dialog.exec()

    # ------------------------------------------------------ file details

    def _show_file_details(self, rel_path: str) -> None:
        name = rel_path.rsplit("/", 1)[-1]
        local = self.local_map.get(rel_path)
        remote = self.remote_map.get(rel_path)
        diff = next((d for d in self.diffs if d.rel_path == rel_path), None)

        lines = [f"Path: {rel_path}", f"Type: {file_type_label(name)}"]
        if local:
            lines.append(f"Local size: {human_size(local.size)}")
            lines.append(f"Local modified: {local.mtime_utc.strftime('%Y-%m-%d %H:%M')}")
        else:
            lines.append("Local: not present")
        if remote:
            lines.append(f"{self.provider_label} size: {human_size(remote.size)}")
            lines.append(f"{self.provider_label} last synced: {remote.client_modified.strftime('%Y-%m-%d %H:%M')}")
        else:
            lines.append(f"{self.provider_label}: not present")
        if diff:
            lines.append(f"Status: {diff.status.value}")

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Information)
        box.setWindowTitle("File details")
        box.setText(f"{name}\n\n" + "\n".join(lines))
        box.exec()

    # ---------------------------------------------------- subclass hooks

    def _build_connect_controls(self) -> list:
        raise NotImplementedError

    def _persist_folders(self) -> None:
        pass

    # ------------------------------------------------- connection state

    def set_connected(self, client: CloudClient) -> None:
        self.client = client
        self.conn_status_label.setText(f"✓ Connected to {self.provider_label}")
        self.conn_status_label.setStyleSheet(f"color: {TERMINAL_GREEN}; font-weight: bold;")
        self.scan_btn.setEnabled(True)
        self.browse_remote_btn.setEnabled(True)
        if hasattr(self, "disconnect_btn"):
            self.disconnect_btn.setEnabled(True)

    def set_disconnected(self, message: str = "Not connected") -> None:
        self.client = None
        self.conn_status_label.setText(f"✗ {message}")
        self.conn_status_label.setStyleSheet(f"color: {ALERT_RED}; font-weight: bold;")
        self.scan_btn.setEnabled(False)
        self.sync_btn.setEnabled(False)
        self.browse_remote_btn.setEnabled(False)
        if hasattr(self, "disconnect_btn"):
            self.disconnect_btn.setEnabled(False)

    def on_connection_lost(self, message: str) -> None:
        self.set_disconnected("Connection lost")
        self.cancel_btn.setEnabled(False)
        self.log_message.emit(f"CONNECTION LOST ({self.provider_label}): {message}")
        QMessageBox.critical(
            self,
            f"{self.provider_label} connection lost",
            f"Your {self.provider_label} session ended:\n\n{message}\n\nPlease reconnect.",
        )

    # ------------------------------------------------------------ scan

    def on_browse_local(self) -> None:
        start_dir = self.local_folder_edit.text().strip() or str(Path.home())
        folder = QFileDialog.getExistingDirectory(self, "Select local folder to sync", start_dir)
        if folder:
            self.local_folder_edit.setText(folder)
            self._persist_folders()

    def on_browse_remote(self) -> None:
        if not self.client:
            QMessageBox.warning(self, "Not connected", f"Connect to {self.provider_label} first.")
            return
        dialog = RemoteFolderPickerDialog(self.client, self.provider_label, self)
        if dialog.exec() == QDialog.Accepted and dialog.selected_path is not None:
            self.remote_folder_edit.setText(dialog.selected_path)
            self._persist_folders()

    def on_scan_clicked(self) -> None:
        if not self.client:
            QMessageBox.warning(self, "Not connected", f"Connect to {self.provider_label} first.")
            return
        local_folder = self.local_folder_edit.text().strip()
        if not local_folder or not Path(local_folder).is_dir():
            QMessageBox.warning(self, "Invalid folder", "Choose a valid local folder first.")
            return
        remote_folder = self.remote_folder_edit.text().strip() or "/"
        self._persist_folders()

        self.scan_btn.setEnabled(False)
        self.sync_btn.setEnabled(False)
        self.browse_remote_btn.setEnabled(False)  # tree populate below yields via processEvents(); avoid reentrancy
        self.tree.populate([])
        self.summary_label.setText("Scanning…")
        self.local_summary_label.setText("Scanning…")
        self.destination_summary_label.setText("Scanning…")

        self.scan_worker = ScanCompareWorker(self.client, Path(local_folder), remote_folder)
        self.scan_worker.log.connect(self.log_message.emit)
        self.scan_worker.finished_ok.connect(self.on_scan_finished)
        self.scan_worker.failed.connect(self.on_scan_failed)
        self.scan_worker.auth_lost.connect(self.on_connection_lost)
        self.scan_worker.start()

    def on_scan_finished(
        self,
        local_map: Dict[str, LocalFileMeta],
        remote_map: Dict[str, CloudFileMeta],
        diffs: List[FileDiff],
        elapsed: float,
    ) -> None:
        self.local_map = local_map
        self.remote_map = remote_map
        self.diffs = diffs

        self.tree.populate(diffs)
        total = len(diffs)
        missing = sum(1 for d in diffs if d.status == SyncStatus.MISSING)
        conflicts = sum(1 for d in diffs if d.status in CONFLICT_STATUSES)
        needs = missing + conflicts
        already_synced = total - needs
        self.summary_label.setText(f"{needs} of {total} files need uploading ({conflicts} conflicting)")

        # already_synced + conflicts are exactly the local files whose rel_path
        # also exists in remote_map. Anything else on the destination is a file
        # the destination has that no local file matched by path at all — call
        # that out explicitly, since "missing" alone doesn't explain the gap
        # between the two totals when the destination isn't a strict subset.
        matched_on_destination = already_synced + conflicts
        remote_total = len(remote_map)
        extra_on_destination = max(remote_total - matched_on_destination, 0)

        self.log_message_colored.emit(f"✓ {already_synced} file(s) already exist on {self.provider_label} and are in sync", "green")
        if missing:
            self.log_message_colored.emit(f"✗ {missing} file(s) missing on {self.provider_label} / need uploading", "red")
        if conflicts:
            self.log_message_colored.emit(f"✗ {conflicts} file(s) CONFLICT — exist on {self.provider_label} but differ", "red")
        if extra_on_destination:
            self.log_message.emit(
                f"{extra_on_destination} file(s) exist on {self.provider_label} but don't match any local "
                "file by path (present only on the destination, or a folder/path mismatch between the two sides)."
            )
        smaller_side = min(total, remote_total) if remote_total else 0
        if smaller_side and matched_on_destination / smaller_side < 0.2:
            self.log_message_colored.emit(
                "⚠ Very few files matched between the local folder and the destination by path. Double-check "
                "the Local folder and destination folder actually point at the same content — a mismatched "
                "top-level folder name (e.g. singular vs. plural, or a different subfolder) is a common cause.",
                "red",
            )
        self.log_message.emit(f"Scan & compare completed in {format_duration(elapsed)} ({elapsed:.1f}s).")

        self.local_tree.populate_from_diffs(diffs, local_map)
        self.local_summary_label.setText(
            f"Total: {total}  |  Missing: {missing}  |  Conflicts: {conflicts}  |  In sync: {already_synced}"
        )

        # Every diff that isn't MISSING has a remote counterpart at that exact
        # rel_path — i.e. it's the set of destination files this scan actually
        # matched. Showing per-file which destination files are "extra" (rather
        # than just a summary count) makes the earlier reconciliation figure
        # something you can actually inspect file by file.
        matched_paths = {d.rel_path for d in diffs if d.status != SyncStatus.MISSING}
        self.destination_tree.populate_from_remote(remote_map, matched_paths)
        self.destination_summary_label.setText(
            f"Total: {remote_total}  |  Matched to local: {matched_on_destination}  |  "
            f"Extra on destination: {extra_on_destination}"
        )

        # Reset rather than leave the progress bar/detail line showing whatever
        # the LAST completed upload reported (e.g. "5558/5558 files") — that
        # stale number has nothing to do with this fresh scan's result and
        # reads as a contradiction next to "N files need uploading".
        self.progress_bar.setMaximum(max(needs, 1))
        self.progress_bar.setValue(0)
        self.progress_detail_label.setText(
            f"Last scan took {format_duration(elapsed)}. {needs} file(s) ready to sync."
        )

        self.scan_btn.setEnabled(True)
        self.sync_btn.setEnabled(needs > 0)
        self.browse_remote_btn.setEnabled(self.client is not None)

    def on_scan_failed(self, message: str) -> None:
        self.scan_btn.setEnabled(True)
        self.browse_remote_btn.setEnabled(self.client is not None)
        self.summary_label.setText("Scan failed")
        self.local_summary_label.setText("Scan failed")
        self.destination_summary_label.setText("Scan failed")
        QMessageBox.critical(self, "Scan/compare failed", message)
        self.log_message.emit(f"ERROR: {message}")

    # ---------------------------------------------------------- upload

    def on_sync_clicked(self) -> None:
        selected = self.tree.selected_diffs()
        if not selected:
            QMessageBox.information(self, "Nothing selected", "Check at least one file to upload.")
            return

        conflicts = [d for d in selected if d.status in CONFLICT_STATUSES]
        to_upload = selected
        if conflicts:
            resolution = self._prompt_conflict_resolution(conflicts)
            if resolution == "cancel":
                return
            if resolution == "skip":
                to_upload = [d for d in selected if d.status not in CONFLICT_STATUSES]
                self.log_message_colored.emit(f"Skipping {len(conflicts)} conflicting file(s) at your request.", "red")

        if not to_upload:
            QMessageBox.information(self, "Nothing to upload", "No files left to upload after resolving conflicts.")
            return

        reply = QMessageBox.question(
            self,
            "Confirm sync",
            f"Upload {len(to_upload)} file(s) from the local folder to {self.provider_label} now, "
            "preserving the folder structure?",
        )
        if reply != QMessageBox.Yes:
            return

        remote_folder = self.remote_folder_edit.text().strip() or "/"
        self.progress_bar.setMaximum(len(to_upload))
        self.progress_bar.setValue(0)
        self.progress_detail_label.setText("Starting upload…")
        self.sync_btn.setEnabled(False)
        self.scan_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)

        self.upload_worker = UploadWorker(
            self.client, self.local_map, to_upload, remote_folder, registry=self.build_registry()
        )
        self.upload_worker.client_limit.connect(self.on_client_limit)
        self.upload_worker.log.connect(self.log_message.emit)
        self.upload_worker.log_colored.connect(self.log_message_colored.emit)
        self.upload_worker.overall_progress.connect(self.on_upload_progress)
        self.upload_worker.finished_ok.connect(self.on_upload_finished)
        self.upload_worker.auth_lost.connect(self.on_connection_lost)
        self.upload_worker.start()

    def registry_app_id(self) -> str:
        """The provider's app id (Dropbox app key / Azure client id). Subclasses override."""
        return ""

    def build_registry(self) -> Optional[ClientRegistry]:
        app_id = self.registry_app_id()
        settings = getattr(self, "settings", None)
        if not (app_id and settings and self.client):
            return None
        return ClientRegistry(self.client, self.provider_label, app_id, settings.machine_client_id)

    def on_client_limit(self, message: str) -> None:
        self.cancel_btn.setEnabled(False)
        self.scan_btn.setEnabled(True)
        self.sync_btn.setEnabled(True)
        self.progress_detail_label.setText("Sync blocked: client limit reached.")
        self.log_message_colored.emit(f"✗ {self.provider_label} sync blocked: {message}", "red")
        QMessageBox.warning(self, "Client limit reached", message)

    def _prompt_conflict_resolution(self, conflicts: List[FileDiff]) -> str:
        preview = "\n".join(f"  • {d.rel_path}  ({d.status.value})" for d in conflicts[:10])
        if len(conflicts) > 10:
            preview += f"\n  …and {len(conflicts) - 10} more"

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Conflicting files found")
        box.setText(
            f"{len(conflicts)} of your selected file(s) already exist on {self.provider_label} but differ "
            f"from the local copy (different size, or the local file is newer):\n\n{preview}\n\n"
            "Uploading these will overwrite the version currently on the destination. How do you want to proceed?"
        )
        overwrite_btn = box.addButton("Overwrite on destination", QMessageBox.ButtonRole.AcceptRole)
        skip_btn = box.addButton("Skip conflicting files", QMessageBox.ButtonRole.DestructiveRole)
        cancel_btn = box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(cancel_btn)
        box.exec()

        clicked = box.clickedButton()
        if clicked is overwrite_btn:
            return "overwrite"
        if clicked is skip_btn:
            return "skip"
        return "cancel"

    def on_upload_progress(self, done: int, total: int, bytes_done: float, bytes_total: float, elapsed: float) -> None:
        self.progress_bar.setMaximum(total)
        self.progress_bar.setValue(done)
        self._last_upload_bytes_done = bytes_done
        self._last_upload_elapsed = elapsed

        speed = bytes_done / elapsed if elapsed > 0 else 0
        remaining_bytes = max(bytes_total - bytes_done, 0)
        eta = remaining_bytes / speed if speed > 0 else 0
        self.progress_detail_label.setText(
            f"{done}/{total} files  |  {human_size(bytes_done)} / {human_size(bytes_total)}  |  "
            f"{human_size(int(speed))}/s  |  Elapsed: {format_duration(elapsed)}  |  ETA: {format_duration(eta)}"
        )

    def on_upload_finished(self, succeeded: int, failed: int, failed_details: List[str], cancelled: bool = False) -> None:
        self.cancel_btn.setEnabled(False)
        self.scan_btn.setEnabled(True)
        self.sync_btn.setEnabled(True)

        record_sync(
            provider=self.provider_label,
            succeeded=succeeded,
            failed=failed,
            cancelled=cancelled,
            total_bytes=int(self._last_upload_bytes_done),
            elapsed_seconds=self._last_upload_elapsed,
        )

        if cancelled:
            self.log_message_colored.emit(
                f"✗ {self.provider_label} sync stopped by user: {succeeded} confirmed, {failed} failed, "
                "remaining files not uploaded.",
                "red",
            )
        elif failed == 0:
            self.log_message_colored.emit(
                f"✓ {self.provider_label} sync confirmed: {succeeded}/{succeeded} file(s) "
                "verified on destination.",
                "green",
            )
        else:
            self.log_message_colored.emit(
                f"✗ {self.provider_label} sync finished with problems: {succeeded} confirmed, "
                f"{failed} failed.",
                "red",
            )

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Information if (failed == 0 and not cancelled) else QMessageBox.Icon.Warning)
        box.setWindowTitle("Upload stopped" if cancelled else "Sync complete")
        text = f"{succeeded} file(s) uploaded and confirmed on {self.provider_label}.\n{failed} file(s) failed."
        if cancelled:
            text += "\nStopped by user — remaining files were not uploaded."
        box.setText(text)
        if failed_details:
            box.setDetailedText("\n".join(failed_details))
        box.exec()

        self.on_scan_clicked()  # re-scan so the dashboard reflects the new state

    def on_cancel_clicked(self) -> None:
        if self.upload_worker:
            self.upload_worker.cancel()
            self.cancel_btn.setEnabled(False)

    def shutdown(self) -> None:
        for worker in (self.scan_worker, self.upload_worker):
            if worker and worker.isRunning():
                worker.wait(2000)
