"""Packaged entry point for Windows and macOS."""
from __future__ import annotations

import sys

from api_server import main as run_server


if __name__ == "__main__":
    requested = sys.argv[1:]
    sys.argv = [sys.argv[0], *(requested or ["--open"])]
    raise SystemExit(run_server())
