# 360 Tour Uploader for Google Street View

Upload equirectangular 360 photos from cameras such as RICOH THETA, Insta360, and GoPro MAX to Google Street View, with a browser-based editor for map layout, connections, heading alignment, and local walkthrough preview.

## Features

- Browser-based tour editor built on OpenStreetMap and Leaflet
- Drag and drop 360 JPG files with automatic GPS placement from EXIF/XMP
- Manual heading alignment with pano preview and EXIF reset
- In-editor walkthrough between linked nodes
- Overlay compare view for visually checking orientation between two nodes
- Save / open the whole project, photos included, as a single `.vrtour` file (a plain zip); **Ctrl+S** saves straight back into it
- **Auto-connect** nodes in the order the photos were taken, or to their nearest neighbours
- Place photos without GPS from a **GPX track** recorded on your phone
- **Blur faces and licence plates** in the editor before uploading, with automatic face detection as a starting point
- **Headings from walking direction**: align one photo by hand, the rest follow the route
- **Tour check** before uploading: wrong image format, missing 360° metadata, missing north, unconnected nodes, overly long links, duplicate positions
- Street map or **satellite view**, thumbnails in the node list and on marker hover, **Shift+drag** to select several nodes
- **Info points** (title, text, link) inside panoramas, and a **minimap** in the exported tour
- English and **German** user interface (Tools → Deutsch / English)
- Export to `tour-config.json`, `tour-viewer.html` and `tour.csv`, or as a ready-to-host **website zip** with all photos
- One-click upload to Google Street View with a step-by-step setup assistant; re-runs skip photos already uploaded and only push changed positions, headings and links; the **Google status** (published / processing / rejected) shows on each marker
- Windows app with installer, tray icon, `.vrtour` file association and in-app updates
- Google Street View Publish API uploader and heading patch utilities

## Repository Layout

```text
streetview-tour-uploader/
├── editor/
│   └── editor.html
├── example/
│   ├── tour.csv.example
│   └── tour-meta.json.example
├── uploader/
│   ├── find_place_id.py
│   ├── gen_tour_csv.py
│   ├── restore_exif.py
│   ├── restore_xmp.py
│   ├── retry_failed.py
│   └── upload_tour.py
├── installer/
│   ├── StreetviewTourEditor.iss
│   └── app.ico
├── packaging/winget/            (winget manifest generator + how-to)
├── app_tray.py                  (tray icon of the Windows app)
├── app_update.py                (in-app updates from GitHub Releases)
├── LICENSE
├── README.md
├── requirements.txt
├── build_exe.py
└── run_editor.py
```

## Requirements

