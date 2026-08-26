# Powerpoint shenans

Themed desktop app that builds a snake-flow block diagram directly inside an
*already-open* PowerPoint window (via COM automation), not a separate file.
Round-trips too: it can pull a numbered list back out of the active slide so
you can edit and re-insert it.

## Requirements

- Windows + desktop PowerPoint (COM automation only)
- Python 3.10+ with `pywin32` installed: `pip install pywin32`

## Usage

1. Open PowerPoint with the presentation you want to draw on.
2. Run `snake_gui.py` (or double-click `run_gui.bat`).
3. Type one item per line, tweak the settings, click "Insert into PowerPoint".

## Building a standalone EXE

See [builder/](builder/) — double-click `builder/build.bat` to produce a
self-contained EXE in `builder/dist/`. No install of Python or `pywin32`
needed on the machine that runs the EXE.

**Sharing it:** the build is `--onedir` — the `.exe` needs its sibling
`_internal\` folder right next to it, so hand over the *whole* versioned
folder (e.g. `Powerpoint shenans v1.1.0\`), not just the `.exe` file.
If you share it via OneDrive/SharePoint, the recipient may hit
"Failed to load Python DLL ... python312.dll" if OneDrive hasn't fully
downloaded that folder yet (Files On-Demand placeholder) — right-click the
folder → "Always keep on this device" and wait for it to fully sync, or
copy it to a local (non-cloud-synced) folder before running.

## Layout

```
snake_gui.py   - entry point / GUI
theme.py       - shared Tk theme
run_gui.bat    - launches snake_gui.py with `python`
builder/       - PyInstaller-based EXE builder (see builder/build.bat)
```
