# PyInstaller build recipe. Run through packaging/build_windows.ps1, or:
#   pyinstaller --noconfirm --clean packaging/rbmp3.spec
#
# What:  Bundles Python, Qt, the app and ffmpeg into one folder:
#        dist/MP3CopyForRekordbox/ with two programs that share the same files:
#          MP3CopyForRekordbox.exe   the window (no console)
#          rbmp3-cli.exe             the command line (console)
# Why:   A folder build (not one big .exe) starts faster, is flagged less often
#        by antivirus, and keeps Qt's LGPL libraries replaceable, as the LGPL asks.
# If it goes wrong: "ffmpeg missing" means packaging/ffmpeg/ is empty; the build
#        script fills it.
import os
import sys

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
FF = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
ff_path = os.path.join(ROOT, "packaging", "ffmpeg", FF)
if not os.path.isfile(ff_path):
    raise SystemExit(f"ffmpeg missing: {ff_path} (run packaging/build_windows.ps1)")

datas = [(ff_path, "ffmpeg"), (os.path.join(ROOT, "LICENSES"), "LICENSES")]
for extra in ("LICENSE.txt", "SOURCE.txt"):
    p = os.path.join(ROOT, "packaging", "ffmpeg", extra)
    if os.path.isfile(p):
        datas.append((p, os.path.join("LICENSES", "ffmpeg")))

a = Analysis(
    [os.path.join(ROOT, "run_app.py")],
    pathex=[ROOT],
    datas=datas,
    excludes=["tkinter", "unittest", "pytest", "PySide6.QtNetwork", "PySide6.QtQml",
              "PySide6.QtQuick", "PySide6.QtPdf", "PySide6.QtSql", "PySide6.QtTest"],
)
pyz = PYZ(a.pure)

gui_exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="MP3CopyForRekordbox",
              console=False, icon=None)
cli_exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="rbmp3-cli", console=True)

COLLECT(gui_exe, cli_exe, a.binaries, a.datas, name="MP3CopyForRekordbox")
