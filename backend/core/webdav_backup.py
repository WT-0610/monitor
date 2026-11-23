# -*- coding: utf-8 -*-
"""Bounded WebDAV backup for config and archive files; no automatic remote deletion."""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
import time
from contextlib import closing
from pathlib import Path
from urllib.parse import quote

import requests


class WebDAVBackup:
    def __init__(self, archive_root: str, config_path: str, cfg, log=None):
        self.root = Path(archive_root)
        self.config_path = Path(config_path)
        self.cfg = cfg
        self.log = log
        self.state_file = self.root / ".webdav-state.json"
        self.busy = False
        self.error = ""
        self.conflicts = []
        self.progress = ""
        self.last_sync = 0.0
        self._lock = threading.Lock()
        self._load_state()
        threading.Thread(target=self._scheduler, daemon=True, name="webdav-scheduler").start()

    def _load_state(self):
        try:
            state = json.loads(self.state_file.read_text(encoding="utf-8"))
            self.last_sync = float(state.get("last_sync") or 0)
        except (OSError, ValueError):
            pass

    def status(self):
        return {"configured": bool(self.cfg.get("webdav_url")), "busy": self.busy,
                "error": self.error, "progress": self.progress,
                "last_sync": self.last_sync, "conflicts": self.conflicts,
                "restore_pending": (self.root / ".webdav-restore" / "pending.json").is_file()}

    def start(self, resolutions=None):
        with self._lock:
            if self.busy:
                raise ValueError("同步正在运行")
            if not self.cfg.get("webdav_url"):
                raise ValueError("先填写 WebDAV 地址")
            self.busy = True
            self.error = ""
            self.progress = "准备文件"
            self.conflicts = []
        threading.Thread(target=self._run, args=(resolutions or {},), daemon=True, name="webdav-sync").start()

    def _scheduler(self):
        while True:
            time.sleep(60)
            if self.last_sync and time.time() - self.last_sync >= 6 * 3600 and not self.busy and not self.conflicts and self.cfg.get("webdav_url"):
                try:
                    self.start()
                except ValueError:
                    pass

    def _session(self):
        session = requests.Session()
        session.auth = (str(self.cfg.get("webdav_username") or ""), str(self.cfg.get("webdav_password") or ""))
        session.trust_env = False
        return session

    def _url(self, name=""):
        base = str(self.cfg.get("webdav_url") or "").strip().rstrip("/")
        if not base.startswith(("https://", "http://")):
            raise ValueError("WebDAV 地址必须以 https:// 或 http:// 开头")
        return base + "/OrangeArchive/" + "/".join(quote(part, safe="") for part in name.split("/") if part)

    def _request(self, session, method, name, **kw):
        for attempt in range(3):
            try:
                resp = session.request(method, self._url(name), timeout=(15, 120), **kw)
                break
            except requests.RequestException as exc:
                if attempt == 2 or not isinstance(exc, requests.ConnectionError):
                    raise RuntimeError(f"WebDAV {method} {name or 'OrangeArchive'} 连接中断：{exc}") from exc
                stream = kw.get("data")
                if hasattr(stream, "seek"):
                    stream.seek(0)
                time.sleep(attempt + 1)
        if resp.status_code == 401:
            raise ValueError("WebDAV 登录失败，请检查账号和密码")
        if resp.status_code == 403:
            raise ValueError(f"WebDAV 拒绝 {method} {name or 'OrangeArchive'}（403）；请检查网盘容量、流量限制和目录写入权限")
        return resp

    def _mkdir(self, session, folder):
        parts = [p for p in folder.split("/") if p]
        for i in range(len(parts) + 1):
            name = "/".join(parts[:i])
            resp = self._request(session, "MKCOL", name)
            if resp.status_code not in (200, 201, 204, 301, 302, 405):
                raise ValueError(f"WebDAV 无法创建目录：{name or 'OrangeArchive'}（{resp.status_code}）")

    def _local_files(self, snapshot):
        files = {"backend/config.json": self.config_path, "data/archive.db": Path(snapshot)}
        for p in self.root.rglob("*"):
            if (not p.is_file() or p.name in ("archive.db", "archive.db-wal", "archive.db-shm", ".webdav-state.json", ".ui-update.json", ".stop_flag")
                    or p.suffix in (".lock", ".pid", ".log") or p.name.endswith(".webdav-tmp")
                    or ".webdav-restore" in p.parts or ".thumbs" in p.parts):
                continue
            files["data/" + p.relative_to(self.root).as_posix()] = p
        return files

    def _target(self, name):
        parts = name.split("/")
        if any(part in ("", ".", "..") or "\\" in part or ":" in part for part in parts):
            raise ValueError("WebDAV 清单包含无效路径")
        if name == "backend/config.json":
            return self.config_path
        if parts[0] != "data" or len(parts) < 2:
            raise ValueError("WebDAV 清单包含归档目录之外的路径")
        target = self.root.joinpath(*parts[1:]).resolve()
        if not target.is_relative_to(self.root.resolve()):
            raise ValueError("WebDAV 清单包含归档目录之外的路径")
        return target

    @staticmethod
    def _fingerprint(path):
        st = path.stat()
        return {"size": st.st_size, "mtime_ns": st.st_mtime_ns}

    def _run(self, resolutions):
        try:
            self._sync(resolutions)
        except Exception as exc:
            self.error = f"{self.progress}：{exc}" if self.progress else str(exc)
            if self.log:
                self.log("WARNING", f"[WebDAV] {self.error}")
        finally:
            self.busy = False
            self.progress = ""

    def _sync(self, resolutions):
        with tempfile.TemporaryDirectory(prefix="orange-webdav-") as temp:
            snapshot = str(Path(temp) / "archive.db")
            with closing(sqlite3.connect(str(self.root / "archive.db"))) as src:
                with closing(sqlite3.connect(snapshot)) as dst:
                    src.backup(dst)
            files = self._local_files(snapshot)
            local = {name: self._fingerprint(path) for name, path in files.items()}
            try:
                state = json.loads(self.state_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                state = {"base": {}}
            base = state.get("base") or {}
            session = self._session()
            self._mkdir(session, "")
            resp = self._request(session, "GET", "manifest.json")
            if resp.status_code == 404:
                remote = {}
            elif resp.ok:
                remote = resp.json().get("files") or {}
            else:
                raise ValueError(f"WebDAV 无法读取清单（{resp.status_code}）")
            for name in remote:
                self._target(name)
            conflicts = []
            for name in set(local) & set(remote):
                old = base.get(name)
                if old is None and remote[name] != local[name]:
                    conflicts.append(name)
                elif old is not None and local[name] != old and remote[name] != old and remote[name] != local[name]:
                    conflicts.append(name)
            pending = [name for name in conflicts if resolutions.get(name) not in ("local", "remote")]
            if pending:
                self.conflicts = pending
                self.error = f"{len(pending)} 个文件两端都有改动，请逐项选择保留哪一份"
                return
            merged = dict(remote)
            total = len(local)
            for index, (name, path) in enumerate(files.items(), 1):
                self.progress = f"{index}/{total} · {name}"
                old = base.get(name)
                if resolutions.get(name) == "remote" or (old is not None and local[name] == old and remote.get(name) != old):
                    target = self._target(name)
                    if name in ("data/archive.db", "data/.media-vault.json"):
                        target = self.root / ".webdav-restore" / ("archive.db" if name == "data/archive.db" else "vault.json")
                        target.parent.mkdir(parents=True, exist_ok=True)
                    response = self._request(session, "GET", name, stream=True)
                    if not response.ok:
                        raise ValueError(f"下载失败：{name}（{response.status_code}）")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    temp_target = str(target) + ".webdav-tmp"
                    with open(temp_target, "wb") as out:
                        for chunk in response.iter_content(1024 * 1024):
                            out.write(chunk)
                    os.replace(temp_target, target)
                    if name in ("data/archive.db", "data/.media-vault.json"):
                        (self.root / ".webdav-restore" / "pending.json").write_text("{}", encoding="utf-8")
                        merged[name] = remote[name]
                    else:
                        merged[name] = self._fingerprint(target)
                elif name not in remote or local[name] != remote[name] or resolutions.get(name) == "local":
                    self._mkdir(session, name.rpartition("/")[0])
                    with open(path, "rb") as stream:
                        response = self._request(session, "PUT", name, data=stream)
                    if response.status_code not in (200, 201, 204):
                        raise ValueError(f"上传失败：{name}（{response.status_code}）")
                    merged[name] = local[name]
            for name in set(remote) - set(local):
                if name == "data/archive.db":
                    continue
                target = self._target(name)
                response = self._request(session, "GET", name, stream=True)
                if not response.ok:
                    raise ValueError(f"下载失败：{name}（{response.status_code}）")
                target.parent.mkdir(parents=True, exist_ok=True)
                temp_target = str(target) + ".webdav-tmp"
                with open(temp_target, "wb") as out:
                    for chunk in response.iter_content(1024 * 1024):
                        out.write(chunk)
                os.replace(temp_target, target)
            response = self._request(session, "PUT", "manifest.json", data=json.dumps({"files": merged}, ensure_ascii=False).encode("utf-8"))
            if response.status_code not in (200, 201, 204):
                raise ValueError(f"WebDAV 清单保存失败（{response.status_code}）")
            self.last_sync = time.time()
            self.conflicts = []
            self.error = ""
            self.state_file.write_text(json.dumps({"base": merged, "last_sync": self.last_sync}, ensure_ascii=False), encoding="utf-8")


def apply_pending_restore(root: str) -> bool:
    """Apply a user-selected remote database before any SQLite connection opens."""
    import shutil
    archive = Path(root)
    pending_dir = archive / ".webdav-restore"
    marker = pending_dir / "pending.json"
    if not marker.is_file():
        return False
    applied = False
    for incoming, current in ((pending_dir / "archive.db", archive / "archive.db"),
                              (pending_dir / "vault.json", archive / ".media-vault.json")):
        if not incoming.is_file():
            continue
        if current.is_file():
            backup = archive / f"{current.name}.before-webdav-{int(time.time())}"
            shutil.copy2(current, backup)
        os.replace(incoming, current)
        applied = True
    marker.unlink()
    return applied
