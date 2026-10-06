"""Common interface both cloud providers (Dropbox, OneDrive) implement, so
the scanner/comparator/upload workers and UI panels don't care which one
they're talking to.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional


@dataclass
class CloudFileMeta:
    size: int
    client_modified: datetime  # naive UTC, second precision


@dataclass
class CloudFolderEntry:
    name: str
    path: str  # full path from the account root, e.g. "/BKP_MASTER_Support"


@dataclass
class UploadResult:
    """Server-reported outcome of an upload — lets the caller confirm the
    file that actually landed on the destination matches what was sent,
    instead of trusting a 2xx status code alone."""
    remote_id: Optional[str]
    remote_size: int


class CloudAuthError(RuntimeError):
    pass


class CloudSyncError(RuntimeError):
    pass


class CloudClient(ABC):
    display_name: str = "Cloud"

    @staticmethod
    def normalize_root(remote_root: str) -> str:
        root = (remote_root or "").strip().replace("\\", "/")
        if not root or root == "/":
            return ""
        if not root.startswith("/"):
            root = "/" + root
        return root.rstrip("/")

    @classmethod
    def join_remote_path(cls, remote_root: str, rel_path: str) -> str:
        root = cls.normalize_root(remote_root)
        rel = rel_path.replace("\\", "/").lstrip("/")
        return f"{root}/{rel}" if root else f"/{rel}"

    @abstractmethod
    def list_folder_recursive(
        self, remote_root: str, log: Optional[Callable[[str], None]] = None
    ) -> Dict[str, CloudFileMeta]:
        """Returns {path relative to remote_root: CloudFileMeta} for every file found."""

    @abstractmethod
    def list_child_folders(self, remote_path: str) -> List[CloudFolderEntry]:
        """Returns the immediate subfolders of remote_path (non-recursive), for
        browsing the account to pick a destination folder without listing
        every file in it — essential on accounts with tens of thousands of
        files, where a full recursive listing would be far too slow/heavy
        just to let the user navigate.
        """

    @abstractmethod
    def upload_file(
        self,
        local_path: Path,
        remote_path: str,
        client_modified: datetime,
        progress_cb: Optional[Callable[[int, int], None]] = None,
    ) -> UploadResult:
        """Uploads local_path to remote_path, creating any missing parent folders.
        Returns the server-confirmed UploadResult so the caller can verify the
        upload actually landed correctly."""

    @abstractmethod
    def download_bytes(self, remote_path: str) -> Optional[bytes]:
        """Returns the file's content, or None if nothing exists at remote_path.
        Used for small control files (the connected-clients registry), not bulk data."""

    def upload_bytes(self, data: bytes, remote_path: str) -> None:
        """Uploads a small in-memory blob by staging it as a temp file and
        reusing upload_file, so both providers get verified-size semantics
        without a second upload code path."""
        import os
        import tempfile

        fd, tmp = tempfile.mkstemp(suffix=".json")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            self.upload_file(Path(tmp), remote_path, datetime.utcnow())
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
