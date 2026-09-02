"""A minimal loopback HTTP server that captures an OAuth redirect after the
user signs in through their real system browser (the RFC 8252 native-app
pattern). This replaces embedding a full Chromium browser in-process, which
crashed with a native "CalledOnValidBrowserThread" Chromium threading fault
on at least one real machine — a class of bug tied to QtWebEngine's own
internals, not something fixable by reordering our callbacks further.
"""
from __future__ import annotations

import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional, Tuple
from urllib.parse import parse_qs, urlparse

CALLBACK_PORT = 43219
CALLBACK_PATH = "/oauth-callback"
REDIRECT_URI = f"http://localhost:{CALLBACK_PORT}{CALLBACK_PATH}"

_SUCCESS_HTML = b"""<!doctype html><html><body style="font-family:sans-serif;text-align:center;padding-top:80px">
<h2>Signed in</h2><p>You can close this window and go back to the app.</p></body></html>"""

_FAILURE_HTML = b"""<!doctype html><html><body style="font-family:sans-serif;text-align:center;padding-top:80px">
<h2>Sign-in failed</h2><p>You can close this window and go back to the app.</p></body></html>"""


class _CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != CALLBACK_PATH:
            self.send_response(404)
            self.end_headers()
            return

        params = parse_qs(parsed.query)
        code = params.get("code", [None])[0]
        error = params.get("error_description", params.get("error", [None]))[0]
        self.server.oauth_result = (code, error)  # type: ignore[attr-defined]

        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(_SUCCESS_HTML if code else _FAILURE_HTML)

    def log_message(self, format, *args) -> None:
        pass  # silence default per-request stderr logging


class LoopbackAuthServer:
    """Starts a background HTTP server on CALLBACK_PORT, waits for exactly
    one OAuth redirect (or a timeout), then stops. `result` is polled from
    the Qt main thread via a QTimer rather than blocking it.
    """

    def __init__(self, timeout_seconds: int = 300):
        self._httpd = HTTPServer(("127.0.0.1", CALLBACK_PORT), _CallbackHandler)
        self._httpd.timeout = 1  # poll granularity for handle_request()
        self._httpd.oauth_result = None  # type: ignore[attr-defined]
        self._timeout_seconds = timeout_seconds
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @property
    def result(self) -> Optional[Tuple[Optional[str], Optional[str]]]:
        return self._httpd.oauth_result  # type: ignore[attr-defined]

    def start(self) -> None:
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        deadline = time.time() + self._timeout_seconds
        while not self._stop_event.is_set() and time.time() < deadline:
            self._httpd.handle_request()
            if self._httpd.oauth_result is not None:  # type: ignore[attr-defined]
                break

    def shutdown(self) -> None:
        self._stop_event.set()
        try:
            self._httpd.server_close()
        except OSError:
            pass
