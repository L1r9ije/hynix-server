# Hynix API server

The server uses only Python's standard library and SQLite. It provides the remote mod catalog, Minecraft client versions, admin CRUD endpoints and the public launcher update manifest.

## Run locally (Windows PowerShell)

```powershell
$env:HYNIX_ADMIN_TOKEN = [Convert]::ToBase64String([Security.Cryptography.RandomNumberGenerator]::GetBytes(48))
$env:HYNIX_HOST = "127.0.0.1"
$env:HYNIX_PORT = "8787"
py server/app.py
```

For local testing use `http://127.0.0.1:8787` in the launcher settings. The admin token must be at least 32 characters. Never publish it in the client, `server-config.json`, Git, screenshots or chat.

## Run on a VPS

Use Python 3.10+ and set `HYNIX_ADMIN_TOKEN` in the service manager/environment. Bind to localhost behind a reverse proxy with HTTPS (Caddy or Nginx). Do not expose the Python HTTP service directly to the public Internet without TLS/rate limiting/firewall rules. Persist `server_data/hynix.sqlite3` and back it up.

```bash
export HYNIX_ADMIN_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
export HYNIX_HOST=127.0.0.1
export HYNIX_PORT=8787
python server/app.py
```

## Admin panel

Open the shield icon in Hynix Loader. Enter the API URL and `HYNIX_ADMIN_TOKEN`. The token remains in the panel's memory only. The panel can manage:

- Modrinth Fabric mod catalog entries (`id`, `projectId`, title, category and description).
- Minecraft/Fabric client versions (`mcVersion`, Fabric, Java, Hynix JAR URL, plan, online/offline status). New versions must include the JAR SHA-1 and exact byte size so damaged client mods can be detected reliably.
- Windows x64 launcher releases (version, direct HTTPS URL, SHA-256, notes, mandatory and published flags). Re-saving the same version updates its release metadata, so drafts can be published later.

Public routes: `GET /health`, `GET /api/catalog/mods`, `GET /api/catalog/versions`, and `GET /api/updates/latest`.
Admin routes require `Authorization: Bearer <HYNIX_ADMIN_TOKEN>`.

## Publish a launcher update

1. Increase `version` in `package.json`.
2. Run `npm ci` then `npm run dist:win` on Windows. The build creates an NSIS Setup installer and a portable executable.
3. Upload the `Hynix Loader-Setup-X.Y.Z.exe` file to an HTTPS host that serves the exact file (not an HTML landing page).
4. Calculate SHA-256:
   ```powershell
   Get-FileHash .\release\Hynix\Hynix\Hynix\Hynix\Hynix Loader-Setup-X.Y.Z.exe -Algorithm SHA256
   ```
   The exact path depends on electron-builder output; use the actual Setup `.exe` inside `release/`.
5. In Admin → Обновления, enter version, direct URL, SHA-256 and release notes. Publish.

NSIS-installed applications check the release manifest on login and every 15 minutes, download the installer, verify SHA-256, launch it silently and quit so the installer can replace the app. Portable builds can detect an update but open the download URL for manual replacement; portable mode is not silently self-updated.

## Mod installs and repair

Mods are queried from Modrinth for the selected Minecraft version and Fabric loader. Required Modrinth dependencies are installed together. JAR files must include SHA-1 and size metadata and are verified before they are committed into `versions/client/game/mods`.

Minecraft repair checks the asset index and SHA-1 of every unique asset object during install-state detection, so same-size corruption is detected too. The explicit **Проверить и восстановить файлы** action re-runs the complete install pipeline: Java, client JAR, libraries (including SHA-1 where the manifest supplies it), natives, assets, Fabric, Hynix JAR and required dependencies. Existing valid files are kept; absent or corrupted artifacts are downloaded to a staging file and replaced only after validation. Native libraries are re-extracted in a staging directory, their DLL SHA-1/size values are recorded in a manifest, and the directory is swapped into place only after extraction and verification succeed. For legacy Hynix JARs without server-side checksum metadata, the first successful repair records a local SHA-1/size pin for subsequent integrity checks.

## Limitations to understand

- The desktop app's current user accounts and license records are still local JSON files. This API does not yet migrate or secure authentication/licensing. Admin panel management is protected by a shared server token, but there is not yet per-admin user accounts or audit log.
- The server API serves launcher update manifests; it does not upload binaries. Store installers on a trusted HTTPS object store/CDN and enter their exact direct URL and SHA-256.
- The current release updater is Windows x64 only. NSIS is the automatically updatable package format.
