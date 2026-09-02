"""OneDrive (Microsoft Graph) client: auth via an embedded-browser
authorization-code + PKCE flow (no app secret, no MSAL needed), delta-based
recursive listing, and resumable chunked upload for large files.
"""
from __future__ import annotations

import base64
import hashlib
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import quote, urlencode

import requests

from .cloud_base import (
    CloudAuthError,
    CloudClient,
    CloudFileMeta,
    CloudFolderEntry,
    CloudSyncError,
    UploadResult,
)
from .oauth_local_server import REDIRECT_URI

GRAPH_ROOT = "https://graph.microsoft.com/v1.0"
AUTHORIZE_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
SCOPES = "Files.ReadWrite User.Read offline_access"
UPLOAD_SESSION_THRESHOLD = 4 * 1024 * 1024  # Graph's simple-PUT ceiling
CHUNK_SIZE = 320 * 1024 * 10  # ~3.2 MB, a multiple of Graph's required 320 KiB


class OneDriveAuthError(CloudAuthError):
    pass


class OneDriveSyncError(CloudSyncError):
    pass


def _make_pkce_pair() -> Tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def build_authorize_url(client_id: str) -> Tuple[str, str]:
    """Returns (authorize_url, code_verifier) for an embedded-browser login."""
    verifier, challenge = _make_pkce_pair()
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "response_mode": "query",
        "scope": SCOPES,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}", verifier


def exchange_code_for_tokens(client_id: str, code: str, code_verifier: str) -> Tuple[str, str]:
    """Returns (access_token, refresh_token)."""
    try:
        resp = requests.post(
            TOKEN_URL,
            data={
                "client_id": client_id,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT_URI,
                "code_verifier": code_verifier,
                "scope": SCOPES,
            },
            timeout=30,
        )
    except requests.RequestException as exc:
        raise OneDriveAuthError(f"Network error completing OneDrive authorization: {exc}") from exc

    if not resp.ok:
        raise OneDriveAuthError(_friendly_azure_error(resp))

    data = resp.json()
    return data["access_token"], data["refresh_token"]


def refresh_access_token(client_id: str, refresh_token: str) -> Tuple[str, str]:
    """Uses a saved refresh token to get a fresh access token without user interaction.
    Returns (access_token, new_refresh_token).
    """
    try:
        resp = requests.post(
            TOKEN_URL,
            data={
                "client_id": client_id,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "scope": SCOPES,
            },
            timeout=30,
        )
    except requests.RequestException as exc:
        raise OneDriveAuthError(f"Network error refreshing OneDrive session: {exc}") from exc

    if not resp.ok:
        raise OneDriveAuthError("OneDrive session expired; please reconnect.")

    data = resp.json()
    return data["access_token"], data.get("refresh_token", refresh_token)


def friendly_auth_error_message(desc: str) -> str:
    """Turns a raw Azure error description into actionable guidance. Used both
    for token-endpoint JSON errors and for AADSTS text scraped directly out of
    the embedded sign-in page (Azure renders those client-side via JS, so they
    never reach a redirect or a JSON response at all).
    """
    if "AADSTS50059" in desc:
        return (
            "OneDrive authorization failed: Azure couldn't identify your app's tenant "
            "(AADSTS50059). This almost always means either (1) you pasted the Directory "
            "(tenant) ID instead of the Application (client) ID from the Azure app's "
            "Overview page, or (2) the app's 'Supported account types' isn't set to "
            "'Accounts in any organizational directory and personal Microsoft accounts'. "
            f"Check both in the Azure Portal and try again.\n\nDetails: {desc}"
        )
    if "AADSTS700016" in desc:
        return (
            "OneDrive authorization failed: this Client ID doesn't match any registered "
            "app (AADSTS700016). This is usually the Directory (tenant) ID pasted by mistake — "
            "double-check you copied the 'Application (client) ID' field, not the 'Directory "
            f"(tenant) ID' field, from the Azure app's Overview page.\n\nDetails: {desc}"
        )
    if "AADSTS9002326" in desc or "redirect_uri" in desc.lower() or "AADSTS50011" in desc:
        return (
            "OneDrive authorization failed: the redirect URI isn't registered for this app. "
            "In the Azure Portal, under Authentication, add a 'Mobile and desktop applications' "
            f"platform with redirect URI exactly: {REDIRECT_URI}\n\nDetails: {desc}"
        )
    if "not enabled for consumers" in desc.lower() or desc.strip().startswith("unauthorized_client"):
        # Signing in with a personal Microsoft account routes through the consumer
        # identity system, which renders this instead of an AADSTS code — same two
        # root causes as AADSTS700016/50059, just worded differently.
        return (
            "OneDrive authorization failed: this app isn't set up for personal Microsoft "
            "accounts (or the Client ID is wrong). In the Azure Portal, check (1) you copied "
            "the 'Application (client) ID', not the 'Directory (tenant) ID', from the app's "
            "Overview page, and (2) 'Supported account types' is set to 'Accounts in any "
            f"organizational directory and personal Microsoft accounts'.\n\nDetails: {desc}"
        )
    return f"OneDrive authorization failed: {desc}"


