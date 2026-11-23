# -*- coding: utf-8 -*-
"""媒体下载器：并发抓取图片与视频到本地。"""

from __future__ import annotations

import os
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List
from urllib.parse import unquote, urlparse

_EXT_RE = re.compile(r"\.([a-zA-Z0-9]{2,5})(?:[?#]|$)")

DEFAULT_IMAGE_EXT = "jpg"
DEFAULT_VIDEO_EXT = "mp4"


def guess_ext(url: str, default: str) -> str:
    """从 URL 推断扩展名。"""
    path = unquote(urlparse(url).path)
    match = _EXT_RE.search(path)
    if match:
        return match.group(1).lower()
    return default


class MediaDownloader:
    """把一条微博的图片 / 视频保存到本地目录。"""

    def __init__(self, client, cfg: Dict[str, Any], logger=None, vault=None):
        self.client = client
        self.cfg = cfg
        self.logger = logger
        self.vault = vault
        self.workers = max(1, int(cfg.get("media_workers", 4)))

    def _log(self, level: str, msg: str) -> None:
        if self.logger:
            self.logger(level, msg)

    # ------------------------------------------------------------------ #
    def download_all(self, mblog_id: str, images: List[Dict[str, str]],
                     videos: List[Dict[str, str]], dest_dir: str) -> List[Dict[str, Any]]:
        """下载整条微博的媒体，返回结果列表（含本地路径与状态）。"""
        os.makedirs(dest_dir, exist_ok=True)
        want_images = bool(self.cfg.get("download_image", True))
        want_videos = bool(self.cfg.get("download_video", True))

        tasks: List[Dict[str, Any]] = []
        idx = 0
        for item in images:
            if not want_images:
                break
            idx += 1
            ext = guess_ext(item["url"], DEFAULT_IMAGE_EXT)
            tasks.append({
                "kind": "image",
                "url": item["url"],
                "path": os.path.join(dest_dir, f"img_{idx:03d}.{ext}"),
            })
        vidx = 0
        for item in videos:
            if not want_videos:
                break
            vidx += 1
            ext = guess_ext(item["url"], DEFAULT_VIDEO_EXT)
            prefix = "livephoto" if item.get("kind") == "livephoto_video" else "video"
            tasks.append({
                "kind": item.get("kind", "video"),
                "url": item["url"],
                "path": os.path.join(dest_dir, f"{prefix}_{vidx:03d}.{ext}"),
            })

        if not tasks:
            return []

        results: List[Dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=min(self.workers, len(tasks))) as pool:
            futures = [pool.submit(self._one, mblog_id, task) for task in tasks]
            for future in futures:
                results.append(future.result())
        return results

    def _one(self, mblog_id: str, task: Dict[str, Any]) -> Dict[str, Any]:
        url, path, kind = task["url"], task["path"], task["kind"]
        # 已存在且非空则跳过，避免重复下载
        if os.path.exists(path) and os.path.getsize(path) > 0:
            return {
                "mblog_id": mblog_id, "kind": kind, "url": url, "path": path,
                "size": os.path.getsize(path), "status": "skipped",
            }
        try:
            if self.cfg.get("encrypt_downloads") and (not self.vault or not self.vault.unlocked):
                raise ValueError("文件加密未解锁，请先在设置中输入主密码")
            size = self.client.download(url, path)
            if self.cfg.get("encrypt_downloads"):
                path = self.vault.encrypt_file(path)
                size = os.path.getsize(path)
            status = "ok"
            self._log("INFO", f"  已保存 {os.path.basename(path)} ({size/1024:.0f} KB)")
        except Exception as exc:  # noqa: BLE001
            if self.cfg.get("encrypt_downloads") and os.path.isfile(path):
                try:
                    os.remove(path)
                except OSError:
                    pass
            size = 0
            status = f"failed: {exc}"
            self._log("WARNING", f"  下载失败 {url} -> {exc}")
        return {
            "mblog_id": mblog_id, "kind": kind, "url": url, "path": path,
            "size": size, "status": status,
        }
