# SnapFrame

A lightweight Windows screenshot and annotation tool. Capture with PrintScreen or Ctrl+Alt+A, annotate, then copy or save locally.

## Run

Requires Windows and Python 3 with Tkinter.

```powershell
py -m pip install -r requirements.txt
py snapframe.py
```

## Build

```powershell
py -m pip install pyinstaller
powershell -ExecutionPolicy Bypass -File .\build.ps1
```

The installer is written to `dist/`. No administrator rights are required.

Version 0.2.0. Rebranded from SnapLite. Settings use `%APPDATA%\SnapFrame`; old SnapLite settings are not migrated automatically.

Windows execution and installer validation must be performed on Windows. Old compiled binaries are intentionally excluded.
