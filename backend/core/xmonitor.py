"""X（Twitter）账号常驻监控。

跟微博 Monitor 同构（start / stop / check_now / tick），但三件事不一样：

1. **增量游标是 rest_id（数字）**，不是字符串时间戳。rest_id 是 Twitter 的
   雪花 id，单调递增，所以"比基线大"就是新内容，直接比 int。
2. **翻页深度取决于有没有 cookie**。匿名抓主页一次只有最新 5~20 条，翻不了页；
   填了 cookie 就能沿 cursor 往回翻，页数由 `x_max_pages` 控制。
3. **节点是每账号粘性绑定的**。一个账号认准一个出口节点，连续失败
   `x_failover_threshold` 次才换，换过之后新绑定写回配置 —— 否则每轮重新选路，
   同一个账号今天走 A 明天走 B，流量账单没法按账号归因，也很容易被 X 判定异常。

历史数据仍然走 `tools/import_x.py` 离线导入（那条路不经过限流）。
"""
from __future__ import annotations

import json
import os
import random
import re
import threading
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import neterr
from .config import resolve_output_dir
from .store import Store
from .xclient import XClient, XError

PLATFORM = "x"
PLATFORM_DIR = "x"
LOCAL_TZ = timezone(timedelta(hours=8))

_IMG_EXT = {"jpg", "jpeg", "png", "webp", "gif"}


def _to_ts(text: str) -> int:
    """'2026-10-27 12:23' → 本地时区 epoch 秒。"""
    try:
        dt = datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=LOCAL_TZ)
        return int(dt.timestamp())
    except (ValueError, TypeError):
        return 0


def safe_name(text: str, limit: int = 40) -> str:
    name = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", (text or "").strip())
    return (name[:limit] or "unknown")


