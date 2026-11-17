@echo off
rem Double-click to build the Windows app. See build_windows.ps1 for the steps.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0build_windows.ps1" %*
pause
