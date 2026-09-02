"""Compares a local file map against a remote (Dropbox) file map."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from enum import Enum
from typing import Dict, List

from .cloud_base import CloudFileMeta
from .scanner import LocalFileMeta

MTIME_TOLERANCE = timedelta(seconds=2)


class SyncStatus(str, Enum):
    MISSING = "Missing on destination"
    SIZE_MISMATCH = "Size differs"
    OUTDATED = "Destination is older"
    IN_SYNC = "In sync"


NEEDS_UPLOAD = {SyncStatus.MISSING, SyncStatus.SIZE_MISMATCH, SyncStatus.OUTDATED}

# A file that exists on both sides but differs is a conflict — uploading it
# overwrites whatever is on the destination, which is a materially different
# (and riskier) action than uploading a file that's simply absent there. The
# UI must not treat these the same way without asking first.
CONFLICT_STATUSES = {SyncStatus.SIZE_MISMATCH, SyncStatus.OUTDATED}


@dataclass
class FileDiff:
    rel_path: str
    local_size: int
    remote_size: int
    status: SyncStatus


def compare(
    local_map: Dict[str, LocalFileMeta], remote_map: Dict[str, CloudFileMeta]
) -> List[FileDiff]:
    diffs: List[FileDiff] = []
    for rel_path, local in local_map.items():
        remote = remote_map.get(rel_path)
        if remote is None:
            diffs.append(FileDiff(rel_path, local.size, 0, SyncStatus.MISSING))
            continue
        if remote.size != local.size:
            diffs.append(FileDiff(rel_path, local.size, remote.size, SyncStatus.SIZE_MISMATCH))
            continue
        if local.mtime_utc - remote.client_modified > MTIME_TOLERANCE:
            diffs.append(FileDiff(rel_path, local.size, remote.size, SyncStatus.OUTDATED))
            continue
        diffs.append(FileDiff(rel_path, local.size, remote.size, SyncStatus.IN_SYNC))

    diffs.sort(key=lambda d: (d.status not in NEEDS_UPLOAD, d.rel_path.lower()))
    return diffs