class XMonitor:
    def __init__(self, cfg: Dict[str, Any], logger: Optional[Any] = None,
                 store: Optional[Store] = None):
        self.cfg = cfg
        self.logger = logger
        self.output_dir = resolve_output_dir(cfg)
        self.archive_dir = os.path.join(self.output_dir, PLATFORM_DIR)
        os.makedirs(self.archive_dir, exist_ok=True)
        self.store = store or Store(
            os.path.join(self.output_dir, "archive.db"), platform=PLATFORM)
        self.client = XClient(cfg, logger=self.log_emit, on_traffic=self._on_traffic,
                              on_query_id=self._on_query_id)

        self._stop = threading.Event()
        self._wakeup = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.running = False
        self.check_count = 0
        self.new_count = 0
        # 纯文本推文被跳过的条数（archive_text_only=False 时才会涨）
        self.text_skipped = 0
        # 多个监控账号转了同一条原推时，被跳过的那几条（只留最早一份）
        self.dup_skipped = 0
        self.last_round = (0.0, 0)
        self.last_tick = 0.0
        self.last_node_id = ""          # 最近一次真正用过的节点（状态栏显示）
        self._clients: Dict[str, XClient] = {}
        self._fails: Dict[str, int] = {}
        # 账号级错误（封禁 / 私密 / id 绑错）的解禁时间：期间整轮跳过，
        # 免得每 5 分钟用同样的错误刷一次日志
        self._blocked: Dict[str, float] = {}
        self._media_retry_after: Dict[str, float] = {}
        # 正在跑「一键回溯」的账号：回溯期间临时退出定时轮询。
        # 两条链路共用同一个 cookie / 出口节点，并行只会把请求量翻倍、
        # 更容易撞 429，日志也会交织在一起没法排障。
        self.backfill_paused: set = set()

        # ---- 由 ApiContext 注入的回调 ---------------------------------- #
        # 给某个账号挑节点。返回 (node_id, proxy)；exclude 里的节点不要选。
        # 绑定关系的持久化和负载均衡都在 ApiContext / NodeManager 里，
        # XMonitor 自己不该知道配置文件长什么样。
        self.route: Optional[Callable[[Dict[str, Any], set], Tuple[str, str]]] = None
        # 抓到数字 id 后回调，用来写回配置（下次省一个请求）
        self.on_user_id: Optional[Callable[[Dict[str, Any], str], None]] = None
        # queryId 自动发现后回调，同样写回配置
        self.on_query_id: Optional[Callable[[str], None]] = None

    # ------------------------------------------------------------------ #
    def log(self, level: str, message: str) -> None:
        if self.logger is not None:
            emit = getattr(self.logger, "emit", None)
            if callable(emit):
                emit(level, message)
            elif callable(self.logger):
                self.logger(f"[{level}] {message}")
        else:
            print(f"[{level}] {message}")

    def log_emit(self, level: str, message: str) -> None:
        self.log(level, message)

    def _on_traffic(self, **kw: Any) -> None:
        """XClient 的流量回调 → 落库。记账失败绝不能拖垮抓取。"""
        try:
            self.store.log_traffic(**kw)
        except Exception:  # noqa: BLE001
            pass

    def _on_query_id(self, query_id: str) -> None:
        """queryId 自动更新后写回配置，下次启动直接用它。"""
        self.cfg["x_query_id"] = query_id
        if self.on_query_id:
            try:
                self.on_query_id(query_id)
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------ #
    def reload(self, cfg: Dict[str, Any]) -> None:
        self.cfg = cfg
        self.client.reload(cfg)
        for client in self._clients.values():
            client.reload(cfg)
        self.log("INFO", "X 配置已更新")

    # ---- 节点与 client ------------------------------------------------- #
    def _client_for(self, node_id: str, proxy: str) -> XClient:
        """每个出口节点一个 client：cookie / 会话 / 记账都按节点分开。"""
        key = node_id or proxy or "direct"
        client = self._clients.get(key)
        if client is None:
            client = XClient(self.cfg, logger=self.log_emit, node_id=node_id,
                             on_traffic=self._on_traffic, on_query_id=self._on_query_id)
            self._clients[key] = client
        if client.proxy != proxy or client.node_id != node_id:
            client.set_proxy(node_id, proxy)
        return client

    def _direct_client(self) -> XClient:
        """最后一条退路：不经任何节点直连。

        只在「换过节点还是失败」或「配置里一个节点都没有」时用。记账挂在
        名为 direct 的虚拟节点上，所以流量不会凭空消失，也不会被算到某个真实
        节点头上 —— 账本要能解释每一字节是从哪出去的。
        """
        self.last_node_id = "direct"
        client = self._client_for("direct", "")
        # 直连是"最后一条路"，不是主力：给它 20 秒超时 × 3 次重试毫无意义 ——
        # 境内直连 x.com 会在 TLS 握手阶段被静默丢包，一路等到超时才结束，
        # 一轮轮询能因此从 5 秒变成 60 秒。这里把它压到 8 秒、只试一次：
        # 通就通，不通就赶紧下一轮。
        try:
            client.timeout = min(int(self.cfg.get("request_timeout", 20) or 20), 8)
            client.retry = 1
        except Exception:  # noqa: BLE001
            pass
        return client

    def _route_for(self, acc: Dict[str, Any], exclude: Optional[set] = None) -> XClient:
        node_id, proxy = "", str((self.cfg.get("x_manual_proxy") if self.cfg.get("x_proxy_mode") == "manual" else self.cfg.get("x_proxy")) or "").strip()
        if self.route is not None:
            try:
                node_id, proxy = self.route(acc, exclude or set())
            except Exception as exc:  # noqa: BLE001  选路失败就用兜底代理
                self.log("WARNING", f"选节点失败（{exc}），用默认代理")
        client = self._client_for(node_id, proxy)
        self.last_node_id = node_id
        return client

    def new_client(self, acc: Dict[str, Any],
                   exclude: Optional[set] = None) -> XClient:
        """给长任务（回溯 / 体检）单开一个 client，**不进 `_clients` 缓存**。

        出口节点还是走同一套粘性选路，但 session / cookie 是独立的：
        `requests.Session` 不是线程安全的，和轮询线程共用一个会互相踩 ——
        实测在 HTTP handler 线程里复用轮询的 client，连接会直接断掉（空响应）。
        """
        node_id, proxy = "", str((self.cfg.get("x_manual_proxy") if self.cfg.get("x_proxy_mode") == "manual" else self.cfg.get("x_proxy")) or "").strip()
        if self.route is not None:
            try:
                node_id, proxy = self.route(acc, set(exclude or ()))
            except Exception as exc:  # noqa: BLE001  选路失败就用兜底代理
                self.log("WARNING", f"选节点失败（{exc}），用默认代理")
        client = XClient(self.cfg, logger=self.log_emit, node_id=node_id,
                         on_traffic=self._on_traffic, on_query_id=self._on_query_id)
        client.set_proxy(node_id, proxy)
        self.last_node_id = node_id
        return client

    def _remember_user_id(self, acc: Dict[str, Any], client: XClient) -> None:
        # 一定要取**本线程**这次抓到的 id：client 按出口节点复用，同一时间可能
        # 有轮询线程和回溯线程各抓一个账号，`client.last_user_id` 是谁最后写谁算。
        # 曾经因此把 A 的数字 id 写进 B 的配置，下一轮 B 抓的就是 A 的时间线。
        uid_num = getattr(client, "thread_user_id", "") or client.last_user_id
        if not uid_num or str(acc.get("x_user_id") or "") == uid_num:
            return
        acc["x_user_id"] = uid_num      # 内存里先改，回调负责落盘
        if self.on_user_id:
            try:
                self.on_user_id(acc, uid_num)
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------ #
    def start(self) -> None:
        if self.running:
            return
        accounts = self._accounts()
        if not accounts:
            self.log("WARNING", "没有启用的 X 账号，X 监控不启动（微博照常跑）")
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="x-monitor", daemon=True)
        self._thread.start()
        self.running = True

    def stop(self, wait: float = 0.0) -> None:
        """停止 X 轮询。`wait=0` 只置标志立刻返回（同 Monitor.stop 的理由）。"""
        self._stop.set()
        self._wakeup.set()
        self.running = False
        thread = self._thread
        if wait > 0 and thread and thread.is_alive():
            thread.join(timeout=wait)

    def check_now(self) -> None:
        self._wakeup.set()

    def _accounts(self) -> List[Dict[str, Any]]:
        return [a for a in self.cfg.get("accounts", [])
                if a.get("enabled") and (a.get("platform") or "weibo") == PLATFORM]

    def _loop(self) -> None:
        interval = self._interval()
        self.log("INFO", f"X 监控已启动，轮询间隔 {interval} 秒")
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001
                self.log("ERROR", f"X 轮询异常：{exc}")
            interval = self._interval()
            jitter = random.uniform(0, max(0, int(self.cfg.get("poll_jitter_sec", 8))))
            self._wakeup.wait(interval + jitter)
            self._wakeup.clear()

    def _interval(self) -> int:
        return max(60, int(self.cfg.get("x_poll_interval_sec", 300) or 300))

    def _pages(self) -> int:
        """每次轮询最多翻几页。没有 cookie 时翻页没意义，抓主页只有第一屏。"""
        pages = max(1, int(self.cfg.get("x_max_pages", 3) or 1))
        return pages if self.client.has_cookie else 1

    # ------------------------------------------------------------------ #
    def tick(self) -> int:
        accounts = self._accounts()
        if not accounts:
            return 0
        total_new = 0
        dups_before = self.dup_skipped
        no_route = ""
        for i, acc in enumerate(accounts):
            name = acc.get("name") or acc.get("uid")
            key = str(acc.get("uid") or "").lower()
            if key in self.backfill_paused:
                continue    # 正在回溯这个号：暂停定时轮询，跑完自动恢复
            if self._blocked.get(key, 0) > time.time():
                continue    # 账号本身的问题，半小时内不再撞
            try:
                total_new += self.check_account(acc)
            except XError as exc:
                self._fail(acc, str(exc))
                if getattr(exc, "fatal", False):
                    # 封禁 / 私密 / id 绑错：换节点和直连都救不了，别烧请求
                    self._blocked[key] = time.time() + 1800
                    self.log("ERROR", f"[{name}] {exc}（30 分钟内不再重试）")
                    continue
                if getattr(exc, "no_route", False):
                    # 节点池是全账号共用的：一个账号没路可走，剩下的必然一样。
                    # 只报一条，别让 5 个账号把日志刷成 15 行。
                    no_route = str(exc)
                    rest = len(accounts) - i - 1
                    self.log("WARNING",
                             f"X 本轮没有可用出口：{exc}；{self._egress_hint()}"
                             + (f"剩余 {rest} 个账号本轮跳过" if rest else ""))
                    break
                self.log("ERROR", f"[{name}] {exc}")
            except Exception as exc:  # noqa: BLE001
                self.log("ERROR", f"[{name}] 检查失败：{exc}")
                self._fail(acc, str(exc))
            if i < len(accounts) - 1:
                time.sleep(random.uniform(1.5, 3.5))
        if not no_route and self.cfg.get("download_image", True):
            self.repair_incomplete_media(limit=2)
        self.check_count += 1
        self.new_count += total_new
        self.last_tick = time.time()
        self.last_round = (self.last_tick, total_new)
        if self.cfg.get("heartbeat_log", True):
            dups = self.dup_skipped - dups_before
            dup_tail = f"（跳过 {dups} 条重复转推）" if dups else ""
            if no_route:
                tail = "0 条（没有可用出口，先检查代理节点）"
            elif total_new:
                tail = f"新增 {total_new} 条{dup_tail}"
            elif dups:
                tail = f"无新内容，{dup_tail.lstrip('（').rstrip('）')}，{self._interval()} 秒后再查"
            else:
                tail = f"无新内容，{self._interval()} 秒后再查"
            self.log("INFO", f"X 本轮完成：{len(accounts)} 个账号，{tail}")
        return total_new

    def repair_incomplete_media(self, limit: int = 2) -> int:
        """Retry media missing from already indexed tweets without blocking new posts."""
        if self.cfg.get("encrypt_downloads") and not getattr(getattr(self, "vault", None), "unlocked", False):
            return 0
        with self.store._connect() as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT p.mblog_id,p.uid,p.created_at,p.dir,p.raw_json,"
                "p.pic_count,p.video_count FROM posts p WHERE p.platform='x' "
                "AND (p.pic_count>0 OR p.video_count>0) "
                "AND (SELECT COUNT(*) FROM media m WHERE m.platform=p.platform "
                "AND m.mblog_id=p.mblog_id) < COALESCE(p.pic_count,0)+COALESCE(p.video_count,0) "
                "ORDER BY p.saved_at DESC LIMIT 100"
            )]
        accounts = {str(a.get("uid") or "").lower(): a for a in self._accounts()}
        repaired = attempted = 0
        for post in rows:
            if attempted >= limit or self._stop.is_set():
                break
            pid = str(post["mblog_id"])
            if self._media_retry_after.get(pid, 0) > time.time():
                continue
            try:
                raw = json.loads(post.get("raw_json") or "{}")
                if not raw.get("media"):
                    self._media_retry_after[pid] = time.time() + 6 * 3600
                    continue
                attempted += 1
                handle = str(post.get("uid") or "")
                date = (post.get("created_at") or "").split(" ")[0].replace("-", "") or "unknown"
                post_dir = post.get("dir") or os.path.join(
                    self.archive_dir, safe_name(handle), f"{date}_{pid}")
                account = accounts.get(handle.lower())
                client = self.new_client(account) if account else XClient(self.cfg)
                with self.store._connect() as conn:
                    removed_urls = {r[0] for r in conn.execute(
                        "SELECT url FROM media WHERE platform='x' AND mblog_id=? "
                        "AND status='user_deleted'", (pid,))}
                raw["media"] = [m for m in raw["media"]
                                if str(m.get("video") or m.get("url") or "") not in removed_urls]
                saved = self._download_media(raw, post_dir, handle, client)
                for item in saved:
                    self.store.save_media(pid, item["kind"], item["url"],
                                          item["path"], item["size"], item.get("status", "ok"))
                if saved:
                    with self.store._connect() as conn:
                        conn.execute("UPDATE posts SET dir=? WHERE platform='x' AND mblog_id=?",
                                     (post_dir, pid))
                    repaired += 1
                else:
                    self._media_retry_after[pid] = time.time() + 6 * 3600
            except Exception as exc:  # noqa: BLE001
                self._media_retry_after[pid] = time.time() + 6 * 3600
                self.log("WARNING", f"X 媒体补下载失败 {pid}：{exc}")
        if repaired:
            self.log("INFO", f"X 媒体补下载：{repaired} 条记录已补")
        return repaired

    def _egress_hint(self) -> str:
        """节点全挂时补一句「本机到底能不能出国」。

        只报节点错误的话，用户会去翻订阅——但订阅分发站（国内/CDN 可达）和
        节点服务器（境外）根本不是一台机器，订阅打得开不代表节点连得上。
        这里用一次境外/境内对照 TCP 把结论钉死。
        """
        try:
            info = neterr.egress_check()
        except Exception:  # noqa: BLE001
            return ""
        state = info.get("state")
        if state == "blocked":
            return info["detail"] + "；"
        if state == "offline":
            return "本机网络不通（国内站点也连不上）；"
        return ""

    def _fail(self, acc: Dict[str, Any], message: str) -> None:
        try:
            self.store.set_state(str(acc.get("uid")), name=acc.get("name") or "",
                                 last_error=message[:200],
                                 last_check=time.strftime("%Y-%m-%d %H:%M:%S"))
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ #
    def check_account(self, acc: Dict[str, Any]) -> int:
        handle = str(acc.get("uid") or "").lstrip("@")
        name = acc.get("name") or handle
        state = self.store.get_state(handle) or {}
        since_id = str(state.get("since_id") or "")
        if self.cfg.get("encrypt_downloads") and not getattr(getattr(self, "vault", None), "unlocked", False):
            raise RuntimeError("文件加密未解锁；本轮不推进账号检查位置")

        client, rows = self._fetch(acc, handle, since_id)
        self._remember_user_id(acc, client)
        if not rows:
            dropped = int(getattr(client, "last_dropped", 0) or 0)
            if dropped:
                # 抓是抓到了，但没有一条是本人发的（或全是转推且开关是「只抓原创」）——
                # 这不算失败，不能进失败计数（否则会白换节点、白刷警告）
                mode = "原创 + 转推" if getattr(client, "keep_retweets", False) else "只抓原创 Posts"
                self.store.set_state(handle, name=name, since_id=since_id,
                                     last_check=time.strftime("%Y-%m-%d %H:%M:%S"),
                                     last_error="", total=int(state.get("total") or 0))
                self.log("INFO", f"[{name}] 这 {dropped} 条都不是本人原创，"
                                 f"按「{mode}」跳过")
                return 0
            self.log("WARNING", f"[{name}] 没抓到内容")
            self._fail(acc, "没抓到内容")
            return 0

        ids = [int(r["id"]) for r in rows if str(r["id"]).isdigit()]
        max_id = max(ids) if ids else 0

        if not since_id:
            catchup = max(1, int(self.cfg.get("x_catchup_pages", 0) or 0))
            if catchup <= 1:
                # 首次：只建基线。历史内容走 tools/import_x.py 离线导入，
                # 免得"抓到最新 20 条"被误当成"归档完了"
                self.store.set_state(handle, name=name, since_id=str(max_id),
                                     last_check=time.strftime("%Y-%m-%d %H:%M:%S"),
                                     last_error="", total=int(state.get("total") or 0))
                self.log("INFO", f"[{name}] X 基线已建立（最新 id={max_id}），"
                                 f"之后的新推文会自动归档")
                return 0
            # 首次回溯：把这一次抓到的（最多 catchup 页）全部入库
            dups_before = self.dup_skipped
            saved = self._save_rows(rows, handle, name, client)
            dups = self.dup_skipped - dups_before
            self.store.set_state(handle, name=name, since_id=str(max_id),
                                 last_check=time.strftime("%Y-%m-%d %H:%M:%S"),
                                 last_error="", total=int(state.get("total") or 0) + saved)
            extra = f"，跳过 {dups} 条重复转推" if dups else ""
            self.log("INFO", f"[{name}] 首次回溯：归档 {saved} 条（{client.last_pages or 1} 页）"
                             f"{extra}，基线 id={max_id}")
            return saved

        base = int(since_id)
        fresh = [r for r in rows if str(r["id"]).isdigit() and int(r["id"]) > base
                 and not self.store.has_post(str(r["id"]))]
        fresh.sort(key=lambda r: int(r["id"]))
        dups_before = self.dup_skipped
        saved = self._save_rows(fresh, handle, name, client)
        dups = self.dup_skipped - dups_before

        self.store.set_state(handle, name=name, since_id=str(max(base, max_id)),
                             last_check=time.strftime("%Y-%m-%d %H:%M:%S"), last_error="",
                             total=int(state.get("total") or 0) + saved)
        if saved or dups:
            pages = f"（翻了 {client.last_pages} 页）" if client.last_pages > 1 else ""
            extra = f"，跳过 {dups} 条重复转推" if dups else ""
            self.log("INFO", f"[{name}] X 新增 {saved} 条已归档{pages}{extra}")
        return saved

    # ------------------------------------------------------------------ #
    def _fetch(self, acc: Dict[str, Any], handle: str,
               since_id: str) -> Tuple[XClient, List[Dict[str, Any]]]:
        """抓一次；失败达到阈值就换个节点重试一次。

        换节点只在"连续失败"时做：偶发超时（X 的网络抖得很厉害）换节点是浪费，
        反而会把本来健康的绑定关系搅乱。
        """
        threshold = max(1, int(self.cfg.get("x_failover_threshold", 2) or 2))
        pages = self._pages()
        if not since_id:
            pages = max(pages, int(self.cfg.get("x_catchup_pages", 0) or 0))

        client = self._route_for(acc)
        name = acc.get("name") or handle
        try:
            rows = client.fetch_user(handle, user_id=str(acc.get("x_user_id") or ""),
                                     pages=pages, stop_id=since_id)
            self._fails.pop(handle, None)
            return client, rows
        except XError as exc:
            if getattr(exc, "fatal", False):
                self._fails.pop(handle, None)
                raise   # 账号级错误：换节点 / 直连都不会变好
            fails = self._fails.get(handle, 0) + 1
            self._fails[handle] = fails
            # 端口都没开的时候，等"第二次失败"纯属浪费一轮：
            # 直接进换节点 / 直连兜底的流程。
            if fails < threshold and not getattr(exc, "dead_node", False):
                raise
            failed_node = client.node_id
            self._fails.pop(handle, None)
            if self.cfg.get("x_proxy_mode") in ("manual", "local"):
                raise  # 明确选定的代理失败时，不切订阅节点或直连
            retry = self._route_for(acc, exclude={failed_node} if failed_node else set())
            err = self._brief(exc)
            if retry is not client:
                self.log("WARNING", f"[{name}] 连续 {fails} 次失败（{exc}），换节点重试")
                try:
                    rows = retry.fetch_user(handle, user_id=str(acc.get("x_user_id") or ""),
                                            pages=pages, stop_id=since_id)
                    self.log("INFO", f"[{name}] 已切到节点 "
                                     f"{retry.node_id or retry.proxy} 并成功")
                    return retry, rows
                except XError as exc2:
                    # 只留最后那段：把「换节点前后」两段错误拼成一篇作文，
                    # 日志里除了噪音没有别的信息
                    err = self._brief(exc2)

            # 节点全挂时的兜底：直连。用户明确选了"宁可多试一条路也不空转一轮"。
            # 代价要记账：这一轮的流量挂在虚拟节点 direct 名下，能看出"这个月的
            # 流量有一部分没经过节点"，而不是悄悄并进某个真实节点。
            direct = self._direct_client()
            if direct is client or direct is retry:
                raise self._hopeless(err)
            try:
                rows = direct.fetch_user(handle, user_id=str(acc.get("x_user_id") or ""),
                                         pages=pages, stop_id=since_id)
            except XError as exc3:
                raise self._hopeless(
                    f"{err}；直连也失败：{self._brief(exc3)}") from exc3
            self.log("INFO", f"[{name}] 直连兜底成功（本轮流量记在 direct）")
            return direct, rows

    @staticmethod
    def _brief(text: Any) -> str:
        """去掉 `请求失败 x.com：` 前缀。

        一段话里连说两遍 host 只是噪音：`请求失败 x.com：A；直连也失败：
        (请求失败 x.com：B)` —— 拼消息前先剥掉外壳。
        """
        return re.sub(r"^请求失败\s+[^\s：]+：", "", str(text))

    @staticmethod
    def _hopeless(message: str) -> XError:
        """把「这一轮确实没路」标记出来，让 tick() 别再拿剩下的账号撞墙。"""
        err = XError(message)
        err.no_route = True
        return err

    def _save_rows(self, rows: List[Dict[str, Any]], handle: str, name: str,
                   client: Optional[XClient] = None) -> int:
        saved = 0
        for row in rows:
            try:
                if self.save_post(row, handle, name, client):
                    saved += 1
            except Exception as exc:  # noqa: BLE001
                self.log("ERROR", f"[{name}] 归档 {row.get('id')} 失败：{exc}")
        return saved

    # ------------------------------------------------------------------ #
    def save_post(self, row: Dict[str, Any], handle: str, name: str,
                  client: Optional[XClient] = None,
                  opts: Optional[Dict[str, Any]] = None) -> bool:
        """归档一条推文（含媒体下载）。返回是否真的入库。

        `opts` 是**单个回溯任务**的临时覆盖，只认 `keep_text`：日常抓取沿用
        `x_archive_text_only`，回溯则按弹窗里勾的「纯文本要不要」来决定，
        不能因为跑一次回溯就把全局抓取规则改掉。
        """
        tid = str(row.get("id") or "")
        if not tid:
            return False

        # 转推要多一道校验：**多个监控账号转同一条原推，只留一条**。
        # 判据是原推 id（`retweet_of`），不是文本相似度 —— 同一个 id 必然是同一条内容。
        # 保留顺序借微博那套 find_duplicate_of：原博已在库就留原博（转推纯属重复），
        # 原博不在库就留最早入库的那份转推。同一账号重复转同一条也被这条挡住。
        rt_of = str(row.get("retweet_of") or "")
        is_rt = bool(row.get("is_retweet")) or bool(rt_of)
        if is_rt and rt_of:
            try:
                dup = self.store.find_duplicate_of(rt_of, tid)
            except Exception:  # noqa: BLE001  判重失败不能拖垮抓取
                dup = None
            if dup:
                self.dup_skipped += 1
                keeper = dup.get("screen_name") or dup.get("mblog_id") or ""
                self.log("INFO", f"[{handle}] 这条转推已经收过（保留 {keeper} 那份），"
                                 f"跳过 | {(row.get('text') or '')[:36]}")
                return False

        # 反过来也要兜住：原创后到（A 先转、原博主 B 下一轮才被查到）时，
        # 库里会先有一份转推副本；原博是真身（作者归属正确、媒体也全），
        # 留它、把副本移进回收站，同一条内容始终只有一份。
        created_at = row.get("time") or ""
        date = (created_at.split(" ")[0] or time.strftime("%Y%m%d")).replace("-", "")
        post_dir = os.path.join(self.archive_dir, safe_name(handle), f"{date}_{tid}")

        media = row.get("media") or []
        pics = sum(1 for m in media if (m.get("type") or "") == "photo")
        vids = sum(1 for m in media if (m.get("type") or "") in ("video", "animated_gif"))

        # X 独立控制纯文本归档；media 列表空就是纯文本。
        # （引用推文带图的话 media 里也会有条目，和微博的转发逻辑等价）。
        allow_text = bool(self.cfg.get("x_archive_text_only", False))
        if opts is not None and "keep_text" in opts:
            allow_text = bool(opts.get("keep_text"))
        if not allow_text and not pics and not vids:
            self.text_skipped += 1
            self.log("INFO", f"[{handle}] 纯文本推文，跳过 | {(row.get('text') or '')[:36]}")
            return False

        if media and self.cfg.get("encrypt_downloads") and not getattr(getattr(self, "vault", None), "unlocked", False):
            raise RuntimeError("文件加密未解锁；本轮暂不归档含媒体内容")
        saved: List[Dict[str, Any]] = []
        if media and self.cfg.get("download_image", True):
            saved = self._download_media(row, post_dir, handle, client or self.client)

        if opts and opts.get("require_media") and not saved:
            return False

        self.store.save_post({
            "platform": PLATFORM,
            "mblog_id": tid,
            "mblogid": tid,
            "uid": handle,
            "screen_name": name,
            "created_at": created_at,
            "created_ts": _to_ts(created_at),
            "text": row.get("text") or "",
            "source": "X",
            "url": row.get("url") or f"https://x.com/{handle}/status/{tid}",
            "is_retweet": 1 if is_rt else 0,
            # 跨账号判重的键：被转发的原推 id（原创为空）
            "retweet_mblog_id": rt_of,
            "pic_count": pics,
            "video_count": vids,
            "dir": post_dir if saved else "",
            "reposts_count": int(row.get("reposts") or 0),
            "comments_count": int(row.get("replies") or 0),
            "attitudes_count": int(row.get("likes") or 0),
            "views": int(row.get("views") or 0),
            "raw_json": json.dumps(row, ensure_ascii=False)[:200000],
        })
        for item in saved:
            self.store.save_media(tid, item["kind"], item["url"],
                                  item["path"], item["size"], item.get("status", "ok"))
        if not is_rt:
            # 原博落库成功后再清副本：万一上面写库失败，副本还在，不至于两头空
            self._drop_retweet_copies(tid, handle)
        return True

    def _drop_retweet_copies(self, tid: str, handle: str) -> int:
        """原创入库时，清掉先前收下的「指向它」的转推副本（进回收站可还原）。

        只在 X 侧用：同一条原推被别的监控账号先转进来过，等原博主本人这条
        到了就没必要留副本。不做的话库里会"一原推一副本"重复占两份。
        """
        try:
            others = self.store.retweets_of(tid)
        except Exception:  # noqa: BLE001
            return 0
        removed = 0
        for mid in others:
            try:
                if self.store.delete_post(mid, tombstone=False).get("exists"):
                    removed += 1
            except Exception:  # noqa: BLE001
                continue
        if removed:
            self.dup_skipped += removed
            self.log("INFO", f"[{handle}] 原创已入档，移走先前 {removed} 条转推副本"
                             f"（同一条只留一份）")
        return removed

    def _download_media(self, row: Dict[str, Any], post_dir: str, handle: str,
                        client: XClient) -> List[Dict[str, Any]]:
        media = [m for m in (row.get("media") or []) if m.get("url") or m.get("video")]
        if not media:
            return []

        # 同一张图被多条推文引用时直接复用已下载的那份（配置里的媒体去重）
        urls = [str(m.get("url") or "") for m in media]
        reused = {}
        if self.cfg.get("media_dedupe", True):
            try:
                reused = self.store.find_media_paths(urls)
            except Exception:  # noqa: BLE001
                reused = {}

        out: List[Dict[str, Any]] = []
        for i, m in enumerate(media, 1):
            is_video = (m.get("type") or "") in ("video", "animated_gif") and m.get("video")
            url = str(m.get("video") or "") if is_video else str(m.get("url") or "")
            if not url:
                continue
            if url in reused and os.path.isfile(reused[url]):
                kind = "video" if is_video else "image"
                os.makedirs(post_dir, exist_ok=True)
                local_path, status = self.store.link_reused_media(reused[url], post_dir, url, kind)
                out.append({"kind": kind, "url": url, "path": local_path,
                            "size": os.path.getsize(local_path), "status": status})
                continue
            ext = "mp4" if is_video else (url.rsplit(".", 1)[-1].split("?")[0].lower() or "jpg")
            if ext not in _IMG_EXT and not is_video:
                ext = "jpg"
            fname = f"{'video' if is_video else 'img'}_{i:03d}.{ext}"
            dst = os.path.join(post_dir, fname)
            src = url if is_video else f"{url}?name=orig"
            try:
                if self.cfg.get("encrypt_downloads") and not getattr(getattr(self, "vault", None), "unlocked", False):
                    raise ValueError("文件加密未解锁，请先在设置中输入主密码")
                size = client.download(src, dst, uid=handle)
                if self.cfg.get("encrypt_downloads"):
                    dst = self.vault.encrypt_file(dst)
                    size = os.path.getsize(dst)
            except Exception as exc:  # noqa: BLE001
                if self.cfg.get("encrypt_downloads") and os.path.isfile(dst):
                    try:
                        os.remove(dst)
                    except OSError:
                        pass
                self.log("WARNING", f"[{handle}] 媒体下载失败 {fname}：{exc}")
                continue
            out.append({"kind": "video" if is_video else "image",
                        "url": str(m.get("url") or url), "path": dst, "size": size})
        return out
