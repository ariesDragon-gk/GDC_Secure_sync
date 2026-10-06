"""Shared registry of which machines sync against one app id, kept inside the
cloud account itself.

Machines syncing from different parts of the world share no server of their
own — the only thing they all see is the Dropbox/OneDrive account — so the
registry is a small JSON file there (``/.sync_registry/<provider>_<app id>.json``).
It records each machine's details and sync times, and caps the number of
distinct machines per app id at MAX_CLIENTS (a machine that is already
registered is never blocked; only a *new* machine past the cap is).

Writes are read-modify-write on a plain file, so two brand-new machines
registering in the same instant could both pass the cap check. ``check_in``
re-reads after writing and retries, which makes that very unlikely but not
impossible; the cap is a guardrail for a backup tool, not a security boundary.
"""
from __future__ import annotations

import json
import os
import platform
import re
import socket
import uuid
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

import requests

from .cloud_base import CloudClient

MAX_CLIENTS = 5
MAX_EVENTS = 200
REGISTRY_DIR = "/.sync_registry"
_VERIFY_ATTEMPTS = 3


class ClientLimitError(RuntimeError):
    """This machine is not registered and the app id already has MAX_CLIENTS."""


def new_client_id() -> str:
    return uuid.uuid4().hex


def registry_path(provider: str, app_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", app_id)
    return f"{REGISTRY_DIR}/{provider.lower()}_{safe}.json"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def lookup_public_location(timeout: float = 4.0) -> Dict[str, str]:
    """Best-effort public IP + rough location of this machine. Contacts
    ipapi.co; returns {} on any failure so a sync is never blocked by it."""
    try:
        resp = requests.get("https://ipapi.co/json/", timeout=timeout)
        if not resp.ok:
            return {}
        data = resp.json()
        return {
            "public_ip": str(data.get("ip") or ""),
            "city": str(data.get("city") or ""),
            "region": str(data.get("region") or ""),
            "country": str(data.get("country_name") or ""),
            "timezone": str(data.get("timezone") or ""),
        }
    except (requests.RequestException, ValueError):
        return {}


def local_machine_info(client_id: str, with_location: bool = True) -> Dict[str, str]:
    try:
        user = os.getlogin()
    except OSError:
        user = os.environ.get("USERNAME") or os.environ.get("USER") or ""
    info = {
        "client_id": client_id,
        "hostname": socket.gethostname(),
        "os": platform.platform(),
        "user": user,
        "public_ip": "",
        "city": "",
        "region": "",
        "country": "",
        "timezone": "",
    }
    if with_location:
        info.update(lookup_public_location())
    return info


# --------------------------------------------------------------- pure logic


def empty_registry(provider: str, app_id: str, max_clients: int = MAX_CLIENTS) -> dict:
    return {"provider": provider, "app_id": app_id, "max_clients": max_clients, "clients": {}, "events": []}


def parse_registry(raw: Optional[bytes], provider: str, app_id: str) -> dict:
    if not raw:
        return empty_registry(provider, app_id)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return empty_registry(provider, app_id)
    if not isinstance(data, dict) or not isinstance(data.get("clients"), dict):
        return empty_registry(provider, app_id)
    data.setdefault("events", [])
    data.setdefault("max_clients", MAX_CLIENTS)
    return data


def can_register(registry: dict, client_id: str, max_clients: int = MAX_CLIENTS) -> bool:
    return client_id in registry["clients"] or len(registry["clients"]) < max_clients


def apply_check_in(registry: dict, machine: Dict[str, str], now: str, max_clients: int = MAX_CLIENTS) -> dict:
    """Registers/refreshes this machine. Raises ClientLimitError for a new
    machine when the cap is already reached."""
    cid = machine["client_id"]
    if not can_register(registry, cid, max_clients):
        raise ClientLimitError(
            f"This app id already has {len(registry['clients'])} of {max_clients} allowed clients "
            "registered. Remove an unused machine from the Clients dialog, or use a different app id."
        )
    entry = registry["clients"].setdefault(cid, {"first_seen": now, "last_sync": "", "sync_count": 0})
    entry.update({k: v for k, v in machine.items() if k != "client_id"})
    entry["last_seen"] = now
    return registry


def apply_sync_event(registry: dict, machine: Dict[str, str], now: str, provider: str,
                     succeeded: int, failed: int, cancelled: bool, total_bytes: int) -> dict:
    cid = machine["client_id"]
    entry = registry["clients"].get(cid)
    if entry is not None:
        entry["last_sync"] = now
        entry["last_seen"] = now
        entry["sync_count"] = int(entry.get("sync_count", 0)) + 1
    registry["events"].append({
        "time": now,
        "client_id": cid,
        "hostname": machine.get("hostname", ""),
        "provider": provider,
        "public_ip": machine.get("public_ip", ""),
        "succeeded": succeeded,
        "failed": failed,
        "cancelled": cancelled,
        "total_bytes": total_bytes,
    })
    registry["events"] = registry["events"][-MAX_EVENTS:]
    return registry


# ------------------------------------------------------------ cloud-backed


class ClientRegistry:
    """Reads/writes the registry through a CloudClient for one (provider, app id)."""

    def __init__(self, client: CloudClient, provider: str, app_id: str, client_id: str,
                 max_clients: int = MAX_CLIENTS, with_location: bool = True):
        self._client = client
        self.provider = provider
        self.app_id = app_id
        self.client_id = client_id
        self.max_clients = max_clients
        self._with_location = with_location
        self._machine: Optional[Dict[str, str]] = None
        self._path = registry_path(provider, app_id)

    @property
    def machine(self) -> Dict[str, str]:
        if self._machine is None:  # looked up once per run — it makes a network call
            self._machine = local_machine_info(self.client_id, self._with_location)
        return self._machine

    def load(self) -> dict:
        return parse_registry(self._client.download_bytes(self._path), self.provider, self.app_id)

    def _save(self, registry: dict) -> None:
        registry["max_clients"] = self.max_clients
        self._client.upload_bytes(json.dumps(registry, indent=2).encode("utf-8"), self._path)

    def check_in(self) -> dict:
        """Registers this machine (raising ClientLimitError if over the cap)
        and verifies the registration survived a concurrent writer."""
        for _ in range(_VERIFY_ATTEMPTS):
            registry = apply_check_in(self.load(), self.machine, utc_now_iso(), self.max_clients)
            self._save(registry)
            confirmed = self.load()
            if self.client_id in confirmed["clients"]:
                if len(confirmed["clients"]) > self.max_clients:
                    # Lost a race with another new machine and both got in. The
                    # later arrival (by first_seen, then id) backs out.
                    order = sorted(confirmed["clients"].items(), key=lambda kv: (kv[1].get("first_seen", ""), kv[0]))
                    allowed = {cid for cid, _ in order[: self.max_clients]}
                    if self.client_id not in allowed:
                        confirmed["clients"].pop(self.client_id, None)
                        self._save(confirmed)
                        raise ClientLimitError(
                            f"Another machine registered at the same moment and took the last of "
                            f"{self.max_clients} client slots."
                        )
                return confirmed
        raise ClientLimitError("Could not confirm this machine's registration; please retry.")

    def record_sync(self, succeeded: int, failed: int, cancelled: bool, total_bytes: int) -> None:
        registry = apply_check_in(self.load(), self.machine, utc_now_iso(), self.max_clients)
        apply_sync_event(registry, self.machine, utc_now_iso(), self.provider,
                         succeeded, failed, cancelled, total_bytes)
        self._save(registry)

    def remove_client(self, client_id: str) -> None:
        registry = self.load()
        registry["clients"].pop(client_id, None)
        self._save(registry)


def describe_location(entry: Dict[str, str]) -> str:
    parts = [entry.get("city", ""), entry.get("region", ""), entry.get("country", "")]
    return ", ".join(p for p in parts if p) or "unknown"


def sorted_clients(registry: dict) -> List[tuple]:
    return sorted(registry["clients"].items(), key=lambda kv: kv[1].get("last_sync", ""), reverse=True)