def _friendly_azure_error(resp: requests.Response) -> str:
    try:
        data = resp.json()
    except ValueError:
        return f"OneDrive authorization failed: {resp.status_code} {resp.text}"
    desc = data.get("error_description", data.get("error", "Unknown error"))
    return friendly_auth_error_message(desc)


def _parse_graph_datetime(value: Optional[str]) -> datetime:
    if not value:
        return datetime(1970, 1, 1)
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.replace(microsecond=0)


def _quote_path(remote_path: str) -> str:
    return quote(remote_path, safe="/")


def _item_url(remote_path: str, suffix: str) -> str:
    return f"{GRAPH_ROOT}/me/drive/root:{_quote_path(remote_path)}:{suffix}"


_MAX_RETRIES = 4


def _get_with_retry(session, url, params=None, timeout=60, log: Optional[Callable[[str], None]] = None):
    """GET with backoff on transient failures (429 throttling, 5xx server
    errors like Graph's "generalException"). These are usually momentary —
    on a 79k-file account a single flaky response shouldn't fail the whole
    scan and force the user to start over."""
    delay = 2.0
    resp = None
    for attempt in range(_MAX_RETRIES + 1):
        resp = session.get(url, params=params, timeout=timeout)
        if resp.status_code != 429 and not (500 <= resp.status_code < 600):
            return resp
        if attempt == _MAX_RETRIES:
            return resp
        retry_after = resp.headers.get("Retry-After") if hasattr(resp, "headers") else None
        wait = float(retry_after) if retry_after and str(retry_after).isdigit() else delay
        if log:
            log(f"OneDrive returned {resp.status_code}; retrying in {wait:.0f}s ({attempt + 1}/{_MAX_RETRIES})...")
        time.sleep(wait)
        delay *= 2
    return resp


