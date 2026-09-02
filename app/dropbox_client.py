"""Thin wrapper around the Dropbox SDK: auth (PKCE, no app secret needed,
via an embedded-browser authorization-code flow), recursive folder
listing, and chunked upload for very large files.
"""
from __future__ import annotations

import base64
import hashlib
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import urlencode

import dropbox
import requests
from dropbox.exceptions import ApiError, AuthError, BadInputError, RateLimitError
from dropbox.files import CommitInfo, FileMetadata, FolderMetadata, UploadSessionCursor, WriteMode

from .cloud_base import (
    CloudAuthError,
    CloudClient,
    CloudFileMeta,
    CloudFolderEntry,
    CloudSyncError,
    UploadResult,
)
from .oauth_local_server import REDIRECT_URI

CHUNK_SIZE = 8 * 1024 * 1024  # 8 MB
SIMPLE_UPLOAD_LIMIT = 150 * 1024 * 1024  # Dropbox's single-shot upload ceiling

AUTHORIZE_URL = "https://www.dropbox.com/oauth2/authorize"
TOKEN_URL = "https://api.dropboxapi.com/oauth2/token"


class DropboxAuthError(CloudAuthError):
    pass


class DropboxSyncError(CloudSyncError):
    pass


def _make_pkce_pair() -> Tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def build_authorize_url(app_key: str) -> Tuple[str, str]:
    """Returns (authorize_url, code_verifier) for an embedded-browser login."""
    verifier, challenge = _make_pkce_pair()
    params = {
        "client_id": app_key,
        "response_type": "code",
        "token_access_type": "offline",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "redirect_uri": REDIRECT_URI,
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}", verifier


def exchange_code_for_token(app_key: str, code: str, code_verifier: str) -> str:
    """Exchanges the authorization code for tokens; returns the long-lived refresh token."""
    try:
        resp = requests.post(
            TOKEN_URL,
            data={
                "code": code,
                "grant_type": "authorization_code",
                "client_id": app_key,
                "redirect_uri": REDIRECT_URI,
                "code_verifier": code_verifier,
            },
            timeout=30,
        )
    except requests.RequestException as exc:
        raise DropboxAuthError(f"Network error completing Dropbox authorization: {exc}") from exc

    if not resp.ok:
        raise DropboxAuthError(_friendly_dropbox_error(resp))

    data = resp.json()
    refresh_token = data.get("refresh_token")
    if not refresh_token:
        raise DropboxAuthError("Dropbox did not return a refresh token.")
    return refresh_token


def friendly_auth_error_message(desc: str, error_code: str = "") -> str:
    """Turns a raw Dropbox error into actionable guidance. Used both for
    token-endpoint JSON errors and for the error_detail/error_name query
    params Dropbox redirects to at /oauth2/authorize_error when the App key
    itself is invalid — that happens before any redirect_uri is involved,
    so it's caught straight from the embedded sign-in page's navigation.
    """
    lowered = desc.lower()
    if "redirect_uri" in lowered or error_code == "redirect_uri_mismatch":
        return (
            "Dropbox authorization failed: the redirect URI isn't registered for this app. "
            "In the Dropbox App Console, under your app's Settings -> OAuth 2 -> Redirect URIs, "
            f"add exactly: {REDIRECT_URI}\n\nDetails: {desc}"
        )
    if error_code in ("invalid_client", "unknown_client_id") or "invalid client_id" in lowered:
        return (
            "Dropbox authorization failed: the App key doesn't match any registered app. "
            f"Double-check it in the Dropbox App Console.\n\nDetails: {desc}"
        )
    return f"Dropbox authorization failed: {desc}"


def _friendly_dropbox_error(resp: requests.Response) -> str:
    try:
        data = resp.json()
    except ValueError:
        return f"Dropbox authorization failed: {resp.status_code} {resp.text}"
    error = data.get("error", "")
    desc = data.get("error_description", error or "Unknown error")
    return friendly_auth_error_message(desc, error)


