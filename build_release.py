"""Build a standalone desktop release on the host operating system."""
from __future__ import annotations

import json
import os
import re
import platform
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent


def check_source() -> None:
    template = json.loads((ROOT / "backend/config.example.json").read_text(encoding="utf-8"))
    for key in ("cookie", "x_cookie", "webdav_password", "subscription"):
        if template.get(key):
            raise SystemExit(f"Release template contains private {key}")
    if template.get("accounts") or template.get("output_dir"):
        raise SystemExit("Release template must start without accounts or archive path")
    forbidden = (ROOT / "backend/config.json", ROOT / "data", ROOT / "_backup", ROOT / "_proxy", ROOT / "x-monitor/public/media")
    for path in forbidden:
        if path.exists():
            raise SystemExit(f"Private path exists in release source: {path.relative_to(ROOT)}")
    for path in ROOT.rglob("*"):
        if not path.is_file() or path.name == "build_release.py" or any(part in {".git", "node_modules", "dist", "build", "release", "__pycache__"} for part in path.relative_to(ROOT).parts):
            continue
        if path.suffix.lower() in {".db", ".sqlite", ".log", ".bak", ".jpg", ".jpeg", ".png", ".mp4"}:
            raise SystemExit(f"Unexpected user data candidate: {path.relative_to(ROOT)}")
        if path.suffix.lower() in {".py", ".ts", ".tsx", ".json", ".md", ".mjs", ".yml", ".yaml"}:
            content = path.read_text(encoding="utf-8", errors="ignore")
            for marker in ("D:\\monitor\\data", "C:\\Users\\Administrator\\WorkBuddy", "@qq.com"):
                if marker in content:
                    raise SystemExit(f"Personal data marker in {path.relative_to(ROOT)}")
            if re.search(r"(?:auth_token|SUB)=[A-Za-z0-9]{20,}", content):
                raise SystemExit(f"Credential-like value in {path.relative_to(ROOT)}")


def main() -> None:
    check_source()
    subprocess.run(["npm.cmd" if os.name == "nt" else "npm", "ci"], cwd=ROOT / "x-monitor", check=True)
    subprocess.run(["npm.cmd" if os.name == "nt" else "npm", "run", "build"], cwd=ROOT / "x-monitor", check=True)
    args = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir", "--windowed",
        "--name", "OrangeMonitor", "--paths", "backend",
        "--add-data", "backend/config.example.json:.",
        "--add-data", "backend/models:models",
        "--add-data", "x-monitor/dist:x-monitor/dist",
    ]
    if not (sys.platform == "darwin" and platform.machine() == "x86_64"):
        args.extend(["--collect-all", "onnxruntime"])
    if os.name == "nt":
        args.extend(["--collect-all", "webview", "--add-data", "desktop_titlebar.js:."])
    args.append("desktop_launcher.py")
    subprocess.run(args, cwd=ROOT, check=True)


if __name__ == "__main__":
    main()