class OneDriveClient(CloudClient):
    display_name = "OneDrive"

    def __init__(self, access_token: str):
        self._session = requests.Session()
        self._session.headers["Authorization"] = f"Bearer {access_token}"
        resp = self._session.get(f"{GRAPH_ROOT}/me")
        if resp.status_code == 401:
            raise OneDriveAuthError("OneDrive access token is invalid or expired.")
        if not resp.ok:
            raise OneDriveAuthError(f"Could not verify OneDrive account: {resp.status_code} {resp.text}")

    def list_folder_recursive(
        self, remote_root: str, log: Optional[Callable[[str], None]] = None
    ) -> Dict[str, CloudFileMeta]:
        root = self.normalize_root(remote_root)
        files: Dict[str, CloudFileMeta] = {}

        url = _item_url(root, "/delta") if root else f"{GRAPH_ROOT}/me/drive/root/delta"
        params = {"$select": "id,name,size,file,folder,deleted,parentReference,fileSystemInfo,lastModifiedDateTime"}
        first = True

        try:
            while url:
                resp = _get_with_retry(self._session, url, params=params if first else None, timeout=60, log=log)
                first = False
                if resp.status_code == 404:
                    if log:
                        log(f"Remote folder '{remote_root}' does not exist yet (treating as empty).")
                    return files
                if resp.status_code == 401:
                    raise OneDriveAuthError("OneDrive session expired; please reconnect.")
                if not resp.ok:
                    raise OneDriveSyncError(f"Failed to list OneDrive folder: {resp.status_code} {resp.text}")

                data = resp.json()
                for item in data.get("value", []):
                    if "file" not in item or "deleted" in item:
                        continue
                    rel = self._relative_path(item, root)
                    if rel is None:
                        continue
                    fsi = item.get("fileSystemInfo", {})
                    modified_raw = fsi.get("lastModifiedDateTime") or item.get("lastModifiedDateTime")
                    files[rel] = CloudFileMeta(
                        size=item.get("size", 0), client_modified=_parse_graph_datetime(modified_raw)
                    )

                url = data.get("@odata.nextLink")
        except requests.RequestException as exc:
            raise OneDriveSyncError(f"Network error listing OneDrive folder: {exc}") from exc

        return files

    def list_child_folders(self, remote_path: str) -> List[CloudFolderEntry]:
        root = self.normalize_root(remote_path)
        url = _item_url(root, "/children") if root else f"{GRAPH_ROOT}/me/drive/root/children"
        params = {"$select": "name,folder,parentReference"}
        first = True
        entries: List[CloudFolderEntry] = []

        try:
            while url:
                resp = _get_with_retry(self._session, url, params=params if first else None, timeout=60)
                first = False
                if resp.status_code == 404:
                    return []
                if resp.status_code == 401:
                    raise OneDriveAuthError("OneDrive session expired; please reconnect.")
                if not resp.ok:
                    raise OneDriveSyncError(f"Failed to list folder: {resp.status_code} {resp.text}")

                data = resp.json()
                for item in data.get("value", []):
                    if "folder" not in item:
                        continue
                    child_path = f"{root.rstrip('/')}/{item['name']}" if root else f"/{item['name']}"
                    entries.append(CloudFolderEntry(name=item["name"], path=child_path))

                url = data.get("@odata.nextLink")
        except requests.RequestException as exc:
            raise OneDriveSyncError(f"Network error listing OneDrive folder: {exc}") from exc
        except (OneDriveAuthError, OneDriveSyncError):
            raise
        except Exception as exc:
            # Last resort so an unanticipated response shape/parsing error
            # still surfaces as a clear message instead of leaving the
            # folder picker silently stuck.
            raise OneDriveSyncError(f"Unexpected error listing folder: {exc}") from exc

        return sorted(entries, key=lambda f: f.name.lower())

    @staticmethod
    def _relative_path(item: dict, root: str) -> Optional[str]:
        parent_path = item.get("parentReference", {}).get("path", "")
        prefix = "/drive/root:"
        if parent_path.startswith(prefix):
            parent_path = parent_path[len(prefix):]
        full_path = f"{parent_path.rstrip('/')}/{item['name']}".strip("/")
        root_norm = root.strip("/")
        if root_norm:
            if full_path == root_norm or not full_path.startswith(root_norm + "/"):
                return None
            return full_path[len(root_norm) + 1:]
        return full_path

    def upload_file(
        self,
        local_path: Path,
        remote_path: str,
        client_modified: datetime,
        progress_cb: Optional[Callable[[int, int], None]] = None,
    ) -> UploadResult:
        file_size = local_path.stat().st_size
        item = None
        try:
            if file_size <= UPLOAD_SESSION_THRESHOLD:
                with open(local_path, "rb") as f:
                    data = f.read()
                resp = self._session.put(_item_url(remote_path, "/content"), data=data, timeout=120)
                if resp.status_code == 401:
                    raise OneDriveAuthError("OneDrive session expired; please reconnect.")
                if not resp.ok:
                    raise OneDriveSyncError(
                        f"Upload failed for '{local_path.name}': {resp.status_code} {resp.text}"
                    )
                item = resp.json()
                if progress_cb:
                    progress_cb(file_size, file_size)
            else:
                resp = self._session.post(
                    _item_url(remote_path, "/createUploadSession"),
                    json={"item": {"@microsoft.graph.conflictBehavior": "replace"}},
                    timeout=60,
                )
                if resp.status_code == 401:
                    raise OneDriveAuthError("OneDrive session expired; please reconnect.")
                if not resp.ok:
                    raise OneDriveSyncError(
                        f"Could not start upload session for '{local_path.name}': {resp.status_code} {resp.text}"
                    )
                upload_url = resp.json()["uploadUrl"]

                with open(local_path, "rb") as f:
                    start = 0
                    while start < file_size:
                        chunk = f.read(CHUNK_SIZE)
                        end = start + len(chunk) - 1
                        headers = {
                            "Content-Length": str(len(chunk)),
                            "Content-Range": f"bytes {start}-{end}/{file_size}",
                        }
                        # Upload-session URLs are pre-authenticated; no bearer header needed/used.
                        r = requests.put(upload_url, headers=headers, data=chunk, timeout=120)
                        if r.status_code not in (200, 201, 202):
                            raise OneDriveSyncError(
                                f"Upload chunk failed for '{local_path.name}': {r.status_code} {r.text}"
                            )
                        start += len(chunk)
                        if progress_cb:
                            progress_cb(min(start, file_size), file_size)
                        if r.status_code in (200, 201):
                            item = r.json()

            if item and item.get("id"):
                self._set_file_system_info(item["id"], client_modified)

            if item is None:
                # The upload session's final chunk didn't hand back item metadata
                # (can happen depending on how Graph batches the last range) —
                # fetch the item directly so we still confirm what actually
                # landed rather than trusting the chunk's 2xx status alone.
                item = self._get_item(remote_path)

            if item is None:
                raise OneDriveSyncError(
                    f"Upload for '{local_path.name}' returned no confirmation from OneDrive."
                )

            remote_size = item.get("size", 0)
            if remote_size != file_size:
                raise OneDriveSyncError(
                    f"Upload for '{local_path.name}' did not verify: OneDrive reports "
                    f"{remote_size} bytes, expected {file_size}."
                )
            return UploadResult(remote_id=item.get("id"), remote_size=remote_size)
        except requests.RequestException as exc:
            raise OneDriveSyncError(f"Network error uploading '{local_path.name}': {exc}") from exc

    def _get_item(self, remote_path: str) -> Optional[dict]:
        url = f"{GRAPH_ROOT}/me/drive/root:{_quote_path(remote_path)}"
        try:
            resp = self._session.get(url, timeout=30)
        except requests.RequestException:
            return None
        if not resp.ok:
            return None
        return resp.json()

    def _set_file_system_info(self, item_id: str, client_modified: datetime) -> None:
        iso = client_modified.replace(microsecond=0).isoformat() + "Z"
        try:
            self._session.patch(
                f"{GRAPH_ROOT}/me/drive/items/{item_id}",
                json={"fileSystemInfo": {"lastModifiedDateTime": iso}},
                timeout=30,
            )
        except requests.RequestException:
            pass  # best-effort: a stale timestamp just means a future re-check re-uploads it
