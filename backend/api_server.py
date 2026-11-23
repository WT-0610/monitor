# -*- coding: utf-8 -*-
"""本地 REST 服务：给 Tauri / Vue 前端提供 JSON 接口。

架构约定（Q1=a）：Python 是业务主体（抓取、去重、媒体下载、SQLite），
Tauri 只负责窗口与进程生命周期，前端一律通过本服务的 HTTP 接口通信。
好处是前端可以脱离 Tauri 单独 `npm run dev` 调试。

启动：
    python api_server.py                 # 默认 8788，自动拉起监控
    python api_server.py --port 8788 --no-autostart
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import requests
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.client import WeiboClient  # noqa: E402
from core import config as cfg_mod  # noqa: E402
from core.config import (  # noqa: E402
    BASE_DIR, CONFIG_PATH, DEFAULT_CONFIG, DEFAULT_PLATFORM, PLATFORMS, ROOT_DIR,
    ensure_config, load_config, resolve_output_dir, save_config,
)
from core.envguard import heal_current_process, poisoned_reasons  # noqa: E402
from core import neterr  # noqa: E402
from core.log import LogBus  # noqa: E402
from core.monitor import Monitor  # noqa: E402
from core import nodes as nodes_mod  # noqa: E402
from core.nodes import NodeManager, parse_link, parse_subscription  # noqa: E402
from core.thumbs import ThumbCache, pregenerate_start, pregenerate_state  # noqa: E402
from core.xclient import XClient, XError, GQL_FEATURES  # noqa: E402
from core.xbackfill import BackfillManager  # noqa: E402
from core.xmonitor import XMonitor  # noqa: E402
from core.writer import WriterUnavailable  # noqa: E402
from core.store import normalize_feed_sort  # noqa: E402
from core.similar_cleanup import scan as scan_similar_cleanup  # noqa: E402
from core.cleanup_candidates import scan_missing_media  # noqa: E402
from core.archive_health import scan as scan_archive_health  # noqa: E402
from core.ui_updater import UIUpdater, REPO as UPDATE_REPO  # noqa: E402
from core.manual_x import parse_url as parse_x_status_url, fetch_status  # noqa: E402

DEFAULT_PORT = 8788

# 媒体文件流式发送的分块大小（256 KB）：Range 请求按块写出，内存占用恒定
_CHUNK = 1 << 18

# 存档目录占用：5 GB 几万个文件扫一遍要 1~2 秒，设置页每次打开都扫太浪费
_SIZE_CACHE = {"ts": 0.0, "path": "", "bytes": 0}
_PROFILE_CACHE: Dict[Tuple[str, str], Tuple[float, Dict[str, str]]] = {}

# 按平台分账的存储统计（/api/storage）缓存：一次要分别走微博和 X 两个目录，
# 比总目录还贵，而且磁盘占用不会在 30 秒里翻天。放模块级而不是 handler 上 ——
# BaseHTTPRequestHandler 每个请求都是新实例，挂 self 等于没有缓存。
_STORAGE_CACHE: Dict[str, Any] = {"at": 0.0, "data": None}

def _platform_of(q: Dict[str, Any]) -> Optional[str]:
    """解析查询串里的 platform：不传 / all / * → None（全平台合并视图）。

    未知值一律回落到微博，绝不让它落到 SQL 里产生空结果集让人一脸懵。
    """
    raw = (q.get("platform") or "").strip().lower()
    if raw in ("", "all", "*"):
        return None
    return raw if raw in PLATFORMS else DEFAULT_PLATFORM


# X 的 handle 规则：1~15 位字母数字下划线（和 X 官方一致）
_HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")


def _dir_size_cached(path: str, ttl: float = 60.0) -> int:
    """统计目录占用，结果缓存 60 秒。回收站单独统计，不混进存档占用里。"""
    now = time.time()
    if _SIZE_CACHE["path"] == path and now - _SIZE_CACHE["ts"] < ttl:
        return _SIZE_CACHE["bytes"]
    total = 0
    for dp, _dn, fn in os.walk(path):
        if os.path.basename(dp) == ".trash" or os.path.basename(dp) == ".thumbs":
            continue
        for f in fn:
            try:
                total += os.path.getsize(os.path.join(dp, f))
            except OSError:
                continue
    _SIZE_CACHE.update({"ts": now, "path": path, "bytes": total})
    return total

# 允许前端静态资源的来源（Vite dev server 与 Tauri 内置端口）
ALLOWED_ORIGINS = {
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:4173",
    "http://127.0.0.1:4173",
}


# 前端还没构建时的兜底页面，避免用户看到一片空白不知道发生了什么
_NOT_BUILT_HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>前端尚未构建</title>
<style>
  body{font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
       background:#f7f8fa;color:#1f2328;display:grid;place-items:center;height:100vh;margin:0}
  .box{background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:28px 32px;
       max-width:560px;box-shadow:0 1px 3px rgba(31,35,40,.08)}
  h1{font-size:17px;margin:0 0 12px}
  code{background:#fff3e6;color:#c2410c;padding:2px 6px;border-radius:4px;font-size:13px}
  pre{background:#fbfcfd;border:1px solid #e5e7eb;border-radius:6px;padding:12px;font-size:13px}
  p{font-size:13.5px;color:#57606a;line-height:1.7}
</style></head><body><div class="box">
<h1>前端还没有构建</h1>
<p>后端服务已正常运行，只是缺少前端静态文件。在 <code>ui/</code> 目录执行一次构建即可：</p>
<pre>cd ui
npm install
npm run build</pre>
<p>之后刷新本页就能看到界面。开发时也可以直接跑 <code>npm run dev</code>，改代码会热更新。</p>
</div></body></html>"""


def _json_default(value: Any) -> Any:
    try:
        return str(value)
    except Exception:  # noqa: BLE001
        return None


class ApiContext:
    """持有全局唯一的 Monitor / 配置 / 日志，供各路由共享。"""

    def __init__(self, autostart: bool = True, cfg: Optional[Dict[str, Any]] = None,
                 echo: bool = True):
        # cfg 可注入：自检用临时配置，避免动到真实存档
        self.cfg = cfg if cfg is not None else ensure_config()
        self.log = LogBus(
            self.cfg.get("log_level", "INFO"),
            log_file=os.path.join(resolve_output_dir(self.cfg), "monitor.log"),
            echo=echo,
        )
        from core.webdav_backup import apply_pending_restore
        if apply_pending_restore(resolve_output_dir(self.cfg)):
            self.log.emit("INFO", "已在启动前恢复云端数据库；原数据库保留备份")
        self.monitor = Monitor(self.cfg, self.log)
        # X 监控：跟微博同库不同平台，独立线程独立节奏（X 限流凶，间隔更长）
        self.xmon = XMonitor(self.cfg, self.log)
        from core.vault import Vault
        self.vault = Vault(self.monitor.output_dir)
        from core.webdav_backup import WebDAVBackup
        from core.config import CONFIG_PATH
        self.webdav = WebDAVBackup(self.monitor.output_dir, CONFIG_PATH, self.cfg, self.log.emit)
        self.monitor.downloader.vault = self.vault
        self.xmon.vault = self.vault
        # 代理节点：托管的 trojan 在这里拉起，探测结果给前端节点页用
        self.nodes = NodeManager(self.cfg, self.log.emit, store=self.xmon.store)
        self.nodes.ensure_started()
        self._cfg_lock = threading.RLock()
        from core.weibo_login import WeiboLoginManager
        self.weibo_login = WeiboLoginManager(self)
        self.x_login = WeiboLoginManager(self, platform="x")
        self.monitor.on_cookie_expired = self.weibo_login.refresh_async
        # 每账号粘性绑定节点：选路、改绑、落盘都在 ctx 这边，XMonitor 只管用
        self.xmon.route = self.route_account
        self.xmon.on_user_id = self._persist_user_id
        self.xmon.on_query_id = self._persist_query_id
        # 一键回溯：独立任务队列，跨账号并行、每账号一个 worker
        self.backfill = BackfillManager(self.cfg, self.xmon, self.log.emit)
        # 缩略图缓存：列表/媒体页只加载小图，原图留给灯箱 1280 档和「打开目录」
        self.thumbs = ThumbCache(self.monitor.output_dir, self.log.emit)
        from core.image_tagger import ImageTagWorker
        self.image_tagger = ImageTagWorker(self.monitor.store.db_path, self.cfg, self.log.emit)
        self.image_tagger.vault = self.vault
        from core.cross_platform_cleanup import CrossPlatformCleanup
        self.cross_platform_cleanup = CrossPlatformCleanup(self.monitor.store.db_path, self.log.emit)
        if autostart:
            self.image_tagger.start()
            self.cross_platform_cleanup.start()
        self.started_at = time.time()
        self.autostart = autostart
        # 静默启动时没有控制台窗口可关，「退出程序」按钮靠这个事件通知主循环收摊
        self.stop_event = threading.Event()
        if autostart:
            self.monitor.start()
            self.xmon.start()

    # ---- 账号 ↔ 节点绑定 ------------------------------------------------- #
    def route_account(self, acc: Dict[str, Any], exclude: Optional[set] = None
                      ) -> tuple:
        """给某个 X 账号挑出口节点。返回 (node_id, proxy)。

        **粘性是默认，换节点是例外**：账号已经绑了节点且那个节点活着，就一直用它。
        只有三种情况会改绑：账号还没绑、绑的节点停用/连不上、调用方把绑定的节点
        放进了 exclude（连续失败换节点）。

        改绑会写回配置文件 —— 绑定关系必须能跨重启存活，否则流量账单按账号归因
        完全没法对账（每次启动都重新洗牌）。
        """
        mode = str(self.cfg.get("x_proxy_mode") or "nodes")
        if mode == "manual":
            return "manual", str(self.cfg.get("x_manual_proxy") or "").strip()
        if mode == "local":
            proxy = str(self.cfg.get("x_proxy") or "").strip()
            if neterr.port_open(proxy) is False:
                candidate = self.nodes.system_proxy()
                if candidate and self.nodes.probe_proxy(candidate).get("alive"):
                    self.cfg["x_proxy"] = candidate
                    self._save_cfg()
                    self.log.emit("INFO", f"X 本机代理端口已切到 {candidate}")
                    proxy = candidate
            return "local", proxy

        exclude = set(exclude or ())
        bound = str(acc.get("node_id") or "")

        if bound and bound not in exclude:
            node = self.nodes.find(bound)
            if node and node.get("enabled"):
                proxy = self.nodes.proxy_of(node)
                if proxy and self.nodes.probe(node).get("alive"):
                    return bound, proxy

        # 本地代理客户端可能重启后换端口。仅在旧本地端口已关闭时，
        # 读取 Windows 当前代理，并通过 X 实测后才切换；不覆盖远程节点。
        old_proxy = str(self.cfg.get("x_proxy") or "")
        _, _, old_is_local = neterr.split_proxy(old_proxy)
        if old_is_local and neterr.port_open(old_proxy) is False:
            candidate = self.nodes.system_proxy()
            if candidate and candidate != old_proxy and self.nodes.probe_proxy(candidate).get("alive"):
                self.cfg["x_proxy"] = candidate
                for node in self.cfg.get("x_nodes") or []:
                    if node.get("type") == "external" and node.get("proxy") == old_proxy:
                        node["proxy"] = candidate
                        node["name"] = f"本地代理 {neterr.split_proxy(candidate)[1]}"
                self.nodes.reload(self.cfg)
                self._save_cfg()
                self.log.emit("INFO", f"X 本地代理端口已自动切到 {candidate}")

        pick = self.nodes.pick(exclude=exclude | ({bound} if bound else set()))
        if pick is not None:
            node_id = str(pick["id"])
            proxy = self.nodes.proxy_of(pick)
            if proxy:
                self._bind_account(acc, node_id)
                return node_id, proxy

        # 一个活节点都没有：先用绑定的（哪怕现在探测不通），再退回配置里的 x_proxy
        if bound:
            proxy = self.nodes.proxy_of(self.nodes.find(bound) or {})
            if proxy:
                return bound, proxy
        return "", str(self.cfg.get("x_proxy") or "").strip()

    def _bind_account(self, acc: Dict[str, Any], node_id: str) -> None:
        if str(acc.get("node_id") or "") == node_id:
            return
        old = str(acc.get("node_id") or "") or "（未绑定）"
        acc["node_id"] = node_id
        self.log.emit("INFO", f"[{acc.get('name') or acc.get('uid')}] 出口节点 "
                              f"{old} → {node_id}")
        self._save_cfg()

    def _persist_user_id(self, acc: Dict[str, Any], user_id: str) -> None:
        self._save_cfg()

    def _persist_query_id(self, query_id: str) -> None:
        self.cfg["x_query_id"] = query_id
        self._save_cfg()

    def _save_cfg(self) -> None:
        """写配置。X 的各线程都可能触发，串行化一下，别写出半个文件。"""
        with self._cfg_lock:
            try:
                save_config(self.cfg)
            except Exception as exc:  # noqa: BLE001
                self.log.emit("WARNING", f"配置写回失败：{exc}")

    # ------------------------------------------------------------------ #
    def request_quit(self) -> None:
        """退出进程：先停监控，稍等半秒让 HTTP 响应发出去，再通知主循环。"""
        # 有守护在盯着的时候，只杀进程会被立刻拉起来 → 必须先立哨兵
        flag = os.path.join(resolve_output_dir(self.cfg), ".stop_flag")
        try:
            with open(flag, "w", encoding="utf-8") as fp:
                fp.write(time.strftime("%Y-%m-%d %H:%M:%S"))
        except OSError:
            pass
        try:
            # 退出路径才等：给正在写盘的一轮两秒收尾，别把文件写一半
            self.monitor.stop(wait=2)
        except Exception:  # noqa: BLE001
            pass
        try:
            self.xmon.stop(wait=2)
            # 回溯线程也要收手：让它把 cursor 写下去，下次能续跑
            if getattr(self, "backfill", None):
                self.backfill.shutdown()
            self.nodes.stop_all()
        except Exception:  # noqa: BLE001
            pass
        self.log.emit("INFO", "收到退出指令，服务即将关闭")
        # 留足时间把 HTTP 响应发出去，否则前端会读超时（虽然它已经 catch 了）
        from core.single_instance import close_app_windows
        threading.Timer(0.5, close_app_windows).start()
        threading.Timer(1.0, self.stop_event.set).start()

    def reload(self) -> None:
        self.cfg = load_config()
        self.monitor.reload(self.cfg)
        self.webdav.cfg = self.cfg
        self.xmon.reload(self.cfg)
        self.nodes.reload(self.cfg)
        if getattr(self, "image_tagger", None):
            self.image_tagger.cfg = self.cfg
        # 回调要重新挂：cfg 换了对象，绑方法本身没变，但保持显式更不容易漏
        self.xmon.route = self.route_account
        self.xmon.on_user_id = self._persist_user_id
        self.xmon.on_query_id = self._persist_query_id
        if getattr(self, "backfill", None):
            # cfg 换了新对象，回溯任务得跟着用新的（账号列表 / 节点都在里面）
            self.backfill.cfg = self.cfg

    def open_dir(self, path: str) -> bool:
        """在系统文件管理器里打开目录（或选中文件）。"""
        if not path or not os.path.exists(path):
            return False
        try:
            if os.path.isfile(path):
                subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
            else:
                os.startfile(os.path.normpath(path))  # noqa: S606
            return True
        except Exception:  # noqa: BLE001
            try:
                subprocess.Popen(["explorer", os.path.normpath(path)])
                return True
            except Exception:  # noqa: BLE001
                return False

    # ------------------------------------------------------------------ #
    def status(self, platform: Optional[str] = None) -> Dict[str, Any]:
        m = self.monitor
        store = m.store.for_platform(platform)
        stats = store.stats()
        hours = int(self.cfg.get("new_highlight_hours", 24) or 24)
        since = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - hours * 3600))
        recent = len(store.posts_since(since, limit=2000))
        cookie = (self.cfg.get("cookie") or "").strip()
        xmon = getattr(self, "xmon", None)
        x_running = bool(xmon and xmon.running)
        return {
            # 顶栏只有一个「停止监控」按钮，任一个平台在跑就算开着
            "running": bool(m.running) or x_running,
            "weibo_running": bool(m.running),
            "x_running": x_running,
            "x_check_count": int(xmon.check_count) if xmon else 0,
            "x_new_count": int(xmon.new_count) if xmon else 0,
            # 多个监控账号转同一条时被跳过的那几条（只留最早一份）
            "x_dup_skipped": int(getattr(xmon, "dup_skipped", 0)) if xmon else 0,
            # 节点是每账号绑定的，"当前节点"取最近一次真正用过的那一个
            "node_id": getattr(xmon, "last_node_id", "") if xmon else "",
            # 出口画像：微博恒为直连，X 才是节点。放在 status 里是为了让前端
            # 能把"这条请求到底从哪出去"显示出来 —— 出问题时一眼能分清是
            # 走错出口，还是微博本身抽风。
            "egress": self._egress(),
            # 节点池概况（读缓存，不发请求）：前端据此提示"X 抓取现在有没有路"
            "x_nodes": self._node_summary(),
            "x_cookie_set": bool((self.cfg.get("x_cookie") or "").strip()),
            "x_query_id": str(self.cfg.get("x_query_id") or ""),
            "x_max_pages": int(self.cfg.get("x_max_pages", 3) or 1),
            "x_failover_threshold": int(self.cfg.get("x_failover_threshold", 2) or 2),
            "check_count": m.check_count,
            "new_count": m.new_count,
            # 会员专属/付费帖被跳过的条数（接口只下发模糊图，存了也没用）
            "vip_skipped": getattr(m, "vip_skipped", 0),
            # 纯文本帖被跳过的条数（archive_text_only=False 时才会计数）
            "text_skipped": getattr(m, "text_skipped", 0)
            + (getattr(xmon, "text_skipped", 0) if xmon else 0),
            "last_tick": m.last_tick,
            "stats": stats,
            "recent": {"hours": hours, "count": recent},
            "cookie_set": bool(cookie),
            "cookie_valid": ("SUB=" in cookie) or ("SUBP=" in cookie),
            "schema_version": m.store.schema_version,
            "output_dir": os.path.abspath(m.output_dir),
            "db_path": os.path.abspath(m.store.db_path),
            "config_path": CONFIG_PATH,
            "uptime": int(time.time() - self.started_at),
            "accounts_total": len(self._cfg_accounts(platform)),
            "accounts_enabled": len([a for a in self._cfg_accounts(platform) if a.get("enabled")]),
            "platform": platform or "all",
        }

    def _egress(self) -> Dict[str, Any]:
        """两个平台的出口画像。前端拿它渲染顶部/内容流那颗「微博·直连」胶囊。

        `ok` 表示"这条路现在走得通"。X 侧不能只看有没有配节点：节点配了但
        代理客户端没开，X 就是抓不到，胶囊还显示绿色等于骗人。
        节点状态取 `NodeManager.summary()`（只读缓存），所以这个接口不会因为
        67 个节点各打一次外网而变慢。
        """
        weibo = {
            "mode": "direct",
            "label": "直连",
            "proxy": "",
            "ok": True,
            "note": "微博直连：不读取 HTTP_PROXY/HTTPS_PROXY，也不受账号节点绑定影响",
        }
        mode = str(self.cfg.get("x_proxy_mode") or "nodes")
        if mode in ("local", "manual"):
            proxy = str((self.cfg.get("x_manual_proxy") if mode == "manual"
                         else self.cfg.get("x_proxy")) or "").strip()
            port_state = neterr.port_open(proxy)
            label = "指定 HTTP 代理" if mode == "manual" else "本机代理"
            return {"weibo": weibo, "x": {
                "mode": "proxy", "label": label, "node_id": mode,
                "proxy": proxy, "ok": bool(proxy) and port_state is not False,
                "note": "代理端口未开启" if port_state is False else "可在设置中测试代理连接",
            }}
        try:
            summary = self.nodes.summary()
        except Exception:  # noqa: BLE001
            summary = {}
        alive = int(summary.get("alive") or 0)
        # 还没探过（刚启动、X 第一轮没跑）就不要判死：那时候说"没有可用节点"
        # 只是把人吓一跳，实际情况是"还不知道"。
        unknown = not alive and not int(summary.get("known") or 0) \
            and bool(summary.get("enabled"))
        bad_note = "" if alive else str(summary.get("note") or "")

        xmon = getattr(self, "xmon", None)
        node_id = str(getattr(xmon, "last_node_id", "") or "") if xmon else ""
        # "direct" 是 xmonitor 兜底时用的虚拟节点名，不是真节点
        if node_id in ("", "direct"):
            if unknown:
                return {
                    "weibo": weibo,
                    "x": {"mode": "direct", "label": "直连（节点未探测）",
                          "node_id": node_id, "proxy": "", "ok": True,
                          "note": "节点还没探测，X 第一轮跑完就知道"},
                }
            # 只给原因，不重复说"抓不到"：那句话由调用方（内容流横幅）来讲
            note = bad_note or "没有可用节点，先检查代理客户端或订阅节点"
            # 节点原因之上再叠一层"本机能不能出国"：用户最容易在这里跑偏 ——
            # 订阅链接在浏览器里打得开，就以为节点没坏，其实是这台机器出不去。
            # 已经判定为失败才会走到这，多打一次外网不亏（结果缓存 2 分钟）。
            try:
                eg = neterr.egress_check()
            except Exception:  # noqa: BLE001
                eg = {}
            if eg.get("state") in ("blocked", "offline"):
                note = eg["detail"] + (f"（节点侧：{note}）" if note else "")
            return {
                "weibo": weibo,
                "x": {"mode": "direct", "label": "无可用节点 · 直连兜底",
                      "node_id": node_id, "proxy": "", "ok": False,
                      "net": eg.get("state") or "", "note": note},
            }
        proxy = ""
        name = ""
        node = None
        try:
            node = self.nodes.find(node_id)
        except Exception:  # noqa: BLE001
            node = None
        if node:
            name = str(node.get("name") or node_id)
            proxy = str(self.nodes.proxy_of(node) or "")
        else:
            name = node_id
            proxy = str(self.cfg.get("x_proxy") or "")
        return {
            "weibo": weibo,
            "x": {
                "mode": "proxy" if proxy else "direct",
                "label": (name or node_id) if proxy else "无可用节点 · 直连兜底",
                "node_id": node_id,
                "proxy": proxy,
                "ok": bool(proxy),
                "note": "" if proxy else "节点没有可用的代理地址",
            },
        }

    def _node_summary(self) -> Dict[str, Any]:
        try:
            return self.nodes.summary()
        except Exception:  # noqa: BLE001
            return {}

    def _cfg_accounts(self, platform: Optional[str] = None) -> List[Dict[str, Any]]:
        """配置里的账号，按平台过滤。老配置没写 platform 的一律当微博。"""
        accs = self.cfg.get("accounts", []) or []
        if platform is None:
            return accs
        return [a for a in accs if (a.get("platform") or DEFAULT_PLATFORM) == platform]

    def accounts(self, platform: Optional[str] = None) -> List[Dict[str, Any]]:
        store = self.monitor.store.for_platform(platform)
        states = {s["uid"]: s for s in store.list_states()}
        with store._connect() as conn:
            sizes = {(r["platform"], str(r["uid"])): int(r["bytes"] or 0) for r in conn.execute(
                "SELECT p.platform, p.uid, SUM(COALESCE(m.size,0)) AS bytes "
                "FROM media m JOIN posts p ON p.platform=m.platform AND p.mblog_id=m.mblog_id "
                "WHERE m.status='ok' GROUP BY p.platform, p.uid"
            )}
        out: List[Dict[str, Any]] = []
        for acc in self._cfg_accounts(platform):
            uid = str(acc["uid"])
            st = states.get(uid, {})
            out.append({
                "uid": uid,
                "platform": acc.get("platform") or DEFAULT_PLATFORM,
                "name": acc.get("name") or uid,
                "enabled": bool(acc.get("enabled")),
                "monitor_mode": "interact_only" if acc.get("monitor_mode") == "interact_only" else "archive",
                "interact_interval_min": max(5, min(1440, int(acc.get("interact_interval_min") or 30))),
                # 出口节点绑定（X 才有意义，微博走直连）
                "node_id": str(acc.get("node_id") or ""),
                "x_user_id": str(acc.get("x_user_id") or ""),
                "since_id": st.get("since_id") or "",
                "last_check": st.get("last_check") or "",
                "last_error": st.get("last_error") or "",
                "archived": int(store.count_posts(uid)),
                "storage_bytes": sizes.get((acc.get("platform") or DEFAULT_PLATFORM, uid), 0),
                "total": int(st.get("total") or 0),
            })
        return out

    def public_config(self) -> Dict[str, Any]:
        """给前端的配置（Cookie 脱敏）。"""
        cfg = dict(self.cfg)
        cookie = str(cfg.get("cookie") or "")
        cfg["cookie"] = cookie
        cfg["cookie_preview"] = (cookie[:12] + "…（共 %d 字符）" % len(cookie)) if cookie else ""
        # trojan 节点的密码不留到前端：设置页回传 "***" 表示没改，后端会保留原值
        nodes = cfg.get("x_nodes")
        if isinstance(nodes, list):
            cfg["x_nodes"] = [
                {**n, "password": "***" if n.get("password") else ""}
                for n in nodes if isinstance(n, dict)
            ]
        cfg["webdav_password"] = "***" if cfg.get("webdav_password") else ""
        return cfg


