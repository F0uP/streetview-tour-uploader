# 360 Tour Uploader for Google Street View

Upload equirectangular 360 photos from cameras such as RICOH THETA, Insta360, and GoPro MAX to Google Street View, with a browser-based editor for map layout, connections, heading alignment, and local walkthrough preview.

## Features

- Browser-based tour editor built on OpenStreetMap and Leaflet
- Drag and drop 360 JPG files with automatic GPS placement from EXIF/XMP
- Manual heading alignment with pano preview and EXIF reset
- In-editor walkthrough between linked nodes
- Overlay compare view for visually checking orientation between two nodes
- Save / open the whole project, photos included, as a single `.vrtour` file (a plain zip)
- Export to `tour-config.json`, `tour-viewer.html`, and `tour.csv`
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
├── LICENSE
├── README.md
├── requirements.txt
├── build_exe.py
└── run_editor.py
```

## Requirements

- Python 3.9+ (not needed for the [standalone .exe](#optional-standalone-windows-app-exe))
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
and a "One-time setup" form appears the first time — paste in the Client ID
and Client Secret and click **Save & Continue**. The app writes
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

Blur faces and license plates before upload. The Street View Publish API does not blur them automatically.

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
- Select a node to edit heading, compare with neighbors, and test the walkthrough

Running Windows without Python installed? See [Optional: standalone .exe](#optional-standalone-windows-app-exe) below — same editor, no `pip install` needed.

### 3. Upload to Google Street View

Click **Upload to Google** in the toolbar (`run_editor.py` must be running):

- First time only: paste your Client ID / Client Secret into the setup form — see [one-time Google setup](#one-time-google-setup)
- Click **Sign in with Google** — a normal Google login/consent screen opens in your browser
- Optionally paste a Google Place ID to link the tour to a location
- Click **Start Upload** — each photo is uploaded, then connections between linked nodes are published; progress is shown live in the log

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

## Optional: standalone Windows app (.exe)

For a version you can run without Python installed - your own PC, or to hand
to someone else - grab a build from **[Releases](../../releases)**, or build
it yourself:

```bash
pip install pyinstaller
python build_exe.py
```

### Building via GitHub Actions

`.github/workflows/build-release.yml` builds the `.exe` on a Windows runner
and attaches it to a GitHub Release automatically:

- **Cut a release:** `git tag v1.0.1 && git push origin v1.0.1` - the
  workflow builds and publishes a Release with `StreetviewTourEditor-windows.zip`
  attached and auto-generated release notes.
- **Test the build without releasing:** open the **Actions** tab → *Build and
  Release* → **Run workflow**. This builds the same zip and attaches it to
  the run as a downloadable artifact, without creating a Release.

No secrets need to be configured for this - the build doesn't embed a Google
API key (see below), so there's nothing sensitive to add to the repo's
Actions secrets.

This creates:

```text
dist/
├── editor/                     (the editor, refreshed on every build)
├── uploader/                    (empty at first - filled in from the UI)
└── StreetviewTourEditor/
    ├── StreetviewTourEditor.exe
    └── _internal/               (PyInstaller runtime files - leave alone)
```

**Where the Google API key goes:** nowhere at build time — the `.exe` is
generic and doesn't need one to be built, so a plain `git clone` + `python
build_exe.py` produces a working app with no secrets baked in. Whoever runs
`StreetviewTourEditor.exe` enters their own Client ID / Client Secret through
the same **Upload to Google → one-time setup form** described
[above](#one-time-google-setup), the first time they click **Upload to
Google**. The app writes it to `dist/uploader/client_secrets.json` - one
level above the `.exe`, as a sibling of the `StreetviewTourEditor/` folder,
not inside it. That's deliberate: PyInstaller deletes and recreates
everything inside `StreetviewTourEditor/` on every build, so anything placed
there would be lost the next time someone ran `build_exe.py`. Keeping
`uploader/` one level up means rebuilding never touches anyone's sign-in, and
the same `.exe` can be handed to several people, each entering their own key
without touching the build.

Double-click `StreetviewTourEditor.exe` to run it - it opens a console
window (leave it open; closing it stops the server) and launches the editor
in your browser, exactly like `python run_editor.py`. A black console
window with no other UI is expected, not an error.

Windows SmartScreen or your antivirus may flag a freshly built `.exe` as
unrecognized since it isn't code-signed - this is a common false positive
for small PyInstaller tools, not a sign anything is wrong. Choose "More
info -> Run anyway", or just keep using `python run_editor.py` if you'd
rather not deal with it.

Rerun `python build_exe.py` any time after changing the code to rebuild -
`uploader/client_secrets.json` / `token.json` survive since they live outside
the folder PyInstaller manages. If a rebuild fails with a file-in-use /
permission error, the previous `.exe` is still running or was only just
closed - close it (or wait a couple seconds) and run the build again.

## Notes

- Uploaded photos are public under your Google account
- Google Maps updates can take minutes to days depending on processing state
- Do not commit `client_secrets.json` or `token.json`

## License

MIT
