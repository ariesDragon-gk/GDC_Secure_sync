"""Background QThread workers so the UI never blocks on network/disk I/O."""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, List, Optional

from PySide6.QtCore import QThread, Signal

from .cloud_base import CloudAuthError, CloudClient, CloudSyncError
from .client_registry import ClientLimitError, ClientRegistry
from .comparator import FileDiff, compare
from .scanner import LocalFileMeta, scan_local

# How many files are uploaded at once. Uploading one file at a time is
# latency-bound, not bandwidth-bound — most of the wall-clock time on typical
# file sizes is spent waiting on the round trip, not actually moving bytes, so
# a small pool of concurrent uploads gets meaningfully higher real-world
# throughput. 4 is a conservative default that keeps well clear of Dropbox's
# and Graph's per-app rate limits.
UPLOAD_CONCURRENCY = 4


class _UploadCancelled(Exception):
    """Raised from inside the progress callback to abort a chunked upload the
    moment Cancel is clicked, instead of only checking between whole files —
    which would leave Cancel unresponsive for however long the file in
    progress takes to finish (minutes, on a large file over a slow link)."""


class ScanCompareWorker(QThread):
    log = Signal(str)
    finished_ok = Signal(dict, dict, list, float)  # local_map, remote_map, diffs, elapsed_seconds
    failed = Signal(str)
    auth_lost = Signal(str)  # connection/session died — distinct from a plain sync error

    def __init__(self, client: CloudClient, local_root: Path, remote_root: str):
        super().__init__()
        self._client = client
        self._local_root = local_root
        self._remote_root = remote_root

    def run(self):
        start_time = time.time()
        try:
            self.log.emit(f"Scanning local folder: {self._local_root}")
            local_map = scan_local(self._local_root, log=self.log.emit)
            self.log.emit(f"Found {len(local_map)} local files.")

            self.log.emit(f"Listing {self._client.display_name} folder: {self._remote_root or '/'}")
            remote_map = self._client.list_folder_recursive(self._remote_root, log=self.log.emit)
            self.log.emit(f"Found {len(remote_map)} files on {self._client.display_name}.")

            diffs = compare(local_map, remote_map)
            elapsed = time.time() - start_time
            self.log.emit(f"Comparison complete: {len(diffs)} local files evaluated in {elapsed:.1f}s.")
            self.finished_ok.emit(local_map, remote_map, diffs, elapsed)
        except CloudAuthError as exc:
            self.auth_lost.emit(str(exc))
        except CloudSyncError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # unexpected — still surface it instead of crashing silently
            self.failed.emit(f"Unexpected error during scan: {exc}")