- Python 3.9+ (not needed for the [standalone .exe](#optional-standalone-windows-app-installer))
- A Google account
- Street View Publish API enabled in Google Cloud Console
- An OAuth desktop client (Client ID + Client Secret) — entered once through the app's own UI, see below

## Installation

```bash
git clone https://github.com/takadakoji-jp/streetview-tour-uploader.git
cd streetview-tour-uploader
pip install -r requirements.txt
```

## One-time Google setup

Google requires every application, including this one, to be registered as
an OAuth client before it can publish photos to Street View on your behalf.
There is no way to upload without this — it applies to any tool using the
Street View Publish API, not just this project. It's free and only needs to
be done once:

1. Go to the [Google Cloud Console](https://console.cloud.google.com/) and create a new project (or pick an existing one).
2. Open **APIs & Services → Library**, search for **Street View Publish API**, and click **Enable**.
3. Open **APIs & Services → OAuth consent screen**. Choose **External**, fill in an app name and your email, and add your own Google account under **Test users** (this keeps the app private to you, no Google review needed).
4. Open **APIs & Services → Credentials → Create Credentials → OAuth client ID**. Choose application type **Desktop app** and create it.
5. Leave this page open — the **Client ID** and **Client Secret** shown here are what you paste into the app next.

**Entering it into the app:** open the editor, click **Upload to Google**,
and a step-by-step setup assistant appears the first time. Each step links
straight to the right Cloud Console page; at the end, paste in the Client ID
and Client Secret (or choose the downloaded JSON file) and click **Save & Continue**. The app writes
`uploader/client_secrets.json` for you; no file download, renaming, or
folder-hunting needed. (Already have the JSON file from Cloud Console
instead? Pasting its full contents into the Client ID field also works — the
form detects it and fills both fields automatically. You can also skip the
UI entirely and save that JSON as `uploader/client_secrets.json` by hand;
both paths end up in the same place.)

That's it — the saved credentials never expire on their own. Signing in
afterwards (via the editor's **Sign in with Google** button or
`upload_tour.py`) just opens a normal Google login/consent screen in your
browser. Use **Use a different Google API client** in the upload dialog to
clear it and set up a different one later.

## Workflow

### 1. Prepare photos

Blur faces and license plates before upload. The Street View Publish API does not blur them automatically. You can do it in the editor: select a node → **Blur faces & plates…**, drag boxes over what should be hidden, **Apply to photo**.

Photos without GPS can be placed from a GPX track: record one with any GPS logger app while shooting, then **Tools → Place photos from GPX track…**.

### 2. Design the tour

```bash
python run_editor.py
```

This starts a local server and opens the editor in your browser. Opening
`editor/editor.html` directly as a file (double-click) also works for basic
use, but the map tiles will be blocked by OpenStreetMap's referer policy and
"Save to folder" will be unavailable, so the script above is recommended.

- Drop your 360 JPG files onto the map, or double-click empty map area to add a point
- Drag markers if GPS is missing or needs correction, or type exact coordinates in the node panel
- Right-click a node, then right-click a second node to link them
- Or let **Tools → Auto-connect nodes…** link them in the order you walked (capture time) or to their nearest neighbours
- **Tools → Headings from walking direction…** sets north for every photo from the route; align one photo by hand first and pick it as reference
- Shift+drag on the map selects several nodes; the layer button (top right of the map) switches to satellite view
- **Info points** in the node panel add clickable "i" markers with text and a link to the panorama; they appear in the preview and the exported tour, which also gets a small map
- Select a node to edit heading, compare with neighbors, and test the walkthrough
- **Ctrl+S** saves the project (photos included) as a `.vrtour` file and afterwards straight back into it; **Ctrl+O** opens one

Running Windows without Python installed? See [Optional: standalone .exe](#optional-standalone-windows-app-installer) below — same editor, no `pip install` needed.

### 3. Upload to Google Street View

Click **Upload to Google** in the toolbar (`run_editor.py` must be running):

- First time only: paste your Client ID / Client Secret into the setup form — see [one-time Google setup](#one-time-google-setup)
- Click **Sign in with Google** — a normal Google login/consent screen opens in your browser
- Optionally paste a Google Place ID to link the tour to a location
- Click **Start Upload** — each photo is uploaded, then connections between linked nodes are published; progress is shown live in the log
- The upload runs a **tour check** first and stops on problems Google would reject (e.g. an image that is not 2:1); warnings are listed in the log and under **Tools → Check tour…**
- The project remembers which photos are already on Google (save it to keep that across sessions). Running the upload again only uploads new photos; photos you moved, turned or linked differently since are **updated on Google without re-uploading** them. Tick **Upload photos again** to force a full re-upload
- **Check status on Google** shows per photo whether it is published, still processing or rejected (coloured dot on the marker, link to the photo on Google Maps in the node panel)

This uses the photos already loaded in the editor session, so no manual CSV
or file paths are needed. Click **Export** first (or anytime) if you also
want local copies of `tour-config.json` / `tour-viewer.html` for the
walkthrough preview.

#### Alternative: command line

For scripting, batch uploads, or retrying later, the same upload can be
driven from the terminal instead of the button:

```bash
python uploader/gen_tour_csv.py \
  --config path/to/tour-config.json \
  --folder path/to/photos \
  --out path/to/photos/tour.csv

python uploader/upload_tour.py \
  --folder path/to/photos \
  --manifest path/to/photos/tour.csv
```

To associate the upload with a Google Maps place, add a `tour-meta.json` file in the photo folder:

```json
{
  "place_id": "ChIJxxxxxxxxxxxxxxxx",
  "note": "Venue name"
}
```

Use `--dry-run` to validate first, or `--connect-only` to retry connections after Google finishes processing. Both the button and the CLI share the same sign-in (`uploader/token.json`), so you can mix and match.

## Optional: standalone Windows app (installer)

For a version you can run without Python installed - your own PC, or to hand
to someone else - download **`StreetviewTourEditor-<version>-Setup.exe`** from
**[Releases](../../releases)** and run it. The installer:

- installs for the current user by default (no admin rights needed; it offers
  "install for all users" too),
- adds a Start menu entry and optionally a desktop icon,
- shows up under *Settings → Apps* with a normal uninstaller,
- updates in place: running a newer Setup.exe replaces the old version.

Prefer no installation at all? `StreetviewTourEditor-<version>-portable.zip`
from the same release contains the app folder - unzip it anywhere and run
`StreetviewTourEditor.exe`.

The app runs from an icon in the notification area (next to the clock) and
opens the editor in your browser, exactly like `python run_editor.py`.
Right-click the icon to reopen the editor, open the data folder, or quit.
Starting it a second time just opens another editor tab, and double-clicking
a `.vrtour` file opens that project.

**Updates:** the editor checks GitHub for a newer release when it starts. If
there is one, a notice appears in the top right: **Update now** downloads the
new Setup.exe, checks it against the release's `SHA256SUMS.txt`, installs it
in the background and reloads the page. (The portable version and `python
run_editor.py` only get a link to the release page.) **Tools → Check for
updates** checks right away.

**Where the Google API key goes:** nowhere at build time - the app is generic
and contains no secrets. Whoever uses it enters their own Client ID / Client
Secret through the **Upload to Google → one-time setup form** described
[above](#one-time-google-setup). The app stores it, and the sign-in token, in
`%APPDATA%\StreetviewTourEditor\`, per Windows user, so updates, reinstalls
and uninstalls never touch it. (Builds before the installer kept these files
in `dist/uploader/`; they are copied over automatically on first start.)

Windows SmartScreen may warn about the Setup.exe while releases aren't
code-signed (see [Code signing](#code-signing)). Choose "More info → Run
anyway", or just keep using `python run_editor.py`.

### Building it yourself

```bash
pip install -r requirements.txt pyinstaller
winget install JRSoftware.InnoSetup   # only needed for the Setup.exe
python build_exe.py
```

This creates, in `dist/`:

```text
dist/
├── StreetviewTourEditor/                        (the app: .exe + _internal/)
├── StreetviewTourEditor-<version>-portable.zip
├── StreetviewTourEditor-<version>-Setup.exe     (only if Inno Setup is installed)
└── SHA256SUMS.txt                               (checked by the in-app updater)
```

The version comes from `git describe`; pass `--version 1.2.0` to override it,
`--no-installer` to skip the Setup.exe, or `--console` for a build with a
console window (handy for debugging; the normal build logs to
`%APPDATA%\StreetviewTourEditor\editor.log`). The installer itself is defined in
`installer/StreetviewTourEditor.iss`. If a rebuild fails with a file-in-use /
permission error, the previous `.exe` is still running - close it and rerun.

### Building via GitHub Actions

`.github/workflows/build-release.yml` builds everything on a Windows runner:

- **Cut a release:** `git tag v1.2.0 && git push origin v1.2.0` - the workflow
  builds and publishes a Release with the Setup.exe, the portable zip and
  `SHA256SUMS.txt` attached, plus auto-generated release notes. Installed
  copies offer the update on their next start.
- **Test the build without releasing:** open the **Actions** tab → *Build and
  Release* → **Run workflow**. The same files are attached to the run as a
  downloadable artifact, without creating a Release.

No secrets are needed for this. Two optional extras are switched on by adding
repository secrets:

### Code signing

Unsigned installers trigger Windows SmartScreen's "unknown publisher" warning.
To sign, set `SIGN_CMD` to a command that signs one file, with `{file}` where
the path goes - `build_exe.py` signs the app, and Inno Setup signs the
Setup.exe and uninstaller with the same command:

```bat
set SIGN_CMD=signtool sign /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 /a "{file}"
python build_exe.py
```

In GitHub Actions the workflow does this with
[Azure Trusted Signing](https://learn.microsoft.com/azure/trusted-signing/)
(a low-cost way to get a publicly trusted signature) once these secrets
exist: `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`,
`TRUSTED_SIGNING_ENDPOINT`, `TRUSTED_SIGNING_ACCOUNT`, `TRUSTED_SIGNING_PROFILE`.

### winget

To make `winget install F0uP.StreetviewTourEditor` work, submit the first
version by hand once, then add a `WINGET_TOKEN` secret so each release is
submitted automatically - see [packaging/winget/README.md](packaging/winget/README.md).

## Notes

- Uploaded photos are public under your Google account
- Google Maps updates can take minutes to days depending on processing state
- Do not commit `client_secrets.json` or `token.json`

## License

MIT
