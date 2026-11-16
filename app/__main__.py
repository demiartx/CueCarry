# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 CueCarry contributors
"""
Entry point.  `python -m app` opens the window; with arguments it runs the
command line instead (python -m app collection.xml --compensate).
The packaged .exe starts here too.
"""
import sys


def main() -> int:
    if len(sys.argv) > 1:
        from .cli import main as cli_main
        return cli_main()
    from .gui import main as gui_main
    return gui_main()


if __name__ == "__main__":
    sys.exit(main())