class UploadWorker(QThread):
    log = Signal(str)
    log_colored = Signal(str, str)  # (message, "green"/"red") — per-file confirmation
    # files_done, files_total, bytes_done, bytes_total, elapsed_seconds
    # bytes_done/bytes_total are declared as float (not int): PySide marshals a
    # Signal's "int" through a 32-bit C int, which overflows past ~2GB — trivially
    # exceeded by the total upload size on a large account. float safely holds
    # integer byte counts exactly up to 2^53 (petabytes), so use it here instead.
    overall_progress = Signal(int, int, float, float, float)
    finished_ok = Signal(int, int, list, bool)  # succeeded, failed, failed_details, cancelled
    auth_lost = Signal(str)  # connection/session died mid-upload
    client_limit = Signal(str)  # this machine would be one client too many for the app id

    def __init__(
        self,
        client: CloudClient,
        local_map: Dict[str, LocalFileMeta],
        diffs: List[FileDiff],
        remote_root: str,
        registry: Optional[ClientRegistry] = None,
    ):
        super().__init__()
        self._registry = registry
        self._client = client
        self._local_map = local_map
        self._diffs = diffs
        self._remote_root = remote_root
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        if self._registry is not None:
            try:
                registry = self._registry.check_in()
                self.log.emit(
                    f"Client registered: {self._registry.machine['hostname']} "
                    f"({len(registry['clients'])}/{self._registry.max_clients} clients on this app id)."
                )
            except ClientLimitError as exc:
                self.client_limit.emit(str(exc))
                return
            except CloudAuthError as exc:
                self.auth_lost.emit(str(exc))
                return
            except Exception as exc:
                # Registry trouble must not stop a backup: the limit can't be
                # verified right now, so say so and carry on.
                self.log_colored.emit(f"⚠ Could not verify client registry ({exc}); continuing sync.", "red")
                self._registry = None

        total = len(self._diffs)
        total_bytes = sum(
            self._local_map[d.rel_path].size for d in self._diffs if d.rel_path in self._local_map
        )
        start_time = time.time()
        provider = self._client.display_name

        # Multiple worker threads update these concurrently, so every mutation
        # (and the progress emit that reports it) happens while holding this
        # lock — that keeps the emitted (done, bytes_done) sequence a strictly
        # ordered, monotonically increasing snapshot regardless of which
        # thread's chunk happens to land first.
        lock = threading.Lock()
        state = {"bytes_done": 0, "done": 0, "succeeded": 0, "failed": 0, "cancelled": False, "auth_lost_reported": False}
        failed_details: List[str] = []

        def emit_progress_locked() -> None:
            self.overall_progress.emit(state["done"], total, state["bytes_done"], total_bytes, time.time() - start_time)

        def fail_one(diff: FileDiff, message: str) -> None:
            with lock:
                state["failed"] += 1
                state["done"] += 1
                failed_details.append(message)
                emit_progress_locked()
            self.log_colored.emit(f"✗ FAILED: {message}", "red")

        def upload_one(diff: FileDiff) -> None:
            if self._cancelled or state["cancelled"]:
                return
            local = self._local_map.get(diff.rel_path)
            if local is None:
                fail_one(diff, f"'{diff.rel_path}': local file no longer found.")
                return

            remote_path = self._client.join_remote_path(self._remote_root, diff.rel_path)
            self.log.emit(f"Uploading {diff.rel_path} -> {remote_path}")
            sent_so_far = 0

            def progress_cb(done: int, _total: int) -> None:
                nonlocal sent_so_far
                if self._cancelled:
                    raise _UploadCancelled()
                with lock:
                    state["bytes_done"] += done - sent_so_far
                    sent_so_far = done
                    emit_progress_locked()

            try:
                result = self._client.upload_file(
                    local.abs_path, remote_path, local.mtime_utc, progress_cb=progress_cb,
                )
                with lock:
                    state["succeeded"] += 1
                    state["done"] += 1
                    emit_progress_locked()
                self.log_colored.emit(
                    f"✓ Confirmed on {provider}: {diff.rel_path} ({result.remote_size} bytes verified)", "green",
                )
            except _UploadCancelled:
                with lock:
                    state["cancelled"] = True
                    state["done"] += 1
                self.log_colored.emit(f"✗ Upload cancelled by user (stopped mid-transfer: {diff.rel_path}).", "red")
            except CloudAuthError as exc:
                # The session died — every in-flight/queued upload would fail the
                # same way, so stop the batch (already-running uploads still get
                # to finish their current chunk boundary, then self-abort via the
                # progress_cb cancellation check above) and surface exactly one
                # connection-lost alert instead of one per concurrent worker. The
                # panel treats auth_lost as terminal, so finished_ok is
                # deliberately not emitted — that would show a misleading
                # "sync complete" alongside the connection-lost dialog.
                self._cancelled = True
                with lock:
                    state["cancelled"] = True
                    state["done"] += 1
                    already_reported = state["auth_lost_reported"]
                    state["auth_lost_reported"] = True
                self.log.emit(f"Connection lost while uploading '{diff.rel_path}': {exc}")
                if not already_reported:
                    self.auth_lost.emit(str(exc))
            except CloudSyncError as exc:
                fail_one(diff, f"{diff.rel_path}: {exc}")
            except Exception as exc:
                fail_one(diff, f"{diff.rel_path}: unexpected error: {exc}")

        with ThreadPoolExecutor(max_workers=UPLOAD_CONCURRENCY) as pool:
            futures = []
            for diff in self._diffs:
                if self._cancelled or state["cancelled"]:
                    break
                futures.append(pool.submit(upload_one, diff))
            for future in futures:
                future.result()

        if state["auth_lost_reported"]:
            return

        if state["cancelled"]:
            self.log_colored.emit("✗ Upload cancelled by user.", "red")

        if self._registry is not None:
            try:
                self._registry.record_sync(
                    state["succeeded"], state["failed"], state["cancelled"], int(state["bytes_done"])
                )
            except Exception as exc:
                self.log.emit(f"Could not record this sync in the client registry: {exc}")

        self.finished_ok.emit(state["succeeded"], state["failed"], failed_details, state["cancelled"])
