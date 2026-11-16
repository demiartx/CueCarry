"""Launcher used by PyInstaller and by `python run_app.py` (same as `python -m app`)."""
import sys

from app.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
