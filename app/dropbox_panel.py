from __future__ import annotations

import webbrowser

from PySide6.QtWidgets import QLabel, QLineEdit, QMessageBox, QPushButton

from . import config
from .config import AppSettings
from .dropbox_client import (
    DropboxAuthError,
    DropboxClient,
    build_authorize_url,
    exchange_code_for_token,
    friendly_auth_error_message,
)
from .oauth_local_server import REDIRECT_URI, LoopbackAuthServer
from .oauth_wait_dialog import BrowserSignInDialog
from .provider_panel import ProviderPanel


class DropboxPanel(ProviderPanel):
    def __init__(self, settings: AppSettings, parent=None):
        self.settings = settings
        super().__init__("Dropbox", "/Website", parent)

        self.app_key_edit.setText(settings.dropbox_app_key)
        self.local_folder_edit.setText(settings.dropbox_local_folder)
        self.remote_folder_edit.setText(settings.dropbox_remote_folder or "/")

        token = config.get_secret(config.DROPBOX_TOKEN_KEY, settings)
        if settings.dropbox_app_key and token:
            self.log_message.emit("Restoring saved Dropbox connection...")
            self._try_connect_with_token(settings.dropbox_app_key, token)

    def _build_connect_controls(self) -> list:
        self.app_key_edit = QLineEdit()
        self.app_key_edit.setPlaceholderText("Dropbox App key (from the App Console)")
        self.connect_btn = QPushButton("Connect to Dropbox")
        self.connect_btn.clicked.connect(self.on_connect_clicked)
        self.disconnect_btn = QPushButton("Disconnect")
        self.disconnect_btn.setEnabled(False)
        self.disconnect_btn.setToolTip(
            "Clear the saved connection. Use this after changing Permissions in the App Console — "
            "your existing saved token won't pick up new scopes on its own; Connect again afterward "
            "to get a fresh one that does."
        )
        self.disconnect_btn.clicked.connect(self.on_disconnect_clicked)
        setup_help_btn = QPushButton("Setup help")
        setup_help_btn.setToolTip("One-time App Console setup required before connecting")
        setup_help_btn.clicked.connect(self.on_setup_help_clicked)
        return [QLabel("App key:"), self.app_key_edit, self.connect_btn, self.disconnect_btn, setup_help_btn]

    def on_disconnect_clicked(self) -> None:
        config.clear_secret(config.DROPBOX_TOKEN_KEY, self.settings)
        config.save_settings(self.settings)
        self.set_disconnected()
        self.log_message.emit("Disconnected from Dropbox. Click Connect to sign in again (e.g. after changing App Console permissions).")

    def on_setup_help_clicked(self) -> None:
        QMessageBox.information(
            self,
            "Dropbox app setup",
            "Before connecting, your Dropbox app needs a Redirect URI registered — "
            "otherwise Dropbox rejects the sign-in with \"Invalid redirect_uri\":\n\n"
            "1. Go to the Dropbox App Console (dropbox.com/developers/apps)\n"
            "2. Select your app -> Settings tab\n"
            "3. Under \"Redirect URIs\", add exactly:\n\n"
            f"   {REDIRECT_URI}\n\n"
            "4. Click Add, then try Connect again.\n\n"
            "This only needs to be done once per app key.",
        )

    def _persist_folders(self) -> None:
        self.settings.dropbox_local_folder = self.local_folder_edit.text().strip()
        self.settings.dropbox_remote_folder = self.remote_folder_edit.text().strip() or "/"
        config.save_settings(self.settings)

    def on_connect_clicked(self) -> None:
        app_key = self.app_key_edit.text().strip()
        if not app_key:
            QMessageBox.warning(self, "Missing App key", "Enter your Dropbox App key first.")
            return

        token = (
            config.get_secret(config.DROPBOX_TOKEN_KEY, self.settings)
            if app_key == self.settings.dropbox_app_key
            else None
        )
        if token and self._try_connect_with_token(app_key, token):
            return

        authorize_url, code_verifier = build_authorize_url(app_key)

        try:
            server = LoopbackAuthServer()
            server.start()
        except OSError as exc:
            QMessageBox.critical(
                self,
                "Could not start sign-in listener",
                f"Couldn't start the local sign-in listener on port 43219: {exc}\n\n"
                "Close whatever else might be using that port and try again.",
            )
            return

        dialog = BrowserSignInDialog("Dropbox", self)
        dialog.code_received.connect(lambda code: self._on_code_received(app_key, code, code_verifier))
        dialog.auth_failed.connect(self._on_auth_failed)
        dialog.cancelled.connect(lambda: self.log_message.emit("Dropbox authorization cancelled."))
        dialog.start(server)

        webbrowser.open(authorize_url)
        self.log_message.emit("Opened your browser to sign in to Dropbox.")
        dialog.exec()

    def _on_code_received(self, app_key: str, code: str, code_verifier: str) -> None:
        try:
            refresh_token = exchange_code_for_token(app_key, code, code_verifier)
            client = DropboxClient(app_key, refresh_token)
        except DropboxAuthError as exc:
            # exchange_code_for_token already raises with a friendly message baked in.
            QMessageBox.critical(self, "Dropbox authorization failed", str(exc))
            self.log_message.emit(f"Dropbox authorization failed: {exc}")
            return

        self.settings.dropbox_app_key = app_key
        config.set_secret(config.DROPBOX_TOKEN_KEY, refresh_token, self.settings)
        config.save_settings(self.settings)
        self.set_connected(client)
        self.log_message.emit("Connected to Dropbox and saved credentials securely for next time.")

    def _on_auth_failed(self, message: str) -> None:
        friendly = friendly_auth_error_message(message)
        QMessageBox.critical(self, "Dropbox authorization failed", friendly)
        self.log_message.emit(f"Dropbox authorization failed: {friendly}")

    def _try_connect_with_token(self, app_key: str, token: str) -> bool:
        try:
            client = DropboxClient(app_key, token)
        except DropboxAuthError as exc:
            self.log_message.emit(f"Stored Dropbox session is no longer valid: {exc}")
            config.clear_secret(config.DROPBOX_TOKEN_KEY, self.settings)
            config.save_settings(self.settings)
            self.set_disconnected()
            return False
        self.set_connected(client)
        return True
