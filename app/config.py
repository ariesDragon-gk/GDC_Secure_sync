"""Persistent local settings + secure secret storage for both providers.

Non-secret settings (client/app ids, last-used folders) live in a small
JSON file under the user's app-data directory. Provider tokens are kept
in the OS credential store via `keyring` when available, falling back to
the JSON file (with a clear warning in the README) if `keyring` can't load.
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, Optional

try:
    import keyring
    _KEYRING_AVAILABLE = True
except Exception:
    _KEYRING_AVAILABLE = False

DROPBOX_TOKEN_KEY = "dropbox_refresh_token"
ONEDRIVE_TOKEN_KEY = "onedrive_refresh_token"

# Both overridable via env var so the test suite can point at an isolated
# location instead of the user's real config file / real OS credential
# store. Read at call time (not baked into a module-level constant) so
# setting the env var before first use is all that's required.


def _service_name() -> str:
    return os.environ.get("SYNC_APP_KEYRING_SERVICE", "DropboxOneDriveSyncApp")


def _config_dir() -> Path:
    override = os.environ.get("SYNC_APP_CONFIG_DIR")
    if override:
        d = Path(override)
        d.mkdir(parents=True, exist_ok=True)
        return d
    base = os.environ.get("APPDATA") or str(Path.home())
    d = Path(base) / "DropboxOneDriveSyncApp"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _config_path() -> Path:
    return _config_dir() / "config.json"


@dataclass
class AppSettings:
    dropbox_app_key: str = ""
    dropbox_local_folder: str = ""
    dropbox_remote_folder: str = "/"

    onedrive_client_id: str = ""
    onedrive_local_folder: str = ""
    onedrive_remote_folder: str = "/"

    # Stable per-machine identity used by the connected-clients registry.
    # Generated on first launch and never shared between machines.
    machine_client_id: str = ""

    secrets_fallback: Dict[str, str] = field(default_factory=dict)


def load_settings() -> AppSettings:
    path = _config_path()
    settings = AppSettings()
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            settings = AppSettings(**{**asdict(AppSettings()), **data})
        except Exception:
            settings = AppSettings()
    if not settings.machine_client_id:
        settings.machine_client_id = uuid.uuid4().hex
        try:
            save_settings(settings)
        except OSError:
            pass  # still usable this run; a new id is just generated next launch
    return settings


def save_settings(settings: AppSettings) -> None:
    path = _config_path()
    path.write_text(json.dumps(asdict(settings), indent=2), encoding="utf-8")


def get_secret(name: str, settings: AppSettings) -> Optional[str]:
    if _KEYRING_AVAILABLE:
        try:
            value = keyring.get_password(_service_name(), name)
            if value:
                return value
        except Exception:
            pass
    return settings.secrets_fallback.get(name) or None


def set_secret(name: str, value: str, settings: AppSettings) -> None:
    if _KEYRING_AVAILABLE:
        try:
            keyring.set_password(_service_name(), name, value)
            settings.secrets_fallback.pop(name, None)
            return
        except Exception:
            pass
    settings.secrets_fallback[name] = value


def clear_secret(name: str, settings: AppSettings) -> None:
    if _KEYRING_AVAILABLE:
        try:
            keyring.delete_password(_service_name(), name)
        except Exception:
            pass
    settings.secrets_fallback.pop(name, None)
