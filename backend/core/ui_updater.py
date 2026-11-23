"""Verified GitHub Release updates for the three browser UI files only."""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import tempfile
import threading
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

import requests


FILES = {"index.html", "app.js", "app.css"}
MAX_BYTES = 25 * 1024 * 1024
REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
TAG = re.compile(r"^ui-v(\d+)\.(\d+)\.(\d+)$")
_install_lock = threading.Lock()


class UIUpdater:
    def __init__(self, dist: str, state: str, repo: str = ""):
        self.dist = Path(dist)
        self.state = Path(state)
        self.repo = repo.strip()

    def current(self) -> str:
        try:
            return str(json.loads(self.state.read_text(encoding="utf-8"))["tag"])
        except (OSError, ValueError, KeyError, TypeError):
            try:
                version = json.loads((self.dist.parent / "package.json").read_text(encoding="utf-8"))["version"]
                if re.fullmatch(r"\d+\.\d+\.\d+", str(version)):
                    return f"ui-v{version}"
            except (OSError, ValueError, KeyError, TypeError):
                pass
            return "ui-v0.1.0"

    def _release(self) -> dict:
        if not REPO.fullmatch(self.repo):
            raise ValueError("请先填写 GitHub 仓库地址")
        url = f"https://api.github.com/repos/{self.repo}/releases?per_page=20"
        response = requests.get(url, headers={"Accept": "application/vnd.github+json"}, timeout=20)
        if response.status_code in (403, 429):
            return self._atom_release()
        response.raise_for_status()
        releases = response.json()
        candidates = [release for release in releases
                      if not release.get("draft") and not release.get("prerelease")
                      and TAG.fullmatch(str(release.get("tag_name") or ""))]
        return max(candidates, key=lambda release: tuple(map(int, TAG.fullmatch(release["tag_name"]).groups()))) if candidates else {}

    def _atom_release(self) -> dict:
        response = requests.get(f"https://github.com/{self.repo}/releases.atom", timeout=20)
        response.raise_for_status()
        root = ET.fromstring(response.content)
        tags = []
        for entry in root.findall("{http://www.w3.org/2005/Atom}entry"):
            link = entry.find("{http://www.w3.org/2005/Atom}link")
            href = str(link.get("href") or "") if link is not None else ""
            tag = href.rsplit("/releases/tag/", 1)[-1] if "/releases/tag/" in href else ""
            if TAG.fullmatch(tag):
                tags.append(tag)
        return {"tag_name": max(tags, key=lambda value: tuple(map(int, TAG.fullmatch(value).groups()))),
                "_atom": True} if tags else {}

    def check(self) -> dict:
        release = self._release()
        if not release:
            return {"current": self.current(), "latest": "", "available": False, "notes": ""}
        tag = str(release["tag_name"])
        if release.get("_atom"):
            base = f"https://github.com/{self.repo}/releases/download/{tag}/"
            checksum = requests.get(base + "ui-update.sha256", timeout=20)
            checksum.raise_for_status()
            digest = checksum.text.strip().split()[0].lower()
            if not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError("更新包校验文件格式不对")
            current = self.current()
            current_match = TAG.fullmatch(current)
            return {"current": current, "latest": tag,
                    "available": bool(current_match and tuple(map(int, TAG.fullmatch(tag).groups())) >
                                      tuple(map(int, current_match.groups()))),
                    "notes": "", "asset": {"url": base + "ui-update.zip", "digest": digest, "size": 0}}
        asset = next((a for a in release.get("assets", []) if a.get("name") == "ui-update.zip"), None)
        if not asset:
            raise ValueError("这个版本缺少界面更新包")
        digest = str(asset.get("digest") or "")
        if not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest):
            raise ValueError("更新包没有 SHA-256 校验信息")
        size = int(asset.get("size") or 0)
        if not 0 < size <= MAX_BYTES:
            raise ValueError("更新包大小不符合要求")
        url = str(asset.get("browser_download_url") or "")
        if not url.startswith(f"https://github.com/{self.repo}/releases/download/"):
            raise ValueError("更新包地址不属于指定仓库")
        current = self.current()
        current_match = TAG.fullmatch(current)
        available = bool(current_match and tuple(map(int, TAG.fullmatch(tag).groups())) >
                         tuple(map(int, current_match.groups())))
        return {"current": current, "latest": tag, "available": available,
                "notes": str(release.get("body") or "")[:500],
                "asset": {"url": url, "digest": digest[7:].lower(), "size": size}}

    @staticmethod
    def _validate_zip(data: bytes) -> dict[str, bytes]:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = archive.infolist()
            if {m.filename for m in members} != FILES or len(members) != len(FILES):
                raise ValueError("更新包文件不完整或包含多余文件")
            if any(m.is_dir() or m.file_size > MAX_BYTES for m in members):
                raise ValueError("更新包内容超出限制")
            if sum(m.file_size for m in members) > MAX_BYTES:
                raise ValueError("更新包解压后超出限制")
            return {name: archive.read(name) for name in FILES}

    def install(self, expected_tag: str) -> dict:
        if not _install_lock.acquire(blocking=False):
            raise ValueError("已有更新正在安装")
        try:
            return self._install(expected_tag)
        finally:
            _install_lock.release()

    def _install(self, expected_tag: str) -> dict:
        info = self.check()
        if not info["available"] or info["latest"] != expected_tag:
            raise ValueError("版本已变化，请重新检查更新")
        asset = info["asset"]
        with requests.get(asset["url"], stream=True, timeout=(15, 60)) as response:
            response.raise_for_status()
            data = bytearray()
            for chunk in response.iter_content(1024 * 1024):
                data.extend(chunk)
                if len(data) > MAX_BYTES:
                    raise ValueError("更新包超过大小限制")
        if hashlib.sha256(data).hexdigest() != asset["digest"]:
            raise ValueError("更新包校验失败，请重新下载")
        files = self._validate_zip(data)
        parent = self.dist.parent
        stage = Path(tempfile.mkdtemp(prefix=".ui-stage-", dir=parent))
        previous = parent / ".ui-previous"
        try:
            for name, content in files.items():
                (stage / name).write_bytes(content)
            if previous.exists():
                shutil.rmtree(previous)
            self.dist.replace(previous)
            try:
                stage.replace(self.dist)
                self.state.parent.mkdir(parents=True, exist_ok=True)
                self.state.write_text(json.dumps({"tag": expected_tag}), encoding="utf-8")
            except Exception:
                if self.dist.exists():
                    shutil.rmtree(self.dist)
                previous.replace(self.dist)
                raise
        finally:
            if stage.exists():
                shutil.rmtree(stage)
        return {"installed": expected_tag, "reload": True}
