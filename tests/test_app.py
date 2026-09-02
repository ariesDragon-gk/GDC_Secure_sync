"""Lightweight regression suite — no pytest dependency, just asserts.

Run with:  python tests/test_app.py

Covers what a live Dropbox/OneDrive account can't easily be exercised
against in CI: worker/thread wiring, the missing-files tree's checkbox
propagation, chunked-upload branching for large files, and remote-listing
pagination/path-scoping — all against mocked transports so no network or
real credentials are required.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# MUST happen before any `app.*` import: redirects config.py's settings file
# and keyring entries to an isolated location so this suite can never touch
# the real user's saved config file or real OS credential store. (A previous
# version of this suite didn't do this and silently overwrote a real saved
# OneDrive refresh token during manual testing — never repeat that.)
_TEST_CONFIG_DIR = tempfile.mkdtemp(prefix="sync_app_test_config_")
os.environ["SYNC_APP_CONFIG_DIR"] = _TEST_CONFIG_DIR
os.environ["SYNC_APP_KEYRING_SERVICE"] = "DropboxOneDriveSyncApp-TEST-ISOLATED"

FAILURES: list[str] = []


def check(name: str, cond: bool) -> None:
    print(f"[{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILURES.append(name)


def test_tree_checkbox_propagation():
    print("\n--- Missing-files tree: hierarchy + checkbox propagation ---")
    from PySide6.QtCore import Qt
    from app.tree_widget import MissingFilesTree
    from app.comparator import FileDiff, SyncStatus

    diffs = [
        FileDiff("a.txt", 10, 0, SyncStatus.MISSING),
        FileDiff("sub/b.txt", 20, 0, SyncStatus.MISSING),
        FileDiff("sub/c.txt", 30, 0, SyncStatus.SIZE_MISMATCH),
        FileDiff("sub/deep/d.txt", 40, 0, SyncStatus.OUTDATED),
        FileDiff("in_sync.txt", 5, 5, SyncStatus.IN_SYNC),
    ]
    tree = MissingFilesTree()
    tree.populate(diffs)

    check("in-sync files excluded from the tree", tree.file_count() == 4)
    check("all missing files pre-checked by default", len(tree.selected_diffs()) == 4)

    sub_item = next(tree.topLevelItem(i) for i in range(tree.topLevelItemCount()) if tree.topLevelItem(i).text(0) == "sub")
    sub_item.setCheckState(0, Qt.Unchecked)
    check("unchecking a folder unchecks all its descendants", [d.rel_path for d in tree.selected_diffs()] == ["a.txt"])

    def find(item, name):
        for i in range(item.childCount()):
            c = item.child(i)
            if c.text(0) == name:
                return c
            found = find(c, name)
            if found:
                return found
        return None

    b_item = find(sub_item, "b.txt")
    b_item.setCheckState(0, Qt.Checked)
    check("re-checking one nested file makes its ancestor folder partially-checked", sub_item.checkState(0) == Qt.PartiallyChecked)

    tree.set_all_checked(True)
    check("select-all checks every file", len(tree.selected_diffs()) == 4)
    tree.set_all_checked(False)
    check("deselect-all unchecks every file", len(tree.selected_diffs()) == 0)


def test_scanner_and_comparator_at_scale():
    print("\n--- Scanner + comparator + tree at scale (3000 files) ---")
    from app.scanner import scan_local
    from app.comparator import compare
    from app.tree_widget import MissingFilesTree

    scale_dir = Path(tempfile.mkdtemp(prefix="scale_"))
    n_folders, n_per_folder = 20, 150
    for i in range(n_folders):
        d = scale_dir / f"folder_{i}"
        d.mkdir()
        for j in range(n_per_folder):
            (d / f"file_{j}.txt").write_text(f"content {i}-{j}")
        (d / "Thumbs.db").write_text("skip me")

    t0 = time.time()
    local_map = scan_local(scale_dir)
    scan_elapsed = time.time() - t0
    total_expected = n_folders * n_per_folder
    check(f"scanner found all {total_expected} files, skipped Thumbs.db", len(local_map) == total_expected)
    check(f"scanner finished {total_expected} files in {scan_elapsed:.2f}s", scan_elapsed < 15)

    diffs = compare(local_map, {})
    check("comparator flags every file missing against an empty remote", len(diffs) == total_expected)

    tree = MissingFilesTree()
    t0 = time.time()
    tree.populate(diffs)
    populate_elapsed = time.time() - t0
    check(f"tree populated {tree.file_count()} nodes in {populate_elapsed:.2f}s", populate_elapsed < 15)


def test_file_tree_correctness():
    print("\n--- Dual-tree dashboard: local/destination population + highlighting ---")
    from PySide6.QtCore import Qt
    from app.cloud_base import CloudFileMeta
    from app.comparator import FileDiff, SyncStatus
    from app.file_tree import FileTree

    diffs = [
        FileDiff("a.txt", 10, 10, SyncStatus.IN_SYNC),
        FileDiff("sub/missing.txt", 20, 0, SyncStatus.MISSING),
        FileDiff("sub/changed.txt", 30, 99, SyncStatus.SIZE_MISMATCH),
    ]
    local_tree = FileTree()
    local_tree.populate_from_diffs(diffs)
    check("local tree shows every local file, not just missing ones", local_tree.file_count() == 3)

    def find(tree, name):
        def walk(item):
            for i in range(item.childCount()):
                c = item.child(i)
                if c.text(0) == name:
                    return c
                found = walk(c)
                if found:
                    return found
            return None
        for i in range(tree.topLevelItemCount()):
            top = tree.topLevelItem(i)
            if top.text(0) == name:
                return top
            found = walk(top)
            if found:
                return found
        return None

    in_sync_item = find(local_tree, "a.txt")
    missing_item = find(local_tree, "missing.txt")
    check("in-sync files are not highlighted", in_sync_item.foreground(0).color().name() != "#ff0000")
    check("missing files are highlighted", missing_item.foreground(0).color() == Qt.GlobalColor.red)

    remote_map = {"a.txt": CloudFileMeta(size=10, client_modified=datetime(2024, 1, 1))}
    dest_tree = FileTree()
    dest_tree.populate_from_remote(remote_map)
    check("destination tree shows exactly what's on the remote", dest_tree.file_count() == 1)


def test_file_type_label():
    print("\n--- File type detection from extension (PDF/Excel/etc.) ---")
    from app.file_tree import file_type_label

    check("PDF is recognized", file_type_label("statement.pdf") == "PDF")
    check("Excel .xlsx is recognized", file_type_label("2553.xlsx") == "Excel")
    check("Excel .xls is recognized", file_type_label("old.xls") == "Excel")
    check("Word doc is recognized", file_type_label("report.docx") == "Word")
    check("image is recognized", file_type_label("photo.JPG") == "Image")  # case-insensitive
    check("a file with no extension falls back to 'File'", file_type_label("README") == "File")
    check("an unrecognized extension falls back to its uppercased form", file_type_label("data.xyz") == "XYZ")


def test_file_tree_columns_show_type_modified_and_match_status():
    print("\n--- Local/Destination trees show Type, Modified, and per-file match status ---")
    from app.cloud_base import CloudFileMeta
    from app.comparator import FileDiff, SyncStatus
    from app.file_tree import FileTree
    from app.scanner import LocalFileMeta

    diffs = [
        FileDiff("report.pdf", 10, 10, SyncStatus.IN_SYNC),
        FileDiff("sheet.xlsx", 20, 0, SyncStatus.MISSING),
    ]
    local_map = {
        "report.pdf": LocalFileMeta(abs_path=Path("report.pdf"), size=10, mtime_utc=datetime(2024, 6, 15, 9, 30)),
        "sheet.xlsx": LocalFileMeta(abs_path=Path("sheet.xlsx"), size=20, mtime_utc=datetime(2024, 7, 1, 12, 0)),
    }

    local_tree = FileTree()
    local_tree.populate_from_diffs(diffs, local_map)

    def find(tree, name):
        for i in range(tree.topLevelItemCount()):
            top = tree.topLevelItem(i)
            if top.text(0) == name:
                return top
        return None

    pdf_item = find(local_tree, "report.pdf")
    check("local tree shows the file Type column", pdf_item.text(2) == "PDF")
    check("local tree shows the local Modified timestamp", pdf_item.text(3) == "2024-06-15 09:30")

    remote_map = {
        "report.pdf": CloudFileMeta(size=10, client_modified=datetime(2024, 6, 15, 9, 30)),
        "extra_on_remote.xlsx": CloudFileMeta(size=5, client_modified=datetime(2024, 1, 1)),
    }
    matched_paths = {"report.pdf"}
    dest_tree = FileTree()
    dest_tree.populate_from_remote(remote_map, matched_paths)

    matched_item = find(dest_tree, "report.pdf")
    extra_item = find(dest_tree, "extra_on_remote.xlsx")
    check("a matched destination file is labeled 'Matched to local'", matched_item.text(4) == "Matched to local")
    check("an unmatched destination file is labeled 'Extra on destination'", extra_item.text(4) == "Extra on destination")
    check("destination tree also shows the file Type column", extra_item.text(2) == "Excel")


def test_tree_file_activated_signal():
    print("\n--- Double-clicking a file row (not a folder row) emits file_activated ---")
    from app.comparator import FileDiff, SyncStatus
    from app.file_tree import FileTree

    diffs = [FileDiff("sub/report.pdf", 10, 10, SyncStatus.IN_SYNC)]
    tree = FileTree()
    tree.populate_from_diffs(diffs)

    activated = []
    tree.file_activated.connect(lambda path: activated.append(path))

    folder_item = tree.topLevelItem(0)
    file_item = folder_item.child(0)
    check("test setup: found the folder and file rows", folder_item.text(0) == "sub" and file_item.text(0) == "report.pdf")

    tree._on_item_double_clicked(folder_item, 0)
    check("double-clicking a folder row does not emit file_activated", activated == [])

    tree._on_item_double_clicked(file_item, 0)
    check("double-clicking a file row emits file_activated with its rel_path", activated == ["sub/report.pdf"])


def test_missing_files_tree_type_column_and_activation():
    print("\n--- Upload-status tree shows file Type and emits file_activated on double-click ---")
    from app.comparator import FileDiff, SyncStatus
    from app.tree_widget import MissingFilesTree

    diffs = [FileDiff("invoice.pdf", 10, 0, SyncStatus.MISSING)]
    tree = MissingFilesTree()
    tree.populate(diffs)

    item = tree.topLevelItem(0)
    check("Type column shows PDF for the missing file", item.text(2) == "PDF")

    activated = []
    tree.file_activated.connect(lambda path: activated.append(path))
    tree._on_item_double_clicked(item, 0)
    check("double-clicking the file row emits file_activated", activated == ["invoice.pdf"])


def test_show_file_details_popup():
    print("\n--- File-details popup shows cross-referenced local/destination info ---")
    import app.dropbox_panel as dp
    from app.cloud_base import CloudFileMeta
    from app.comparator import FileDiff, SyncStatus
    from app.config import AppSettings
    from app.scanner import LocalFileMeta
    from PySide6.QtWidgets import QMessageBox

    panel = dp.DropboxPanel(AppSettings())
    panel.local_map = {"report.pdf": LocalFileMeta(abs_path=Path("report.pdf"), size=10, mtime_utc=datetime(2024, 6, 15, 9, 30))}
    panel.remote_map = {"report.pdf": CloudFileMeta(size=10, client_modified=datetime(2024, 6, 15, 9, 30))}
    panel.diffs = [FileDiff("report.pdf", 10, 10, SyncStatus.IN_SYNC)]

    shown = []
    orig_exec = QMessageBox.exec
    QMessageBox.exec = lambda self: shown.append(self) or 0
    try:
        panel._show_file_details("report.pdf")
        text = shown[-1].text()
        check("details popup shows the file type", "PDF" in text)
        check("details popup shows local size", "10 B" in text)
        check("details popup shows the local modified timestamp", "2024-06-15 09:30" in text)
        check("details popup shows the destination's last-synced timestamp", "last synced: 2024-06-15 09:30" in text)
        check("details popup shows the sync status", "In sync" in text)

        shown.clear()
        panel._show_file_details("missing_everywhere.pdf")
        text2 = shown[-1].text()
        check(
            "a file with no local/remote/diff record still shows a graceful popup",
            "Local: not present" in text2 and text2.count("not present") == 2,
        )
    finally:
        QMessageBox.exec = orig_exec


def test_dual_tree_performance_at_realistic_scale():
    print("\n--- Dual-tree performance at the reported real-world scale (~80,000 files) ---")
    from app.comparator import FileDiff, SyncStatus
    from app.cloud_base import CloudFileMeta
    from app.file_tree import FileTree

    n = 80_000
    diffs = [
        FileDiff(f"folder_{i % 200}/file_{i}.txt", 100, 100, SyncStatus.IN_SYNC if i % 10 else SyncStatus.MISSING)
        for i in range(n)
    ]
    remote_map = {
        f"folder_{i % 200}/file_{i}.txt": CloudFileMeta(size=100, client_modified=datetime(2024, 1, 1))
        for i in range(n)
    }

    local_tree = FileTree()
    t0 = time.time()
    local_tree.populate_from_diffs(diffs)
    local_elapsed = time.time() - t0
    check(f"local tree ({n} files) populated in {local_elapsed:.2f}s", local_tree.file_count() == n)
    print(f"    [timing] local tree populate: {local_elapsed:.2f}s for {n} files")

    dest_tree = FileTree()
    t0 = time.time()
    dest_tree.populate_from_remote(remote_map)
    dest_elapsed = time.time() - t0
    check(f"destination tree ({n} files) populated in {dest_elapsed:.2f}s", dest_tree.file_count() == n)
    print(f"    [timing] destination tree populate: {dest_elapsed:.2f}s for {n} files")

    check(
        "combined dual-tree populate time is under 30s (generous ceiling to catch pathological blowups)",
        local_elapsed + dest_elapsed < 30,
    )


def test_dropbox_chunked_upload():
    print("\n--- Dropbox: chunked upload branch for large files ---")
    from app.dropbox_client import DropboxClient, SIMPLE_UPLOAD_LIMIT

    tmp_dir = Path(tempfile.mkdtemp(prefix="dbx_"))
    big_file = tmp_dir / "big.bin"
    big_size = SIMPLE_UPLOAD_LIMIT + 5 * 1024 * 1024
    with open(big_file, "wb") as f:
        f.truncate(big_size)

    class FakeSession:
        session_id = "sess-1"

    class FakeMeta:
        def __init__(self, size):
            self.size = size
            self.id = "id:fake"

    class FakeDbx:
        def __init__(self, reported_size=None):
            self.simple_calls, self.start_calls, self.append_calls, self.finish_calls = [], [], [], []
            self._reported_size = reported_size

        def files_upload(self, data, path, mode, client_modified, mute):
            self.simple_calls.append(len(data))
            return FakeMeta(self._reported_size if self._reported_size is not None else len(data))

        def files_upload_session_start(self, chunk):
            self.start_calls.append(len(chunk))
            return FakeSession()

        def files_upload_session_append_v2(self, chunk, cursor):
            self.append_calls.append(len(chunk))
            cursor.offset += len(chunk)

        def files_upload_session_finish(self, chunk, cursor, commit):
            self.finish_calls.append(len(chunk))
            return FakeMeta(self._reported_size if self._reported_size is not None else cursor.offset + len(chunk))

    client = object.__new__(DropboxClient)
    client._dbx = FakeDbx(reported_size=big_size)
    events = []
    result = client.upload_file(big_file, "/W/big.bin", datetime.utcnow(), progress_cb=lambda d, t: events.append((d, t)))
    check("upload_file returns a confirmed UploadResult for the chunked path", result.remote_size == big_size and result.remote_id == "id:fake")

    check("large file skips simple upload", len(client._dbx.simple_calls) == 0)
    check("large file opens exactly one upload session", len(client._dbx.start_calls) == 1)
    check("large file finishes the session exactly once", len(client._dbx.finish_calls) == 1)
    total = client._dbx.start_calls[0] + sum(client._dbx.append_calls) + sum(client._dbx.finish_calls)
    check("total bytes across chunks equals file size", total == big_size)
    check("progress callback reports full completion", events[-1] == (big_size, big_size))

    small_file = tmp_dir / "small.txt"
    small_file.write_text("hi")
    client2 = object.__new__(DropboxClient)
    client2._dbx = FakeDbx()
    client2.upload_file(small_file, "/W/small.txt", datetime.utcnow())
    check("small file uses simple upload, not a session", len(client2._dbx.simple_calls) == 1 and len(client2._dbx.start_calls) == 0)


def test_dropbox_upload_confirmation():
    print("\n--- Dropbox: upload is confirmed against the server's reported size, not just success ---")
    from app.dropbox_client import DropboxClient, DropboxSyncError
    from app.cloud_base import UploadResult

    class FakeMeta:
        def __init__(self, size):
            self.size = size
            self.id = "id:fake"

    class FakeDbxOk:
        def files_upload(self, data, path, mode, client_modified, mute):
            return FakeMeta(len(data))

    class FakeDbxMismatch:
        def files_upload(self, data, path, mode, client_modified, mute):
            return FakeMeta(len(data) - 1)  # server reports fewer bytes than sent

    class FakeDbxNoMeta:
        def files_upload(self, data, path, mode, client_modified, mute):
            return None  # SDK call "succeeded" but handed back nothing usable

    tmp = Path(tempfile.mkdtemp(prefix="dbx_confirm_"))
    f = tmp / "a.txt"
    f.write_text("confirm me")

    ok_client = object.__new__(DropboxClient)
    ok_client._dbx = FakeDbxOk()
    result = ok_client.upload_file(f, "/W/a.txt", datetime.utcnow())
    check("matching size returns a confirmed UploadResult", result == UploadResult(remote_id="id:fake", remote_size=f.stat().st_size))

    for label, dbx in [("size mismatch", FakeDbxMismatch()), ("no metadata at all", FakeDbxNoMeta())]:
        bad_client = object.__new__(DropboxClient)
        bad_client._dbx = dbx
        try:
            bad_client.upload_file(f, "/W/a.txt", datetime.utcnow())
            check(f"Dropbox {label} raises instead of reporting success", False)
        except DropboxSyncError as exc:
            check(f"Dropbox {label} raises instead of reporting success", "did not verify" in str(exc))


def test_onedrive_chunked_upload():
    print("\n--- OneDrive: chunked upload branch for large files ---")
    import app.onedrive_client as odc
    from app.onedrive_client import OneDriveClient, UPLOAD_SESSION_THRESHOLD

    od_dir = Path(tempfile.mkdtemp(prefix="od_"))
    big_file = od_dir / "big.bin"
    big_size = UPLOAD_SESSION_THRESHOLD + 2 * 1024 * 1024
    with open(big_file, "wb") as f:
        f.truncate(big_size)

    class FakeResp:
        def __init__(self, status_code=200, data=None):
            self.status_code = status_code
            self.ok = 200 <= status_code < 300
            self._data = data or {}
            self.text = str(self._data)

        def json(self):
            return self._data

    class FakeSession:
        def put(self, url, data=None, timeout=None):
            raise AssertionError("large file must not use the simple content PUT")

        def post(self, url, json=None, timeout=None):
            return FakeResp(200, {"uploadUrl": "https://upload.example/s1"})

        def patch(self, url, json=None, timeout=None):
            return FakeResp(200, {})

    chunk_calls = []

    def fake_put(url, headers=None, data=None, timeout=None):
        chunk_calls.append((url, headers, len(data)))
        start, rest = headers["Content-Range"].replace("bytes ", "").split("-")
        end, total = rest.split("/")
        is_last = int(end) + 1 == int(total)
        return FakeResp(201 if is_last else 202, {"id": "item", "size": int(total)} if is_last else {})

    orig_put = odc.requests.put
    odc.requests.put = fake_put
    try:
        client = object.__new__(OneDriveClient)
        client._session = FakeSession()
        events = []
        result = client.upload_file(big_file, "/W/big.bin", datetime.utcnow(), progress_cb=lambda d, t: events.append((d, t)))
        check("upload_file returns a confirmed UploadResult for the chunked path", result.remote_size == big_size and result.remote_id == "item")

        check("all chunks went to the pre-authenticated session URL", all(c[0] == "https://upload.example/s1" for c in chunk_calls))
        total = sum(c[2] for c in chunk_calls)
        check("total chunk bytes equals file size", total == big_size)
        check("progress callback reports full completion", events[-1] == (big_size, big_size))

        ranges = []
        for _, headers, _ in chunk_calls:
            s, rest = headers["Content-Range"].replace("bytes ", "").split("-")
            e, _ = rest.split("/")
            ranges.append((int(s), int(e)))
        ranges.sort()
        check("Content-Range chunks are contiguous with no gaps", all(ranges[i][1] + 1 == ranges[i + 1][0] for i in range(len(ranges) - 1)))
    finally:
        odc.requests.put = orig_put


def test_onedrive_upload_confirmation():
    print("\n--- OneDrive: upload is confirmed against the server's reported size, not just a 2xx status ---")
    from app.cloud_base import UploadResult
    from app.onedrive_client import OneDriveClient, OneDriveSyncError

    od_dir = Path(tempfile.mkdtemp(prefix="od_confirm_"))
    small_file = od_dir / "small.txt"
    small_file.write_text("hello onedrive")
    file_size = small_file.stat().st_size

    class FakeResp:
        def __init__(self, status_code=200, data=None):
            self.status_code = status_code
            self.ok = 200 <= status_code < 300
            self._data = data or {}
            self.text = str(self._data)

        def json(self):
            return self._data

    class FakeSession:
        def __init__(self, put_data, get_data=None):
            self._put_data = put_data
            self._get_data = get_data
            self.get_calls = 0

        def put(self, url, data=None, timeout=None):
            return FakeResp(200, self._put_data)

        def patch(self, url, json=None, timeout=None):
            return FakeResp(200, {})

        def get(self, url, timeout=None):
            self.get_calls += 1
            return FakeResp(200, self._get_data or {})

    client = object.__new__(OneDriveClient)
    client._session = FakeSession({"id": "abc", "size": file_size})
    result = client.upload_file(small_file, "/W/small.txt", datetime.utcnow())
    check("matching size returns a confirmed UploadResult", result == UploadResult(remote_id="abc", remote_size=file_size))

    client2 = object.__new__(OneDriveClient)
    client2._session = FakeSession({"id": "abc", "size": file_size - 1})
    try:
        client2.upload_file(small_file, "/W/small.txt", datetime.utcnow())
        check("a size mismatch from the server raises instead of reporting success", False)
    except OneDriveSyncError as exc:
        check("a size mismatch from the server raises instead of reporting success", "did not verify" in str(exc))

    # If the chunked-upload path never gets item metadata back inline (e.g. the
    # final chunk response omits it), upload_file must fall back to a direct
    # GET rather than silently trusting the last chunk's 2xx status.
    import app.onedrive_client as odc
    from app.onedrive_client import UPLOAD_SESSION_THRESHOLD

    big_file = od_dir / "big.bin"
    big_size = UPLOAD_SESSION_THRESHOLD + 1024 * 1024
    with open(big_file, "wb") as f:
        f.truncate(big_size)

    class ChunkedNoItemSession(FakeSession):
        def post(self, url, json=None, timeout=None):
            return FakeResp(200, {"uploadUrl": "https://upload.example/s1"})

    def fake_put_no_item(url, headers=None, data=None, timeout=None):
        return FakeResp(202, {})  # never carries item metadata, on any chunk

    orig_put = odc.requests.put
    odc.requests.put = fake_put_no_item
    try:
        client3 = object.__new__(OneDriveClient)
        client3._session = ChunkedNoItemSession({}, get_data={"id": "xyz", "size": big_size})
        result3 = client3.upload_file(big_file, "/W/big.bin", datetime.utcnow())
        check(
            "missing inline item metadata falls back to a GET to confirm the upload",
            client3._session.get_calls == 1 and result3.remote_size == big_size,
        )
    finally:
        odc.requests.put = orig_put


def test_remote_listing_pagination():
    print("\n--- Remote listing: pagination + path scoping (both providers) ---")
    from app.dropbox_client import DropboxClient
    from dropbox.files import FileMetadata, FolderMetadata

    class FakeFileMeta(FileMetadata):
        def __init__(self, path_display, size, client_modified):
            self.path_display = path_display
            self.size = size
            self.client_modified = client_modified

    class FakeResult:
        def __init__(self, entries, has_more=False, cursor=None):
            self.entries, self.has_more, self.cursor = entries, has_more, cursor

    class FakeDbx:
        def files_list_folder(self, path, recursive):
            return FakeResult([FakeFileMeta("/W/a.txt", 10, datetime(2024, 1, 1)), FolderMetadata()], True, "c1")

        def files_list_folder_continue(self, cursor):
            return FakeResult([FakeFileMeta("/W/sub/b.txt", 20, datetime(2024, 1, 2))], False)

    dbx_client = object.__new__(DropboxClient)
    dbx_client._dbx = FakeDbx()
    files = dbx_client.list_folder_recursive("/W")
    check("Dropbox pagination + folder-filtering + path-stripping all correct", set(files.keys()) == {"a.txt", "sub/b.txt"})

    from app.onedrive_client import OneDriveClient

    class FakeResp:
        def __init__(self, status_code=200, data=None):
            self.status_code = status_code
            self.ok = 200 <= status_code < 300
            self._data = data or {}
            self.text = str(self._data)

        def json(self):
            return self._data

    def item(name, parent, is_file=True):
        d = {"name": name, "size": 1, "parentReference": {"path": parent}, "fileSystemInfo": {"lastModifiedDateTime": "2024-01-01T00:00:00Z"}}
        d["file" if is_file else "folder"] = {}
        return d

    class FakeSession:
        def __init__(self):
            self.calls = 0

        def get(self, url, params=None, timeout=None):
            self.calls += 1
            if self.calls == 1:
                return FakeResp(200, {
                    "value": [item("a.txt", "/drive/root:/Website"), item("sub", "/drive/root:/Website", False),
                              item("outside.txt", "/drive/root:/Other")],
                    "@odata.nextLink": "https://graph.microsoft.com/v1.0/next",
                })
            return FakeResp(200, {"value": [item("b.txt", "/drive/root:/Website/sub")]})

    od_client = object.__new__(OneDriveClient)
    od_client._session = FakeSession()
    files_od = od_client.list_folder_recursive("/Website")
    check("OneDrive pagination + folder-filtering + out-of-scope filtering all correct", set(files_od.keys()) == {"a.txt", "sub/b.txt"})

    class FakeSession404:
        def get(self, url, params=None, timeout=None):
            return FakeResp(404, {})

    od_client2 = object.__new__(OneDriveClient)
    od_client2._session = FakeSession404()
    check("OneDrive 404 on a not-yet-existing folder returns empty, not an exception", od_client2.list_folder_recursive("/New") == {})


def test_onedrive_transient_error_retry():
    print("\n--- OneDrive: a transient 500 'generalException' is retried instead of failing the whole scan ---")
    import app.onedrive_client as odc
    from app.onedrive_client import OneDriveClient, OneDriveSyncError

    class FakeResp:
        def __init__(self, status_code=200, data=None):
            self.status_code = status_code
            self.ok = 200 <= status_code < 300
            self._data = data or {}
            self.text = str(self._data)
            self.headers = {}

        def json(self):
            return self._data

    sleep_calls = []
    orig_sleep = odc.time.sleep
    odc.time.sleep = lambda s: sleep_calls.append(s)
    try:
        # Fails twice with a transient 500, then succeeds on the third attempt.
        class FlakySession:
            def __init__(self):
                self.calls = 0

            def get(self, url, params=None, timeout=None):
                self.calls += 1
                if self.calls <= 2:
                    return FakeResp(500, {"error": {"code": "generalException", "message": "General exception while processing"}})
                return FakeResp(200, {"value": [{"name": "a.txt", "size": 5, "file": {}, "parentReference": {"path": "/drive/root:"}}]})

        client = object.__new__(OneDriveClient)
        client._session = FlakySession()
        files = client.list_folder_recursive("")
        check("recovers after transient 500s and returns the eventual successful listing", set(files.keys()) == {"a.txt"})
        check("retried exactly twice before the successful third attempt", client._session.calls == 3)
        check("backs off between retries instead of hammering the API", len(sleep_calls) == 2)

        # Persistent 500s (server genuinely down) must still surface as an error, not hang forever.
        class AlwaysFailingSession:
            def __init__(self):
                self.calls = 0

            def get(self, url, params=None, timeout=None):
                self.calls += 1
                return FakeResp(500, {"error": {"code": "generalException", "message": "General exception while processing"}})

        sleep_calls.clear()
        client2 = object.__new__(OneDriveClient)
        client2._session = AlwaysFailingSession()
        try:
            client2.list_folder_recursive("")
            check("persistent 500s eventually raise instead of retrying forever", False)
        except OneDriveSyncError as exc:
            check("persistent 500s eventually raise instead of retrying forever", "500" in str(exc))
        check("retry attempts are capped, not unbounded", client2._session.calls == 5)
    finally:
        odc.time.sleep = orig_sleep


def test_onedrive_url_construction():
    print("\n--- OneDrive: constructed Graph URLs are well-formed (regression for the double-colon bug) ---")
    # A previous version built "root:{path}::/delta" (double colon) because
    # _item_url already inserts its own colon before the suffix, and every
    # caller ALSO included a leading colon in the suffix it passed. Real
    # Dropbox/OneDrive tests all mocked the session and never asserted the
    # actual URL string, so this shipped and broke every nested-folder scan
    # in production before being caught by manual testing. Assert the exact
    # URL string everywhere it's built, permanently, so this can't recur.
    from app.onedrive_client import _item_url, OneDriveClient

    root = OneDriveClient.normalize_root("/My files/BKP_MASTER_Support")
    delta_url = _item_url(root, "/delta")
    check("no double colon in the delta URL", "::" not in delta_url)
    check("delta URL ends with the correct single-colon suffix", delta_url.endswith(":/delta"))
    check("spaces are percent-encoded", "My%20files" in delta_url and " " not in delta_url)

    content_url = _item_url("/Folder/file.txt", "/content")
    check("no double colon in the content URL", "::" not in content_url)
    check("content URL ends with the correct single-colon suffix", content_url.endswith(":/content"))

    session_url = _item_url("/Folder/file.txt", "/createUploadSession")
    check("no double colon in the createUploadSession URL", "::" not in session_url)
    check("createUploadSession URL ends with the correct suffix", session_url.endswith(":/createUploadSession"))

    # Also assert it end-to-end through list_folder_recursive, which is what
    # actually broke — mock the session and capture the exact URL it was called with.
    class CapturingSession:
        def __init__(self):
            self.urls = []

        def get(self, url, params=None, timeout=None):
            self.urls.append(url)

            class R:
                status_code = 200
                ok = True
                text = "{}"

                def json(self):
                    return {"value": []}

            return R()

    od_client = object.__new__(OneDriveClient)
    od_client._session = CapturingSession()
    od_client.list_folder_recursive("/My files/BKP_MASTER_Support")
    check(
        "list_folder_recursive calls the well-formed delta URL, no double colon",
        len(od_client._session.urls) == 1 and "::" not in od_client._session.urls[0],
    )


def test_list_child_folders():
    print("\n--- Browsing the account for a destination folder (both providers) ---")
    from app.dropbox_client import DropboxClient
    from dropbox.files import FileMetadata, FolderMetadata

    class FakeFolderMeta(FolderMetadata):
        def __init__(self, name, path_display):
            self.name = name
            self.path_display = path_display

    class FakeFileMeta2(FileMetadata):
        def __init__(self, name, path_display):
            self.name = name
            self.path_display = path_display
            self.size = 1

    class FakeResult:
        def __init__(self, entries, has_more=False, cursor=None):
            self.entries, self.has_more, self.cursor = entries, has_more, cursor

    class FakeDbxChildren:
        def __init__(self):
            self.calls = []

        def files_list_folder(self, path, recursive):
            self.calls.append((path, recursive))
            return FakeResult(
                [FakeFolderMeta("Zeta", "/Zeta"), FakeFileMeta2("ignored.txt", "/ignored.txt"), FakeFolderMeta("Alpha", "/Alpha")],
                has_more=True,
                cursor="c1",
            )

        def files_list_folder_continue(self, cursor):
            return FakeResult([FakeFolderMeta("Middle", "/Middle")], has_more=False)

    dbx_client = object.__new__(DropboxClient)
    dbx_client._dbx = FakeDbxChildren()
    folders = dbx_client.list_child_folders("/")
    check("Dropbox: files are excluded, only folders returned", {f.name for f in folders} == {"Zeta", "Alpha", "Middle"})
    check("Dropbox: non-recursive listing requested (recursive=False)", dbx_client._dbx.calls[0] == ("", False))
    check("Dropbox: results sorted by name", [f.name for f in folders] == ["Alpha", "Middle", "Zeta"])
    check("Dropbox: pagination followed for child-folder listing too", len(dbx_client._dbx.calls) == 1 and dbx_client._dbx.calls[0][1] is False)

    from app.onedrive_client import OneDriveClient

    class FakeRespC:
        def __init__(self, status_code=200, data=None):
            self.status_code = status_code
            self.ok = 200 <= status_code < 300
            self._data = data or {}
            self.text = str(self._data)

        def json(self):
            return self._data

    class FakeSessionChildren:
        def get(self, url, params=None, timeout=None):
            check("OneDrive: root child-folder listing hits /me/drive/root/children", url.endswith("/me/drive/root/children"))
            return FakeRespC(200, {
                "value": [
                    {"name": "Attachments", "folder": {}},
                    {"name": "BKP_MASTER_Support", "folder": {}},
                    {"name": "notes.txt", "file": {}},
                ]
            })

    od_client = object.__new__(OneDriveClient)
    od_client._session = FakeSessionChildren()
    root_folders = od_client.list_child_folders("/")
    check("OneDrive: files excluded from child-folder listing", {f.name for f in root_folders} == {"Attachments", "BKP_MASTER_Support"})
    check("OneDrive: child paths are correct at root", {f.path for f in root_folders} == {"/Attachments", "/BKP_MASTER_Support"})

    class FakeSessionNested:
        def get(self, url, params=None, timeout=None):
            check("OneDrive: nested child-folder listing uses well-formed URL (no double colon)", "::" not in url)
            return FakeRespC(200, {"value": [{"name": "TAX RETURNS", "folder": {}}]})

    od_client2 = object.__new__(OneDriveClient)
    od_client2._session = FakeSessionNested()
    nested = od_client2.list_child_folders("/BKP_MASTER_Support")
    check("OneDrive: nested child path built correctly", nested[0].path == "/BKP_MASTER_Support/TAX RETURNS")

    class FakeSession404b:
        def get(self, url, params=None, timeout=None):
            return FakeRespC(404, {})

    od_client3 = object.__new__(OneDriveClient)
    od_client3._session = FakeSession404b()
    check("OneDrive: 404 on child-folder listing returns empty list, not an exception", od_client3.list_child_folders("/Missing") == [])


def test_dropbox_list_child_folders_pagination_errors_are_caught():
    print("\n--- Dropbox: an error during child-folder PAGINATION is caught, not left to crash the picker ---")
    # Real gap: the `while result.has_more:` continuation loop used to sit
    # OUTSIDE list_child_folders' try block entirely, so any failure fetching
    # a later page (rate limit, transient API error, a bug) propagated
    # uncaught out of the folder picker, which just looked permanently stuck
    # with no error shown ("destination folder not loading").
    from app.dropbox_client import DropboxClient, DropboxSyncError
    from dropbox.files import FolderMetadata

    class FakeFolderMeta(FolderMetadata):
        def __init__(self, name, path_display):
            self.name = name
            self.path_display = path_display

    class FakeResult:
        def __init__(self, entries, has_more=False, cursor=None):
            self.entries, self.has_more, self.cursor = entries, has_more, cursor

    class FlakyPaginationDbx:
        def files_list_folder(self, path, recursive):
            return FakeResult([FakeFolderMeta("Alpha", "/Alpha")], has_more=True, cursor="c1")

        def files_list_folder_continue(self, cursor):
            raise RuntimeError("simulated failure fetching the next page")

    client = object.__new__(DropboxClient)
    client._dbx = FlakyPaginationDbx()
    try:
        client.list_child_folders("/")
        check("a pagination failure raises a clear DropboxSyncError instead of an uncaught error", False)
    except DropboxSyncError as exc:
        check("a pagination failure raises a clear DropboxSyncError instead of an uncaught error", "Unexpected error" in str(exc))
    except RuntimeError:
        check("a pagination failure raises a clear DropboxSyncError instead of an uncaught error", False)


def test_onedrive_list_child_folders_unexpected_error_wrapped():
    print("\n--- OneDrive: an unexpected parsing error is wrapped, not left to crash the picker ---")
    from app.onedrive_client import OneDriveClient, OneDriveSyncError

    class FakeResp:
        def __init__(self, status_code=200, data=None):
            self.status_code = status_code
            self.ok = 200 <= status_code < 300
            self._data = data or {}
            self.text = str(self._data)

        def json(self):
            return self._data

    class MalformedSession:
        def get(self, url, params=None, timeout=None):
            return FakeResp(200, {"value": [{"folder": {}}]})  # missing "name" -> KeyError in the parsing loop

    client = object.__new__(OneDriveClient)
    client._session = MalformedSession()
    try:
        client.list_child_folders("/")
        check("a malformed response raises a clear OneDriveSyncError instead of a raw KeyError", False)
    except OneDriveSyncError as exc:
        check("a malformed response raises a clear OneDriveSyncError instead of a raw KeyError", "Unexpected error" in str(exc))
    except KeyError:
        check("a malformed response raises a clear OneDriveSyncError instead of a raw KeyError", False)


def test_dropbox_missing_scope_gives_actionable_guidance():
    print("\n--- Dropbox: a missing OAuth scope (BadInputError) maps to actionable App Console guidance ---")
    # Real production error observed: BadInputError isn't a subclass of
    # ApiError/AuthError, so it used to slip past every except clause in
    # list_folder_recursive/list_child_folders/upload_file. It's now caught
    # everywhere and translated into a message telling the user exactly what
    # to enable and that they must reconnect afterward.
    from app.dropbox_client import DropboxClient, DropboxSyncError, _describe_dropbox_api_error
    from dropbox.exceptions import BadInputError

    scope_error = BadInputError(
        "req123",
        "Error in call to API function 'files/list_folder': Your app is not permitted to access this "
        "endpoint because it does not have the required scope 'files.metadata.read'. The owner of the "
        "app can enable the scope for the app using the Permissions tab on the App Console.",
    )

    msg = _describe_dropbox_api_error(scope_error)
    check("missing-scope guidance names the App Console Permissions tab", "Permissions tab" in msg)
    check("missing-scope guidance tells the user to reconnect afterward", "reconnect" in msg.lower())
    check("missing-scope guidance preserves the original scope name", "files.metadata.read" in msg)

    generic_error = RuntimeError("some other unrelated failure")
    generic_msg = _describe_dropbox_api_error(generic_error)
    check("a non-scope error still gets a generic but clear message", "Unexpected error" in generic_msg and "Permissions tab" not in generic_msg)

    class ScopeBlockedDbx:
        def files_list_folder(self, path, recursive):
            raise scope_error

    client = object.__new__(DropboxClient)
    client._dbx = ScopeBlockedDbx()
    try:
        client.list_folder_recursive("/")
        check("list_folder_recursive surfaces the missing-scope guidance, not a raw BadInputError", False)
    except DropboxSyncError as exc:
        check("list_folder_recursive surfaces the missing-scope guidance, not a raw BadInputError", "Permissions tab" in str(exc))

    client2 = object.__new__(DropboxClient)
    client2._dbx = ScopeBlockedDbx()
    try:
        client2.list_child_folders("/")
        check("list_child_folders surfaces the missing-scope guidance, not a raw BadInputError", False)
    except DropboxSyncError as exc:
        check("list_child_folders surfaces the missing-scope guidance, not a raw BadInputError", "Permissions tab" in str(exc))

    class ScopeBlockedUploadDbx:
        def files_upload(self, data, path, mode, client_modified, mute):
            raise scope_error

    tmp = Path(tempfile.mkdtemp(prefix="dbx_scope_"))
    f = tmp / "a.txt"
    f.write_text("hi")
    client3 = object.__new__(DropboxClient)
    client3._dbx = ScopeBlockedUploadDbx()
    try:
        client3.upload_file(f, "/W/a.txt", datetime.utcnow())
        check("upload_file surfaces the missing-scope guidance, not a raw BadInputError", False)
    except DropboxSyncError as exc:
        check("upload_file surfaces the missing-scope guidance, not a raw BadInputError", "Permissions tab" in str(exc))


def test_dropbox_missing_scope_via_autherror_not_treated_as_session_expired():
    print("\n--- Dropbox: AuthError('missing_scope', ...) is NOT treated as an expired session ---")
    # Real production error observed: some endpoints report a missing scope
    # as AuthError('missing_scope', TokenScopeError(...)) rather than
    # BadInputError. Since AuthError was already caught (unlike BadInputError
    # last time), this didn't crash — but it WAS silently mapped to
    # DropboxAuthError, which the UI treats as "session expired, please
    # reconnect". That's actively misleading: reconnecting alone does nothing
    # for a missing-scope problem until the Permissions tab is fixed first.
    import dropbox.auth as dropbox_auth
    from dropbox.exceptions import AuthError as SdkAuthError
    from app.dropbox_client import DropboxAuthError, DropboxSyncError, _raise_from_auth_error

    scope_reason = dropbox_auth.AuthError.missing_scope(dropbox_auth.TokenScopeError(required_scope="files.metadata.read"))
    missing_scope_error = SdkAuthError("req123", scope_reason)

    # Call the shared helper the same way every AuthError call site in
    # dropbox_client.py does, and confirm it raises DropboxSyncError (a
    # permissions/config problem) rather than DropboxAuthError (a session
    # problem the UI would tell the user to "just reconnect" to fix).
    try:
        _raise_from_auth_error(missing_scope_error)
        check("a missing-scope AuthError raises DropboxSyncError, not DropboxAuthError", False)
    except DropboxSyncError as exc:
        check("a missing-scope AuthError raises DropboxSyncError, not DropboxAuthError", True)
        check("the guidance names the App Console Permissions tab", "Permissions tab" in str(exc))
        check("the guidance includes the specific missing scope", "files.metadata.read" in str(exc))
        check("the guidance tells the user to reconnect only after fixing permissions", "reconnect" in str(exc).lower())
    except DropboxAuthError:
        check("a missing-scope AuthError raises DropboxSyncError, not DropboxAuthError", False)

    # A genuine, unrelated auth failure (bad/expired token) must still map to
    # DropboxAuthError so the app correctly treats it as "reconnect required".
    genuine_expired_error = SdkAuthError("req456", dropbox_auth.AuthError.invalid_access_token)
    try:
        _raise_from_auth_error(genuine_expired_error)
        check("a genuine expired-token AuthError still raises DropboxAuthError", False)
    except DropboxAuthError:
        check("a genuine expired-token AuthError still raises DropboxAuthError", True)
    except DropboxSyncError:
        check("a genuine expired-token AuthError still raises DropboxAuthError", False)


def test_folder_picker_lazy_loading():
    print("\n--- Folder picker: loads lazily, never eagerly recurses the whole account ---")
    from app.cloud_base import CloudFolderEntry
    from app.folder_picker import RemoteFolderPickerDialog

    class FakeClient:
        display_name = "Fake"

        def __init__(self):
            self.calls = []

        def list_child_folders(self, path):
            self.calls.append(path)
            if path == "/":
                return [CloudFolderEntry("Sub", "/Sub")]
            if path == "/Sub":
                return [CloudFolderEntry("Deep", "/Sub/Deep")]
            return []

    client = FakeClient()
    dlg = RemoteFolderPickerDialog(client, "TestProvider")
    check("root's children are loaded immediately on open", client.calls == ["/"])

    root_item = dlg.tree.topLevelItem(0)
    check("root item is expanded by default", root_item.isExpanded())
    sub_item = root_item.child(0)
    check("root's child folder appears in the tree", sub_item is not None and sub_item.text(0) == "Sub")
    check("the nested 'Deep' folder is NOT loaded yet (lazy)", client.calls == ["/"])

    dlg.tree.expandItem(sub_item)
    check("expanding 'Sub' triggers loading only its own children", client.calls == ["/", "/Sub"])

    deep_item = sub_item.child(0)
    check("'Deep' folder now appears after expansion", deep_item is not None and deep_item.text(0) == "Deep")

    dlg.tree.setCurrentItem(sub_item)
    check("selecting a folder updates selected_path", dlg.selected_path == "/Sub")


def test_folder_picker_shows_error_for_unexpected_exception():
    print("\n--- Folder picker: an unanticipated error shows a message instead of leaving the item stuck loading ---")
    from app.cloud_base import CloudFolderEntry
    from app.folder_picker import LOADED_ROLE, RemoteFolderPickerDialog
    from PySide6.QtWidgets import QMessageBox

    class BuggyClient:
        def list_child_folders(self, remote_path):
            if remote_path == "/":
                return [CloudFolderEntry(name="Broken", path="/Broken")]
            raise RuntimeError("simulated unexpected bug")

    warnings = []
    orig_warning = QMessageBox.warning
    QMessageBox.warning = staticmethod(lambda *a, **k: warnings.append(a) or QMessageBox.StandardButton.Ok)
    try:
        dlg = RemoteFolderPickerDialog(BuggyClient(), "Dropbox")
        root_item = dlg.tree.topLevelItem(0)
        broken_item = root_item.child(0)
        dlg.tree.expandItem(broken_item)  # triggers _on_item_expanded -> _load_children -> raises RuntimeError

        check("an unexpected exception shows a warning instead of crashing the picker", len(warnings) == 1)
        check(
            "the warning message includes the underlying error",
            warnings and "simulated unexpected bug" in " ".join(str(a) for a in warnings[0]),
        )
        check(
            "the item is marked loaded so it isn't stuck showing 'Loading…' forever",
            broken_item.data(0, LOADED_ROLE) is True,
        )
    finally:
        QMessageBox.warning = orig_warning


def test_config_and_secret_roundtrip():
    print("\n--- Config: settings + secret storage round trip ---")
    from app import config

    settings = config.AppSettings(dropbox_app_key="k1", onedrive_client_id="c1")
    config.set_secret(config.DROPBOX_TOKEN_KEY, "tok-1", settings)
    config.save_settings(settings)

    reloaded = config.load_settings()
    check("settings JSON round-trips", reloaded.dropbox_app_key == "k1" and reloaded.onedrive_client_id == "c1")

    fetched = config.get_secret(config.DROPBOX_TOKEN_KEY, reloaded)
    check("secret round-trips via keyring/fallback", fetched == "tok-1")

    config.clear_secret(config.DROPBOX_TOKEN_KEY, reloaded)
    check("secret is gone after clear", config.get_secret(config.DROPBOX_TOKEN_KEY, reloaded) is None)


def test_config_isolated_from_real_user_data():
    print("\n--- Config: this suite is isolated from the real config file and real keyring ---")
    from app import config

    check(
        "config dir is redirected to the isolated test dir, not %APPDATA%",
        str(config._config_dir()) == _TEST_CONFIG_DIR,
    )
    check(
        "keyring service name is redirected to an isolated name, not the real app's",
        config._service_name() == "DropboxOneDriveSyncApp-TEST-ISOLATED",
    )
    real_config_path = Path(os.environ.get("APPDATA", "")) / "DropboxOneDriveSyncApp" / "config.json"
    check(
        "the real production config path is untouched by this run",
        config._config_path() != real_config_path,
    )


def test_pkce_authorize_urls():
    print("\n--- System-browser OAuth: PKCE authorize URL construction ---")
    import base64
    import hashlib
    from urllib.parse import parse_qs, urlparse

    from app.dropbox_client import REDIRECT_URI as DBX_REDIRECT
    from app.dropbox_client import build_authorize_url as dbx_build
    from app.onedrive_client import REDIRECT_URI as OD_REDIRECT
    from app.onedrive_client import build_authorize_url as od_build

    url, verifier = dbx_build("my-app-key")
    q = parse_qs(urlparse(url).query)
    check("Dropbox authorize URL carries the app key as client_id", q["client_id"][0] == "my-app-key")
    check("Dropbox redirect_uri is the app's loopback listener", q["redirect_uri"][0] == DBX_REDIRECT and DBX_REDIRECT.startswith("http://localhost:"))
    expected_challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    check("Dropbox PKCE code_challenge matches SHA256(verifier)", q["code_challenge"][0] == expected_challenge)

    url2, _ = od_build("my-client-id")
    q2 = parse_qs(urlparse(url2).query)
    check("OneDrive authorize URL carries the client id", q2["client_id"][0] == "my-client-id")
    check("OneDrive redirect_uri is the app's loopback listener", q2["redirect_uri"][0] == OD_REDIRECT and OD_REDIRECT.startswith("http://localhost:"))
    check("Dropbox and OneDrive use the same loopback redirect_uri", DBX_REDIRECT == OD_REDIRECT)
    check("OneDrive requests offline_access (needed for a refresh token)", "offline_access" in q2["scope"][0])
    check("OneDrive requests Files.ReadWrite", "Files.ReadWrite" in q2["scope"][0])


def test_friendly_auth_error_messages():
    print("\n--- Auth error messages give actionable guidance, not raw Azure/Dropbox text ---")
    from app.dropbox_client import _friendly_dropbox_error
    from app.onedrive_client import _friendly_azure_error

    class FakeResp:
        def __init__(self, data):
            self._data = data
            self.status_code = 400
            self.text = str(data)

        def json(self):
            return self._data

    msg = _friendly_azure_error(FakeResp({"error": "invalid_request", "error_description": "AADSTS50059: No tenant-identifying information found..."}))
    check("AADSTS50059 message points at the tenant-ID/client-ID mix-up", "Directory (tenant) ID" in msg and "Application (client) ID" in msg)

    msg2 = _friendly_dropbox_error(FakeResp({"error": "redirect_uri_mismatch", "error_description": "redirect_uri did not match"}))
    check("Dropbox redirect_uri_mismatch message tells you what to register", "Redirect URIs" in msg2 and "http://localhost" in msg2)


def test_loopback_auth_server():
    print("\n--- Loopback auth server: captures the OAuth redirect from a real browser ---")
    import urllib.request

    from app.oauth_local_server import CALLBACK_PATH, CALLBACK_PORT, LoopbackAuthServer

    server = LoopbackAuthServer(timeout_seconds=10)
    server.start()
    try:
        check("no result before any request arrives", server.result is None)
        urllib.request.urlopen(f"http://127.0.0.1:{CALLBACK_PORT}{CALLBACK_PATH}?code=ABC123&state=xyz", timeout=5)
        deadline = time.time() + 5
        while server.result is None and time.time() < deadline:
            time.sleep(0.05)
        check("a request with 'code' is captured as (code, None)", server.result == ("ABC123", None))
    finally:
        server.shutdown()

    server2 = LoopbackAuthServer(timeout_seconds=10)
    server2.start()
    try:
        urllib.request.urlopen(
            f"http://127.0.0.1:{CALLBACK_PORT}{CALLBACK_PATH}?error=access_denied&error_description=User+denied+access",
            timeout=5,
        )
        deadline = time.time() + 5
        while server2.result is None and time.time() < deadline:
            time.sleep(0.05)
        check("a request with 'error' is captured as (None, description)", server2.result == (None, "User denied access"))
    finally:
        server2.shutdown()


def test_browser_sign_in_dialog():
    print("\n--- Browser sign-in wait dialog: polls the loopback server correctly ---")
    from app.oauth_wait_dialog import BrowserSignInDialog

    class FakeServer:
        def __init__(self):
            self.result = None
            self.shutdown_called = False

        def shutdown(self):
            self.shutdown_called = True

    dlg = BrowserSignInDialog("TestProvider")
    events = {"code": None, "failed": None}
    dlg.code_received.connect(lambda c: events.update(code=c))
    dlg.auth_failed.connect(lambda m: events.update(failed=m))
    fake_server = FakeServer()
    dlg.start(fake_server)
    check("no result yet -> nothing fires", events["code"] is None and events["failed"] is None)

    fake_server.result = ("ABC123", None)
    dlg._poll()
    check("a captured code fires code_received", events["code"] == "ABC123")
    check("the server is shut down once a result arrives", fake_server.shutdown_called is True)
    check("a second poll after completion is a no-op", (dlg._poll(), events["code"])[1] == "ABC123")

    dlg2 = BrowserSignInDialog("TestProvider")
    events2 = {"failed": None}
    dlg2.auth_failed.connect(lambda m: events2.update(failed=m))
    fake_server2 = FakeServer()
    dlg2.start(fake_server2)
    fake_server2.result = (None, "access_denied")
    dlg2._poll()
    check("a captured error fires auth_failed", events2["failed"] == "access_denied")

    dlg3 = BrowserSignInDialog("TestProvider")
    events3 = {"cancelled": False}
    dlg3.cancelled.connect(lambda: events3.update(cancelled=True))
    fake_server3 = FakeServer()
    dlg3.start(fake_server3)
    dlg3.reject()
    check("clicking Cancel fires cancelled and shuts down the server", events3["cancelled"] is True and fake_server3.shutdown_called is True)


def test_consumer_account_error_message():
    print("\n--- A personal-account sign-in renders a different error format (no AADSTS prefix) ---")
    from app.onedrive_client import friendly_auth_error_message

    # Captured verbatim from the real login.microsoftonline.com page when
    # signing in with a personal account against a bad client_id.
    real_error = (
        "unauthorized_client: The client does not exist or is not enabled for consumers. "
        "If you are the application developer, configure a new application through the "
        "App Registrations in the Azure Portal at https://go.microsoft.com/fwlink/?linkid=2083908."
    )
    friendly = friendly_auth_error_message(real_error)
    check(
        "it maps to the same tenant/client-ID guidance as AADSTS700016",
        "Application (client) ID" in friendly and "Directory (tenant) ID" in friendly,
    )


def test_friendly_auth_error_message_from_raw_text():
    print("\n--- Friendly error mapping works on raw scraped text, not just JSON responses ---")
    from app.dropbox_client import friendly_auth_error_message as dbx_friendly
    from app.onedrive_client import friendly_auth_error_message as od_friendly

    real_aadsts = (
        "AADSTS700016: Application with identifier '634b50e3-...' was not found in the "
        "directory '634b50e3-...'. This can happen if the application has not been "
        "installed by the administrator of the tenant or consented to by any user in the tenant."
    )
    msg = od_friendly(real_aadsts)
    check(
        "real AADSTS700016 text maps to tenant/client-ID mix-up guidance",
        "Directory (tenant) ID" in msg and "Application (client) ID" in msg,
    )

    msg2 = dbx_friendly('Invalid client_id: "bad-key".', "unknown_client_id")
    check("Dropbox's real invalid-client_id text maps to App key guidance", "App key" in msg2)


class _FakeSignal:
    """Minimal drop-in for a Qt Signal, used to fake BrowserSignInDialog
    without spinning up a real embedded browser (that needs a human).
    """

    def __init__(self):
        self._slot = None

    def connect(self, slot):
        self._slot = slot

    def emit(self, *a):
        if self._slot:
            self._slot(*a)


def test_connect_flow_wiring():
    print("\n--- Connect flow: browser opens, loopback captures code, exchange -> connected ---")
    import app.dropbox_panel as dp
    import app.onedrive_panel as odp
    from app.config import AppSettings

    class FakeServer:
        def __init__(self, *a, **k):
            self.result = None
            self.started = False

        def start(self):
            self.started = True

        def shutdown(self):
            pass

    class FakeDialog:
        def __init__(self, provider_label, parent=None):
            self.code_received = _FakeSignal()
            self.auth_failed = _FakeSignal()
            self.cancelled = _FakeSignal()
            self._server = None

        def start(self, server):
            self._server = server

        def exec(self):
            # Stands in for the user completing sign-in in their real browser.
            self.code_received.emit("fake-auth-code-123")

    class FakeDropboxClient:
        display_name = "Dropbox"

        def __init__(self, app_key, refresh_token):
            assert app_key == "test-app-key" and refresh_token == "fake-refresh-token"

    browser_opened = []
    orig_dialog, orig_server, orig_exchange, orig_client, orig_browser = (
        dp.BrowserSignInDialog, dp.LoopbackAuthServer, dp.exchange_code_for_token, dp.DropboxClient, dp.webbrowser.open,
    )
    dp.BrowserSignInDialog = FakeDialog
    dp.LoopbackAuthServer = FakeServer
    dp.exchange_code_for_token = lambda app_key, code, verifier: "fake-refresh-token"
    dp.DropboxClient = FakeDropboxClient
    dp.webbrowser.open = lambda url: browser_opened.append(url)
    try:
        panel = dp.DropboxPanel(AppSettings())
        panel.app_key_edit.setText("test-app-key")
        panel.on_connect_clicked()
        check("Dropbox: connect flow ends connected", panel.client is not None)
        check("Dropbox: status label shows a green checkmark", "✓" in panel.conn_status_label.text())
        check("Dropbox: the system browser was opened for sign-in", len(browser_opened) == 1)
    finally:
        dp.BrowserSignInDialog, dp.LoopbackAuthServer, dp.exchange_code_for_token, dp.DropboxClient, dp.webbrowser.open = (
            orig_dialog, orig_server, orig_exchange, orig_client, orig_browser,
        )

    class FakeOneDriveClient:
        display_name = "OneDrive"

        def __init__(self, access_token):
            assert access_token == "fake-access-token"

    browser_opened2 = []
    orig_od, orig_os, orig_oex, orig_oc, orig_obrowser = (
        odp.BrowserSignInDialog, odp.LoopbackAuthServer, odp.exchange_code_for_tokens, odp.OneDriveClient, odp.webbrowser.open,
    )
    odp.BrowserSignInDialog = FakeDialog
    odp.LoopbackAuthServer = FakeServer
    odp.exchange_code_for_tokens = lambda client_id, code, verifier: ("fake-access-token", "fake-refresh-token")
    odp.OneDriveClient = FakeOneDriveClient
    odp.webbrowser.open = lambda url: browser_opened2.append(url)
    try:
        panel2 = odp.OneDrivePanel(AppSettings())
        panel2.client_id_edit.setText("test-client-id")
        panel2.on_connect_clicked()
        check("OneDrive: connect flow ends connected", panel2.client is not None)
        check("OneDrive: status label shows a green checkmark", "✓" in panel2.conn_status_label.text())
        check("OneDrive: the system browser was opened for sign-in", len(browser_opened2) == 1)
    finally:
        odp.BrowserSignInDialog, odp.LoopbackAuthServer, odp.exchange_code_for_tokens, odp.OneDriveClient, odp.webbrowser.open = (
            orig_od, orig_os, orig_oex, orig_oc, orig_obrowser,
        )


def test_disconnect_clears_saved_token_and_forces_fresh_reconnect():
    print("\n--- Disconnect clears the saved token so Connect does a FRESH sign-in, not a silent token reuse ---")
    # Real bug: a saved token that predates an App Console permission change
    # still passes the basic identity check DropboxClient.__init__ does, so
    # clicking "Connect" just silently reused it and reported "connected" —
    # never actually picking up the new scope. There was also no way to clear
    # the saved token from the UI at all. Disconnect fixes both: it clears the
    # secret, and the next Connect click can no longer take the cached-token
    # shortcut, so it goes through the full browser consent flow again.
    import app.dropbox_panel as dp
    from app import config
    from app.config import AppSettings

    class FakeServer:
        def __init__(self, *a, **k):
            self.result = None

        def start(self):
            pass

        def shutdown(self):
            pass

    class FakeDialog:
        def __init__(self, provider_label, parent=None):
            self.code_received = _FakeSignal()
            self.auth_failed = _FakeSignal()
            self.cancelled = _FakeSignal()

        def start(self, server):
            pass

        def exec(self):
            self.code_received.emit("fake-auth-code-123")

    class FakeDropboxClient:
        display_name = "Dropbox"

        def __init__(self, app_key, refresh_token):
            pass

    browser_opened = []
    orig_dialog, orig_server, orig_exchange, orig_client, orig_browser = (
        dp.BrowserSignInDialog, dp.LoopbackAuthServer, dp.exchange_code_for_token, dp.DropboxClient, dp.webbrowser.open,
    )
    dp.BrowserSignInDialog = FakeDialog
    dp.LoopbackAuthServer = FakeServer
    dp.exchange_code_for_token = lambda app_key, code, verifier: "fake-refresh-token"
    dp.DropboxClient = FakeDropboxClient
    dp.webbrowser.open = lambda url: browser_opened.append(url)
    try:
        settings = AppSettings()
        panel = dp.DropboxPanel(settings)
        panel.app_key_edit.setText("test-app-key")

        panel.on_connect_clicked()
        check("connected after the first sign-in", panel.client is not None)
        check("browser opened once for the first sign-in", len(browser_opened) == 1)
        check("Disconnect button is enabled once connected", panel.disconnect_btn.isEnabled())
        check("a token was saved after connecting", config.get_secret(config.DROPBOX_TOKEN_KEY, panel.settings) is not None)

        panel.on_disconnect_clicked()
        check("client is cleared after Disconnect", panel.client is None)
        check("status label shows disconnected", "✗" in panel.conn_status_label.text())
        check("Disconnect button is disabled once disconnected", not panel.disconnect_btn.isEnabled())
        check("the saved token is cleared after Disconnect", config.get_secret(config.DROPBOX_TOKEN_KEY, panel.settings) is None)

        panel.on_connect_clicked()
        check(
            "Connect after Disconnect goes through the browser again, not a silent cached-token reuse",
            len(browser_opened) == 2,
        )
        check("connected again after the fresh sign-in", panel.client is not None)
    finally:
        dp.BrowserSignInDialog, dp.LoopbackAuthServer, dp.exchange_code_for_token, dp.DropboxClient, dp.webbrowser.open = (
            orig_dialog, orig_server, orig_exchange, orig_client, orig_browser,
        )


def test_setup_help_shows_exact_redirect_uri():
    print("\n--- 'Setup help' tells the user the exact redirect URI to register (real bug: Dropbox rejected an unregistered one) ---")
    import app.dropbox_panel as dp
    import app.onedrive_panel as odp
    from app.config import AppSettings
    from app.oauth_local_server import REDIRECT_URI
    from PySide6.QtWidgets import QMessageBox

    shown = []
    orig_information = QMessageBox.information
    QMessageBox.information = staticmethod(lambda *a, **k: shown.append(a) or QMessageBox.StandardButton.Ok)
    try:
        dbx_panel = dp.DropboxPanel(AppSettings())
        dbx_panel.on_setup_help_clicked()
        check("Dropbox setup help was shown", len(shown) == 1)
        check("Dropbox setup help includes the exact redirect URI to register", REDIRECT_URI in " ".join(str(a) for a in shown[0]))
        check("Dropbox setup help points at the App Console", "App Console" in " ".join(str(a) for a in shown[0]))

        shown.clear()
        od_panel = odp.OneDrivePanel(AppSettings())
        od_panel.on_setup_help_clicked()
        check("OneDrive setup help was shown", len(shown) == 1)
        check("OneDrive setup help includes the exact redirect URI to register", REDIRECT_URI in " ".join(str(a) for a in shown[0]))
        check("OneDrive setup help points at the Azure portal", "portal.azure.com" in " ".join(str(a) for a in shown[0]))
    finally:
        QMessageBox.information = orig_information


def test_hacker_log_widget():
    print("\n--- Hacker-terminal log widget: typewriter reveal + blinking cursor ---")
    from PySide6.QtTest import QTest
    from app.hacker_log import HackerLogWidget, _CURSOR_GLYPH
    from app.theme import ALERT_RED, TERMINAL_GREEN

    def pump(ms=50):
        QTest.qWait(ms)

    def wait_until_idle(widget, timeout_s=5):
        deadline = time.time() + timeout_s
        while (widget._typing or widget._queue) and time.time() < deadline:
            pump(20)
        pump(20)

    log = HackerLogWidget()
    log.append_line("first line", "green")
    check("text is NOT fully present immediately (typewriter is async)", "first line" not in log.toPlainText())

    wait_until_idle(log)
    check("text is fully present once typing finishes", "first line" in log.toPlainText())

    log.append_line("second line", "red")
    log.append_line("third line", "green")
    wait_until_idle(log)
    text = log.toPlainText()
    check("multiple queued lines all appear", "second line" in text and "third line" in text)
    check("queued lines appear in the order they were queued", text.index("second line") < text.index("third line"))

    def color_of(widget, needle: str):
        it = widget.document().begin()
        while it.isValid():
            frag_it = it.begin()
            while not frag_it.atEnd():
                frag = frag_it.fragment()
                if frag.isValid() and needle in frag.text():
                    return frag.charFormat().foreground().color().name()
                frag_it += 1
            it = it.next()
        return None

    check("'green' maps to the theme's terminal green", color_of(log, "first line") == TERMINAL_GREEN)
    check("'red' maps to the theme's alert red", color_of(log, "second line") == ALERT_RED)

    # Blinking cursor: only shown while idle, toggles on/off over time.
    # Poll rather than assert exact on/off states at fixed 500ms intervals —
    # QTest.qWait timing has enough jitter that asserting precise toggle
    # parity across several consecutive windows is flaky by construction.
    states_seen = set()
    toggles = 0
    last_state = _CURSOR_GLYPH in log.toPlainText()
    states_seen.add(last_state)
    deadline = time.time() + 3
    while time.time() < deadline and (len(states_seen) < 2 or toggles < 2):
        pump(80)
        state = _CURSOR_GLYPH in log.toPlainText()
        if state != last_state:
            toggles += 1
            last_state = state
        states_seen.add(state)
    check("the cursor glyph is shown at some point while idle", True in states_seen)
    check("the cursor glyph is hidden at some point while idle", False in states_seen)
    check("the cursor visibly toggles (blinks) at least twice within 3s", toggles >= 2)

    log.append_line("fourth line", "green")
    check("appending a new line removes the idle cursor glyph immediately", _CURSOR_GLYPH not in log.toPlainText())
    wait_until_idle(log)


def test_hacker_log_backlog_catchup():
    print("\n--- Hacker log: a large backlog is flushed instantly, not typewritten for hours ---")
    # Real-world bug: per-file upload logging on a large account (tens of
    # thousands of lines) queued far faster than the 8ms/char typewriter could
    # ever drain, so the log fell further behind with every new line — showing
    # stale, old-timestamped activity indefinitely and burying the completion
    # summary. Once the backlog crosses a threshold it must flush instantly.
    from PySide6.QtTest import QTest
    from app.hacker_log import HackerLogWidget, _MAX_BLOCK_COUNT

    def pump(ms=50):
        QTest.qWait(ms)

    log = HackerLogWidget()

    n = _MAX_BLOCK_COUNT + 200
    for i in range(n):
        log.append_line(f"file {i} uploaded", "green")
    log.append_line("SYNC COMPLETE SUMMARY", "green")

    deadline = time.time() + 3
    while (log._typing or log._queue) and time.time() < deadline:
        pump(20)

    check(f"a backlog of {n + 1} lines catches up within a few seconds, not hours", not log._typing and not log._queue)
    check("the final summary line is visible once caught up", "SYNC COMPLETE SUMMARY" in log.toPlainText())
    check("stale backlog beyond the display cap is dropped rather than replayed", "file 0 uploaded" not in log.toPlainText())


def test_conflict_detection_and_resolution():
    print("\n--- Conflicting files (exist but differ) are never silently overwritten ---")
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QMessageBox

    import app.dropbox_panel as dp
    import app.provider_panel as pp
    from app.cloud_base import CloudFileMeta
    from app.comparator import SyncStatus
    from app.config import AppSettings

    def pump(ms=50):
        QTest.qWait(ms)

    def wait_scan(panel):
        deadline = time.time() + 10
        while panel.scan_worker and panel.scan_worker.isRunning() and time.time() < deadline:
            pump(20)
        pump(80)

    orig_question = QMessageBox.question
    orig_warning = QMessageBox.warning
    orig_information = QMessageBox.information
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
    QMessageBox.warning = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok)
    QMessageBox.information = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok)

    class RecordingUploadWorker:
        instances = []

        def __init__(self, client, local_map, diffs, remote_root):
            self.diffs = list(diffs)
            self.log = _FakeSignal()
            self.log_colored = _FakeSignal()
            self.overall_progress = _FakeSignal()
            self.finished_ok = _FakeSignal()
            self.auth_lost = _FakeSignal()
            RecordingUploadWorker.instances.append(self)

        def start(self):
            pass

        def isRunning(self):
            return False

    try:
        tmp = Path(tempfile.mkdtemp())
        (tmp / "new_file.txt").write_text("brand new, doesn't exist remotely")
        (tmp / "changed_file.txt").write_text("this local version is different from the remote one")

        remote_meta = {"changed_file.txt": CloudFileMeta(size=1, client_modified=datetime(2020, 1, 1))}

        class FakeClient:
            display_name = "Dropbox"

            def list_folder_recursive(self, remote_root, log=None):
                return remote_meta

        def make_scanned_panel():
            panel = dp.DropboxPanel(AppSettings())
            panel.set_connected(FakeClient())
            panel.local_folder_edit.setText(str(tmp))
            panel.remote_folder_edit.setText("/W")
            panel.on_scan_clicked()
            wait_scan(panel)
            return panel

        panel = make_scanned_panel()
        statuses = {d.rel_path: d.status for d in panel.diffs}
        check("new_file.txt is detected as MISSING (not a conflict)", statuses["new_file.txt"] == SyncStatus.MISSING)
        check("changed_file.txt is detected as a CONFLICT (size differs)", statuses["changed_file.txt"] == SyncStatus.SIZE_MISMATCH)

        # --- Cancel: nothing gets uploaded ---
        RecordingUploadWorker.instances.clear()
        orig_worker_cls = pp.UploadWorker
        pp.UploadWorker = RecordingUploadWorker
        panel._prompt_conflict_resolution = lambda conflicts: "cancel"
        try:
            panel.on_sync_clicked()
            check("choosing Cancel starts no upload at all", len(RecordingUploadWorker.instances) == 0)
        finally:
            pp.UploadWorker = orig_worker_cls

        # --- Skip: only the non-conflicting file is uploaded ---
        panel2 = make_scanned_panel()
        RecordingUploadWorker.instances.clear()
        pp.UploadWorker = RecordingUploadWorker
        panel2._prompt_conflict_resolution = lambda conflicts: "skip"
        try:
            panel2.on_sync_clicked()
            check("choosing Skip starts exactly one upload batch", len(RecordingUploadWorker.instances) == 1)
            uploaded_paths = {d.rel_path for d in RecordingUploadWorker.instances[0].diffs}
            check("Skip uploads only the missing file, not the conflict", uploaded_paths == {"new_file.txt"})
        finally:
            pp.UploadWorker = orig_worker_cls

        # --- Overwrite: both files (including the conflict) are uploaded ---
        panel3 = make_scanned_panel()
        RecordingUploadWorker.instances.clear()
        pp.UploadWorker = RecordingUploadWorker
        panel3._prompt_conflict_resolution = lambda conflicts: "overwrite"
        try:
            panel3.on_sync_clicked()
            check("choosing Overwrite starts exactly one upload batch", len(RecordingUploadWorker.instances) == 1)
            uploaded_paths = {d.rel_path for d in RecordingUploadWorker.instances[0].diffs}
            check("Overwrite uploads both the missing file and the conflict", uploaded_paths == {"new_file.txt", "changed_file.txt"})
        finally:
            pp.UploadWorker = orig_worker_cls

        # --- No conflicts at all: sync proceeds without ever prompting ---
        panel4 = dp.DropboxPanel(AppSettings())

        class FakeClientNoConflict:
            display_name = "Dropbox"

            def list_folder_recursive(self, remote_root, log=None):
                return {}

        panel4.set_connected(FakeClientNoConflict())
        panel4.local_folder_edit.setText(str(tmp))
        panel4.remote_folder_edit.setText("/W")
        panel4.on_scan_clicked()
        wait_scan(panel4)

        prompt_calls = []
        panel4._prompt_conflict_resolution = lambda conflicts: prompt_calls.append(conflicts) or "cancel"
        RecordingUploadWorker.instances.clear()
        pp.UploadWorker = RecordingUploadWorker
        try:
            panel4.on_sync_clicked()
            check("no conflicts -> the resolution prompt is never shown", len(prompt_calls) == 0)
            check("no conflicts -> upload proceeds directly with everything selected", len(RecordingUploadWorker.instances) == 1)
        finally:
            pp.UploadWorker = orig_worker_cls
    finally:
        QMessageBox.question = orig_question
        QMessageBox.warning = orig_warning
        QMessageBox.information = orig_information


def test_conflict_prompt_button_mapping():
    print("\n--- Conflict prompt: each button maps to the right resolution ---")
    from PySide6.QtWidgets import QMessageBox

    import app.dropbox_panel as dp
    from app.comparator import FileDiff, SyncStatus
    from app.config import AppSettings

    panel = dp.DropboxPanel(AppSettings())
    conflicts = [FileDiff("a.txt", 10, 20, SyncStatus.SIZE_MISMATCH)]

    for role_to_click, expected in [
        (QMessageBox.ButtonRole.AcceptRole, "overwrite"),
        (QMessageBox.ButtonRole.DestructiveRole, "skip"),
        (QMessageBox.ButtonRole.RejectRole, "cancel"),
    ]:
        orig_exec = QMessageBox.exec

        def fake_exec(self, _role=role_to_click):
            for btn in self.buttons():
                if self.buttonRole(btn) == _role:
                    self.setProperty("_clicked", btn)
                    break
            return 0

        orig_clicked_button = QMessageBox.clickedButton
        QMessageBox.exec = fake_exec
        QMessageBox.clickedButton = lambda self: self.property("_clicked")
        try:
            result = panel._prompt_conflict_resolution(conflicts)
            check(f"clicking the {role_to_click.name} button resolves to '{expected}'", result == expected)
        finally:
            QMessageBox.exec = orig_exec
            QMessageBox.clickedButton = orig_clicked_button


def test_sync_history_record_and_summarize():
    print("\n--- Sync history: durable per-date sync log persists across 'restarts' ---")
    from app.sync_history import load_history, record_sync, summarize_by_date

    orig_dir = os.environ.get("SYNC_APP_CONFIG_DIR")
    scratch_dir = tempfile.mkdtemp(prefix="sync_history_test_")
    os.environ["SYNC_APP_CONFIG_DIR"] = scratch_dir
    try:
        check("a fresh install has no sync history", load_history() == [])

        record_sync(provider="Dropbox", succeeded=10, failed=0, cancelled=False, total_bytes=1_000_000, elapsed_seconds=10.0)
        record_sync(provider="OneDrive", succeeded=5, failed=1, cancelled=False, total_bytes=500_000, elapsed_seconds=5.0)

        records = load_history()
        check("both recorded syncs are persisted", len(records) == 2)
        check("records preserve the provider name", {r.provider for r in records} == {"Dropbox", "OneDrive"})

        # Simulate the app being closed and reopened: a fresh load_history()
        # call (no cached state) must read back exactly what was written.
        records_again = load_history()
        check("history survives a fresh load (simulated restart)", len(records_again) == 2)

        summary = summarize_by_date(records)
        check("summary has exactly one date bucket (both syncs happened today)", len(summary) == 1)
        date, count, succeeded, failed, total_bytes, elapsed = summary[0]
        check("date bucket counts both syncs", count == 2)
        check("date bucket sums succeeded across both syncs", succeeded == 15)
        check("date bucket sums failed across both syncs", failed == 1)
        check("date bucket sums bytes across both syncs", total_bytes == 1_500_000)
    finally:
        if orig_dir is not None:
            os.environ["SYNC_APP_CONFIG_DIR"] = orig_dir
        else:
            os.environ.pop("SYNC_APP_CONFIG_DIR", None)
        shutil.rmtree(scratch_dir, ignore_errors=True)


def test_sync_history_survives_malformed_file():
    print("\n--- Sync history: a corrupted history file degrades gracefully instead of crashing ---")
    from app.sync_history import _history_path, load_history

    orig_dir = os.environ.get("SYNC_APP_CONFIG_DIR")
    scratch_dir = tempfile.mkdtemp(prefix="sync_history_corrupt_test_")
    os.environ["SYNC_APP_CONFIG_DIR"] = scratch_dir
    try:
        _history_path().write_text("{ not valid json ][", encoding="utf-8")
        check("a corrupted history file loads as empty rather than raising", load_history() == [])
    finally:
        if orig_dir is not None:
            os.environ["SYNC_APP_CONFIG_DIR"] = orig_dir
        else:
            os.environ.pop("SYNC_APP_CONFIG_DIR", None)
        shutil.rmtree(scratch_dir, ignore_errors=True)


def test_upload_finished_records_sync_history():
    print("\n--- Completing a sync writes a durable history record (not just an in-memory log line) ---")
    import app.dropbox_panel as dp
    from app.config import AppSettings
    from app.sync_history import load_history
    from PySide6.QtWidgets import QMessageBox

    orig_exec = QMessageBox.exec
    QMessageBox.exec = lambda self: 0
    orig_warning = QMessageBox.warning
    QMessageBox.warning = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok)
    try:
        panel = dp.DropboxPanel(AppSettings())
        panel._last_upload_bytes_done = 12345.0
        panel._last_upload_elapsed = 2.5

        before = len(load_history())
        panel.on_upload_finished(7, 1, ["x.txt: failed"], cancelled=False)
        after = load_history()

        check("exactly one new history record was written", len(after) == before + 1)
        newest = after[-1]
        check("the record captures succeeded/failed counts", newest.succeeded == 7 and newest.failed == 1)
        check("the record captures the provider", newest.provider == "Dropbox")
        check("the record captures the bytes transferred from the last progress update", newest.total_bytes == 12345)
        check("the record captures elapsed time from the last progress update", newest.elapsed_seconds == 2.5)
    finally:
        QMessageBox.exec = orig_exec
        QMessageBox.warning = orig_warning


def test_sync_history_dialog_populates():
    print("\n--- Sync History dialog renders the by-date summary and the individual runs ---")
    from app.sync_history_dialog import SyncHistoryDialog

    orig_dir = os.environ.get("SYNC_APP_CONFIG_DIR")
    scratch_dir = tempfile.mkdtemp(prefix="sync_history_dialog_test_")
    os.environ["SYNC_APP_CONFIG_DIR"] = scratch_dir
    try:
        from app.sync_history import record_sync

        record_sync(provider="Dropbox", succeeded=3, failed=0, cancelled=False, total_bytes=300, elapsed_seconds=3.0)
        record_sync(provider="OneDrive", succeeded=2, failed=1, cancelled=True, total_bytes=200, elapsed_seconds=1.0)

        dialog = SyncHistoryDialog()
        check("by-date table has one row (both runs today)", dialog.by_date_table.rowCount() == 1)
        check("by-date row reports 2 syncs", dialog.by_date_table.item(0, 1).text() == "2")
        check("runs table lists both individual sync runs", dialog.runs_table.rowCount() == 2)
        providers_shown = {dialog.runs_table.item(r, 1).text() for r in range(dialog.runs_table.rowCount())}
        check("runs table shows both providers", providers_shown == {"Dropbox", "OneDrive"})
        cancelled_values = {dialog.runs_table.item(r, 4).text() for r in range(dialog.runs_table.rowCount())}
        check("runs table shows Yes/No for cancelled runs", cancelled_values == {"Yes", "No"})
    finally:
        if orig_dir is not None:
            os.environ["SYNC_APP_CONFIG_DIR"] = orig_dir
        else:
            os.environ.pop("SYNC_APP_CONFIG_DIR", None)
        shutil.rmtree(scratch_dir, ignore_errors=True)


def test_uploads_actually_run_concurrently():
    print("\n--- Uploads run several files at once instead of one at a time (the 'faster uploading' feature) ---")
    import threading

    from PySide6.QtTest import QTest
    from app.cloud_base import UploadResult
    from app.comparator import FileDiff, SyncStatus
    from app.scanner import LocalFileMeta
    from app.workers import UPLOAD_CONCURRENCY, UploadWorker

    tmp = Path(tempfile.mkdtemp())
    local_map = {}
    diffs = []
    for i in range(UPLOAD_CONCURRENCY):
        p = tmp / f"f{i}.bin"
        p.write_bytes(b"x")
        local_map[f"f{i}.bin"] = LocalFileMeta(abs_path=p, size=1, mtime_utc=datetime(2024, 1, 1))
        diffs.append(FileDiff(f"f{i}.bin", 1, 0, SyncStatus.MISSING))

    class FakeClient:
        display_name = "Dropbox"

        def __init__(self):
            self.lock = threading.Lock()
            self.active = 0
            self.max_active_seen = 0

        @staticmethod
        def join_remote_path(remote_root, rel_path):
            return f"/{rel_path}"

        def upload_file(self, local_path, remote_path, client_modified, progress_cb=None):
            with self.lock:
                self.active += 1
                self.max_active_seen = max(self.max_active_seen, self.active)
            time.sleep(0.1)  # simulate network latency long enough for overlap to be observable
            with self.lock:
                self.active -= 1
            return UploadResult(remote_id="id", remote_size=1)

    fake_client = FakeClient()
    worker = UploadWorker(fake_client, local_map, diffs, "/W")
    finished = []
    worker.finished_ok.connect(lambda *a: finished.append(a))

    t0 = time.time()
    worker.run()
    QTest.qWait(100)
    elapsed = time.time() - t0

    check(
        f"multiple files ({fake_client.max_active_seen} at once) upload concurrently, not strictly one at a time",
        fake_client.max_active_seen >= 2,
    )
    check(f"all {UPLOAD_CONCURRENCY} files succeeded", finished and finished[0][0] == UPLOAD_CONCURRENCY)
    check(
        f"wall-clock time ({elapsed:.2f}s) reflects overlap, not {UPLOAD_CONCURRENCY} x 0.1s run back-to-back",
        elapsed < UPLOAD_CONCURRENCY * 0.1,
    )


def test_upload_progress_stats():
    print("\n--- Upload progress reports accurate byte totals, speed, and ETA ---")
    from PySide6.QtTest import QTest
    from app.cloud_base import CloudSyncError
    from app.comparator import FileDiff, SyncStatus
    from app.scanner import LocalFileMeta
    from app.workers import UploadWorker

    tmp = Path(tempfile.mkdtemp())
    file_a = tmp / "a.bin"
    file_b = tmp / "b.bin"
    file_a.write_bytes(b"x" * 1000)
    file_b.write_bytes(b"y" * 2000)

    local_map = {
        "a.bin": LocalFileMeta(abs_path=file_a, size=1000, mtime_utc=datetime(2024, 1, 1)),
        "b.bin": LocalFileMeta(abs_path=file_b, size=2000, mtime_utc=datetime(2024, 1, 1)),
    }
    diffs = [
        FileDiff("a.bin", 1000, 0, SyncStatus.MISSING),
        FileDiff("b.bin", 2000, 0, SyncStatus.MISSING),
    ]

    from app.cloud_base import UploadResult

    class FakeClient:
        display_name = "Dropbox"

        @staticmethod
        def join_remote_path(remote_root, rel_path):
            return f"/{rel_path}"

        def upload_file(self, local_path, remote_path, client_modified, progress_cb=None):
            size = local_path.stat().st_size
            if progress_cb:
                progress_cb(size // 2, size)
                progress_cb(size, size)
            return UploadResult(remote_id="fake-id", remote_size=size)

    worker = UploadWorker(FakeClient(), local_map, diffs, "/W")
    events = []
    confirmations = []
    worker.overall_progress.connect(lambda *args: events.append(args))
    worker.log_colored.connect(lambda msg, color: confirmations.append((msg, color)))
    worker.run()  # blocks until the concurrent upload pool drains
    # Uploads run on a real thread pool now, so these signal emissions cross
    # threads relative to this test's connect() call and land as queued
    # connections — pump the event loop once to actually deliver them.
    QTest.qWait(100)

    check("both uploads emitted a green confirmation line", len(confirmations) == 2 and all(c[1] == "green" for c in confirmations))

    check("progress events were emitted", len(events) > 0)
    bytes_totals = {e[3] for e in events}
    check("bytes_total is constant across all events and equals the sum of file sizes", bytes_totals == {3000})

    final = events[-1]
    check("final event reports all files done", final[0] == 2 and final[1] == 2)
    check("final event reports all bytes done", final[2] == 3000)
    check("elapsed time is non-negative and increases monotonically", all(events[i][4] <= events[i + 1][4] for i in range(len(events) - 1)))

    bytes_done_sequence = [e[2] for e in events]
    check("bytes_done never exceeds bytes_total", all(b <= 3000 for b in bytes_done_sequence))
    check("bytes_done is monotonically non-decreasing", all(bytes_done_sequence[i] <= bytes_done_sequence[i + 1] for i in range(len(bytes_done_sequence) - 1)))


def test_upload_progress_handles_multi_gigabyte_totals():
    print("\n--- Upload progress: multi-GB byte totals don't overflow the Qt progress signal ---")
    # Real production crash: overall_progress was declared Signal(int, int, int, int,
    # float). PySide marshals a Signal "int" through a 32-bit C int (max ~2.147 GB),
    # so any account whose total upload size exceeds that raised OverflowError on
    # every single progress_cb call — which, on a large account, is constantly true.
    from PySide6.QtTest import QTest
    from app.cloud_base import UploadResult
    from app.comparator import FileDiff, SyncStatus
    from app.scanner import LocalFileMeta
    from app.workers import UploadWorker

    tmp = Path(tempfile.mkdtemp())
    f = tmp / "huge.bin"
    f.write_bytes(b"x")  # content is irrelevant; the accounted size is faked below

    huge_size = 5 * 1024 ** 3  # 5 GB, comfortably past the 32-bit int ceiling
    local_map = {"huge.bin": LocalFileMeta(abs_path=f, size=huge_size, mtime_utc=datetime(2024, 1, 1))}
    diffs = [FileDiff("huge.bin", huge_size, 0, SyncStatus.MISSING)]

    class FakeClient:
        display_name = "OneDrive"

        @staticmethod
        def join_remote_path(remote_root, rel_path):
            return f"/{rel_path}"

        def upload_file(self, local_path, remote_path, client_modified, progress_cb=None):
            if progress_cb:
                progress_cb(huge_size // 2, huge_size)
                progress_cb(huge_size, huge_size)
            return UploadResult(remote_id="id", remote_size=huge_size)

    worker = UploadWorker(FakeClient(), local_map, diffs, "/W")
    events = []
    worker.overall_progress.connect(lambda *args: events.append(args))
    try:
        worker.run()
        QTest.qWait(100)  # deliver the queued cross-thread signal emissions
        check("emitting multi-GB byte counts does not raise OverflowError", True)
    except OverflowError:
        check("emitting multi-GB byte counts does not raise OverflowError", False)

    check("the final event reports the full multi-GB bytes_total correctly", events[-1][3] == huge_size)
    check("the final event reports the full multi-GB bytes_done correctly", events[-1][2] == huge_size)


def test_upload_cancel_mid_transfer():
    print("\n--- Cancel stops an upload mid-transfer, not only after the current file finishes ---")
    from PySide6.QtTest import QTest
    import app.workers as workers_module
    from app.cloud_base import UploadResult
    from app.comparator import FileDiff, SyncStatus
    from app.scanner import LocalFileMeta
    from app.workers import UploadWorker

    # Uploads now run concurrently (see UPLOAD_CONCURRENCY); pin it to 1 here
    # so this test's exact "stops after exactly N chunks, file B never even
    # starts" assertions stay deterministic rather than racing against a real
    # thread pool. The single worker thread processes tasks strictly in
    # submission order, and file A's cancellation is set synchronously on that
    # same thread before it can move on to file B's task.
    orig_concurrency = workers_module.UPLOAD_CONCURRENCY
    workers_module.UPLOAD_CONCURRENCY = 1

    tmp = Path(tempfile.mkdtemp())
    file_a = tmp / "a.bin"
    file_b = tmp / "b.bin"
    file_a.write_bytes(b"x" * 1000)
    file_b.write_bytes(b"y" * 1000)

    local_map = {
        "a.bin": LocalFileMeta(abs_path=file_a, size=1000, mtime_utc=datetime(2024, 1, 1)),
        "b.bin": LocalFileMeta(abs_path=file_b, size=1000, mtime_utc=datetime(2024, 1, 1)),
    }
    diffs = [
        FileDiff("a.bin", 1000, 0, SyncStatus.MISSING),
        FileDiff("b.bin", 1000, 0, SyncStatus.MISSING),
    ]
    worker_holder = {}

    class FakeClient:
        display_name = "Dropbox"

        def __init__(self):
            self.chunk_calls = 0

        @staticmethod
        def join_remote_path(remote_root, rel_path):
            return f"/{rel_path}"

        def upload_file(self, local_path, remote_path, client_modified, progress_cb=None):
            # Simulate a large chunked transfer with several progress callbacks;
            # Cancel is clicked (by the "user") partway through the first file.
            for done in (250, 500, 750, 1000):
                self.chunk_calls += 1
                if self.chunk_calls == 2:
                    worker_holder["worker"].cancel()
                if progress_cb:
                    progress_cb(done, 1000)
            return UploadResult(remote_id="id", remote_size=1000)

    fake_client = FakeClient()
    worker = UploadWorker(fake_client, local_map, diffs, "/W")
    worker_holder["worker"] = worker
    finished = []
    worker.finished_ok.connect(lambda *a: finished.append(a))
    colored = []
    worker.log_colored.connect(lambda msg, color: colored.append((msg, color)))
    try:
        worker.run()  # blocks until the (single-worker) pool drains — deterministic
        QTest.qWait(100)  # deliver the queued cross-thread signal emissions
    finally:
        workers_module.UPLOAD_CONCURRENCY = orig_concurrency

    check("the transfer stops right after the cancel point, not after finishing the file's remaining chunks", fake_client.chunk_calls == 2)
    check("the second file is never attempted once cancelled", finished[0][0] == 0)
    check("finished_ok reports cancelled=True", finished[0][3] is True)
    check("a red 'cancelled' log line is emitted", any("cancelled" in m.lower() and c == "red" for m, c in colored))


def test_upload_finished_confirmation_report():
    print("\n--- Upload finished: confirmation report (dialog + colored log line) ---")
    import app.dropbox_panel as dp
    from app.config import AppSettings
    from PySide6.QtWidgets import QMessageBox

    orig_exec = QMessageBox.exec
    orig_warning = QMessageBox.warning
    boxes = []
    QMessageBox.exec = lambda self: boxes.append(self) or 0
    QMessageBox.warning = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok)
    try:
        panel = dp.DropboxPanel(AppSettings())
        colored = []
        panel.log_message_colored.connect(lambda msg, color: colored.append((msg, color)))

        panel.on_upload_finished(5, 0, [])
        check("all-success: dialog reports 0 failed", "0 file(s) failed" in boxes[-1].text())
        check("all-success: dialog has no detailed failure text", boxes[-1].detailedText() == "")
        check("all-success: dialog icon is Information, not a warning", boxes[-1].icon() == QMessageBox.Icon.Information)
        check("all-success: colored summary line is green and mentions confirmation", colored[-1][1] == "green" and "confirmed" in colored[-1][0].lower())

        boxes.clear()
        colored.clear()
        panel.on_upload_finished(3, 2, ["a.txt: size mismatch", "b.txt: network error"])
        check("partial failure: dialog reports the failure count", "2 file(s) failed" in boxes[-1].text())
        check("partial failure: dialog lists every failed file in the detail text", "a.txt" in boxes[-1].detailedText() and "b.txt" in boxes[-1].detailedText())
        check("partial failure: colored summary line is red and mentions failure", colored[-1][1] == "red" and "failed" in colored[-1][0].lower())
        check("partial failure: dialog icon escalates to Warning", boxes[-1].icon() == QMessageBox.Icon.Warning)

        boxes.clear()
        colored.clear()
        panel.on_upload_finished(2, 0, [], cancelled=True)
        check("cancelled: dialog title reflects that it was stopped, not completed", boxes[-1].windowTitle() == "Upload stopped")
        check("cancelled: dialog text says remaining files were not uploaded", "not uploaded" in boxes[-1].text().lower())
        check("cancelled: dialog icon is a Warning even though nothing failed", boxes[-1].icon() == QMessageBox.Icon.Warning)
        check("cancelled: colored summary line is red and says stopped by user", colored[-1][1] == "red" and "stopped by user" in colored[-1][0].lower())
    finally:
        QMessageBox.exec = orig_exec
        QMessageBox.warning = orig_warning


def test_colored_scan_summary():
    print("\n--- Scan summary: green tick for in-sync files, red for missing ---")
    from PySide6.QtTest import QTest

    import app.dropbox_panel as dp
    from app.cloud_base import CloudFileMeta
    from app.config import AppSettings

    def pump(ms=50):
        QTest.qWait(ms)

    tmp = Path(tempfile.mkdtemp())
    (tmp / "already_here.txt").write_text("same on both sides")
    (tmp / "brand_new.txt").write_text("needs upload")

    synced_path = tmp / "already_here.txt"
    st = synced_path.stat()
    synced_mtime = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).replace(microsecond=0, tzinfo=None)

    class FakeClient:
        display_name = "Dropbox"

        def list_folder_recursive(self, remote_root, log=None):
            return {"already_here.txt": CloudFileMeta(size=st.st_size, client_modified=synced_mtime)}

    panel = dp.DropboxPanel(AppSettings())
    panel.set_connected(FakeClient())
    panel.local_folder_edit.setText(str(tmp))
    panel.remote_folder_edit.setText("/W")

    events = []
    panel.log_message_colored.connect(lambda msg, color: events.append((msg, color)))

    panel.on_scan_clicked()
    deadline = time.time() + 10
    while panel.scan_worker and panel.scan_worker.isRunning() and time.time() < deadline:
        pump(20)
    pump(100)

    check("exactly one green and one red summary line emitted", len(events) == 2)
    green_events = [e for e in events if e[1] == "green"]
    red_events = [e for e in events if e[1] == "red"]
    check("green line reports the 1 in-sync file", len(green_events) == 1 and "1" in green_events[0][0])
    check("red line reports the 1 missing file", len(red_events) == 1 and "1" in red_events[0][0])
    check("green line mentions being in sync", "sync" in green_events[0][0].lower())
    check("red line mentions missing/uploading", "missing" in red_events[0][0].lower() or "upload" in red_events[0][0].lower())


def test_scan_summary_reconciles_destination_extras():
    print("\n--- Scan summary: destination files that don't match any local path are called out, not silently dropped ---")
    from PySide6.QtTest import QTest

    import app.dropbox_panel as dp
    from app.cloud_base import CloudFileMeta
    from app.config import AppSettings

    def pump(ms=50):
        QTest.qWait(ms)

    tmp = Path(tempfile.mkdtemp())
    for i in range(10):
        (tmp / f"local_{i}.txt").write_text(f"local file {i}")

    # Remote has 8 files but only ONE shares a path with anything local — this
    # mirrors a real report where Local: 79549 / OneDrive: 68589 but "Missing"
    # was 73309, which is inexplicable unless most of the remote files simply
    # don't correspond to any local rel_path (e.g. a folder-name mismatch).
    remote_meta = {"local_0.txt": CloudFileMeta(size=13, client_modified=datetime(2020, 1, 1))}
    for i in range(7):
        remote_meta[f"remote_only_{i}.txt"] = CloudFileMeta(size=1, client_modified=datetime(2020, 1, 1))

    class FakeClient:
        display_name = "OneDrive"

        def list_folder_recursive(self, remote_root, log=None):
            return remote_meta

    panel = dp.DropboxPanel(AppSettings())
    panel.provider_label = "OneDrive"
    panel.set_connected(FakeClient())
    panel.local_folder_edit.setText(str(tmp))
    panel.remote_folder_edit.setText("/W")

    plain_events = []
    colored_events = []
    panel.log_message.connect(lambda msg: plain_events.append(msg))
    panel.log_message_colored.connect(lambda msg, color: colored_events.append((msg, color)))

    panel.on_scan_clicked()
    deadline = time.time() + 10
    while panel.scan_worker and panel.scan_worker.isRunning() and time.time() < deadline:
        pump(20)
    pump(100)

    check(
        "destination summary reports total, matched, and extra counts",
        panel.destination_summary_label.text() == "Total: 8  |  Matched to local: 1  |  Extra on destination: 7",
    )
    check(
        "a plain log line calls out the 7 unmatched destination files",
        any("7 file(s)" in m and "don't match any local" in m for m in plain_events),
    )
    check(
        "a red warning fires when the match rate between the two sides is very low",
        any(color == "red" and ("mismatched" in msg.lower() or "same content" in msg.lower()) for msg, color in colored_events),
    )


def test_scan_resets_stale_progress_bar():
    print("\n--- A fresh scan resets the progress bar instead of leaving the last completed upload's numbers ---")
    from PySide6.QtTest import QTest

    import app.dropbox_panel as dp
    from app.cloud_base import CloudFileMeta
    from app.config import AppSettings

    def pump(ms=50):
        QTest.qWait(ms)

    tmp = Path(tempfile.mkdtemp())
    (tmp / "new_file.txt").write_text("needs upload")

    class FakeClient:
        display_name = "Dropbox"

        def list_folder_recursive(self, remote_root, log=None):
            return {}

    panel = dp.DropboxPanel(AppSettings())
    panel.set_connected(FakeClient())
    panel.local_folder_edit.setText(str(tmp))
    panel.remote_folder_edit.setText("/W")

    # Simulate a stale progress bar left over from a previous, unrelated
    # completed upload run (e.g. "5558/5558 files") — the real bug reported.
    panel.progress_bar.setMaximum(5558)
    panel.progress_bar.setValue(5558)
    panel.progress_detail_label.setText("5558/5558 files  |  500.1 MB / 500.1 MB")

    panel.on_scan_clicked()
    deadline = time.time() + 10
    while panel.scan_worker and panel.scan_worker.isRunning() and time.time() < deadline:
        pump(20)
    pump(100)

    check("progress bar value resets to 0 on a fresh scan", panel.progress_bar.value() == 0)
    check("progress bar maximum reflects the new scan's pending-upload count, not the old run", panel.progress_bar.maximum() == 1)
    check("the stale byte totals are gone from the detail label", "500.1 MB" not in panel.progress_detail_label.text())
    check("the detail label reflects the new scan's ready-to-sync count", "1 file(s) ready to sync" in panel.progress_detail_label.text())


def test_main_window_colored_log_rendering():
    print("\n--- MainWindow renders colored hacker-terminal log lines correctly ---")
    from PySide6.QtTest import QTest
    from app.main_window import MainWindow
    from app.theme import ALERT_RED, TERMINAL_GREEN

    def pump(ms=50):
        QTest.qWait(ms)

    window = MainWindow()
    try:
        window.log("plain line")
        window.log_colored("green summary line", "green")
        window.log_colored("red summary line", "red")

        # Lines are revealed by a typewriter effect (one char per timer tick),
        # not inserted synchronously — wait for the queue to fully drain.
        deadline = time.time() + 5
        while (window.log_view._typing or window.log_view._queue) and time.time() < deadline:
            pump(20)
        pump(50)

        text = window.log_view.toPlainText()
        check("all three lines appear in the log", "plain line" in text and "green summary line" in text and "red summary line" in text)
        check("each line got a [HH:MM:SS] timestamp prefix", text.count("[") >= 3 and text.count("]") >= 3)

        def color_of(needle: str):
            it = window.log_view.document().begin()
            while it.isValid():
                frag_it = it.begin()
                while not frag_it.atEnd():
                    frag = frag_it.fragment()
                    if frag.isValid() and needle in frag.text():
                        return frag.charFormat().foreground().color().name()
                    frag_it += 1
                it = it.next()
            return None

        check("the green line is colored with the theme's terminal green", color_of("green summary line") == TERMINAL_GREEN)
        check("the red line is colored with the theme's alert red", color_of("red summary line") == ALERT_RED)
    finally:
        window.dropbox_panel.shutdown()
        window.onedrive_panel.shutdown()


def test_expand_tree_popup_restores_original_position():
    print("\n--- Expand-tree popup returns each tree to its original spot when closed ---")
    import app.dropbox_panel as dp
    import app.provider_panel as pp
    from app.config import AppSettings

    panel = dp.DropboxPanel(AppSettings())

    for label, tree in (("Local files tree", panel.local_tree), ("Destination files tree", panel.destination_tree), ("Upload status tree", panel.tree)):
        owner_group = tree.parentWidget()
        owner_layout = owner_group.layout()
        original_index = owner_layout.indexOf(tree)
        check(f"{label}: starts inside its dashboard group box", original_index != -1)

        dialog = pp._ExpandedTreeDialog(f"{label} — expanded", tree, owner_layout, original_index)
        check(f"{label}: reparented into the popup while open", tree.parentWidget() is not owner_group)

        dialog.done(0)
        check(f"{label}: restored to its original group box on close", tree.parentWidget() is owner_group)
        check(f"{label}: restored at the same layout position", owner_layout.indexOf(tree) == original_index)


def test_hacker_log_flush_now():
    print("\n--- Hacker log: manual Refresh renders pending content immediately ---")
    from app.hacker_log import HackerLogWidget

    log = HackerLogWidget()
    log.append_line("first", "green")
    log.append_line("second", "red")
    log.append_line("third", "green")
    # Deliberately no QTest.qWait — the typewriter timer for "first" hasn't ticked yet.
    check("before Refresh, later queued lines are not yet visible", "second" not in log.toPlainText())

    log.flush_now()

    check(
        "Refresh renders every queued/in-progress line immediately",
        all(t in log.toPlainText() for t in ("first", "second", "third")),
    )
    check("Refresh leaves nothing pending", not log._typing and not log._queue)


def test_main_window_log_export_and_refresh():
    print("\n--- MainWindow: log Export and Refresh controls ---")
    from PySide6.QtWidgets import QFileDialog
    from app.main_window import MainWindow

    window = MainWindow()
    try:
        window.log_view.append_line("line to export", "green")
        window.on_refresh_log_clicked()
        check("Refresh flushes the log view with no pending backlog", not window.log_view._typing and not window.log_view._queue)
        check("the flushed line is visible after Refresh", "line to export" in window.log_view.toPlainText())

        tmp_dir = Path(tempfile.mkdtemp())
        export_path = str(tmp_dir / "exported.txt")
        orig_get_save = QFileDialog.getSaveFileName
        QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: (export_path, "Text files (*.txt)"))
        try:
            window.on_export_log_clicked()
        finally:
            QFileDialog.getSaveFileName = orig_get_save

        check("Export writes the log content to the chosen file", Path(export_path).exists())
        exported_text = Path(export_path).read_text(encoding="utf-8")
        check("exported file contains the current log content", "line to export" in exported_text)

        QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: ("", ""))
        try:
            window.on_export_log_clicked()
            check("cancelling the export dialog (empty path) is a safe no-op", True)
        except Exception:
            check("cancelling the export dialog (empty path) is a safe no-op", False)
        finally:
            QFileDialog.getSaveFileName = orig_get_save
    finally:
        window.dropbox_panel.shutdown()
        window.onedrive_panel.shutdown()


def test_connection_loss_handling():
    print("\n--- Connection loss mid-scan and mid-upload surfaces a clear alert ---")
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QMessageBox

    import app.dropbox_panel as dp
    from app.cloud_base import CloudAuthError
    from app.config import AppSettings

    critical_calls = []
    orig_critical = QMessageBox.critical
    orig_question = QMessageBox.question
    orig_warning = QMessageBox.warning
    orig_information = QMessageBox.information
    QMessageBox.critical = staticmethod(lambda *a, **k: (critical_calls.append(a[1:]), QMessageBox.Ok)[1])
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)
    QMessageBox.warning = staticmethod(lambda *a, **k: QMessageBox.Ok)
    QMessageBox.information = staticmethod(lambda *a, **k: QMessageBox.Ok)

    def pump(ms=50):
        QTest.qWait(ms)

    def wait_for(worker_attr_getter, timeout_s=10):
        deadline = time.time() + timeout_s
        while True:
            w = worker_attr_getter()
            if not (w and w.isRunning()):
                break
            if time.time() > deadline:
                break
            pump(20)
        pump(100)

    try:
        tmp = Path(tempfile.mkdtemp())
        (tmp / "a.txt").write_text("hello")
        (tmp / "b.txt").write_text("world")

        class DyingClientOnScan:
            display_name = "Dropbox"

            def list_folder_recursive(self, remote_root, log=None):
                raise CloudAuthError("simulated expired token")

        panel = dp.DropboxPanel(AppSettings())
        panel.set_connected(DyingClientOnScan())
        panel.local_folder_edit.setText(str(tmp))
        panel.remote_folder_edit.setText("/W")
        critical_calls.clear()
        panel.on_scan_clicked()
        wait_for(lambda: panel.scan_worker)

        check("scan: auth failure marks the panel disconnected", panel.client is None)
        check("scan: status label shows a red X", "✗" in panel.conn_status_label.text())
        check("scan: exactly one critical alert shown", len(critical_calls) == 1)
        check(
            "scan: alert text mentions the connection loss",
            critical_calls and "connection lost" in " ".join(str(a) for a in critical_calls[0]).lower(),
        )

        class DyingClientOnUpload:
            display_name = "Dropbox"

            def __init__(self):
                self.upload_attempts = 0

            def list_folder_recursive(self, remote_root, log=None):
                return {}

            def upload_file(self, local_path, remote_path, client_modified, progress_cb=None):
                self.upload_attempts += 1
                raise CloudAuthError("simulated session expired mid-upload")

            @staticmethod
            def normalize_root(remote_root):
                return "" if not remote_root or remote_root == "/" else remote_root.strip("/")

            @classmethod
            def join_remote_path(cls, remote_root, rel_path):
                root = cls.normalize_root(remote_root)
                return f"/{root}/{rel_path}" if root else f"/{rel_path}"

        dying = DyingClientOnUpload()
        panel2 = dp.DropboxPanel(AppSettings())
        panel2.set_connected(dying)
        panel2.local_folder_edit.setText(str(tmp))
        panel2.remote_folder_edit.setText("/W")
        panel2.on_scan_clicked()
        wait_for(lambda: panel2.scan_worker)
        check("upload-loss setup: scan found both missing files", panel2.tree.file_count() == 2)

        critical_calls.clear()
        panel2.on_sync_clicked()
        wait_for(lambda: panel2.upload_worker)

        # With concurrent uploads, a handful of in-flight files (bounded by
        # UPLOAD_CONCURRENCY) can all hit the same auth failure before the
        # batch stops — it's no longer guaranteed to be exactly one attempt,
        # but it's still bounded and nowhere near a per-file retry storm.
        check("upload: auth failure stops the batch quickly, not a per-file retry storm", 1 <= dying.upload_attempts <= 2)
        check("upload: panel marked disconnected after connection loss", panel2.client is None)
        check("upload: exactly one critical alert (not a misleading 'sync complete')", len(critical_calls) == 1)
        check("upload: cancel button disabled after connection loss", not panel2.cancel_btn.isEnabled())
    finally:
        QMessageBox.critical = orig_critical
        QMessageBox.question = orig_question
        QMessageBox.warning = orig_warning
        QMessageBox.information = orig_information


def _cleanup_test_isolation() -> None:
    shutil.rmtree(_TEST_CONFIG_DIR, ignore_errors=True)
    try:
        from app import config

        for key in (config.DROPBOX_TOKEN_KEY, config.ONEDRIVE_TOKEN_KEY, "test_secret_key"):
            config.clear_secret(key, config.AppSettings())
    except Exception:
        pass


def main():
    try:
        test_config_isolated_from_real_user_data()  # run first: fail loudly before anything else can touch real data
        test_tree_checkbox_propagation()
        test_scanner_and_comparator_at_scale()
        test_file_tree_correctness()
        test_file_type_label()
        test_file_tree_columns_show_type_modified_and_match_status()
        test_tree_file_activated_signal()
        test_missing_files_tree_type_column_and_activation()
        test_show_file_details_popup()
        test_dual_tree_performance_at_realistic_scale()
        test_dropbox_chunked_upload()
        test_dropbox_upload_confirmation()
        test_onedrive_chunked_upload()
        test_onedrive_upload_confirmation()
        test_remote_listing_pagination()
        test_onedrive_transient_error_retry()
        test_onedrive_url_construction()
        test_list_child_folders()
        test_dropbox_list_child_folders_pagination_errors_are_caught()
        test_onedrive_list_child_folders_unexpected_error_wrapped()
        test_dropbox_missing_scope_gives_actionable_guidance()
        test_dropbox_missing_scope_via_autherror_not_treated_as_session_expired()
        test_folder_picker_lazy_loading()
        test_folder_picker_shows_error_for_unexpected_exception()
        test_config_and_secret_roundtrip()
        test_pkce_authorize_urls()
        test_friendly_auth_error_messages()
        test_loopback_auth_server()
        test_browser_sign_in_dialog()
        test_consumer_account_error_message()
        test_friendly_auth_error_message_from_raw_text()
        test_connect_flow_wiring()
        test_disconnect_clears_saved_token_and_forces_fresh_reconnect()
        test_setup_help_shows_exact_redirect_uri()
        test_hacker_log_widget()
        test_hacker_log_backlog_catchup()
        test_conflict_detection_and_resolution()
        test_conflict_prompt_button_mapping()
        test_uploads_actually_run_concurrently()
        test_upload_progress_stats()
        test_upload_progress_handles_multi_gigabyte_totals()
        test_upload_cancel_mid_transfer()
        test_upload_finished_confirmation_report()
        test_colored_scan_summary()
        test_scan_summary_reconciles_destination_extras()
        test_scan_resets_stale_progress_bar()
        test_sync_history_record_and_summarize()
        test_sync_history_survives_malformed_file()
        test_upload_finished_records_sync_history()
        test_sync_history_dialog_populates()
        test_main_window_colored_log_rendering()
        test_expand_tree_popup_restores_original_position()
        test_hacker_log_flush_now()
        test_main_window_log_export_and_refresh()
        test_connection_loss_handling()
    finally:
        _cleanup_test_isolation()

    print("\n=== SUMMARY ===")
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S):")
        for f in FAILURES:
            print(" -", f)
        sys.exit(1)
    print("ALL CHECKS PASSED")


if __name__ == "__main__":
    from PySide6.QtWidgets import QApplication

    _app = QApplication(sys.argv)  # QTreeWidget/QWidget/QDialog need a QApplication to exist
    main()
