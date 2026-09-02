# Local → Cloud Sync Dashboard

Desktop app (PySide6) with a **tab per cloud destination — Dropbox and
OneDrive** — that compares a local folder against a folder in that
account, shows exactly which local files are missing there in a
**tree view matching your folder structure**, and lets you **Sync**
(upload) only the files you select, preserving that same tree structure
on the destination — including files larger than each provider's
single-request upload limit (handled via chunked/resumable uploads).

## Dashboard layout

- **Dropbox tab** and **OneDrive tab** — each fully independent: its own
  connection, its own local folder, its own destination folder, its own
  "Missing files" tree, and its own **Sync missing files** button.
- The **Missing files** tree mirrors your local folder's directory
  structure. Checking/unchecking a folder checks/unchecks everything
  under it; checking individual files gives ancestor folders a
  partially-checked state.
- A shared log panel at the bottom shows what's happening across both
  tabs.

## How comparison works

For every file under the local folder, the app checks the same relative
path on the destination:

| Result | Meaning |
|---|---|
| Missing on destination | No file at that path in the cloud |
| Size differs | A file exists but its size doesn't match |
| Destination is older | Sizes match but the local file was modified more recently |
| In sync | Nothing to do (not shown in the tree) |

Only the first three ever appear in the tree, and they're pre-checked;
adjust the selection before clicking **Sync**.

## One-time setup

```
pip install -r requirements.txt
python main.py
```

Both providers sign in the same way: clicking Connect opens **your real,
default system browser** to the provider's own sign-in page — type your
email/password there exactly as you normally would (autofill, saved
passwords, and MFA all work, since it's your actual browser). The app
starts a tiny local listener on `http://localhost:43219/oauth-callback`
to catch the redirect once you finish, then closes automatically. Your
password is never seen by this app — it goes straight from your browser
to Dropbox/Microsoft.

### Dropbox tab

1. Go to https://www.dropbox.com/developers/apps → "Create app".
2. Choose **Scoped access**, then either **Full Dropbox** or **App
   folder** access (App folder sandboxes the app to `/Apps/<YourApp>`).
3. On **Permissions**, enable `files.metadata.read`, `files.content.read`,
   `files.content.write`, then Submit.
4. On **Settings**, under **OAuth 2 → Redirect URIs**, add exactly:
   `http://localhost:43219/oauth-callback`
5. Also on **Settings**, copy the **App key** (no app secret needed —
   this app uses the secret-free PKCE OAuth flow).
6. In the app's Dropbox tab: paste the App key, click **Connect to
   Dropbox**, and sign in in the browser window that opens.

### OneDrive tab

1. Go to https://portal.azure.com → **Microsoft Entra ID** → **App
   registrations** → **New registration**.
2. Name it anything. Under **Supported account types**, choose
   "Accounts in any organizational directory and personal Microsoft
   accounts" (needed for personal OneDrive).
3. Under **Redirect URI**, choose platform **"Mobile and desktop
   applications"** and add exactly: `http://localhost:43219/oauth-callback`
   (You can add this later too, under Authentication → Add a platform.)
4. Open **API permissions** → **Add a permission** → **Microsoft Graph**
   → **Delegated permissions** → add `Files.ReadWrite` and `User.Read`.
   (No admin consent needed for a personal account.)
5. Copy the **Application (client) ID** from the Overview page — not the
   Directory (tenant) ID just above/below it; that's the single most
   common setup mistake and produces a confusing "app not found" error.
6. In the app's OneDrive tab: paste the Client ID, click **Connect to
   OneDrive**, and sign in in the browser window that opens.

### Using it

1. In either tab, choose the **local folder** to check and type the
   **destination folder path** (e.g. `/Website`, or `/` for the root).
2. Click **Scan & Compare**.
3. Review the **Missing files** tree, adjust the checkboxes if needed.
4. Click **Sync missing files to Dropbox/OneDrive**.

Both tabs remember your connection (via the OS credential manager) and
your last-used folders, so you don't need to reconnect each run.

## Running the regression tests

```
python tests/test_app.py
```

No live Dropbox/OneDrive account needed — it mocks the transport layer to
check the pieces that matter most: the missing-files tree's checkbox
propagation, the scanner/comparator/tree at a few-thousand-file scale,
the large-file chunked-upload branch for both providers (byte-range
math, session vs. simple upload), remote-listing pagination and
path-scoping, the loopback sign-in listener, and settings/secret
persistence.

## Notes

- Sign-in briefly needs local port `43219` free (used only to catch the
  browser redirect, then released). If something else is already using
  it, connecting will show a clear error asking you to free it up.
- If `keyring` can't access an OS credential store on your machine,
  tokens fall back to a local config file at
  `%APPDATA%\DropboxOneDriveSyncApp\config.json` — fine for personal use,
  just be aware it's stored in plaintext in that fallback case.
- Timestamps are compared with a 2-second tolerance to absorb filesystem
  rounding differences.
- The uploader preserves the local file's modified time on the
  destination, so future comparisons stay accurate.
- Files are matched by their path *relative to the chosen local folder*
  against the same relative path under the chosen destination folder —
  uploads automatically recreate any missing subfolders on the
  destination, so the tree structure is preserved without any extra
  "create folder" step.
