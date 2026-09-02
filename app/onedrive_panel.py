from __future__ import annotations

import webbrowser

from PySide6.QtWidgets import QLabel, QLineEdit, QMessageBox, QPushButton

from . import config
from .config import AppSettings
from .oauth_local_server import REDIRECT_URI, LoopbackAuthServer
from .oauth_wait_dialog import BrowserSignInDialog
from .onedrive_client import (
    OneDriveAuthError,
    OneDriveClient,
    build_authorize_url,
    exchange_code_for_tokens,
    friendly_auth_error_message,
    refresh_access_token,
)
from .provider_panel import ProviderPanel


class OneDrivePanel(ProviderPanel):
    def __init__(self, settings: AppSettings, parent=None):
        self.settings = settings
        super().__init__("OneDrive", "/Website", parent)

        self.client_id_edit.setText(settings.onedrive_client_id)
        self.local_folder_edit.setText(settings.onedrive_local_folder)
        self.remote_folder_edit.setText(settings.onedrive_remote_folder or "/")

        token = config.get_secret(config.ONEDRIVE_TOKEN_KEY, settings)
        if settings.onedrive_client_id and token:
            self.log_message.emit("Restoring saved OneDrive connection...")
            self._try_connect_with_token(settings.onedrive_client_id, token)

    def _build_connect_controls(self) -> list:
        self.client_id_edit = QLineEdit()
        self.client_id_edit.setPlaceholderText("Azure App (client) ID")
        self.connect_btn = QPushButton("Connect to OneDrive")
        self.connect_btn.clicked.connect(self.on_connect_clicked)
        self.disconnect_btn = QPushButton("Disconnect")
        self.disconnect_btn.setEnabled(False)
        self.disconnect_btn.setToolTip(
            "Clear the saved connection. Use this after changing API permissions in the Azure app "
            "registration — your existing saved token won't pick up new scopes on its own; Connect "
            "again afterward to get a fresh one that does."
        )
        self.disconnect_btn.clicked.connect(self.on_disconnect_clicked)
        setup_help_btn = QPushButton("Setup help")
        setup_help_btn.setToolTip("One-time Azure app registration setup required before connecting")
        setup_help_btn.clicked.connect(self.on_setup_help_clicked)
        return [QLabel("Client ID:"), self.client_id_edit, self.connect_btn, self.disconnect_btn, setup_help_btn]

    def on_disconnect_clicked(self) -> None:
        config.clear_secret(config.ONEDRIVE_TOKEN_KEY, self.settings)
        config.save_settings(self.settings)
        self.set_disconnected()
        self.log_message.emit("Disconnected from OneDrive. Click Connect to sign in again (e.g. after changing app permissions).")

    def on_setup_help_clicked(self) -> None:
        QMessageBox.information(
            self,
            "OneDrive app setup",
            "Before connecting, your Azure app registration needs a Redirect URI "
            "registered as a \"Mobile and desktop applications\" platform entry — "
            "otherwise sign-in fails with an AADSTS error:\n\n"
            "1. Go to portal.azure.com -> Microsoft Entra ID -> App registrations\n"
            "2. Select your app -> Authentication\n"
            "3. Add a platform -> \"Mobile and desktop applications\"\n"
            "4. Under \"Custom redirect URIs\", add exactly:\n\n"
            f"   {REDIRECT_URI}\n\n"
            "5. Save, then try Connect again.\n\n"
            "This only needs to be done once per app registration.",
        )

    def _persist_folders(self) -> None:
        self.settings.onedrive_local_folder = self.local_folder_edit.text().strip()
        self.settings.onedrive_remote_folder = self.remote_folder_edit.text().strip() or "/"
        config.save_settings(self.settings)

    def _try_connect_with_token(self, client_id: str, refresh_token: str) -> bool:
        try:
            access_token, new_refresh_token = refresh_access_token(client_id, refresh_token)
            client = OneDriveClient(access_token)
        except OneDriveAuthError as exc:
            self.log_message.emit(f"Stored OneDrive session is no longer valid: {exc}")
            config.clear_secret(config.ONEDRIVE_TOKEN_KEY, self.settings)
            config.save_settings(self.settings)
            self.set_disconnected()
            return False
        config.set_secret(config.ONEDRIVE_TOKEN_KEY, new_refresh_token, self.settings)
        config.save_settings(self.settings)
        self.set_connected(client)
        return True

    def on_connect_clicked(self) -> None:
        client_id = self.client_id_edit.text().strip()
        if not client_id:
            QMessageBox.warning(self, "Missing Client ID", "Enter your Azure App (client) ID first.")
            return

        token = (
            config.get_secret(config.ONEDRIVE_TOKEN_KEY, self.settings)
            if client_id == self.settings.onedrive_client_id
            else None
        )
        if token and self._try_connect_with_token(client_id, token):
            return

        authorize_url, code_verifier = build_authorize_url(client_id)

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

        dialog = BrowserSignInDialog("OneDrive", self)
        dialog.code_received.connect(lambda code: self._on_code_received(client_id, code, code_verifier))
        dialog.auth_failed.connect(self._on_auth_failed)
        dialog.cancelled.connect(lambda: self.log_message.emit("OneDrive authorization cancelled."))
        dialog.start(server)

        webbrowser.open(authorize_url)
        self.log_message.emit("Opened your browser to sign in to OneDrive.")
        dialog.exec()

    def _on_code_received(self, client_id: str, code: str, code_verifier: str) -> None:
        try:
            access_token, refresh_token = exchange_code_for_tokens(client_id, code, code_verifier)
            client = OneDriveClient(access_token)
        except OneDriveAuthError as exc:
            # exchange_code_for_tokens already raises with a friendly message baked in.
            QMessageBox.critical(self, "OneDrive authorization failed", str(exc))
            self.log_message.emit(f"OneDrive authorization failed: {exc}")
            return

        self.settings.onedrive_client_id = client_id
        config.set_secret(config.ONEDRIVE_TOKEN_KEY, refresh_token, self.settings)
        config.save_settings(self.settings)
        self.set_connected(client)
        self.log_message.emit("Connected to OneDrive and saved credentials securely for next time.")

    def _on_auth_failed(self, message: str) -> None:
        friendly = friendly_auth_error_message(message)
        QMessageBox.critical(self, "OneDrive authorization failed", friendly)
        self.log_message.emit(f"OneDrive authorization failed: {friendly}")
