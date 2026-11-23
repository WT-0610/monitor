# -*- coding: utf-8 -*-
"""Own isolated Edge profiles for Weibo and X sign-in."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

import websocket

from core.client import WeiboClient


def _edge_path() -> str:
    for candidate in (
        Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("PROGRAMFILES", "")) / "Microsoft/Edge/Application/msedge.exe",
    ):
        if candidate.is_file():
            return str(candidate)
    found = shutil.which("msedge")
    if found:
        return found
    raise RuntimeError("找不到 Microsoft Edge，请先安装 Edge")


class WeiboLoginManager:
    def __init__(self, ctx, platform="weibo"):
        self.ctx = ctx
        self.platform = platform
        base = os.environ.get("LOCALAPPDATA") or ctx.monitor.output_dir
        self.profile = Path(base) / "OrangeMonitor" / f"{platform}-login"
        self._lock = threading.RLock()
        self._thread = None
        self._process = None
        self._cancel = threading.Event()
        self._last_attempt = 0.0
        self._state = "idle"
        self._message = ""
        self._updated_at = 0.0

    def status(self):
        with self._lock:
            return {"state": self._state, "message": self._message,
                    "updated_at": self._updated_at,
                    "profile_ready": (self.profile / "Default").exists()}

    def start(self):
        with self._lock:
            if self._thread and self._thread.is_alive():
                return self.status()
            self._cancel.clear()
            self._state = "opening"
            self._message = f"正在打开{'X' if self.platform == 'x' else '微博'}登录窗口"
            self._thread = threading.Thread(target=self._run, args=(False,), daemon=True,
                                            name=f"{self.platform}-login")
            self._thread.start()
            return self.status()

    def refresh_async(self):
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            if not (self.profile / "Default").exists():
                self._state = "needs_login"
                self._message = f"登录已过期，请打开{'X' if self.platform == 'x' else '微博'}登录窗口"
                return
            if time.time() - self._last_attempt < 1800:
                return
            self._last_attempt = time.time()
            self._cancel.clear()
            self._state = "refreshing"
            self._message = "正在尝试更新登录状态"
            self._thread = threading.Thread(target=self._run, args=(True,), daemon=True,
                                            name=f"{self.platform}-cookie-refresh")
            self._thread.start()

    def cancel(self):
        self._cancel.set()
        proc = self._process
        if proc and proc.poll() is None:
            proc.terminate()
        with self._lock:
            self._state = "idle"
            self._message = "已关闭登录窗口"

    def _set(self, state, message):
        with self._lock:
            if not self._cancel.is_set():
                self._state = state
                self._message = message

    def _run(self, headless):
        proc = None
        try:
            self.profile.mkdir(parents=True, exist_ok=True)
            active_port = self.profile / "DevToolsActivePort"
            active_port.unlink(missing_ok=True)
            args = [_edge_path(), f"--user-data-dir={self.profile}",
                    "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=0",
                    "--no-first-run", "--no-default-browser-check"]
            if self.platform == "weibo":
                args.append("--no-proxy-server")
            else:
                proxy = self.ctx.nodes.active_proxy()[1] or str(self.ctx.cfg.get("x_proxy") or "")
                if proxy:
                    args.append(f"--proxy-server={proxy}")
            if headless:
                args.extend(["--headless=new", "--disable-gpu"])
            args.append("https://x.com/login" if self.platform == "x" else "https://weibo.com/")
            proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self._process = proc
            deadline = time.monotonic() + 20
            while not active_port.is_file() and time.monotonic() < deadline:
                if self._cancel.wait(0.3):
                    return
                if proc.poll() is not None:
                    raise RuntimeError(f"{'X' if self.platform == 'x' else '微博'}登录窗口未能启动")
            if not active_port.is_file():
                raise RuntimeError(f"无法连接{'X' if self.platform == 'x' else '微博'}登录窗口")
            port = int(active_port.read_text(encoding="utf-8").splitlines()[0])
            if not headless:
                self._set("waiting", f"请在打开的窗口登录{'X' if self.platform == 'x' else '微博'}")
            end = time.monotonic() + (90 if headless else 600)
            last_cookie = ""
            while time.monotonic() < end and not self._cancel.is_set():
                if proc.poll() is not None:
                    raise RuntimeError(f"{'X' if self.platform == 'x' else '微博'}登录窗口已关闭")
                try:
                    cookie = self._cookies(port, self.platform)
                    ready = ("auth_token=" in cookie and "ct0=" in cookie) if self.platform == "x" else "SUB=" in cookie
                    if ready and cookie != last_cookie:
                        last_cookie = cookie
                        if self.platform == "x":
                            with self.ctx._cfg_lock:
                                self.ctx.cfg["x_cookie"] = cookie
                                self.ctx._save_cfg()
                                self.ctx.xmon.reload(self.ctx.cfg)
                            if any(a.get("platform") == "x" and a.get("enabled") for a in self.ctx.cfg.get("accounts", [])):
                                self.ctx.xmon.check_now()
                            self._updated_at = time.time()
                            self._set("ready", "X 登录信息已保存，请测试连接")
                            self.ctx.log.emit("INFO", "X Cookie 已从应用登录窗口更新")
                            return
                        uid = next((str(a.get("uid")) for a in self.ctx.cfg.get("accounts", [])
                                    if a.get("platform", "weibo") == "weibo" and a.get("enabled")), "")
                        if uid:
                            client = WeiboClient(cookie, timeout=10, retry=1)
                            client.fetch_page(uid, page=1)
                        with self.ctx._cfg_lock:
                            self.ctx.cfg["cookie"] = cookie
                            self.ctx._save_cfg()
                            self.ctx.monitor.reload(self.ctx.cfg)
                        if uid:
                            self.ctx.monitor.check_now()
                        self._updated_at = time.time()
                        self._set("ready", "微博登录状态已更新" if uid else "登录信息已保存，可以添加监控账号")
                        self.ctx.log.emit("INFO", "微博 Cookie 已从应用登录窗口更新")
                        return
                except Exception:
                    pass  # 登录中的跳转和验证码页面还没有有效 Cookie
                self._cancel.wait(3)
            name = "X" if self.platform == "x" else "微博"
            self._set("needs_login", f"登录仍未完成，请重新打开{name}登录窗口" if not headless
                      else f"自动更新失败，请重新登录{name}")
        except Exception as exc:
            self._set("error", str(exc))
        finally:
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
            self._process = None

    @staticmethod
    def _cookies(port: int, platform: str = "weibo") -> str:
        # Only speak to the isolated browser we launched on loopback.
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=3) as response:
            endpoint = json.load(response)["webSocketDebuggerUrl"]
        ws = websocket.create_connection(endpoint, timeout=12, suppress_origin=True, http_no_proxy=["127.0.0.1", "localhost"])
        try:
            ws.send(json.dumps({"id": 1, "method": "Browser.getVersion"}))
            ws.send(json.dumps({"id": 2, "method": "Storage.getCookies"}))
            while True:
                message = json.loads(ws.recv())
                if message.get("id") == 2:
                    cookies = message.get("result", {}).get("cookies", [])
                    domains = ("x.com", "twitter.com") if platform == "x" else ("weibo.com",)
                    selected = {item["name"]: item["value"] for item in cookies
                                if any(str(item.get("domain", "")).lstrip(".") == domain
                                       or str(item.get("domain", "")).lstrip(".").endswith("." + domain)
                                       for domain in domains)
                                and item.get("name") and item.get("value")}
                    return "; ".join(f"{name}={value}" for name, value in selected.items())
        finally:
            ws.close()