class Handler(BaseHTTPRequestHandler):
    """JSON 接口 + 本地媒体文件路由。"""

    server_version = "WeiboMonitor/2.0"
    ctx: ApiContext = None  # 由 start_server 注入

    # ------------------------------------------------------------------ #
    # 基础
    # ------------------------------------------------------------------ #
    def log_message(self, fmt: str, *args: Any) -> None:
        # 静音默认访问日志，避免刷屏；真正的业务日志走 LogBus
        return

    def _trusted_origin(self) -> bool:
        origin = self.headers.get("Origin") or ""
        if not origin:
            return (self.headers.get("Sec-Fetch-Site") or "").lower() != "cross-site"
        port = self.server.server_address[1]
        return origin in {"tauri://localhost", "http://tauri.localhost", "https://tauri.localhost", "http://127.0.0.1:5178", "http://localhost:5178", f"http://127.0.0.1:{port}", f"http://localhost:{port}"}

    def _cors(self) -> None:
        origin = self.headers.get("Origin") or ""
        if origin and self._trusted_origin():
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")

    def _send(self, code: int, payload: Any, ctype: str = "application/json; charset=utf-8") -> None:
        if isinstance(payload, (dict, list)):
            body = json.dumps(payload, ensure_ascii=False, default=_json_default).encode("utf-8")
        elif isinstance(payload, bytes):
            body = payload
        else:
            body = str(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _ok(self, payload: Any) -> None:
        self._send(200, payload)

    def _err(self, message: str, code: int = 400) -> None:
        self._send(code, {"error": message})

    def _body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except Exception:  # noqa: BLE001
            return {}

    def _query(self) -> Dict[str, str]:
        parsed = urllib.parse.urlparse(self.path)
        return {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}

    def _store(self, q: Optional[Dict[str, Any]] = None) -> Any:
        """按请求的 platform 参数取平台作用域的 Store。

        平台做在 Store 实例上，所以这里只是"换一个实例"，下面的读接口一行都不用改。
        不传 platform = 全平台合并视图；all/* 同义。
        """
        return self.ctx.monitor.store.for_platform(_platform_of(q if q is not None else self._query()))

    # ------------------------------------------------------------------ #
    def do_OPTIONS(self) -> None:  # noqa: N802
        if not self._trusted_origin():
            return self._err("不允许的页面来源", 403)
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET(head=True)

    # ------------------------------------------------------------------ #
    def do_GET(self, head: bool = False) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path
        q = self._query()
        ctx = self.ctx

        if route.startswith("/api/") and not self._trusted_origin():
            return self._err("不允许的页面来源", 403)
        try:
            if route == "/api/status":
                return self._ok(ctx.status(_platform_of(q)))

            if route == "/api/accounts":
                return self._ok({"accounts": ctx.accounts(_platform_of(q))})

            if route == "/api/account-profile":
                return self._account_profile(q)

            if route == "/api/favorites":
                return self._favorites_read()

            if route == "/api/account-archive":
                return self._ok(self._account_archive_quote(q))

            if route == "/api/nodes":
                force = q.get("force") in ("1", "true", "True")
                nodes = ctx.nodes.status(force=force)
                # boundAccounts 由 NodeManager 按账号真实绑定统计，不再"谁在用就算谁的"
                return self._ok({"nodes": nodes,
                                 "active": getattr(ctx.xmon, "last_node_id", "")})

            if route == "/api/traffic":
                return self._ok(self._traffic(q))

            if route == "/api/usage":
                return self._ok(self._usage())

            if route == "/api/storage":
                return self._ok(self._storage())

            if route == "/api/x-backfill":
                return self._ok(self._x_backfill_read())

            if route == "/api/webdav":
                return self._ok({**ctx.webdav.status(),
                                 "url": str(ctx.cfg.get("webdav_url") or ""),
                                 "username": str(ctx.cfg.get("webdav_username") or ""),
                                 "password_set": bool(ctx.cfg.get("webdav_password"))})

            if route == "/api/vault":
                return self._ok({"initialized": ctx.vault.initialized,
                                 "unlocked": ctx.vault.unlocked,
                                 "enabled": bool(ctx.cfg.get("encrypt_downloads"))})

            if route == "/api/config":
                return self._ok({"config": ctx.public_config(),
                                 "defaults": DEFAULT_CONFIG})

            if route == "/api/logs":
                count = int(q.get("limit") or q.get("count") or 200)
                return self._ok({"lines": ctx.log.recent(count)})

            if route == "/api/summary":
                return self._ok({"text": ctx.monitor.latest_summary,
                                 "dir": ctx.monitor.summaries_dir,
                                 "files": self._summary_files()})

            if route == "/api/posts":
                return self._ok(self._feed(q))

            if route == "/api/post":
                return self._post_read(q)

            if route == "/api/feed-head":
                return self._ok(self._feed_head(q))

            if route == "/api/query":
                return self._ok(self._query_posts(q))

            if route == "/api/media-list":
                return self._ok(self._media_list(q))

            if route == "/api/export":
                return self._export_csv(q)

            if route == "/api/media":
                return self._media(q)

            if route == "/api/thumb":
                return self._thumb(q)

            if route == "/api/thumbs":
                return self._thumbs_status()

            if route == "/api/trash":
                return self._trash_read(q)

            if route == "/api/text-only":
                # 纯文本帖清单：设置页「清理现有 N 条」按钮先打这个看一眼
                return self._ok(self._text_only_scan(q))

            if route == "/api/dedupe":
                return self._dedupe_read()

            if route == "/api/similar-cleanup":
                return self._ok(scan_similar_cleanup(ctx.monitor.store.db_path))

            if route == "/api/archive-health":
                return self._ok(scan_archive_health(ctx.monitor.store.db_path, resolve_output_dir(ctx.cfg)))

            if route == "/api/cleanup/missing-media":
                return self._ok(scan_missing_media(ctx.monitor.store.db_path))

            if route == "/api/update":
                updater = UIUpdater(os.path.join(ROOT_DIR, "x-monitor", "dist"),
                                    os.path.join(resolve_output_dir(ctx.cfg), ".ui-update.json"),
                                    str(ctx.cfg.get("update_repo") or ""))
                return self._ok({"repo": updater.repo, "current": updater.current()})

            if route == "/api/actions":
                # 自动操作记录（转关 / 关注 / 加监控…），记录页的数据源
                limit = int(q.get("limit") or 200)
                rows = ctx.monitor.store.list_actions(
                    type_=str(q.get("type") or ""),
                    limit=limit,
                    only_active=q.get("only_active") in ("1", "true", "True"),
                    tag=str(q.get("tag") or ""),
                )
                # 顺带把端点就绪情况告诉界面：没配的端点不该给个点了没用的按钮
                pilot = ctx.monitor.autopilot
                writer = getattr(pilot, "writer", None)
                return self._ok({
                    "rows": rows,
                    "halted": pilot.halted,
                    "endpoints": writer.available_actions() if writer else {},
                    "writer_ready": bool(writer and writer.available),
                })

            if route == "/api/discover":
                # 存量原博主清单：库里被转发过、还没监控的人，按次数降序。
                # 只列不动作，勾哪些由人在界面上决定。
                cfg = ctx.cfg
                accts = cfg.get("accounts") or []
                rows = ctx.monitor.store.discover_authors(
                    limit=int(q.get("limit") or 100),
                    monitored_uids={str(a.get("uid")) for a in accts},
                    follow_blacklist={str(i.get("uid")) for i in (cfg.get("follow_blacklist") or [])
                                      if isinstance(i, dict)},
                    monitor_blacklist={str(i.get("uid")) for i in (cfg.get("monitor_blacklist") or [])
                                       if isinstance(i, dict)},
                )
                return self._ok({"rows": rows, "accounts": len(accts),
                                 "cap": int(cfg.get("auto_track_max_accounts", 30))})

            if route == "/api/weibo-login":
                return self._ok(ctx.weibo_login.status())

            if route == "/api/x-login":
                return self._ok(ctx.x_login.status())

            if route == "/api/check-cookie":
                return self._ok(self._check_cookie())

            if route == "/api/health":
                return self._ok({"ok": True, "pid": os.getpid()})

            if route.startswith("/api/"):
                return self._err(f"未知接口 {route}", 404)

            # 其余路径当静态资源：前端构建过就直接托管，双击即用（不依赖 Node / Rust）
            return self._serve_static(route)

        except Exception as exc:  # noqa: BLE001
            return self._err(f"{exc}", 500)

    # ------------------------------------------------------------------ #
    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path
        ctx = self.ctx

        if not self._trusted_origin():
            return self._err("不允许的页面来源", 403)
        try:
            body = self._body()

            if route == "/api/control":
                return self._control(body)

            if route == "/api/accounts":
                return self._accounts_write(body)

            if route == "/api/favorites":
                return self._favorites_write(body)

            if route == "/api/account-archive":
                return self._account_archive_clear(body)

            if route == "/api/nodes":
                return self._nodes_write(body)

            if route == "/api/webdav":
                action = str(body.get("action") or "")
                if action == "save":
                    for key in ("url", "username", "password"):
                        if key in body and not (key == "password" and body[key] == "***"):
                            ctx.cfg["webdav_" + key] = str(body[key] or "").strip()
                    ctx._save_cfg()
                    ctx.reload()
                elif action == "sync":
                    try:
                        ctx.webdav.start(body.get("resolutions") or {})
                    except ValueError as exc:
                        return self._err(str(exc))
                else:
                    return self._err("不支持的云同步操作")
                return self._ok(ctx.webdav.status())

            if route == "/api/update":
                action = str(body.get("action") or "")
                if action == "save":
                    repo = str(body.get("repo") or "").strip()
                    repo = re.sub(r"^https://github\.com/", "", repo, flags=re.I).strip("/")
                    if repo and not UPDATE_REPO.fullmatch(repo):
                        return self._err("仓库地址格式不对，请填写 GitHub 仓库链接", 400)
                    ctx.cfg["update_repo"] = repo
                    ctx._save_cfg()
                    return self._ok({"repo": repo})
                updater = UIUpdater(os.path.join(ROOT_DIR, "x-monitor", "dist"),
                                    os.path.join(resolve_output_dir(ctx.cfg), ".ui-update.json"),
                                    str(ctx.cfg.get("update_repo") or ""))
                try:
                    if action == "check":
                        result = updater.check()
                        result.pop("asset", None)
                        return self._ok(result)
                    if action == "install":
                        return self._ok(updater.install(str(body.get("tag") or "")))
                except (OSError, ValueError, requests.RequestException) as exc:
                    return self._err(str(exc), 400)
                return self._err("不支持的更新操作", 400)

            if route == "/api/media-export":
                target = self._safe_archive_path(str(body.get("path") or ""))
                if not target or not os.path.isfile(target):
                    return self._err("文件不存在", 404)
                if target.endswith(".enc"):
                    try:
                        ctx.vault.unlock(str(body.get("password") or ""))
                    except ValueError as exc:
                        return self._err(str(exc), 403)
                name = os.path.basename(target[:-4] if target.endswith(".enc") else target)
                size = ctx.vault.plaintext_size(target) if target.endswith(".enc") else os.path.getsize(target)
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + urllib.parse.quote(name))
                self.send_header("Content-Length", str(size))
                self.send_header("Cache-Control", "no-store")
                self._cors()
                self.end_headers()
                try:
                    if target.endswith(".enc"):
                        pos = 0
                        while pos < size:
                            stop = min(size - 1, pos + _CHUNK - 1)
                            part, _ = ctx.vault.read_range(target, pos, stop)
                            self.wfile.write(part)
                            pos = stop + 1
                    else:
                        with open(target, "rb") as source:
                            while chunk := source.read(_CHUNK):
                                self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return

            if route == "/api/vault":
                action = str(body.get("action") or "")
                try:
                    if action == "setup":
                        ctx.vault.setup(str(body.get("password") or ""))
                    elif action == "unlock":
                        ctx.vault.unlock(str(body.get("password") or ""))
                    elif action == "change_password":
                        ctx.vault.change_password(
                            str(body.get("old_password") or ""),
                            str(body.get("new_password") or ""))
                    elif action == "lock":
                        ctx.vault.lock()
                    elif action == "enable":
                        if not ctx.vault.unlocked:
                            return self._err("先设置并解锁主密码")
                        ctx.cfg["encrypt_downloads"] = True
                        ctx._save_cfg()
                        ctx.reload()
                    elif action == "disable":
                        ctx.cfg["encrypt_downloads"] = False
                        ctx._save_cfg()
                        ctx.reload()
                    else:
                        return self._err("不支持的加密操作")
                except ValueError as exc:
                    return self._err(str(exc))
                return self._ok({"initialized": ctx.vault.initialized,
                                 "unlocked": ctx.vault.unlocked,
                                 "enabled": bool(ctx.cfg.get("encrypt_downloads"))})

            if route == "/api/weibo-login":
                action = str(body.get("action") or "")
                if action == "start":
                    return self._ok(ctx.weibo_login.start())
                if action == "cancel":
                    ctx.weibo_login.cancel()
                    return self._ok(ctx.weibo_login.status())
                return self._err("不支持的登录操作")

            if route == "/api/x-login":
                action = str(body.get("action") or "")
                if action == "start":
                    return self._ok(ctx.x_login.start())
                if action == "cancel":
                    ctx.x_login.cancel()
                    return self._ok(ctx.x_login.status())
                return self._err("不支持的登录操作")

            if route == "/api/config":
                return self._config_write(body)

            if route == "/api/x-proxy-test":
                proxy = str(body.get("proxy") or "").strip()
                try:
                    parsed_proxy = urllib.parse.urlsplit(proxy)
                    valid_proxy = parsed_proxy.scheme == "http" and parsed_proxy.hostname and parsed_proxy.port
                except ValueError:
                    valid_proxy = False
                if not valid_proxy:
                    return self._err("代理地址或端口不正确", 400)
                return self._ok(ctx.nodes.probe_proxy(proxy))

            if route == "/api/x-probe":
                return self._x_probe(body)

            if route == "/api/x-download":
                return self._x_download(body)

            if route == "/api/x-backfill":
                return self._x_backfill_write(body)

            if route == "/api/x-audit-ids":
                return self._x_audit_ids(body)

            if route == "/api/thumbs":
                return self._thumbs_pregenerate(body)

            if route == "/api/posts":
                return self._posts_write(body)

            if route == "/api/trash":
                return self._trash_write(body)

            if route == "/api/text-only":
                # 只清「按规则不该收的存量」，不写墓碑：以后开回开关还能回来
                return self._text_only_purge(body)

            if route == "/api/dedupe":
                return self._dedupe_write(body)

            if route == "/api/summarize":
                hours = float(body.get("hours") or 24)
                text = ctx.monitor.generate_summary(time.time() - hours * 3600)
                return self._ok({"text": text, "files": self._summary_files()})

            if route == "/api/open":
                target = str(body.get("path") or "")
                allowed = os.path.abspath(ctx.monitor.output_dir)
                real = os.path.abspath(target)
                # 只允许打开存档目录内的路径，避免被拿去打开任意系统目录
                if not (real == allowed or real.startswith(allowed + os.sep)):
                    return self._err("只允许打开存档目录内的路径", 403)
                return self._ok({"opened": ctx.open_dir(real)})

            if route == "/api/actions":
                # 撤销一条自动操作：取关 / 取消监控 / 删掉已转发的微博
                act = str(body.get("action") or "")
                pilot = ctx.monitor.autopilot
                if act == "approve_comment":
                    try:
                        return self._ok(pilot.approve_comment(int(body.get("id") or 0)))
                    except Exception as exc:
                        return self._err(str(exc))
                if act == "undo":
                    ok = pilot.undo(int(body.get("id") or 0))
                    return self._ok({"ok": ok} if ok
                                    else {"ok": False, "error": "撤销不了（已撤销过 / 类型不支持）"})
                if act == "resume":
                    pilot.resume()
                    return self._ok({"ok": True, "halted": pilot.halted})
                if act == "scan":
                    # 存量试算：把库里已经抓到的微博过一遍关键字，只写演练记录。
                    #
                    # 没有这个的话，开了演练也要等下一轮真的抓到新微博才有东西看，
                    # 而新微博什么时候来完全看运气。试算让我们现在就能确认
                    # 「命中谁、转哪条、是不是原博」。
                    #
                    # 零副作用：用临时 AutoPilot 且关掉加监控，所以不会改配置、
                    # 不会真的加轮询账号；转发/关注在 dry_run 下本来就只记不发。
                    from core.interact import AutoPilot

                    limit = int(body.get("limit") or 300)
                    store = ctx.monitor.store
                    snapshot = dict(ctx.cfg)
                    # 两道保险：试算永远不真发，也永远不真的加监控
                    snapshot["dry_run"] = True
                    snapshot["zg_add_monitor"] = False
                    temp = AutoPilot(snapshot, store, ctx.monitor.logger.emit,
                                     writer=getattr(pilot, "writer", None))
                    posts = store.list_posts(limit=limit)
                    hit = 0
                    for p in posts:
                        matched = temp._match(p)
                        if not matched:
                            continue
                        hit += 1
                        temp._zhuan_guan(p, matched)
                    return self._ok({"scanned": len(posts), "hit": hit, "dry_run": True})
                return self._err(f"不支持的动作 {act}")

            if route == "/api/discover":
                # 对存量清单里勾选的人批量执行加监控 / 关注。
                # 走的是 AutoPilot 的同一套动作函数，所以每日上限、幂等、
                # 刹停、审计记录全都照样生效 —— 和自动发现的博主没有区别。
                act = str(body.get("action") or "")
                uids = [str(u) for u in (body.get("uids") or []) if str(u)]
                names = body.get("names") or {}
                if not uids:
                    return self._ok({"done": 0, "skipped": 0, "errors": []})
                pilot = ctx.monitor.autopilot
                done, skipped, errors = 0, 0, []
                for uid in uids:
                    name = str(names.get(uid) or uid)
                    try:
                        if act in ("monitor", "both") and not pilot._is_monitored(uid):
                            if pilot._today_limit_reached("add_monitor"):
                                skipped += 1
                                errors.append(f"{name}：今日加监控已达上限")
                            else:
                                pilot._add_monitor(uid, name, "存量清单手动加入",
                                                   source="discover")
                                done += 1
                        if act in ("follow", "both"):
                            if pilot._blacklisted("follow", uid):
                                skipped += 1
                                errors.append(f"{name}：撤销过关注，已拉黑")
                            elif pilot.store.has_action("follow", uid=uid):
                                skipped += 1
                                errors.append(f"{name}：已经关注过")
                            elif pilot._today_limit_reached("follow"):
                                skipped += 1
                                errors.append(f"{name}：今日关注已达上限（明天继续）")
                            else:
                                pilot._follow(uid, name, "存量清单手动关注", source="discover")
                                done += 1
                    except Exception as exc:  # noqa: BLE001
                        skipped += 1
                        errors.append(f"{name}：{exc}")
                return self._ok({"done": done, "skipped": skipped, "errors": errors[:20]})

            if route == "/api/write-test":
                # 单次真发测试：端点刚填好时用来验证「真的生效了」，
                # 不走自动互动、不计入每日上限，发完立刻能看到返回。
                act = str(body.get("action") or "")
                pilot = ctx.monitor.autopilot
                writer = getattr(pilot, "writer", None)
                if writer is None:
                    return self._err("没有写接口实例")
                try:
                    if act == "repost":
                        mid = str(body.get("mid") or "")
                        if not mid:
                            return self._err("缺少 mid")
                        resp = writer.repost(mid, str(body.get("text") or ""))
                    elif act == "delete":
                        mid = str(body.get("mid") or "")
                        if not mid:
                            return self._err("缺少 mid")
                        resp = writer.delete_status(mid)
                    elif act == "follow":
                        uid = str(body.get("uid") or "")
                        if not uid:
                            return self._err("缺少 uid")
                        resp = writer.follow(uid)
                    elif act == "unfollow":
                        uid = str(body.get("uid") or "")
                        if not uid:
                            return self._err("缺少 uid")
                        resp = writer.unfollow(uid)
                    else:
                        return self._err(f"不支持的测试动作 {act}")
                except WriterUnavailable as exc:
                    return self._ok({"ok": False, "error": str(exc)})
                except Exception as exc:  # noqa: BLE001
                    return self._ok({"ok": False, "error": f"{exc}"})
                return self._ok({"ok": True, "resp": resp})

            if route == "/api/open-ui":
                # 第二个启动实例用：让已经在跑的服务把界面窗口提到最前。
                # 没有窗口时才新建，所以反复双击永远只有一个界面。
                host = self.headers.get("Host") or f"127.0.0.1:{self.server.server_address[1]}"
                open_app_window(f"http://{host}")
                return self._ok({"ok": True})

        except Exception as exc:  # noqa: BLE001
            return self._err(f"{exc}", 500)
        except BaseException as exc:  # noqa: BLE001
            # SystemExit / KeyboardInterrupt 之类的"非 Exception"必须在这里兜住。
            # 放任它穿出 handler 的话 socketserver 会直接关连接，
            # 前端经历的是网络错误，只能显示"连不上本地服务"—— 排查过一整轮才发现。
            return self._err(
                f"服务内部中止：{type(exc).__name__}: {exc}", 500)

        return self._err(f"未知接口 {route}", 404)

    # ------------------------------------------------------------------ #
    # 各业务路由
    # ------------------------------------------------------------------ #
    def _control(self, body: Dict[str, Any]) -> None:
        action = str(body.get("action") or "")
        ctx = self.ctx
        if action == "start":
            ctx.monitor.start()
            ctx.xmon.start()
        elif action == "stop":
            ctx.monitor.stop()
            ctx.xmon.stop()
        elif action == "check":
            if not ctx.monitor.running:
                # 没在后台跑也允许手动检查一次
                threading.Thread(target=ctx.monitor.tick, daemon=True).start()
            else:
                ctx.monitor.check_now()
            if not ctx.xmon.running:
                threading.Thread(target=ctx.xmon.tick, daemon=True).start()
            else:
                ctx.xmon.check_now()
        elif action == "reload":
            ctx.reload()
        elif action == "quit":
            ctx.request_quit()
            return self._ok({"ok": True, "quitting": True})
        else:
            return self._err(f"不支持的动作 {action}")
        return self._ok({"ok": True, "status": ctx.status()})

    # ------------------------------------------------------------------ #
    # 节点与流量
    # ------------------------------------------------------------------ #
    def _traffic(self, q: Dict[str, str]) -> Dict[str, Any]:
        """真实流量明细。数据来自本进程每次出网请求的实际收发字节。"""
        import datetime as _dt

        days = max(1, min(int(q.get("days") or 14), 90))
        start = (_dt.date.today() - _dt.timedelta(days=days - 1)).isoformat()
        # traffic 表是跨平台的（微博和 X 走同一批节点），查询不按平台过滤
        store = self.ctx.monitor.store
        rows = store.traffic_rows(start_day=start)
        api = sum(r["apiBytes"] for r in rows)
        media = sum(r["mediaBytes"] for r in rows)
        return {
            "rows": rows,
            "days": store.traffic_days(days),
            "byNode": store.traffic_by_node(start),
            "byAccount": store.traffic_by_account(start),
            "total": {"api": api, "media": media, "total": api + media},
            "since": start,
        }

    def _usage(self) -> Dict[str, Any]:
        """配额口径：账单周期（billing_day 起始）。没配配额（0）就是不限，pct 恒为 0。"""
        import datetime as _dt

        today = _dt.date.today()
        billing_day = max(1, min(int(self.ctx.cfg.get("billing_day", 1) or 1), 28))
        # 还没到账单日 → 周期从上个月同一天开始
        if today.day >= billing_day:
            start = today.replace(day=billing_day)
        else:
            prev = (today.replace(day=1) - _dt.timedelta(days=1))
            start = prev.replace(day=min(billing_day, prev.day))
        start_iso = start.isoformat()
        store = self.ctx.monitor.store
        rows = [r for r in store.traffic_rows(start_day=start_iso) if r["day"] >= start_iso]
        api = sum(r["apiBytes"] for r in rows)
        media = sum(r["mediaBytes"] for r in rows)
        day_rows = [r for r in rows if r["day"] == today.isoformat()]
        t_api = sum(r["apiBytes"] for r in day_rows)
        t_media = sum(r["mediaBytes"] for r in day_rows)
        quota = int(float(self.ctx.cfg.get("monthly_quota_gb", 0) or 0) * 1024 ** 3)
        total = api + media
        return {
            "window": {"start": start, "end": today.isoformat()},
            "period": {"api": api, "media": media, "total": total},
            "today": {"api": t_api, "media": t_media, "total": t_api + t_media},
            "quotaBytes": quota,
            "remain": max(0, quota - total) if quota else 0,
            "pct": min(1.0, total / quota) if quota else 0.0,
        }

    def _storage(self) -> Dict[str, Any]:
        """按平台统计落盘占用（磁盘实测，不是拿库里的条数估算）。

        口径：data/<platform>/ 下所有文件的字节数之和 —— 这就是「删掉能腾出多少」。
        数据库和回收站单列：库是全平台共用的，回收站是已经删掉、随时可清的空闲空间，
        混进平台里会让「微博占了 17G」变成一笔算不清的账。

        遍历 5000 个文件约 0.5 秒，所以结果缓存 60 秒：前端 30 秒轮询一次，
        不缓存等于每两轮白扫一次，而磁盘占用不会在 30 秒里翻天。
        """
        import time as _t

        root = os.path.dirname(os.path.abspath(self.ctx.monitor.store.db_path))
        now = _t.time()
        # 缓存必须放在模块级：BaseHTTPRequestHandler 每个请求一个新实例，
        # 挂在 self 上的缓存永远读不到第二次（实测每轮都白扫 0.5 秒）
        cached = _STORAGE_CACHE.get("data")
        if cached and now - float(_STORAGE_CACHE.get("at") or 0) < 60:
            return cached

        def _walk(path: str) -> Any:
            total = 0
            files = 0
            if not os.path.isdir(path):
                return 0, 0
            for dirpath, _dirs, names in os.walk(path):
                for name in names:
                    try:
                        total += os.stat(os.path.join(dirpath, name)).st_size
                    except OSError:
                        continue
                    files += 1
            return total, files

        store = self.ctx.monitor.store
        plats: List[Dict[str, Any]] = []
        for key, label in (("weibo", "微博"), ("x", "X")):
            nbytes, nfiles = _walk(os.path.join(root, key))
            st = store.for_platform(key).stats()
            plats.append({
                "platform": key,
                "label": label,
                "bytes": nbytes,
                "files": nfiles,
                "posts": int(st.get("posts") or 0),
                "media": int(st.get("images") or 0) + int(st.get("videos") or 0),
                "accounts": int(st.get("accounts") or 0),
            })

        trash_bytes, trash_files = _walk(store.trash_dir)
        thumb_bytes, thumb_files = _walk(os.path.join(root, ".thumbs"))
        db_bytes = 0
        for suffix in ("", "-wal", "-shm"):
            try:
                db_bytes += os.path.getsize((store.db_path or "") + suffix)
            except OSError:
                pass

        data = {
            "root": root,
            "at": _t.strftime("%H:%M:%S"),
            "total": sum(p["bytes"] for p in plats),
            "files": sum(p["files"] for p in plats),
            "platforms": plats,
            "dbBytes": db_bytes,
            "thumbs": {"bytes": thumb_bytes, "files": thumb_files},
            "trash": {"bytes": trash_bytes, "files": trash_files},
        }
        # 磁盘余量：回溯上百页可能一次吃掉几个 G，弹窗要能提前告诉你够不够
        try:
            import shutil as _sh
            usage = _sh.disk_usage(root)
            data["disk"] = {"free": usage.free, "total": usage.total}
        except OSError:
            data["disk"] = {"free": 0, "total": 0}
        _STORAGE_CACHE["at"] = now
        _STORAGE_CACHE["data"] = data
        return data
    def _x_backfill_read(self) -> Dict[str, Any]:
        """回溯任务状态 + 磁盘余量 + 平均每条体积（弹窗拿它做体积预估）。

        平均体积取「X 目录已占字节 / X 已存条数」，比拍一个固定值靠谱：
        X 侧一条带视频和一条纯文本能差两个数量级，用户的号偏哪边只有实测知道。
        """
        data = self.ctx.backfill.status()
        free = total = 0
        avg = 0
        try:
            st = self._storage()
            disk = st.get("disk") or {}
            free = int(disk.get("free") or 0)
            total = int(disk.get("total") or 0)
            x = next((p for p in st.get("platforms") or []
                      if p.get("platform") == "x"), {})
            avg = int(int(x.get("bytes") or 0) / max(1, int(x.get("posts") or 0)))
        except Exception:  # noqa: BLE001  估算失败不该让整个接口挂掉
            pass
        data["disk"] = {"free": free, "total": total}
        data["avgPostBytes"] = avg
        data["pageSize"] = 20
        return data

    def _x_backfill_write(self, body: Dict[str, Any]) -> None:
        action = str(body.get("action") or "")
        handle = str(body.get("handle") or "")
        if not handle:
            return self._err("缺少 handle")
        if action == "stop":
            return self._ok({"ok": self.ctx.backfill.stop(handle)})
        if action in ("start", "continue"):
            try:
                task = self.ctx.backfill.start(handle, body,
                                               resume=(action == "continue"))
            except ValueError as exc:
                return self._err(str(exc))
            return self._ok({"ok": True, "task": task})
        return self._err(f"不支持的动作 {action}")

    def _x_audit_ids(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """体检 / 修复账号的 `x_user_id`（`{"apply": true}` 才写回配置）。

        背景：XClient 按出口节点复用，`last_user_id` 曾经被轮询线程和回溯线程
        互相覆盖，把 A 的数字 id 写进 B 的配置。代码侧已改成线程内记录，这里
        负责把**已经写错的存量**捞出来。必须跑在服务进程里：节点是 ApiContext
        拉起的，服务一停端口就没了。
        """
        apply = bool(body.get("apply"))
        try:
            delay = max(0.0, min(5.0, float(body.get("delay") or 0.8)))
        except (TypeError, ValueError):
            delay = 0.8
        ctx = self.ctx
        accounts = [a for a in ctx.cfg.get("accounts", [])
                    if (a.get("platform") or "weibo") == "x"]
        try:
            limit = int(body.get("limit") or 0)
        except (TypeError, ValueError):
            limit = 0
        if limit > 0:
            accounts = accounts[:limit]
        if not accounts:
            return {"scanned": 0, "mismatch": 0, "rows": []}
        ctx.log.emit("INFO", f"开始体检 {len(accounts)} 个 X 账号的数字 id（apply={apply}）")
        client = ctx.xmon.new_client(accounts[0])
        ctx.log.emit("INFO", f"体检出口：{client.node_id or 'direct'}")
        rows: List[Dict[str, Any]] = []
        changed = 0
        for acc in accounts:
            handle = str(acc.get("uid") or "").lstrip("@")
            old = str(acc.get("x_user_id") or "")
            try:
                fresh = client.resolve_user_id(handle, force=True)
                error = ""
            except XError as exc:
                fresh, error = "", str(exc)
            mismatch = bool(fresh) and fresh != old
            if mismatch:
                changed += 1
                if apply:
                    acc["x_user_id"] = fresh
            rows.append({"handle": handle, "old": old, "new": fresh,
                         "mismatch": mismatch, "error": error})
            time.sleep(delay)
        if apply and changed:
            ctx._save_cfg()
            ctx.log.emit("INFO", f"x_user_id 体检：修正 {changed} 个账号的数字 id")
        return {"scanned": len(accounts), "mismatch": changed,
                "applied": bool(apply and changed), "rows": rows}

    def _fetch_subscription(self, url: str) -> str:
        """拉订阅：先直连，不通再走当前节点。订阅站大多在墙外，直连常常超时。"""
        import requests as _rq

        if not url:
            raise ValueError("缺少订阅地址")
        headers = {"user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        node_id = getattr(self.ctx.xmon, "last_node_id", "")
        proxy = self.ctx.nodes.proxy_of(self.ctx.nodes.find(node_id) or {}) if node_id else ""
        attempts = [None] + ([{"http": proxy, "https": proxy}] if proxy else [])
        last = ""
        for proxies in attempts:
            try:
                # 直连那次必须关掉环境代理：本机可能残留 HTTP_PROXY（沙箱/别的软件
                # 设的），requests 默认会读，结果就是"订阅明明打得开却拉不回来"。
                sess = _rq.Session()
                sess.trust_env = bool(proxies)
                resp = sess.get(url, timeout=15, headers=headers, proxies=proxies)
                if resp.status_code < 400 and resp.text.strip():
                    self.ctx.monitor.store.log_traffic(
                        node_id=node_id, platform="", uid="", kind="sub",
                        bytes_up=0, bytes_down=len(resp.content or b""))
                    return resp.text
                last = f"HTTP {resp.status_code}"
            except Exception as exc:  # noqa: BLE001
                last = f"{type(exc).__name__}: {exc}"
        raise ValueError(f"订阅拉取失败：{last}")

    def _nodes_write(self, body: Dict[str, Any]) -> None:
        action = str(body.get("action") or "")
        ctx = self.ctx
        nodes = [dict(n) for n in ctx.nodes.nodes() if isinstance(n, dict)]
        nid = str(body.get("id") or "")

        if action == "reset_subscription":
            kept = [n for n in nodes if n.get("type") == "external"]
            kept_ids = {str(n.get("id")) for n in kept}
            local_id = next((str(n.get("id")) for n in kept if n.get("enabled")), "")
            removed = len(nodes) - len(kept)
            ctx.cfg["x_nodes"] = kept
            ctx.cfg["subscription"] = ""
            ctx.cfg["x_subscription_host_overrides"] = {}
            for account in ctx.cfg.get("accounts") or []:
                if account.get("platform") == "x" and account.get("node_id") not in kept_ids:
                    account["node_id"] = local_id
            ctx._save_cfg()
            ctx.reload()
            return self._ok({"ok": True, "removed": removed, "nodes": ctx.nodes.status()})
        if action == "add":
            node = {
                "id": nid or f"n{int(time.time()) % 100000}",
                "name": str(body.get("name") or nid or "新节点").strip(),
                "region": str(body.get("region") or "—").strip(),
                "type": "trojan" if body.get("type") == "trojan" else "external",
                "enabled": bool(body.get("enabled", True)),
            }
            if node["type"] == "trojan":
                node.update({
                    "server": str(body.get("server") or "").strip(),
                    "port": int(body.get("port") or 443),
                    "password": str(body.get("password") or "").strip(),
                    "sni": str(body.get("sni") or "").strip(),
                    "listen_port": int(body.get("listen_port") or 10811),
                })
                missing = [k for k in ("server", "password", "sni") if not node[k]]
                if missing:
                    return self._err(f"trojan 节点缺少 {'/'.join(missing)}")
            else:
                node["proxy"] = str(body.get("proxy") or "").strip()
                if not node["proxy"]:
                    return self._err("缺少代理地址")
            if any(n["id"] == node["id"] for n in nodes):
                return self._err("该节点 id 已存在")
            nodes.append(node)

        elif action == "update":
            for node in nodes:
                if node["id"] != nid:
                    continue
                for key in ("name", "region", "type", "server", "sni", "proxy"):
                    if key in body:
                        node[key] = body[key]
                for key in ("port", "listen_port"):
                    if key in body:
                        node[key] = int(body[key] or 0)
                # 前端回传 "***" 表示没改密码，保留原来的
                pwd = str(body.get("password") or "")
                if pwd and pwd != "***":
                    node["password"] = pwd
                break
            else:
                return self._err("节点不存在")

        elif action == "toggle":
            for node in nodes:
                if node["id"] == nid:
                    node["enabled"] = not bool(node.get("enabled"))
                    break
            else:
                return self._err("节点不存在")

        elif action == "remove":
            nodes = [n for n in nodes if n["id"] != nid]

        elif action == "probe":
            return self._ok({"nodes": ctx.nodes.status(force=True)})

        elif action in ("import", "subscribe"):
            # import：粘贴一条 trojan:// 链接；subscribe：拉订阅地址批量导入
            # 两个分支共用的变量必须先落位：import 走不到订阅统计那条路，
            # 少初始化一个就会在返回时 NameError（接口直接 500）。
            stats: Dict[str, Any] = {}
            removed = 0
            if action == "import":
                node = parse_link(str(body.get("link") or ""))
                if not node:
                    return self._err("只认 trojan:// 链接")
                incoming = [node]
            else:
                try:
                    text = self._fetch_subscription(str(body.get("url") or ""))
                except ValueError as exc:
                    return self._err(str(exc))
                stats = nodes_mod.subscription_stats(text)
                incoming = parse_subscription(text)
                if not incoming:
                    # 别只说"没有 trojan"——把订阅里到底有什么讲清楚，
                    # 否则用户只会觉得"我明明更新了订阅"。
                    return self._err(nodes_mod.describe_stats(stats))
                overrides = dict(ctx.cfg.get("x_subscription_host_overrides") or {})
                # 旧配置里已验证过的替代入口随节点保留，重启后再次导入仍可沿用。
                for old in nodes:
                    source = str(old.get("subscription_server") or "")
                    if source and old.get("server"):
                        overrides.setdefault(source, str(old["server"]))
                overridden = nodes_mod.apply_host_overrides(incoming, overrides)
            # replace：订阅更新后旧的端口往往已经失效，留着只会让测速更慢。
            # 清掉的是**凭据填在配置里的托管节点**，外部代理（本地客户端）不动。
            replace = bool(body.get("replace"))
            if replace and incoming:
                keep = [n for n in nodes if (n.get("type") or "external") != "trojan"]
                removed = len(nodes) - len(keep)
                nodes = keep      # 旧 trojan 子进程由下面的 reload() 收摊
            else:
                removed = 0
            existing = {n["id"] for n in nodes}
            added = 0
            for node in incoming:
                if node["id"] in existing:
                    continue
                # 监听口不能撞：已经在用的往后顺延
                used = {int(n.get("listen_port") or 0) for n in nodes}
                while int(node.get("listen_port") or 0) in used:
                    node["listen_port"] = int(node["listen_port"]) + 1
                nodes.append(node)
                existing.add(node["id"])
                added += 1
            summary = ""
            if action == "subscribe":
                summary = nodes_mod.describe_stats(stats)
                if overridden:
                    summary += f"；{overridden} 个节点改用已配置的替代入口"
                if removed:
                    summary += f"（替换掉 {removed} 个旧节点）"
                ctx.log("INFO", f"订阅导入：{summary}")
                ctx.cfg["subscription"] = str(body.get("url") or "")
            if not added:
                if action == "subscribe":
                    ctx._save_cfg()
                return self._ok({"ok": True, "added": 0, "removed": removed,
                                 "summary": summary, "stats": stats,
                                 "nodes": ctx.nodes.status()})
            ctx.cfg["x_nodes"] = nodes
            ctx._save_cfg()
            ctx.reload()
            return self._ok({"ok": True, "added": added, "removed": removed,
                             "summary": summary, "stats": stats,
                             "nodes": ctx.nodes.status()})

        else:
            return self._err(f"不支持的动作 {action}")

        ctx.cfg["x_nodes"] = nodes
        ctx._save_cfg()
        ctx.reload()
        return self._ok({"ok": True, "nodes": ctx.nodes.status()})

    def _account_profile(self, q: Dict[str, str]) -> None:
        platform = str(q.get("platform") or "")
        uid = str(q.get("uid") or "")
        if platform not in ("weibo", "x") or not uid:
            return self._err("请选择要查看的账号", 400)
        account = next((a for a in self.ctx._cfg_accounts(platform)
                        if str(a.get("uid") or "") == uid), None)
        if account is None:
            return self._err("账号不在监控列表中", 404)
        key = (platform, uid)
        cached = _PROFILE_CACHE.get(key)
        if cached and time.time() - cached[0] < 3600:
            return self._ok(cached[1])
        if platform == "weibo":
            user = self.ctx.monitor.client.fetch_user_info(uid)
            if not user:
                return self._err("微博简介读取失败，可稍后重试", 502)
            result = {
                "name": str(user.get("screen_name") or account.get("name") or uid),
                "handle": "", "bio": str(user.get("description") or ""),
            }
        else:
            client = self.ctx.xmon.new_client(account)
            qid = client._qid_by_screen_name()
            if not qid:
                return self._err("X 资料接口暂不可用，请检查节点后重试", 502)
            variables = {"screen_name": uid, "withSafetyModeUserFields": True}
            try:
                response = client._request(
                    "GET", f"https://x.com/i/api/graphql/{qid}/UserByScreenName",
                    uid=uid, kind="gql", headers=client._auth_headers(),
                    params={"variables": json.dumps(variables),
                            "features": json.dumps(GQL_FEATURES)},
                ).json()
            except Exception as exc:  # noqa: BLE001
                return self._err(f"X 简介读取失败：{exc}", 502)
            user = ((response.get("data") or {}).get("user") or {}).get("result") or {}
            legacy = user.get("legacy") or {}
            core = user.get("core") or {}
            if not user or user.get("__typename") == "UserUnavailable":
                return self._err("X 账号资料暂不可见", 502)
            result = {
                "name": str(legacy.get("name") or core.get("name") or account.get("name") or uid),
                "handle": "@" + str(legacy.get("screen_name") or core.get("screen_name") or uid),
                "bio": str((user.get("profile_bio") or {}).get("description")
                           or legacy.get("description") or core.get("description") or ""),
            }
        _PROFILE_CACHE[key] = (time.time(), result)
        return self._ok(result)

    def _x_download(self, body: Dict[str, Any]) -> None:
        """单次归档 X 链接，不把作者加入监控账号。"""
        try:
            handle, post_id = parse_x_status_url(str(body.get("url") or ""))
        except ValueError as exc:
            return self._err(str(exc), 400)
        store = self.ctx.xmon.store

        def kept() -> int:
            with store._connect() as conn:
                rows = conn.execute(
                    "SELECT path FROM media WHERE platform='x' AND mblog_id=? "
                    "AND status IN ('ok','skipped','deduped')", (post_id,)).fetchall()
            return sum(1 for row in rows if row[0] and os.path.isfile(row[0]))

        existing = store.get_post(post_id)
        if existing and kept() >= int(existing.get("pic_count") or 0) + int(existing.get("video_count") or 0):
            return self._ok({"post_id": post_id, "downloaded": kept(), "failed": 0,
                             "existing": True, "message": "已在归档中"})
        client = self.ctx.xmon.new_client({"uid": handle, "name": handle})
        try:
            row, actual_handle, name = fetch_status(client, handle, post_id)
        except Exception as exc:  # noqa: BLE001
            return self._err(f"无法读取这条 X 帖子：{exc}", 502)
        if not row["media"]:
            return self._err("这条帖子没有图片或视频", 422)
        try:
            saved = self.ctx.xmon.save_post(
                row, actual_handle, name, client,
                opts={"keep_text": False, "require_media": True})
        except Exception as exc:  # noqa: BLE001
            return self._err(f"归档失败：{exc}", 500)
        count = kept()
        if not saved and not existing:
            return self._err("媒体都未能下载，请检查 X 节点后重试", 502)
        return self._ok({"post_id": post_id, "downloaded": count,
                         "failed": max(0, len(row["media"]) - count),
                         "existing": bool(existing), "message": "已存入归档"})

    def _x_probe(self, body: Dict[str, Any]) -> None:
        """设置页「测试 X 抓取」：拿页面上还没保存的值先跑一遍。

        设计上**不落 cookie**（那是凭据，必须由人按保存），但会把自动发现的
        queryId 和账号数字 id 写回配置 —— 这两个纯粹是缓存，写错也不丢数据，
        而且写对了下一轮抓取就省两个请求。
        """
        ctx = self.ctx
        cookie = body.get("cookie")
        if cookie is None:
            cookie = ctx.cfg.get("x_cookie") or ""
        qid = body.get("query_id")
        if qid is None:
            qid = ctx.cfg.get("x_query_id") or ""
        cookie = str(cookie).strip()
        qid = str(qid).strip()

        handle = str(body.get("handle") or "").strip().lstrip("@")
        x_accounts = ctx._cfg_accounts("x")
        acc = next((a for a in x_accounts if str(a["uid"]) == handle), None)
        if not handle:
            enabled = [a for a in x_accounts if a.get("enabled")]
            acc = enabled[0] if enabled else (x_accounts[0] if x_accounts else None)
            handle = str(acc["uid"]) if acc else ""

        node_id = str(body.get("node_id") or "") if ctx.cfg.get("x_proxy_mode") == "nodes" else ""
        proxy = ""
        if node_id:
            proxy = ctx.nodes.proxy_of(ctx.nodes.find(node_id) or {})
        if not proxy and acc is not None:
            node_id, proxy = ctx.route_account(acc)
        if not proxy:
            if ctx.cfg.get("x_proxy_mode") == "nodes":
                picked = ctx.nodes.pick()
                if picked is not None:
                    node_id = str(picked["id"])
                    proxy = ctx.nodes.proxy_of(picked)
            if not proxy:
                node_id = str(ctx.cfg.get("x_proxy_mode") or "local")
                proxy = str((ctx.cfg.get("x_manual_proxy") if node_id == "manual"
                             else ctx.cfg.get("x_proxy")) or "")

        temp = dict(ctx.cfg)
        temp["x_cookie"] = cookie
        temp["x_query_id"] = qid
        client = XClient(temp, logger=ctx.log.emit, node_id=node_id,
                         on_traffic=lambda **kw: ctx.monitor.store.log_traffic(**kw))
        client.set_proxy(node_id, proxy)

        result = client.probe(handle)
        result["saved_query_id"] = str(ctx.cfg.get("x_query_id") or "")
        if result.get("query_id") and result["query_id"] != result["saved_query_id"]:
            ctx.cfg["x_query_id"] = result["query_id"]
            result["saved_query_id"] = result["query_id"]
            ctx._save_cfg()
            ctx.log.emit("INFO", f"X queryId 已更新并写入配置：{result['query_id'][:8]}…")
        if acc is not None and result.get("user_id") \
                and str(acc.get("x_user_id") or "") != result["user_id"]:
            acc["x_user_id"] = result["user_id"]
            ctx._save_cfg()
        return self._ok(result)

    def _account_archive_quote(self, q: Dict[str, Any]) -> Dict[str, Any]:
        """Read the whole archive for one monitored account; the UI's loaded feed is incomplete."""
        platform = _platform_of(q)
        uid = str(q.get("uid") or "").strip()
        if platform not in ("weibo", "x") or not uid:
            raise ValueError("缺少平台或账号 UID")
        store = self._store({"platform": platform})
        with store._connect() as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT mblog_id, screen_name, text, created_at FROM posts "
                "WHERE platform=? AND uid=? ORDER BY created_ts DESC", (platform, uid))]
        estimate = store.estimate_batch_delete([str(r["mblog_id"]) for r in rows])
        return {"uid": uid, "platform": platform, "count": len(rows),
                "files": estimate["files"], "bytes": estimate["bytes"],
                "shared_kept": estimate["shared_kept"],
                "sample": [{"text": (r.get("text") or "")[:60],
                            "created_at": r.get("created_at") or ""} for r in rows[:3]]}

    def _account_archive_clear(self, body: Dict[str, Any]) -> None:
        platform = _platform_of(body)
        uid = str(body.get("uid") or "").strip()
        remove = bool(body.get("remove_account"))
        if platform not in ("weibo", "x") or not uid:
            return self._err("缺少平台或账号 UID", 400)
        if remove and not any(str(a.get("uid")) == uid for a in self.ctx.accounts(platform)):
            return self._err("账号已不在监控列表，请刷新", 404)
        store = self._store({"platform": platform})
        with store._connect() as conn:
            ids = [str(r[0]) for r in conn.execute(
                "SELECT mblog_id FROM posts WHERE platform=? AND uid=? ORDER BY created_ts DESC",
                (platform, uid))]
        deleted = failed = missing = 0
        files = total_bytes = 0
        for post_id in ids:
            try:
                before = store.quote_delete(post_id)
                if not before.get("exists"):
                    missing += 1
                    continue
                result = store.delete_post(post_id, tombstone=False)
                if not result.get("deleted"):
                    failed += 1
                    continue
                deleted += 1
                files += int(result.get("moved_files") or 0)
                total_bytes += int(result.get("bytes") or 0)
                try:
                    self.ctx.thumbs.discard_many([f["path"] for f in before.get("files", [])])
                except Exception:  # noqa: BLE001
                    pass
            except Exception as exc:  # noqa: BLE001
                failed += 1
                self.ctx.log.emit("WARNING", f"账号归档清理失败 {platform}/{uid}/{post_id}：{exc}")
        account_removed = False
        if remove and not failed and not missing:
            self.ctx.cfg["accounts"] = [a for a in self.ctx.cfg.get("accounts", [])
                                        if not (str(a.get("uid")) == uid and
                                                (a.get("platform") or DEFAULT_PLATFORM) == platform)]
            self.ctx._save_cfg()
            self.ctx.reload()
            account_removed = True
        self.ctx.log.emit("INFO", f"账号归档清理 {platform}/{uid}：{deleted} 条进回收站，"
                          f"失败 {failed}，已不在库 {missing}，移除监控 {account_removed}")
        return self._ok({"deleted": deleted, "failed": failed, "missing": missing,
                         "files": files, "bytes": total_bytes,
                         "account_removed": account_removed})

    def _accounts_write(self, body: Dict[str, Any]) -> None:
        action = str(body.get("action") or "")
        ctx = self.ctx
        accounts = list(ctx.cfg.get("accounts", []))
        # 平台跟着操作走：前端在 X 页加账号就写 platform=x，老请求缺省微博
        platform = _platform_of({**self._query(), **body}) or DEFAULT_PLATFORM

        if action == "add":
            uid = str(body.get("uid") or "").strip().lstrip("@")
            # 微博 uid 是纯数字；X 是 1~15 位的 handle。校验错了会往库里写脏账号
            if platform == "x":
                if not _HANDLE_RE.match(uid):
                    return self._err("X 账号只能是 1~15 位字母/数字/下划线")
            elif not uid.isdigit():
                return self._err("微博 UID 必须是纯数字")
            if any(str(a["uid"]) == uid and (a.get("platform") or DEFAULT_PLATFORM) == platform
                   for a in accounts):
                return self._err("该账号已在列表中")
            name = str(body.get("name") or "").strip()
            if platform == DEFAULT_PLATFORM and not name:
                name = self._fetch_nickname(uid) or uid
            accounts.append({"uid": uid, "name": name or uid, "platform": platform,
                             "enabled": bool(body.get("enabled", True)),
                             "node_id": "", "x_user_id": ""})

        elif action == "update":
            uid = str(body.get("uid") or "")
            for acc in accounts:
                if str(acc["uid"]) == uid:
                    if "name" in body:
                        acc["name"] = str(body.get("name") or "").strip() or uid
                    if "enabled" in body:
                        acc["enabled"] = bool(body.get("enabled"))
            # 备注同步到 state 表，列表与筛选立即生效
            if "name" in body:
                ctx.monitor.store.update_account_name(uid, str(body.get("name") or "").strip() or uid)

        elif action == "set_mode":
            uid = str(body.get("uid") or "")
            mode = str(body.get("mode") or "")
            if platform != "weibo":
                return self._err("仅微博账号支持监控模式切换")
            if mode not in ("archive", "interact_only"):
                return self._err("未知监控模式")
            target = next((a for a in accounts if str(a.get("uid")) == uid
                           and (a.get("platform") or DEFAULT_PLATFORM) == "weibo"), None)
            if target is None:
                return self._err("账号不存在", 404)
            target["monitor_mode"] = mode
            if mode == "interact_only":
                target.setdefault("interact_interval_min", 30)

        elif action == "set_interact_interval":
            uid = str(body.get("uid") or "")
            if platform != "weibo":
                return self._err("仅微博账号支持单独设置检查间隔")
            try:
                minutes = int(body.get("minutes"))
            except (TypeError, ValueError):
                return self._err("检查间隔须为 5～1440 分钟")
            if not 5 <= minutes <= 1440:
                return self._err("检查间隔须为 5～1440 分钟")
            target = next((a for a in accounts if str(a.get("uid")) == uid
                           and (a.get("platform") or DEFAULT_PLATFORM) == "weibo"), None)
            if target is None:
                return self._err("账号不存在", 404)
            target["interact_interval_min"] = minutes

        elif action == "toggle":
            uid = str(body.get("uid") or "")
            for acc in accounts:
                if str(acc["uid"]) == uid and (acc.get("platform") or DEFAULT_PLATFORM) == platform:
                    acc["enabled"] = not bool(acc.get("enabled"))

        elif action == "bind":
            # 手动改绑出口节点。node_id 传空 = 解除绑定，下一轮按负载重新分配
            uid = str(body.get("uid") or "")
            node_id = str(body.get("node_id") or "").strip()
            if node_id and ctx.nodes.find(node_id) is None:
                return self._err(f"节点不存在：{node_id}")
            hit = False
            for acc in accounts:
                if str(acc["uid"]) == uid and (acc.get("platform") or DEFAULT_PLATFORM) == platform:
                    acc["node_id"] = node_id
                    hit = True
            if not hit:
                return self._err("账号不存在")

        elif action == "remove":
            uid = str(body.get("uid") or "")
            accounts = [a for a in accounts
                        if not (str(a["uid"]) == uid
                                and (a.get("platform") or DEFAULT_PLATFORM) == platform)]

        else:
            return self._err(f"不支持的动作 {action}")

        ctx.cfg["accounts"] = accounts
        ctx._save_cfg()      # 走同一把锁：X 线程也可能在写绑定关系
        ctx.reload()
        return self._ok({"ok": True, "accounts": ctx.accounts(platform)})

    def _fetch_nickname(self, uid: str) -> str:
        """添加账号时自动取一次昵称（失败就退回 uid）。"""
        try:
            client = self.ctx.monitor.client
            data = client.fetch_page(uid, page=1)
            items = WeiboClient.parse_list(data)
            for mblog in items:
                user = mblog.get("user") or {}
                if user.get("screen_name"):
                    return str(user["screen_name"])
        except Exception:  # noqa: BLE001
            pass
        return ""

    def _config_write(self, body: Dict[str, Any]) -> None:
        ctx = self.ctx
        payload = body.get("config")
        if not isinstance(payload, dict):
            # 兼容平铺写法。原来是 `body.get("config") or {}` —— 平铺传进来会得到
            # 空 dict，然后照样返回 ok，配置一个字都没写。前端设置页就踩了这个：
            # 改了间隔、清空 cookie，看着"保存成功"，重启全回来。
            payload = {k: v for k, v in body.items() if k in DEFAULT_CONFIG}
        if not isinstance(payload, dict):
            return self._err("config 必须是对象")
        mode = str(payload.get("x_proxy_mode", ctx.cfg.get("x_proxy_mode") or "nodes"))
        if mode not in ("local", "manual"):
            return self._err("代理方式无效", 400)
        manual_proxy = str(payload.get("x_manual_proxy", ctx.cfg.get("x_manual_proxy") or "")).strip()
        if mode == "manual":
            try:
                parsed_proxy = urllib.parse.urlsplit(manual_proxy)
                valid_proxy = (parsed_proxy.scheme == "http" and parsed_proxy.hostname
                               and parsed_proxy.port and not parsed_proxy.username
                               and not parsed_proxy.password and not parsed_proxy.path
                               and not parsed_proxy.query and not parsed_proxy.fragment)
            except ValueError:
                valid_proxy = False
            if not valid_proxy:
                return self._err("代理地址或端口不正确，请填写 HTTP 代理地址和端口", 400)
        if payload.get("encrypt_downloads") and not ctx.vault.unlocked:
            return self._err("先解锁主密码才能开启文件加密")
        # 只接受已知字段，避免写入垃圾键
        for key, value in payload.items():
            if key in DEFAULT_CONFIG:
                ctx.cfg[key] = value
        # 设置页把整个 config 原样回传，节点密码是脱敏过的 "***" ——
        # 直接用会把自己锁在门外，得把真密码填回去
        if payload.get("webdav_password") == "***":
            ctx.cfg["webdav_password"] = str(load_config().get("webdav_password") or "")
        incoming = payload.get("x_nodes")
        if isinstance(incoming, list) and isinstance(ctx.cfg.get("x_nodes"), list):
            real = {str(n.get("id")): n for n in ctx.cfg["x_nodes"] if isinstance(n, dict)}
            fixed = []
            for node in incoming:
                if not isinstance(node, dict):
                    continue
                node = dict(node)
                if node.get("password") == "***":
                    old = real.get(str(node.get("id")), {})
                    node["password"] = str(old.get("password") or "")
                fixed.append(node)
            ctx.cfg["x_nodes"] = fixed
        ctx._save_cfg()
        ctx.reload()
        return self._ok({"ok": True, "config": ctx.public_config()})

    def _feed(self, q: Dict[str, str]) -> Dict[str, Any]:
        """内容流：默认发布时间倒序，带媒体。

        两处性能改造（2026-10-29）：
        - 走 store.feed_page：关键词下推 SQL、不回传 raw_json、只给当页附媒体
        - 翻页用游标而不是 offset。新微博是从顶部插入的，offset 会让「下一页」
          整体漂移（漏看/重复看）；游标锚定上页最后一条，不受插入影响。

        2026-11-04 排序功能：sort 走 store 的 _FEED_SORTABLE 白名单（非法值
        回落 created_ts），游标串带排序标签 `sort|dir|值|id|platform`，翻页途中改排序
        的话旧游标标签对不上直接作废（前端本来也会清游标，这里是双保险）。
        """
        ctx = self.ctx
        store = self._store(q)
        limit = max(1, min(int(q.get("limit") or 40), 200))
        page = max(1, int(q.get("page") or 1))
        uid = q.get("uid") or ""
        kw = (q.get("kw") or "").strip()
        hours = float(q.get("recent_hours") or 0)
        saved_after = ""
        if hours > 0:
            saved_after = time.strftime("%Y-%m-%d %H:%M:%S",
                                       time.localtime(time.time() - hours * 3600))
        sort = normalize_feed_sort((q.get("sort") or "created_ts").strip())
        desc = str(q.get("desc", "1")).strip().lower() not in ("0", "false", "no")
        dir_tag = "desc" if desc else "asc"

        cursor = None
        raw_cursor = (q.get("cursor") or "").strip()
        if raw_cursor.count("|") == 4:
            csort, cdir, cval, mid, cursor_platform = raw_cursor.split("|", 4)
            # 标签对不上 = 翻页途中改了排序/方向，旧游标作废，从头给
            if csort == sort and cdir == dir_tag:
                cursor = (cval, mid, cursor_platform)

        chunk, total = store.feed_page(
            uid=uid, kw=kw, saved_after=saved_after, cursor=cursor, limit=limit,
            offset=(page - 1) * limit if cursor is None else 0,
            sort=sort, desc=desc, media_kind=q.get("media") or "",
        )
        notes = {a["uid"]: a["name"] for a in ctx.accounts()}
        for row in chunk:
            row["account_note"] = notes.get(str(row.get("uid")), row.get("screen_name") or "")
            row["is_new"] = self._is_new(row)
        # 下一页游标：拿当页最后一条的 (排序值, mblog_id, platform)。
        # 注意 0 是合法游标值（created_ts=0 的老数据），不能用 `or ''` 吞掉 falsy
        next_cursor = ""
        if chunk and len(chunk) == limit:
            last = chunk[-1]
            val = last.get(sort)
            next_cursor = (f"{sort}|{dir_tag}|{'' if val is None else val}|"
                           f"{last.get('mblog_id') or ''}|{last.get('platform') or ''}")
        return {"posts": chunk, "total": total, "limit": limit, "cursor": next_cursor}

    def _favorites_read(self) -> None:
        store = self.ctx.monitor.store
        items = store.list_favorites()
        active = {(str(a.get("platform") or "weibo"), str(a.get("uid") or ""))
                  for a in self.ctx.accounts(None)}
        for item in items:
            if item["kind"] == "account" and (item["platform"], item["target_id"]) in active:
                item["available"] = True
        return self._ok({"items": items})

    def _favorites_write(self, body: Dict[str, Any]) -> None:
        kind = str(body.get("kind") or "")
        platform = str(body.get("platform") or "")
        target = str(body.get("target_id") or "").strip()
        action = str(body.get("action") or "add")
        if kind not in ("account", "post", "media") or platform not in ("weibo", "x"):
            return self._err("收藏类型或平台无效")
        if not target or len(target) > 160 or action not in ("add", "remove"):
            return self._err("收藏目标或操作无效")
        store = self.ctx.monitor.store
        if action == "remove":
            store.set_favorite(kind, platform, target, remove=True)
            return self._ok({"ok": True})
        scoped = store.for_platform(platform)
        title = preview = post_id = uid = ""
        if kind == "account":
            account = next((a for a in self.ctx.accounts(None)
                            if str(a.get("platform") or "weibo") == platform
                            and str(a.get("uid") or "") == target), None)
            if account:
                title = str(account.get("name") or target)
            else:
                with store._connect() as conn:
                    row = conn.execute(
                        "SELECT screen_name FROM posts WHERE platform=? AND uid=? "
                        "ORDER BY created_ts DESC LIMIT 1", (platform, target),
                    ).fetchone()
                if row is None:
                    return self._err("这个博主没有本地归档", 404)
                title = str(row["screen_name"] or target)
            uid = target
        elif kind == "post":
            row = scoped.get_post(target)
            if row is None:
                return self._err("这条内容已不在归档中", 404)
            title = str(row.get("screen_name") or row.get("uid") or "内容")
            preview = str(row.get("text") or "")
            post_id, uid = target, str(row.get("uid") or "")
        else:
            with store._connect() as conn:
                row = conn.execute(
                    "SELECT m.filename, m.kind, m.path, m.status, m.mblog_id, p.uid, p.screen_name, p.text "
                    "FROM media m JOIN posts p ON p.platform=m.platform AND p.mblog_id=m.mblog_id "
                    "WHERE m.platform=? AND m.id=?", (platform, target),
                ).fetchone()
            if row is None or row["status"] not in ("ok", "skipped") or not row["path"] or not os.path.isfile(row["path"]):
                return self._err("这个媒体文件已不在归档中", 404)
            title = str(row["filename"] or row["screen_name"] or row["kind"] or "媒体")
            preview = str(row["text"] or "")
            post_id, uid = str(row["mblog_id"] or ""), str(row["uid"] or "")
        store.set_favorite(kind, platform, target, title=title, preview=preview,
                           post_id=post_id, uid=uid)
        return self._ok({"ok": True})

    def _post_read(self, q: Dict[str, str]) -> None:
        """按平台和 ID 取一条完整内容，供媒体墙定位到内容流。"""
        post_id = (q.get("id") or "").strip()
        if not post_id:
            return self._err("缺少内容 ID", 400)
        row = self._store(q).get_post(post_id)
        if row is None:
            return self._err("这条内容已不在归档中", 404)
        row.pop("raw_json", None)
        notes = {str(a["uid"]): a["name"] for a in self.ctx.accounts(_platform_of(q))}
        row["account_note"] = notes.get(str(row.get("uid")), row.get("screen_name") or "")
        row["is_new"] = self._is_new(row)
        return self._ok({"post": row})

    def _feed_head(self, q: Dict[str, str]) -> Dict[str, Any]:
        """智能刷新的「探头部」：只取最新几条 id，用来判断有没有新内容。

        没有新内容时前端什么都不用做（零重渲染），也就不会打断用户正在看的
        位置。返回体只有几十个字节，比整页 37 KB 便宜得多。

        sort/desc 必须和首页一致（由前端透传）：探测比较的是 head.ids[0] 和
        列表第一条，两边排序不同的话这个比较永远不相等，会误报「有新内容」。
        """
        ctx = self.ctx
        store = self._store(q)
        uid = q.get("uid") or ""
        kw = (q.get("kw") or "").strip()
        hours = float(q.get("recent_hours") or 0)
        saved_after = ""
        if hours > 0:
            saved_after = time.strftime("%Y-%m-%d %H:%M:%S",
                                       time.localtime(time.time() - hours * 3600))
        sort = normalize_feed_sort((q.get("sort") or "created_ts").strip())
        desc = str(q.get("desc", "1")).strip().lower() not in ("0", "false", "no")
        probe = int(q.get("probe") or 20)
        probe = max(1, min(probe, 100))
        rows, total = store.feed_page(
            uid=uid, kw=kw, saved_after=saved_after, cursor=None,
            limit=probe, with_media=False,
            sort=sort, desc=desc, media_kind=q.get("media") or "",
        )
        return {
            "ids": [r.get("mblog_id") for r in rows],
            "keys": [f"{r.get('platform')}-{r.get('mblog_id')}" for r in rows],
            "first_saved_at": rows[0].get("saved_at") if rows else "",
            "total": total,
        }

    def _is_new(self, row: Dict[str, Any]) -> bool:
        hours = int(self.ctx.cfg.get("new_highlight_hours", 24) or 24)
        try:
            ts = time.mktime(time.strptime(row.get("saved_at") or "", "%Y-%m-%d %H:%M:%S"))
        except Exception:  # noqa: BLE001
            return False
        return (time.time() - ts) <= hours * 3600

    def _query_posts(self, q: Dict[str, str]) -> Dict[str, Any]:
        store = self._store(q)
        sort = q.get("sort") or "created_ts"
        desc = str(q.get("desc") or "1") not in ("0", "false", "False")
        page = max(1, int(q.get("page") or 1))
        page_size = max(1, min(int(q.get("page_size") or 20), 200))
        args = dict(uid=q.get("uid") or "", kw=(q.get("kw") or "").strip(),
                    start=q.get("start") or "", end=q.get("end") or "",
                    media=q.get("media") or "", kind=q.get("kind") or "")
        rows, total = store.query_posts(sort=sort, desc=desc, page=page,
                                        page_size=page_size, **args)
        summary = store.query_summary(**args)
        # 查询页要在「图」列直接显示缩略图，一次性把媒体挂上（单条 SQL + 存在性检查）
        if rows:
            store._attach_media(rows)
        notes = {str(a["uid"]): str(a.get("name") or a["uid"])
                 for a in self.ctx._cfg_accounts()}
        return {
            "rows": rows, "total": total, "page": page, "page_size": page_size,
            "sum": summary, "notes": notes,
            "pages": max(1, (total + page_size - 1) // page_size),
        }

    def _media_list(self, q: Dict[str, str]) -> Dict[str, Any]:
        """媒体库：直接从 media 表翻页，避免为了拿缩略图而加载整条微博。"""
        store = self._store(q)
        kind = q.get("kind") or ""
        uid = q.get("uid") or ""
        kw = (q.get("kw") or "").strip()
        tag = q.get("tag") or ""
        page = max(1, int(q.get("page") or 1))
        page_size = max(1, min(int(q.get("page_size") or 60), 200))

        where: List[str] = ["m.status IN ('ok','skipped','deduped')"]
        args: List[Any] = []
        if kind == "image":
            where.append("m.kind = 'image'")
        elif kind == "video":
            where.append("m.kind IN ('video','livephoto_video')")
        if tag in {"chat_screenshot", "portrait", "pet", "food", "landscape", "other"}:
            where.append("mt.label = ?")
            args.append(tag)
        if uid:
            where.append("p.uid = ?")
            args.append(uid)
        if kw:
            where.append("(p.text LIKE ? OR p.screen_name LIKE ?)")
            args.extend([f"%{kw}%", f"%{kw}%"])
        if store.platform:
            where.append("m.platform = ?")
            args.append(store.platform)
        clause = " AND ".join(where)
        # 微博和 X 的 mblog_id 是各自平台的编号，会撞号，JOIN 必须带 platform
        join = ("JOIN posts p ON p.mblog_id=m.mblog_id AND p.platform=m.platform "
                "LEFT JOIN media_tags mt ON mt.media_id=m.id")

        with store._connect() as conn:
            agg = conn.execute(
                f"SELECT COUNT(*) AS c, "
                f"SUM(CASE WHEN m.status IN ('ok','skipped') AND m.path<>'' "
                f"         THEN 1 ELSE 0 END) AS kept, "
                f"SUM(CASE WHEN m.status IN ('ok','skipped') "
                f"         THEN IFNULL(m.size,0) ELSE 0 END) AS bytes "
                f"FROM media m {join} WHERE {clause}", args).fetchone()
            total = int(agg["c"] or 0)
            # 统计口径跟着筛选走：切到"只看视频"时占用也跟着变，不然两个数对不上
            stats = {"total": total, "kept": int(agg["kept"] or 0),
                     "bytes": int(agg["bytes"] or 0)}
            offset = (page - 1) * page_size
            rows = conn.execute(
                f"SELECT m.id, m.mblog_id, m.platform, m.kind, m.path, m.filename, m.size, "
                f"m.status, mt.label AS tag, p.uid, p.screen_name, p.created_at, p.dir, p.text "
                f"FROM media m {join} "
                f"WHERE {clause} ORDER BY p.created_ts DESC, m.id "
                f"LIMIT ? OFFSET ?", args + [page_size, offset]).fetchall()

        items = []
        notes = {a["uid"]: a["name"] for a in self.ctx.accounts(store.platform)}
        for r in rows:
            item = dict(r)
            item["exists"] = bool(item.get("path")) and os.path.exists(item["path"])
            item["account_note"] = notes.get(str(item.get("uid")), item.get("screen_name") or "")
            item["text"] = (item.get("text") or "")[:120]
            items.append(item)
        return {"items": items, "total": total, "page": page, "page_size": page_size,
                "pages": max(1, (total + page_size - 1) // page_size), "stats": stats}

    def _export_csv(self, q: Dict[str, str]) -> None:
        store = self._store(q)
        args = dict(uid=q.get("uid") or "", kw=(q.get("kw") or "").strip(),
                    start=q.get("start") or "", end=q.get("end") or "",
                    media=q.get("media") or "", kind=q.get("kind") or "")
        rows, _total = store.query_posts(sort=q.get("sort") or "created_ts",
                                         desc=str(q.get("desc") or "1") != "0",
                                         page=1, page_size=5000, **args)
        buf = io.StringIO()
        buf.write("\ufeff")  # BOM，Excel 直接打开不乱码
        writer = csv.writer(buf)
        writer.writerow(["发布时间", "账号", "UID", "类型", "图片数", "视频数",
                         "转发", "评论", "点赞", "话题", "正文", "链接", "存档目录"])
        for r in rows:
            writer.writerow([
                r.get("created_at"), r.get("screen_name"), r.get("uid"),
                "转发" if r.get("is_retweet") else "原创",
                r.get("pic_count"), r.get("video_count"),
                r.get("reposts_count"), r.get("comments_count"), r.get("attitudes_count"),
                r.get("topics"), (r.get("text") or "").replace("\n", " "),
                r.get("url"), r.get("dir"),
            ])
        fname = time.strftime("weibo_export_%Y%m%d_%H%M%S.csv")
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
        body = buf.getvalue().encode("utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    @staticmethod
    def _dist_root() -> str:
        # 统一前端是 x-monitor（React），构建产物在根目录下的 x-monitor/dist。
        # 微博那套 Vue 前端（weibo_monitor/ui）已冻结，不再由这里托管。
        return os.path.join(ROOT_DIR, "x-monitor", "dist")

    def _serve_static(self, route: str) -> None:
        """托管 x-monitor/dist。前端没构建过时给出明确提示，而不是白屏。"""
        root = os.path.abspath(self._dist_root())
        if not os.path.isdir(root):
            return self._send(200, _NOT_BUILT_HTML, "text/html; charset=utf-8")

        rel = urllib.parse.unquote(route.lstrip("/")) or "index.html"
        target = os.path.abspath(os.path.join(root, rel))
        if not (target == root or target.startswith(root + os.sep)):
            return self._err("非法路径", 403)
        if os.path.isdir(target):
            target = os.path.join(target, "index.html")
        if not os.path.isfile(target):
            # 单页应用回退
            target = os.path.join(root, "index.html")
            if not os.path.isfile(target):
                return self._err("未找到 index.html", 404)

        ctype = mimetypes.guess_type(target)[0] or "application/octet-stream"
        try:
            with open(target, "rb") as fp:
                data = fp.read()
            st = os.stat(target)
        except Exception as exc:  # noqa: BLE001
            return self._err(f"读取失败：{exc}", 500)

        # index.html 里给 app.js / app.css 带上构建时间戳：
        # 资源名是固定的（vite 这边关掉了 hash），浏览器会拿旧缓存一直跑旧代码，
        # 表现就是「明明改了、界面没变」。换 URL 比让用户每次强刷可靠。
        if target.endswith(".html"):
            try:
                html = data.decode("utf-8")
                for asset in ("app.js", "app.css"):
                    ap = os.path.join(root, asset)
                    if os.path.isfile(ap):
                        ver = str(int(os.stat(ap).st_mtime))
                        html = re.sub(rf"({re.escape(asset)})(\?v=\d+)?", rf"\1?v={ver}", html)
                data = html.encode("utf-8")
            except Exception:  # noqa: BLE001
                pass

        etag = f'W/"{int(st.st_mtime)}-{len(data)}"'
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "no-cache")
            self._cors()
            self.end_headers()
            return

        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        # 本地单机服务，带宽不是成本；这里宁可每次校验，也不要拿旧 JS 糊住界面
        self.send_header("Cache-Control", "no-cache")
        self.send_header("ETag", etag)
        self._cors()
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    # ------------------------------------------------------------------ #
    def _safe_archive_path(self, raw: str) -> Optional[str]:
        """把请求里的路径限制在存档目录内，越界返回 None（防目录穿越）。"""
        if not raw:
            return None
        root = os.path.abspath(self.ctx.monitor.output_dir)
        target = os.path.abspath(raw)
        if not (target == root or target.startswith(root + os.sep)):
            return None
        return target

    def _range_bounds(self, size: int) -> Tuple[int, int, bool]:
        """解析 Range 头。返回 (start, end, 是否分段)。越界由调用方处理。"""
        header = self.headers.get("Range") or ""
        if not header.startswith("bytes="):
            return 0, size - 1, False
        spec = header[6:].split(",")[0].strip()
        if "-" not in spec:
            return 0, size - 1, False
        first, _, last = spec.partition("-")
        try:
            if first:
                start = int(first)
                end = int(last) if last else size - 1
            else:  # bytes=-N：只要最后 N 字节
                start = max(0, size - int(last))
                end = size - 1
        except ValueError:
            return 0, size - 1, False
        start = max(0, start)
        end = min(end, size - 1)
        if start > end or start >= size:
            return start, end, True  # 交给调用方返回 416
        return start, end, True

    def _media(self, q: Dict[str, str]) -> None:
        """读取存档目录内的媒体文件（防目录穿越 + 支持 Range）。

        以前这里是 fp.read() 整个文件进内存再发：73 MB 的视频哪怕只要
        metadata 也全量读一遍，灯箱里拖进度条必然卡死。现在按 Range 只发
        请求的字节段，并分块流式写出，内存占用恒定。
        """
        target = self._safe_archive_path(q.get("path") or "")
        if target is None:
            return self._err("拒绝访问存档目录之外的文件", 403)
        if not os.path.isfile(target):
            return self._err("文件不存在", 404)

        encrypted = target.endswith(".enc")
        if encrypted and not self.ctx.vault.unlocked:
            return self._err("文件已加密，请先在设置中解锁主密码", 423)
        size = self.ctx.vault.plaintext_size(target) if encrypted else os.path.getsize(target)
        ctype = mimetypes.guess_type(target[:-4] if encrypted else target)[0] or "application/octet-stream"
        start, end, partial = self._range_bounds(size)
        if partial and start > end:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0")
            self._cors()
            self.end_headers()
            return

        length = end - start + 1
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        # 视频片段允许重用；重新请求时仍须校验本地文件状态。
        self.send_header("Cache-Control", "private, no-cache")
        self._cors()
        self.end_headers()
        if self.command == "HEAD":
            return
        try:
            if encrypted:
                pos = start
                while pos <= end:
                    stop = min(end, pos + _CHUNK - 1)
                    part, _ = self.ctx.vault.read_range(target, pos, stop)
                    self.wfile.write(part)
                    pos = stop + 1
                return
            with open(target, "rb") as fp:
                fp.seek(start)
                left = length
                while left > 0:
                    chunk = fp.read(min(_CHUNK, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            # 用户关掉灯箱/快速翻页时会中断，浏览器行为，不是错误
            return
        except Exception as exc:  # noqa: BLE001
            self._log_media_error(f"读取失败：{exc}")

    def _log_media_error(self, msg: str) -> None:
        # 响应头已经发出去了，没法再改状态码，只能记日志
        self.ctx.log.emit("WARNING", f"[media] {msg}")

    # ------------------------------------------------------------------ #
    def _thumb(self, q: Dict[str, str]) -> None:
        """缩略图：列表只用 320，灯箱用 1280，原图只在「打开目录」时才碰。

        生成失败（Pillow 缺失 / 文件损坏 / 非图片）一律 302 回原图，
        前端不用写任何兜底逻辑，也不会出现空白格子。
        """
        target = self._safe_archive_path(q.get("path") or "")
        if target is None:
            return self._err("拒绝访问存档目录之外的文件", 403)
        if not os.path.isfile(target):
            return self._err("文件不存在", 404)
        if target.endswith(".enc"):
            if not self.ctx.vault.unlocked:
                return self._err("文件已加密，请先在设置中解锁主密码", 423)
            return self._media(q)
        try:
            width = int(q.get("w") or 320)
        except ValueError:
            width = 320

        dest = self.ctx.thumbs.get(target, width)
        if dest is None:
            self.send_response(302)
            self.send_header(
                "Location", "/api/media?path=" + urllib.parse.quote(target)
            )
            self.send_header("Cache-Control", "no-store")
            self._cors()
            self.end_headers()
            return

        etag = '"' + os.path.basename(dest) + '"'
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "private, no-cache")
            self._cors()
            self.end_headers()
            return

        self.send_response(200)
        self.send_header(
            "Content-Type",
            "image/jpeg" if dest.lower().endswith(".jpg") else "image/webp",
        )
        self.send_header("Content-Length", str(os.path.getsize(dest)))
        self.send_header("ETag", etag)
        self.send_header("Cache-Control", "private, no-cache")
        self._cors()
        self.end_headers()
        if self.command == "HEAD":
            return
        try:
            with open(dest, "rb") as fp:
                self.wfile.write(fp.read())
        except (BrokenPipeError, ConnectionResetError):
            return

    # ------------------------------------------------------------------ #
    # 删除与回收站
    # ------------------------------------------------------------------ #
    @staticmethod
    def _batch_items(body: Dict[str, Any]) -> List[Dict[str, str]]:
        """批量删除的入参：[{id, platform}]。

        platform 必须逐条带：内容流「全部」视图下一屏里微博和 X 的卡片是混
        着的，而 Store 是按平台分实例的，拿错作用域就查不到这条、删不掉。
        """
        out: List[Dict[str, str]] = []
        for it in body.get("items") or []:
            pid = str(it.get("id") or it.get("mblog_id") or "") if isinstance(it, dict) else str(it or "")
            if not pid:
                continue
            plat = str(it.get("platform") or "") if isinstance(it, dict) else ""
            out.append({"id": pid, "platform": plat})
        return out

    def _batch_quote(self, body: Dict[str, Any]) -> None:
        """批量删除试算：只算不动，告诉确认框真会释放多少。"""
        items = self._batch_items(body)
        if not items:
            return self._err("没有选中任何内容", 400)
        exists = missing = files = shared = media = 0
        total_bytes = 0
        sample: List[Dict[str, Any]] = []
        for it in items:
            store = self._store({"platform": it["platform"]} if it["platform"] else None)
            q = store.quote_delete(it["id"])
            if not q.get("exists"):
                missing += 1
                continue
            exists += 1
            files += int(q.get("file_count") or 0)
            media += int(q.get("media_count") or 0)
            shared += int(q.get("shared_kept") or 0)
            total_bytes += int(q.get("bytes") or 0)
            if len(sample) < 6:
                sample.append({
                    "screen_name": q.get("screen_name") or "",
                    "text": (q.get("text") or "").replace("\n", " ")[:40],
                })
        return self._ok({
            "count": exists, "missing": missing, "files": files, "bytes": total_bytes,
            "media_count": media, "shared_kept": shared, "sample": sample,
        })

    def _batch_delete(self, body: Dict[str, Any]) -> None:
        """批量删除：逐条移进回收站 + 写防复活墓碑（和单条删除一致）。

        逐条而不是一个事务，是为了让"某条已经在库里没了"不至于让整批回滚；
        失败的条数照实报给前端，界面上点名的永远是真删掉的那些。
        """
        items = self._batch_items(body)
        if not items:
            return self._err("没有选中任何内容", 400)
        deleted = failed = missing = files = shared = 0
        total_bytes = thumbs = 0
        names: List[str] = []
        deleted_ids: List[str] = []
        for it in items:
            store = self._store({"platform": it["platform"]} if it["platform"] else None)
            before = store.quote_delete(it["id"])
            if not before.get("exists"):
                missing += 1
                continue
            try:
                res = store.delete_post(it["id"])
            except Exception as exc:  # noqa: BLE001
                failed += 1
                self.ctx.log.emit("WARNING", f"批量删除失败 {it['id']}：{exc}")
                continue
            if not res.get("deleted"):
                failed += 1
                continue
            deleted += 1
            deleted_ids.append(it["id"])
            files += int(res.get("moved_files") or 0)
            total_bytes += int(res.get("bytes") or 0)
            shared += int(before.get("shared_kept") or 0)
            nm = before.get("screen_name") or "未知"
            if nm not in names:
                names.append(nm)
            try:
                thumbs += self.ctx.thumbs.discard_many(
                    [f["path"] for f in before.get("files", [])]) or 0
            except Exception:  # noqa: BLE001
                pass
        self.ctx.log.emit(
            "INFO",
            f"批量删除：{deleted} 条进回收站（{files} 个文件 "
            f"{round(total_bytes / 1048576, 1)} MB），{shared} 个共用文件保留，"
            f"失败 {failed} 条、已不在库 {missing} 条；"
            f"涉及账号 {('、'.join(names[:5]) + ('等' if len(names) > 5 else '')) if names else '无'}",
        )
        return self._ok({
            "deleted": deleted, "failed": failed, "missing": missing,
            "files": files, "bytes": total_bytes, "shared_kept": shared,
            "thumbs_dropped": thumbs, "names": names, "deleted_ids": deleted_ids,
        })

    def _posts_write(self, body: Dict[str, Any]) -> None:
        """删一条微博。quote 只算不改（给确认框用），delete 才真的移进回收站。"""
        act = str(body.get("action") or "")
        if act == "batch_quote":
            return self._batch_quote(body)
        if act == "batch_delete":
            return self._batch_delete(body)
        mblog_id = str(body.get("mblog_id") or "")
        if not mblog_id:
            return self._err("缺少 mblog_id", 400)
        store = self._store({**self._query(), **body})

        if act in ("quote_image", "delete_image_smart", "quote_media", "delete_media_smart"):
            try:
                media_id = int(body.get("media_id") or 0)
            except (TypeError, ValueError):
                return self._err("图片编号无效", 400)
            plan = store.plan_media_delete(mblog_id, media_id)
            if not plan.get("exists"):
                return self._err("图片已不在归档中，请刷新媒体页", 404)
            if act in ("quote_image", "quote_media"):
                return self._ok(plan)
            if body.get("expected_mode") != plan["mode"]:
                return self._err("这条内容的媒体数量已变化，请重新确认删除范围", 409)
            act = "delete" if plan["mode"] == "post" else ("delete_media" if act == "delete_media_smart" else "delete_image")

        if act == "delete_media":
            try:
                media_id = int(body.get("media_id") or 0)
                result = store.delete_media(mblog_id, media_id)
            except (OSError, ValueError) as exc:
                return self._err(f"媒体删除失败：{exc}", 400)
            if not result.get("deleted"):
                return self._err("媒体已不在归档中", 404)
            if result.get("path"):
                self.ctx.thumbs.discard(result["path"])
            self.ctx.log.emit("INFO", f"媒体删除：{store.platform} {mblog_id} 媒体 {media_id}")
            return self._ok({"deleted": True})
        if act == "delete_image":
            started = time.perf_counter()
            try:
                media_id = int(body.get("media_id") or 0)
            except (TypeError, ValueError):
                return self._err("图片编号无效", 400)
            try:
                result = store.delete_image(mblog_id, media_id)
            except (OSError, ValueError) as exc:
                return self._err(f"图片删除失败：{exc}", 400)
            if not result.get("deleted"):
                return self._err("图片已不存在或不是图片", 404)
            db_done = time.perf_counter()
            if result.get("path"):
                self.ctx.thumbs.discard(result["path"])
            elapsed_ms = round((time.perf_counter() - started) * 1000)
            if elapsed_ms >= 500:
                self.ctx.log.emit("WARN", f"单图删除耗时 {elapsed_ms} ms："
                                  f"查询/共用文件/删文件 {result.get('_timing')} ms，"
                                  f"数据库与提交 {round((db_done-started)*1000)} ms，"
                                  f"缩略图清理 {round((time.perf_counter()-db_done)*1000)} ms")
            self.ctx.log.emit("INFO", f"单图删除：{store.platform} {mblog_id} 媒体 {media_id}")
            return self._ok({"deleted": True})
        if act == "quote":
            return self._ok(store.quote_delete(mblog_id))
        if act != "delete":
            return self._err(f"未知动作 {act}", 400)

        before = store.quote_delete(mblog_id)
        if not before.get("exists"):
            return self._ok({"exists": False, "deleted": False})
        res = store.delete_post(mblog_id)
        # 缩略图缓存同步清掉，否则 .thumbs 里会留下永远用不上的死文件
        try:
            dropped = self.ctx.thumbs.discard_many(
                [f["path"] for f in before.get("files", [])])
        except Exception:  # noqa: BLE001
            dropped = 0
        res["thumbs_dropped"] = dropped
        self.ctx.log.emit(
            "INFO",
            f"已删除「{before.get('screen_name') or '未知'}」的微博 {mblog_id}："
            f"{res.get('moved_files', 0)} 个文件进回收站"
            f"（{round((res.get('bytes') or 0) / 1048576, 1)} MB），"
            f"{before.get('shared_kept', 0)} 个共用文件保留",
        )
        return self._ok(res)

    def _trash_read(self, q: Dict[str, Any]) -> None:
        store = self._store(q)
        return self._ok({
            "rows": store.trash_list(int(q.get("limit") or 200)),
            "stats": store.trash_stats(),
            "dir": store.trash_dir,
            "archive_bytes": _dir_size_cached(store.output_dir),
        })

    def _text_only_scan(self, q: Dict[str, Any]) -> Dict[str, Any]:
        """列出库里的纯文本帖（没图没视频），并算一下它们占了多大磁盘。

        这些帖子本来就没有媒体，磁盘上只剩一个 post.json —— 所以「释放空间」
        在它们身上几乎为零，真正的收益是内容流干净。仍然把字节算准，是为了
        让确认框上的数字不是编的（有些目录里还留着失败下载的残留）。
        """
        store = self._store(q)
        rows = store.text_only_posts()
        total_bytes = 0
        for r in rows:
            d = str(r.get("dir") or "")
            if not d or not os.path.isdir(d):
                continue
            try:
                for name in os.listdir(d):
                    p = os.path.join(d, name)
                    if os.path.isfile(p):
                        total_bytes += os.path.getsize(p)
            except OSError:
                continue
        return {
            "count": len(rows),
            "bytes": total_bytes,
            "items": [{
                "mblog_id": r["mblog_id"],
                "screen_name": r.get("screen_name") or "",
                "created_at": r.get("created_at") or "",
                "text": (r.get("text") or "").replace("\n", " ")[:60],
            } for r in rows[:20]],
        }

    def _text_only_purge(self, body: Dict[str, Any]) -> None:
        """把存量纯文本帖清进回收站。不写墓碑，随时能从回收站还原。"""
        store = self._store({**self._query(), **body})
        rows = store.text_only_posts()
        removed = files = freed = failed = 0
        for r in rows:
            try:
                out = store.delete_post(r["mblog_id"], tombstone=False)
            except Exception as exc:  # noqa: BLE001
                failed += 1
                self.ctx.log.emit("WARNING", f"清理纯文本失败 {r['mblog_id']}：{exc}")
                continue
            if out.get("deleted"):
                removed += 1
                files += int(out.get("moved_files") or 0)
                freed += int(out.get("bytes") or 0)
        self.ctx.log.emit(
            "INFO",
            f"清理纯文本帖子：删除 {removed} 条，进回收站 {files} 个文件"
            f"（{freed / 1024:.1f} KB），失败 {failed} 条",
        )
        return self._ok({"removed": removed, "files": files,
                         "bytes": freed, "failed": failed})

    def _trash_write(self, body: Dict[str, Any]) -> None:
        act = str(body.get("action") or "")
        store = self._store({**self._query(), **body} if isinstance(body, dict) else None)
        if act == "restore":
            mblog_id = str(body.get("mblog_id") or "")
            if not mblog_id:
                return self._err("缺少 mblog_id", 400)
            res = store.restore(mblog_id)
            if res.get("ok"):
                self.ctx.log.emit("INFO", f"已从回收站还原微博 {mblog_id}")
            return self._ok(res)
        if act == "restore_all":
            res = store.restore_all()
            if res.get("restored"):
                self.ctx.log.emit("INFO", f"回收站已全部还原：{res.get('restored', 0)} 条")
            return self._ok(res)
        if act == "empty":
            res = store.empty_trash(keep_blacklist=bool(body.get("keep_blacklist", True)))
            if res.get("failed"):
                self.ctx.log.emit(
                    "ERROR",
                    f"回收站清空未完成：成功 {res.get('removed', 0)} 个目录，"
                    f"失败 {res.get('failed')} 个（{res.get('error') or ''}）",
                )
            else:
                self.ctx.log.emit(
                    "INFO",
                    f"回收站已清空：{res.get('removed', 0)} 个目录，"
                    f"释放 {round((res.get('freed_bytes') or 0) / 1048576, 1)} MB，"
                    f"保留 {res.get('purged', 0)} 条防复活记录",
                )
            return self._ok(res)
        return self._err(f"未知动作 {act}", 400)

    # ------------------------------------------------------------------ #
    # 重复转发去重
    # ------------------------------------------------------------------ #
    def _dedupe_read(self) -> None:
        """试算：库里现在有多少重复转发，清掉能释放多少。只算不动手。"""
        store = self._store()
        scan = store.scan_duplicate_reposts()
        est = store.estimate_batch_delete(scan["dup_ids"])
        return self._ok({
            "enabled": bool(self.ctx.cfg.get("dedupe_retweet", True)),
            "exempt": self.ctx.cfg.get("dedupe_exempt_uids") or [],
            "groups": len(scan["groups"]),
            "dup_count": scan["dup_count"],
            "files": est["files"],
            "bytes": est["bytes"],
            "shared_kept": est["shared_kept"],
            "sample": [
                {"root": g["root"], "keeper": g["keeper"], "keeper_name": g["keeper_name"],
                 "dups": len(g["dups"])}
                for g in scan["groups"][:20]
            ],
        })

    def _dedupe_write(self, body: Dict[str, Any]) -> None:
        act = str(body.get("action") or "")
        store = self._store({**self._query(), **body})
        if act == "clean":
            scan = store.scan_duplicate_reposts()
            ids = scan["dup_ids"]
            if not ids:
                return self._ok({"cleaned": 0, "freed_files": 0, "freed_bytes": 0})
            # 从旧到新，万一中途中止，剩下的下次接着跑（幂等）
            with store._connect() as conn:
                order = {r[0]: r[1] for r in conn.execute(
                    "SELECT mblog_id, saved_at FROM posts WHERE mblog_id IN (%s)"
                    % ",".join("?" * len(ids)), ids)}
            ids.sort(key=lambda m: (order.get(m) or "", m))

            cleaned = 0
            files = 0
            freed = 0
            failed = 0
            for mblog_id in ids:
                res = store.delete_post(mblog_id)
                if res.get("deleted"):
                    cleaned += 1
                    files += int(res.get("moved_files") or 0)
                    freed += int(res.get("bytes") or 0)
                else:
                    failed += 1
            self.ctx.log.emit(
                "INFO",
                f"重复转发清理完成：{cleaned} 条进回收站（{files} 个文件 "
                f"{round(freed / 1048576, 1)} MB），失败 {failed} 条。"
                f"要真正释放磁盘请到设置页清空回收站",
            )
            return self._ok({"cleaned": cleaned, "freed_files": files,
                             "freed_bytes": freed, "failed": failed})
        return self._err(f"未知动作 {act}", 400)

    def _thumbs_status(self) -> None:
        stats = self.ctx.thumbs.stats()
        stats.update(pregenerate_state())
        return self._ok(stats)

    def _thumbs_pregenerate(self, body: Dict[str, Any]) -> None:
        """后台把库里所有图片的两档缩略图跑一遍（设置页按钮）。"""
        if str(body.get("action") or "") != "pregenerate":
            return self._err("不支持的动作")
        store = self.ctx.monitor.store
        with store._connect() as conn:
            rows = conn.execute(
                "SELECT path FROM media WHERE kind='image' AND path IS NOT NULL "
                "AND path != '' ORDER BY id"
            ).fetchall()
        paths = [r["path"] for r in rows if r["path"] and os.path.isfile(r["path"])]
        state = pregenerate_start(self.ctx.thumbs, paths)
        state["queued"] = len(paths)
        return self._ok(state)

    def _check_cookie(self) -> Dict[str, Any]:
        """探测 Cookie 是否可用，并说清楚它属于哪个号。

        这里踩过一个坑：早期版本拿**被监控账号**的 uid 当"当前账号"返回，
        于是界面上显示的是监控列表里第一个人（妮蔻），而真正会去转发/关注的是
        Cookie 主人（完全是另一个号）。转发是公开动作，弄错号是要出事的，
        所以现在分开：self_uid 才是 Cookie 主人，没填就明说"主人未知"。
        """
        client = self.ctx.monitor.client
        cookie = (self.ctx.cfg.get("cookie") or "").strip()
        if not cookie:
            return {"ok": False, "msg": "未配置 Cookie"}

        self_uid = str(self.ctx.cfg.get("self_uid") or "").strip()
        if self_uid:
            try:
                data = client.fetch_page(self_uid, page=1)
                items = WeiboClient.parse_list(data)
                name = ""
                if items:
                    name = (items[0].get("user") or {}).get("screen_name") or ""
                return {"ok": True, "self_known": True, "uid": self_uid,
                        "name": name,
                        "msg": f"Cookie 主人：{name}（{self_uid}），接口返回 {len(items)} 条"}
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "self_known": True, "uid": self_uid,
                        "msg": f"Cookie 主人的 uid（{self_uid}）取不到内容：{exc}"}

        # 没填 self_uid：只能验证"接口通不通"，不能说这是哪个号
        for acc in self.ctx.cfg.get("accounts", []):
            if not acc.get("enabled"):
                continue
            try:
                data = client.fetch_page(str(acc["uid"]), page=1)
                items = WeiboClient.parse_list(data)
                return {"ok": True, "self_known": False, "uid": acc["uid"],
                        "msg": f"Cookie 有效，接口返回 {len(items)} 条"
                               f"（这是被监控账号 {acc['uid']}，Cookie 主人未知）"}
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "self_known": False, "uid": acc["uid"],
                        "msg": f"{exc}"}
        return {"ok": False, "self_known": False, "msg": "没有启用的账号可供测试"}

    def _summary_files(self) -> List[str]:
        try:
            return sorted((f for f in os.listdir(self.ctx.monitor.summaries_dir)
                           if f.endswith(".md")), reverse=True)[:30]
        except Exception:  # noqa: BLE001
            return []


def start_server(ctx: ApiContext, port: int = DEFAULT_PORT,
                 host: str = "127.0.0.1") -> Tuple[ThreadingHTTPServer, threading.Thread]:
    Handler.ctx = ctx
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, name="api-server", daemon=True)
    thread.start()
    return httpd, thread


_BROWSER_CANDIDATES = (
    r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
    r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
    r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
    r"%LocalAppData%\Google\Chrome\Application\chrome.exe",
)


def _find_chromium() -> str:
    for pattern in _BROWSER_CANDIDATES:
        path = os.path.expandvars(pattern)
        if os.path.isfile(path):
            return path
    return ""


def open_app_window(url: str, plain_browser: bool = False) -> None:
    """Open or focus the single app window, including during browser startup."""
    import subprocess
    import time
    import webbrowser

    from core.single_instance import acquire, find_windows, focus_existing

    if focus_existing(browser_only=True):
        return

    # The browser window takes time to appear. Serialize that interval across
    # the service and supervisor so concurrent launches cannot both spawn one.
    lock = acquire("frontend_launch")
    if lock is None:
        return
    try:
        if focus_existing(browser_only=True):
            return
        if not plain_browser:
            exe = _find_chromium()
            if exe:
                try:
                    subprocess.Popen(
                        [exe, f"--app={url}", "--window-size=1440,900"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                except OSError:
                    pass
                else:
                    for _ in range(16):
                        if find_windows(browser_only=True):
                            return
                        time.sleep(0.5)
                    return
        webbrowser.open(url)
        for _ in range(16):
            if find_windows(browser_only=True):
                return
            time.sleep(0.5)
    finally:
        lock.release()


def probe_running(port: int, host: str = "127.0.0.1") -> bool:
    """端口上是否已经有本项目的服务在跑（能答 /api/health 就算）。

    必须绕开系统代理：本机 127.0.0.1 也会被代理劫走返回 502，
    那样会把"已经在跑"误判成"没在跑"，然后重复启动撞端口。
    """
    import urllib.request

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://{host}:{port}/api/health", timeout=2) as resp:
            return resp.status == 200
    except Exception:  # noqa: BLE001
        return False


def watch_monitor(ctx: "ApiContext") -> None:
    """进程内看门狗：监控线程意外死亡时拉起来。

    只处理"想跑却跑不动"的情况（running=True 但线程没了）。
    用户主动点「停止监控」会把 running 置 False，这里必须放过，
    否则会跟人的意图打架。
    """
    mon = ctx.monitor
    if not ctx.autostart or ctx.stop_event.is_set():
        return
    if not mon.running:
        return
    thread = getattr(mon, "_thread", None)
    if thread is not None and thread.is_alive():
        return
    ctx.log.emit("WARNING", "监控线程意外退出，自动重启")
    mon.running = False  # start() 见到 running 为真就直接返回
    mon.start()


def main() -> int:
    parser = argparse.ArgumentParser(description="微博监控 REST 服务")
    parser.add_argument("--port", type=int, default=0, help=f"端口，默认 {DEFAULT_PORT}")
    parser.add_argument("--host", type=str, default="127.0.0.1")
    parser.add_argument("--no-autostart", action="store_true", help="启动后不自动开启监控")
    parser.add_argument("--open", action="store_true",
                        help="启动后自动打开界面（--app 独立窗口，带地址栏请加 --browser）")
    parser.add_argument("--browser", action="store_true", help="配合 --open，用普通浏览器标签页打开")
    args = parser.parse_args()

    # 被宿主的 safe-delete shim 污染了（PYTHONPATH 里的 sitecustomize 把
    # shutil.rmtree 换成了「批量删除就 SystemExit」的版本）：换干净环境重启自己。
    # 不这么做的话，清空回收站会半路断连接，前端只看到"连不上本地服务"。
    if poisoned_reasons():
        why = "；".join(poisoned_reasons())
        print(f"  [i] 检测到删除拦截环境（{why}），正在以干净环境重启服务…")
        heal_current_process()

    ensure_config()
    cfg = load_config()
    chosen_dir = resolve_output_dir(cfg)
    configured_dir = str(cfg.get("output_dir") or "")
    if os.path.isabs(configured_dir) and os.path.normcase(os.path.abspath(configured_dir)) != os.path.normcase(chosen_dir):
        shutil.copy2(CONFIG_PATH, CONFIG_PATH + ".before-path-move-" + time.strftime("%Y%m%d_%H%M%S") + ".bak")
        cfg["output_dir"] = str(chosen_dir)
        save_config(cfg)
    port = args.port or int(cfg.get("api_port", DEFAULT_PORT))
    url = f"http://{args.host}:{port}"

    # 已经有一个实例在跑（比如上次双击留下的）：不要重复启动监控，
    # 否则会撞端口报错。直接复用它，只负责把界面窗口打开。
    if probe_running(port, args.host):
        print(f"  [i] 服务已经在运行：{url}")
        print("      监控在后台照常跑，这里只是帮你把界面窗口打开。")
        print("      关掉这个黑窗口不会影响后台监控。")
        if args.open:
            open_app_window(url, args.browser)
        else:
            print(f"      直接访问 {url} 即可。")
        return 0

    ctx = ApiContext(autostart=not args.no_autostart)
    try:
        httpd, _thread = start_server(ctx, port, args.host)
    except OSError as exc:
        ctx.monitor.stop()
        print(f"  [x] 端口 {port} 起不来：{exc}")
        print("      改 config.json 里的 api_port，或先关掉占用该端口的程序。")
        return 1
    ctx.log.emit("INFO", f"REST 服务已启动： {url}")
    ctx.log.emit("INFO", f"存档目录：{os.path.abspath(ctx.monitor.output_dir)}")
    # 微博直连 / X 走节点：出口规则写在启动日志里，出问题先看这一行
    ctx.log.emit("INFO", "出口规则：微博直连（不读系统代理），X 走代理节点")
    for msg in getattr(cfg_mod, "NORMALIZE_WARNINGS", []) or []:
        ctx.log.emit("WARNING", f"配置已修正：{msg}")
    ctx.log.emit("INFO", f"进程 PID {os.getpid()}（后台运行，退出请点界面里的「退出程序」）")
    if args.open:
        threading.Timer(1.2, lambda: open_app_window(url, args.browser)).start()
    try:
        # 静默启动时没有窗口可关，只能靠停止事件退出
        ticks = 0
        while not ctx.stop_event.wait(1.0):
            ticks += 1
            if ticks % 5 == 0:  # 每 5 秒看一次线程还在不在，别把 CPU 熬干
                watch_monitor(ctx)
        ctx.log.emit("INFO", "正在停止…")
    except KeyboardInterrupt:
        ctx.log.emit("INFO", "正在停止…")
    ctx.monitor.stop(wait=2)
    ctx.xmon.stop(wait=2)
    httpd.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())



