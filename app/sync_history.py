"""Durable record of every completed sync run — date, provider, file counts,
timing, and speed — persisted to disk so "how many syncs happened, and how
fast" survives app restarts. The live log view is capped at a couple thousand
lines and starts empty on every launch; this is the permanent record.
"""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Tuple

from .config import _config_dir

HISTORY_FILENAME = "sync_history.json"


@dataclass
class SyncRecord:
    timestamp: str  # ISO 8601, local time, second precision
    provider: str
    succeeded: int
    failed: int
    cancelled: bool
    total_bytes: int
    elapsed_seconds: float

    @property
    def date(self) -> str:
        return self.timestamp.split("T")[0]

    @property
    def avg_speed_bytes_per_sec(self) -> float:
        return self.total_bytes / self.elapsed_seconds if self.elapsed_seconds > 0 else 0.0


def _history_path() -> Path:
    return _config_dir() / HISTORY_FILENAME


def record_sync(
    provider: str,
    succeeded: int,
    failed: int,
    cancelled: bool,
    total_bytes: int,
    elapsed_seconds: float,
) -> SyncRecord:
    record = SyncRecord(
        timestamp=datetime.now().isoformat(timespec="seconds"),
        provider=provider,
        succeeded=succeeded,
        failed=failed,
        cancelled=cancelled,
        total_bytes=total_bytes,
        elapsed_seconds=elapsed_seconds,
    )
    records = load_history()
    records.append(record)
    path = _history_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump([asdict(r) for r in records], f, indent=2)
    return record


def load_history() -> List[SyncRecord]:
    path = _history_path()
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    records = []
    for entry in raw:
        try:
            records.append(SyncRecord(**entry))
        except TypeError:
            continue  # skip a malformed/older-schema entry rather than fail the whole load
    return records


def summarize_by_date(records: List[SyncRecord]) -> List[Tuple[str, int, int, int, int, float]]:
    """Returns (date, sync_count, total_succeeded, total_failed, total_bytes,
    total_elapsed_seconds) rows, newest date first."""
    buckets: dict = defaultdict(lambda: [0, 0, 0, 0, 0.0])
    for r in records:
        b = buckets[r.date]
        b[0] += 1
        b[1] += r.succeeded
        b[2] += r.failed
        b[3] += r.total_bytes
        b[4] += r.elapsed_seconds
    return sorted(
        ((date, vals[0], vals[1], vals[2], vals[3], vals[4]) for date, vals in buckets.items()),
        key=lambda row: row[0],
        reverse=True,
    )
