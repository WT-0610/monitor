"""Packaged entry point for Windows and macOS."""
from __future__ import annotations

import sys




if __name__ == "__main__":
    requested = sys.argv[1:]
    if sys.platform == "win32" and len(requested) == 2 and requested[0] == "--desktop-window":
        from desktop_window import open_window
        open_window(requested[1])
        raise SystemExit(0)
    from api_server import main as run_server
    sys.argv = [sys.argv[0], *(requested or ["--open"])]
    raise SystemExit(run_server())
