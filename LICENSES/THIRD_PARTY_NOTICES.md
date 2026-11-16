# Third-party software in this app

The packaged app (`MP3CopyForRekordbox.exe`) includes these components.

| Component | What it does here | License | Source code |
|---|---|---|---|
| FFmpeg (LGPL build by BtbN) | Decodes FLAC/WAV/AIFF/ALAC and encodes MP3 | LGPL 2.1 or later. The exact build and its license are in `LICENSES/ffmpeg/` | https://ffmpeg.org/download.html#get-sources and build scripts at https://github.com/BtbN/FFmpeg-Builds |
| LAME (inside FFmpeg) | MP3 encoder | LGPL 2 or later | https://lame.sourceforge.io |
| Qt 6 and Qt for Python (PySide6) | The window | LGPL 3 | https://code.qt.io and https://wiki.qt.io/Qt_for_Python |
| Python | Runs the app | PSF License | https://www.python.org/downloads/source/ |
| PyInstaller bootloader | Starts the app | GPL 2 with an exception that allows distributing built apps under any license | https://github.com/pyinstaller/pyinstaller |

**Replacing the LGPL libraries.** Qt's libraries ship as separate files in the `_internal` folder and ffmpeg as a separate program in `_internal/ffmpeg/`, so you can swap in your own builds, as the LGPL allows.

**Trademarks.** rekordbox is a trademark of AlphaTheta Corporation. This app is not affiliated with or endorsed by AlphaTheta.
