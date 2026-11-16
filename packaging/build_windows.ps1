# build_windows.ps1 - builds the Windows app folder and a zip to share.
#
# Run from anywhere:  double-click packaging\build_windows.bat
#           or:       powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
#
# Result:  dist\CueCarry\                 the app (run CueCarry.exe)
#          dist\CueCarry-<ver>-windows.zip the same folder, zipped for sharing
#
# Steps (each one prints a line as it starts):
#   1. Check Python
#   2. Make a private Python environment (.venv-build) and install the packages
#   3. Download an LGPL build of ffmpeg (only the first time)
#   4. Check that this ffmpeg can make MP3s
#   5. Run the tests
#   6. Build with PyInstaller
#   7. Add the license files and zip the result

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
function Step($n, $text) { Write-Host "`n[$n/7] $text" -ForegroundColor Cyan }

# --- 1. Python -----------------------------------------------------------------
Step 1 "Checking Python"
if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
    throw "Python not found. Install it with:  winget install Python.Python.3.12  then open a new window."
}
py -3 -c "import sys; assert sys.version_info >= (3, 10), sys.version"
if ($LASTEXITCODE -ne 0) { throw "Python 3.10 or newer is needed." }

# --- 2. Private environment ------------------------------------------------------
Step 2 "Installing packages into .venv-build"
if (-not (Test-Path ".venv-build\Scripts\python.exe")) { py -3 -m venv .venv-build }
$Py = Join-Path $Root ".venv-build\Scripts\python.exe"
& $Py -m pip install --quiet --upgrade pip
& $Py -m pip install --quiet -r requirements.txt -r requirements-dev.txt
if ($LASTEXITCODE -ne 0) { throw "pip install failed." }

# --- 3. ffmpeg (LGPL build from BtbN/FFmpeg-Builds) -----------------------------
Step 3 "Getting ffmpeg"
$FfDir = Join-Path $Root "packaging\ffmpeg"
$FfExe = Join-Path $FfDir "ffmpeg.exe"
if (-not (Test-Path $FfExe)) {
    New-Item -ItemType Directory -Force $FfDir | Out-Null
    $rel = Invoke-RestMethod "https://api.github.com/repos/BtbN/FFmpeg-Builds/releases/latest" `
        -Headers @{ "User-Agent" = "cuecarry-build" }
    # Prefer a release-branch build (ffmpeg-n7.x...), else the master build. Never "shared".
    $assets = $rel.assets | Where-Object { $_.name -like "*win64-lgpl*.zip" -and $_.name -notlike "*shared*" }
    $asset = ($assets | Where-Object { $_.name -like "ffmpeg-n*" } | Sort-Object name -Descending | Select-Object -First 1)
    if (-not $asset) { $asset = $assets | Select-Object -First 1 }
    if (-not $asset) { throw "No win64 LGPL ffmpeg build found in the latest BtbN release." }
    Write-Host "  Downloading $($asset.name)"
    $zip = Join-Path $env:TEMP $asset.name
    Invoke-WebRequest $asset.browser_download_url -OutFile $zip
    $tmp = Join-Path $env:TEMP "cuecarry-ffmpeg"
    if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp }
    Expand-Archive $zip $tmp
    $top = Get-ChildItem $tmp | Select-Object -First 1
    Copy-Item (Join-Path $top.FullName "bin\ffmpeg.exe") $FfExe
    Copy-Item (Join-Path $top.FullName "LICENSE.txt") (Join-Path $FfDir "LICENSE.txt")
    @"
FFmpeg build: $($asset.name)
Release:      $($rel.tag_name)
Download:     $($asset.browser_download_url)
Build scripts (how this binary was made): https://github.com/BtbN/FFmpeg-Builds
FFmpeg source code: https://ffmpeg.org/download.html#get-sources
License: LGPL (see LICENSE.txt in this folder)
"@ | Set-Content (Join-Path $FfDir "SOURCE.txt") -Encoding UTF8
}

# --- 4. MP3 encoder check --------------------------------------------------------
Step 4 "Checking that ffmpeg has the MP3 encoder (libmp3lame)"
$enc = & $FfExe -hide_banner -encoders 2>$null | Out-String
if ($enc -notmatch "libmp3lame") {
    throw "This ffmpeg has no libmp3lame. Delete packaging\ffmpeg\ and put a build that has it there."
}

# --- 5. Tests --------------------------------------------------------------------
Step 5 "Running the tests"
$env:CUECARRY_FFMPEG = $FfExe
$env:QT_QPA_PLATFORM = "offscreen"
& $Py -m pytest -q
if ($LASTEXITCODE -ne 0) { throw "Tests failed; the app was not built." }
Remove-Item Env:QT_QPA_PLATFORM

# --- 6. PyInstaller ----------------------------------------------------------------
Step 6 "Building with PyInstaller"
& $Py -m PyInstaller --noconfirm --clean packaging\cuecarry.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed." }

# --- 7. Licenses and zip -------------------------------------------------------------
Step 7 "Adding licenses and zipping"
$App = "dist\CueCarry"
Copy-Item -Recurse -Force LICENSES "$App\LICENSES"
New-Item -ItemType Directory -Force "$App\LICENSES\ffmpeg" | Out-Null
Copy-Item packaging\ffmpeg\LICENSE.txt, packaging\ffmpeg\SOURCE.txt "$App\LICENSES\ffmpeg\"
Copy-Item README.md, LICENSE "$App\"
$ver = (Select-String -Path app\version.py -Pattern 'VERSION = "(.+)"').Matches[0].Groups[1].Value
$out = "dist\CueCarry-$ver-windows.zip"
if (Test-Path $out) { Remove-Item $out }
Compress-Archive "dist\CueCarry" $out
Write-Host "`nDone." -ForegroundColor Green
Write-Host "  App:  dist\CueCarry\CueCarry.exe"
Write-Host "  Zip:  $out"