def _describe_dropbox_api_error(exc: Exception) -> str:
    """Turns an API-call-time exception into actionable guidance. This app
    requests no explicit `scope` param when authorizing (Dropbox uses
    whatever's enabled in the App Console's Permissions tab instead), so a
    newly created scoped app missing a required permission is a common
    one-time setup gap — call it out specifically rather than surfacing the
    raw BadInputError repr."""
    text = str(exc)
    if "required scope" in text:
        return (
            "Dropbox rejected this request: your app is missing a required permission (scope).\n\n"
            f"Details: {text}\n\n"
            "Fix: in the Dropbox App Console, open your app -> Permissions tab, enable the scope(s) "
            "named above (typically files.metadata.read, files.content.read, files.content.write), "
            "click Submit, then disconnect and reconnect in this app — your existing saved connection "
            "was issued before the new permission and won't have it until you reconnect."
        )
    return f"Unexpected error: {text}"


def _raise_from_auth_error(exc: AuthError) -> None:
    """AuthError covers two very different situations: a genuinely
    expired/invalid session, and a missing OAuth scope (Dropbox reports the
    latter as AuthError('missing_scope', TokenScopeError(...)) rather than
    BadInputError for some endpoints). The missing-scope case is a one-time
    App Console permissions gap, not a session problem — treating it as
    "reconnect and it'll work again" is actively misleading, since
    reconnecting alone changes nothing until the scope is actually enabled.
    """
    reason = getattr(exc, "error", None)
    if reason is not None and hasattr(reason, "is_missing_scope") and reason.is_missing_scope():
        required = reason.get_missing_scope().required_scope
        raise DropboxSyncError(
            f"Dropbox rejected this request: your app is missing a required permission (scope) '{required}'.\n\n"
            "Fix: in the Dropbox App Console, open your app -> Permissions tab, enable this scope "
            "(typically alongside files.metadata.read, files.content.read, files.content.write), click "
            "Submit, then disconnect and reconnect in this app — your existing saved connection was "
            "issued before the new permission and won't have it until you reconnect."
        ) from exc
    raise DropboxAuthError(f"Dropbox authentication failed: {exc}") from exc


