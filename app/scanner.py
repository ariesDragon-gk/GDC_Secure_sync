"""Recursively scans a local folder into {relative_posix_path: LocalFileMeta}."""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Optional

_SKIP_FILENAMES = {"thumbs.db", "desktop.ini", ".ds_store"}


@dataclass
class LocalFileMeta:
    abs_path: Path
    size: int
    mtime_utc: datetime  # naive UTC, second precision


def scan_local(
    root: Path, log: Optional[Callable[[str], None]] = None
) -> Dict[str, LocalFileMeta]:
    files: Dict[str, LocalFileMeta] = {}
    if not root.exists():
        if log:
            log(f"Local folder '{root}' does not exist.")
        return files

    for dirpath, dirnames, filenames in os.walk(root):
        for name in filenames:
            if name.lower() in _SKIP_FILENAMES:
                continue
            abs_path = Path(dirpath) / name
            try:
                stat = abs_path.stat()
            except OSError as exc:
                if log:
                    log(f"Skipping unreadable file '{abs_path}': {exc}")
                continue
            rel_path = abs_path.relative_to(root).as_posix()
            mtime_utc = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).replace(
                microsecond=0, tzinfo=None
            )
            files[rel_path] = LocalFileMeta(abs_path=abs_path, size=stat.st_size, mtime_utc=mtime_utc)

    return files