class DropboxClient(CloudClient):
    display_name = "Dropbox"

    def __init__(self, app_key: str, refresh_token: str):
        self._dbx = dropbox.Dropbox(oauth2_refresh_token=refresh_token, app_key=app_key)
        try:
            self._dbx.users_get_current_account()
        except AuthError as exc:
            _raise_from_auth_error(exc)

    def list_folder_recursive(
        self, remote_root: str, log: Optional[Callable[[str], None]] = None
    ) -> Dict[str, CloudFileMeta]:
        root = self.normalize_root(remote_root)
        files: Dict[str, CloudFileMeta] = {}

        def _consume(entries):
            for entry in entries:
                if not isinstance(entry, FileMetadata):
                    continue
                rel = entry.path_display[len(root):] if root else entry.path_display
                rel = rel.lstrip("/")
                cm = entry.client_modified
                if cm.tzinfo is not None:
                    cm = cm.astimezone(timezone.utc).replace(tzinfo=None)
                files[rel] = CloudFileMeta(size=entry.size, client_modified=cm)

        try:
            try:
                result = self._dbx.files_list_folder(root, recursive=True)
            except ApiError as exc:
                if (
                    isinstance(exc.error, dropbox.files.ListFolderError)
                    and exc.error.is_path()
                    and exc.error.get_path().is_not_found()
                ):
                    if log:
                        log(f"Remote folder '{remote_root}' does not exist yet (treating as empty).")
                    return files
                raise
            _consume(result.entries)
            while result.has_more:
                result = self._dbx.files_list_folder_continue(result.cursor)
                _consume(result.entries)
        except RateLimitError as exc:
            time.sleep(getattr(exc, "backoff", 5) or 5)
            return self.list_folder_recursive(remote_root, log)
        except AuthError as exc:
            _raise_from_auth_error(exc)
        except ApiError as exc:
            raise DropboxSyncError(f"Failed to list remote folder: {exc}") from exc
        except BadInputError as exc:
            raise DropboxSyncError(_describe_dropbox_api_error(exc)) from exc

        return files

    def list_child_folders(self, remote_path: str) -> List[CloudFolderEntry]:
        root = self.normalize_root(remote_path)
        entries: List[FolderMetadata] = []
        try:
            result = self._dbx.files_list_folder(root, recursive=False)
            entries.extend(e for e in result.entries if isinstance(e, FolderMetadata))
            # The pagination continuation used to sit outside this try block —
            # any failure there (rate limit, transient API error) propagated
            # uncaught out of the folder picker, which just looked "stuck" with
            # no error shown. It's now covered by the same handling as the
            # initial call.
            while result.has_more:
                result = self._dbx.files_list_folder_continue(result.cursor)
                entries.extend(e for e in result.entries if isinstance(e, FolderMetadata))
        except RateLimitError as exc:
            time.sleep(getattr(exc, "backoff", 5) or 5)
            return self.list_child_folders(remote_path)
        except ApiError as exc:
            if (
                isinstance(exc.error, dropbox.files.ListFolderError)
                and exc.error.is_path()
                and exc.error.get_path().is_not_found()
            ):
                return []
            raise DropboxSyncError(f"Failed to list folder: {exc}") from exc
        except AuthError as exc:
            _raise_from_auth_error(exc)
        except Exception as exc:
            # Last resort so an unanticipated SDK/network error (including a
            # missing-scope BadInputError) still surfaces as a clear, actionable
            # message instead of leaving the picker silently stuck.
            raise DropboxSyncError(_describe_dropbox_api_error(exc)) from exc

        return sorted(
            (CloudFolderEntry(name=e.name, path=e.path_display) for e in entries),
            key=lambda f: f.name.lower(),
        )

    def upload_file(
        self,
        local_path: Path,
        remote_path: str,
        client_modified: datetime,
        progress_cb: Optional[Callable[[int, int], None]] = None,
    ) -> UploadResult:
        cm = client_modified.replace(microsecond=0)
        now = datetime.utcnow().replace(microsecond=0)
        if cm > now:
            cm = now

        file_size = local_path.stat().st_size
        mode = WriteMode.overwrite
        meta: Optional[FileMetadata] = None

        try:
            with open(local_path, "rb") as f:
                if file_size <= SIMPLE_UPLOAD_LIMIT:
                    data = f.read()
                    meta = self._retrying(
                        lambda: self._dbx.files_upload(
                            data, remote_path, mode=mode, client_modified=cm, mute=True
                        )
                    )
                    if progress_cb:
                        progress_cb(file_size, file_size)
                else:
                    start = self._retrying(
                        lambda: self._dbx.files_upload_session_start(f.read(CHUNK_SIZE))
                    )
                    cursor = UploadSessionCursor(session_id=start.session_id, offset=f.tell())
                    commit = CommitInfo(path=remote_path, mode=mode, client_modified=cm, mute=True)
                    if progress_cb:
                        progress_cb(f.tell(), file_size)

                    while f.tell() < file_size:
                        remaining = file_size - f.tell()
                        chunk = f.read(CHUNK_SIZE)
                        if remaining <= CHUNK_SIZE:
                            meta = self._retrying(
                                lambda: self._dbx.files_upload_session_finish(chunk, cursor, commit)
                            )
                        else:
                            self._retrying(
                                lambda: self._dbx.files_upload_session_append_v2(chunk, cursor)
                            )
                            cursor.offset = f.tell()
                        if progress_cb:
                            progress_cb(f.tell(), file_size)
        except AuthError as exc:
            _raise_from_auth_error(exc)
        except ApiError as exc:
            raise DropboxSyncError(f"Failed to upload '{local_path.name}': {exc}") from exc
        except BadInputError as exc:
            raise DropboxSyncError(_describe_dropbox_api_error(exc)) from exc

        if meta is None or meta.size != file_size:
            reported = meta.size if meta is not None else "none"
            raise DropboxSyncError(
                f"Upload for '{local_path.name}' did not verify: Dropbox reports "
                f"{reported} bytes, expected {file_size}."
            )
        return UploadResult(remote_id=meta.id, remote_size=meta.size)

    @staticmethod
    def _retrying(call: Callable, attempts: int = 3):
        last_exc = None
        for _ in range(attempts):
            try:
                return call()
            except RateLimitError as exc:
                last_exc = exc
                time.sleep(getattr(exc, "backoff", 5) or 5)
        if last_exc:
            raise last_exc
