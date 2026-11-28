# -*- coding: utf-8 -*-
"""监控主循环：轮询微博账号，发现新内容立即归档。"""

from __future__ import annotations

import json
import os
import random
import re
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from .client import WeiboClient, WeiboError
from .config import PLATFORM_DIR, resolve_output_dir
from .downloader import MediaDownloader
from .extract import (extract_media, extract_meta, extract_text, has_media,
                      parse_created_at, vip_state)
from .interact import AutoPilot
from .log import LogBus
from .store import Store
from .writer import WeiboWriter

_ILLEGAL = re.compile(r'[\\/:*?"<>|\r\n\t]')
_SPACE_RE = re.compile(r"\s+")


def safe_name(text: str, limit: int = 40) -> str:
    """把任意文本转成安全的文件/目录名。"""
    text = _SPACE_RE.sub(" ", (text or "").strip())
    text = _ILLEGAL.sub("", text)
    return text[:limit].strip() or "unnamed"


def to_timestamp(value: str) -> int:
    """'2026-10-27 12:00:00' -> unix 秒。"""
    try:
        return int(datetime.strptime(value, "%Y-%m-%d %H:%M:%S").timestamp())
    except Exception:  # noqa: BLE001
        return 0


def _as_int(value: Any) -> int:
    try:
        return int(str(value))
    except Exception:  # noqa: BLE001
        return 0


class Monitor:
    """微博账号监控器。"""

    def __init__(self, cfg: Dict[str, Any], logger: Optional[LogBus] = None):
        self.cfg = cfg
        self.logger = logger or LogBus(cfg.get("log_level", "INFO"))
        self.output_dir = resolve_output_dir(cfg)
        # 库、回收站、缩略图缓存、总结都在 output_dir 根上（多平台共用）；
        # 只有「每个账号一个文件夹」的媒体存档按平台再分一层，
        # 否则微博和 X 的目录会混在 data/ 底下，将来没法整块搬走。
        self.archive_dir = os.path.join(self.output_dir, PLATFORM_DIR)
        os.makedirs(self.archive_dir, exist_ok=True)
        self.store = Store(os.path.join(self.output_dir, "archive.db"))
        self.client = WeiboClient(
            cookie=cfg.get("cookie", ""),
            timeout=int(cfg.get("request_timeout", 20)),
            retry=int(cfg.get("retry", 3)),
            logger=self.logger,
        )
        self.downloader = MediaDownloader(self.client, cfg, self.logger)

        # 自动互动（跟踪转发原博主 + 关键字互动）。默认全关，且 dry_run=True。
        self.autopilot = AutoPilot(cfg, self.store, self.logger.emit, writer=WeiboWriter(
            cookie=cfg.get("cookie", ""),
            timeout=int(cfg.get("request_timeout", 20)),
            logger=self.logger.emit,
            fp=cfg.get("writer_fp", ""),
            enabled=bool(cfg.get("write_enabled", False)),
            extra_headers=cfg.get("write_extra_headers"),
        ))

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._wakeup = threading.Event()
        self._force_next_tick = False
        self.running = False
        self.check_count = 0
        self.new_count = 0
        # 会员专属/付费内容被跳过的条数。单独计数：它不是"重复"也不是"失败"，
        # 是接口压根不下发原图，看板上要能和这两种区分开。
        self.vip_skipped = 0
        # 纯文本帖被跳过的条数（archive_text_only=False 时才会涨）。
        self.text_skipped = 0
        self.last_round = (0.0, 0)
        self.last_tick = 0.0

        self.summaries_dir = os.path.join(self.output_dir, "summaries")
        os.makedirs(self.summaries_dir, exist_ok=True)
        self._last_summary_ts = 0.0
        self.latest_summary = self._read_latest_summary()

    # ------------------------------------------------------------------ #
    def log(self, level: str, message: str) -> None:
        self.logger.emit(level, message)

    def reload(self, cfg: Dict[str, Any]) -> None:
        """热更新配置（Cookie、账号、间隔等）。"""
        self.cfg = cfg
        self.output_dir = resolve_output_dir(cfg)
        self.archive_dir = os.path.join(self.output_dir, PLATFORM_DIR)
        os.makedirs(self.archive_dir, exist_ok=True)
        self.client.cookie = cfg.get("cookie", "")
        self.client.timeout = int(cfg.get("request_timeout", 20))
        self.client.retry = int(cfg.get("retry", 3))
        self.downloader.cfg = cfg
        self.downloader.workers = max(1, int(cfg.get("media_workers", 4)))
        self.logger.level = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40}.get(
            str(cfg.get("log_level", "INFO")).upper(), 20)
        # 自动互动与写接口同样要跟着更新，否则界面改了开关不生效
        pilot = getattr(self, "autopilot", None)
        if pilot is not None:
            pilot.cfg = cfg
            if getattr(pilot, "writer", None) is not None:
                pilot.writer.apply_config(cfg)
        self.log("INFO", "配置已更新")

    # ------------------------------------------------------------------ #
    def start(self) -> None:
        """启动后台监控线程。"""
        if self.running:
            return
        if not self.cfg.get("cookie"):
            self.log("ERROR", "未配置 Cookie，请先填写 config.json 中的 cookie 字段")
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="weibo-monitor", daemon=True)
        self._thread.start()
        self.running = True

    def stop(self, wait: float = 0.0) -> None:
        """停止监控线程。

        `wait=0`（默认）只置标志就返回 —— 正在飞的这一轮会自己跑完，
        但不会再排下一轮。HTTP 接口必须用默认：线程可能卡在一轮抓取里，
        join 5 秒必超时，点「停止监控」会整整卡 10 秒没反应。
        需要同步等待的场景（自检 / 退出）显式传 wait。
        """
        self._stop.set()
        self._wakeup.set()
        self.running = False
        thread = self._thread
        if wait > 0 and thread and thread.is_alive():
            thread.join(timeout=wait)
        self.log("INFO", "监控已停止")

    def check_now(self) -> None:
        """请求立刻执行一次检查（不等下一个周期）。"""
        self._force_next_tick = True
        self._wakeup.set()

    # ------------------------------------------------------------------ #
    def _loop(self) -> None:
        self.log("INFO", f"监控已启动，轮询间隔 {self.cfg.get('poll_interval_sec')} 秒")
        while not self._stop.is_set():
            try:
                force = self._force_next_tick
                self._force_next_tick = False
                self.tick(force=force)
            except Exception as exc:  # noqa: BLE001
                self.log("ERROR", f"轮询异常：{exc}")
            interval = max(10, int(self.cfg.get("poll_interval_sec", 60)))
            jitter = random.uniform(0, max(0, int(self.cfg.get("poll_jitter_sec", 8))))
            self._wakeup.wait(interval + jitter)
            self._wakeup.clear()

    def tick(self, force: bool = False) -> int:
        """对所有启用账号执行一次检查，返回新增微博数。"""
        # 只跑微博：X 的账号也在这个列表里（账号管理页要一起显示），
        # 但拿 handle 去请求微博接口只会刷一屏报错
        accounts = [a for a in self.cfg.get("accounts", [])
                    if a.get("enabled") and (a.get("platform") or "weibo") == "weibo"]
        if not accounts:
            self.log("WARNING", "没有启用的监控账号，请在配置中添加 uid")
            return 0
        total_new = 0
        cookie_expired = False
        checked = 0
        for account in accounts:
            if not force and account.get("monitor_mode") == "interact_only":
                minutes = max(5, min(1440, int(account.get("interact_interval_min") or 30)))
                state = self.store.get_state(str(account["uid"])) or {}
                last_check = to_timestamp(state.get("last_check") or "")
                if last_check and not state.get("last_error") and time.time() - last_check < minutes * 60:
                    continue
            checked += 1
            try:
                total_new += self.check_account(account)
            except WeiboError as exc:
                if "ok=-100" in str(exc):
                    cookie_expired = True
                    if getattr(self, "on_cookie_expired", None):
                        self.on_cookie_expired()
                self.log("ERROR", f"[{account.get('name')}] {exc}")
                self.store.set_state(account["uid"], name=account.get("name"),
                                     last_error=str(exc), last_check=time.strftime("%Y-%m-%d %H:%M:%S"))
                if cookie_expired:
                    break
            except Exception as exc:  # noqa: BLE001
                self.log("ERROR", f"[{account.get('name')}] 检查失败：{exc}")
                self.store.set_state(account["uid"], name=account.get("name"),
                                     last_error=str(exc), last_check=time.strftime("%Y-%m-%d %H:%M:%S"))
            time.sleep(random.uniform(0.8, 2.0))
        self.check_count += 1
        self.new_count += total_new
        self.last_tick = time.time()
        self.last_round = (self.last_tick, total_new)
        self._maybe_summarize()
        if cookie_expired:
            self.log("WARNING", "微博登录已失效，本轮停止检查其他账号；等待自动更新或重新登录")
            return total_new
        if self.cfg.get("heartbeat_log", True):
            interval = max(10, int(self.cfg.get("poll_interval_sec", 60)))
            if total_new:
                self.log("INFO", f"本轮完成：{checked} 个账号，新增 {total_new} 条")
            else:
                self.log("INFO", f"本轮完成：{checked} 个账号，无新内容，{interval} 秒后再查")
        return total_new

    # ------------------------------------------------------------------ #
    def check_account(self, account: Dict[str, Any]) -> int:
        """检查单个账号，返回新增条数。"""
        uid = str(account["uid"])
        name = account.get("name") or uid
        state = self.store.get_state(uid)
        since_id = (state or {}).get("since_id") or ""
        if self.cfg.get("encrypt_downloads") and not getattr(getattr(self.downloader, "vault", None), "unlocked", False):
            raise RuntimeError("文件加密未解锁；本轮不推进账号检查位置")

        if not since_id:
            return self._bootstrap(account, uid, name)

        base = _as_int(since_id)
        fresh: List[Dict[str, Any]] = []
        max_id = base
        seen: set = set()
        latest_at = ""
        # 一次更新超过一页时继续深翻，直到遇到不比基线新的内容为止
        pages = max(1, int(self.cfg.get("poll_pages", 2)))
        for page in range(1, pages + 1):
            # 关键：不能传 since_id，否则接口返回的是往旧方向翻页的游标页，永远看不到新微博
            data = self.client.fetch_page(uid, page=page)
            items = WeiboClient.parse_list(data)
            if not items:
                break
            page_min: Optional[int] = None
            for mblog in items:
                mid = _as_int(mblog.get("id") or mblog.get("mid"))
                if not mid or mid in seen:
                    continue
                seen.add(mid)
                max_id = max(max_id, mid)
                page_min = mid if page_min is None else min(page_min, mid)
                if not latest_at:
                    latest_at = parse_created_at(mblog.get("created_at") or "")
                if mid > base and not self.store.has_post(str(mblog.get("id"))):
                    fresh.append(mblog)
            if page_min is not None and page_min <= base:
                break  # 本页已含旧内容，后面更旧，无需再翻

        # 新微博按发布时间升序处理；同一批里先归档原创、再处理转发。
        # 否则一次 catchup 同时拉到原博和它的转发时，谁先被处理决定了谁被保留，
        # 结果会随接口返回顺序漂移（保留对象应该是原博，稳定的那个）。
        fresh.sort(key=lambda m: (1 if isinstance(m.get("retweeted_status"), dict) else 0,
                                  _as_int(m.get("id"))))
        saved = 0
        for mblog in fresh:
            if account.get("monitor_mode") == "interact_only":
                mid = str(mblog.get("id") or mblog.get("mblogid") or "")
                if mid and not self.store.is_deleted(mid):
                    self.autopilot.on_keyword_only(self.build_post(mblog, uid, name))
                continue
            if not self.save_post(mblog, uid, name).get("skipped"):
                saved += 1

        if not fresh:
            self.log("DEBUG", f"[{name}] 无更新，最新一条 {latest_at or '未知'}")

        if max_id != base:
            self.store.set_state(uid, name=name, since_id=str(max_id),
                                 last_check=time.strftime("%Y-%m-%d %H:%M:%S"),
                                 last_error="",
                                 total=int((state or {}).get("total") or 0) + saved)
        else:
            self.store.set_state(uid, name=name, last_check=time.strftime("%Y-%m-%d %H:%M:%S"),
                                 last_error="")

        if fresh:
            skipped = len(fresh) - saved
            self.log("INFO", f"[{name}] 发现 {len(fresh)} 条新内容，归档 {saved} 条"
                             + (f"，跳过 {skipped} 条（重复/会员专属/已删除）" if skipped else ""))
        return saved

    def _bootstrap(self, account: Dict[str, Any], uid: str, name: str) -> int:
        """首次运行：建立基线，可选回溯若干页历史。"""
        data = self.client.fetch_page(uid, page=1)
        items = WeiboClient.parse_list(data)
        if not items:
            self.log("WARNING", f"[{name}] 首页无内容，检查 uid 是否正确或 Cookie 是否有效")
            self.store.set_state(uid, name=name, last_error="首页无内容")
            return 0

        ids = [_as_int(m.get("id")) for m in items if _as_int(m.get("id"))]
        max_id = max(ids) if ids else 0
        self.store.set_state(uid, name=name, since_id=str(max_id),
                             last_check=time.strftime("%Y-%m-%d %H:%M:%S"), last_error="", total=0)
        if account.get("monitor_mode") == "interact_only":
            self.log("INFO", f"[{name}] 仅转关评模式：已建立检查基线，不归档历史内容")
            return 0

        # 自动加入的账号不回溯历史：它是"刚被发现的博主"，从现在往后看就行。
        # 回溯会抓回一整页旧内容，其中大半已经作为转发的原博存过媒体，
        # 白占空间也算不上新发现。
        pages = 0 if account.get("auto_added") else max(0, int(self.cfg.get("catchup_pages", 0)))
        if pages == 0:
            if account.get("auto_added"):
                self.log("INFO", f"[{name}] 自动加入的账号：只建基线、不回溯历史")
            self.log("INFO", f"[{name}] 已建立基线（最新 id={max_id}），之后的新内容会自动保存")
            return 0

        saved = 0
        seen: set = set()
        for page in range(1, pages + 1):
            page_data = data if page == 1 else self.client.fetch_page(uid, page=page)
            for mblog in WeiboClient.parse_list(page_data):
                key = str(mblog.get("id"))
                if key in seen or self.store.has_post(key):
                    continue
                seen.add(key)
                if self.save_post(mblog, uid, name).get("skipped"):
                    continue      # 已删除的不算进"保存 N 条"
                saved += 1
            time.sleep(random.uniform(1.0, 2.0))
        self.log("INFO", f"[{name}] 回溯完成，保存 {saved} 条历史内容")
        return saved

    # ------------------------------------------------------------------ #
    def build_post(self, mblog: Dict[str, Any], uid: str, name: str, post_dir: str = "",
                   pic_count: Optional[int] = None, video_count: Optional[int] = None,
                   saved_at: str = "") -> Dict[str, Any]:
        """把原始 mblog 组装成一行 posts 记录（不落盘、不下载）。

        监控入库与历史回灌共用这一份逻辑，保证字段完全一致。
        """
        mblog_id = str(mblog.get("id") or mblog.get("mblogid") or "")
        mblogid = str(mblog.get("mblogid") or mblog_id)
        created = parse_created_at(mblog.get("created_at") or "")
        text = extract_text(mblog)

        retweeted = mblog.get("retweeted_status")
        if isinstance(retweeted, dict):
            r_text = extract_text(retweeted)
            if r_text:
                text = f"{text}\n\n【转发原文】{r_text}".strip()

        user = mblog.get("user") or {}
        post: Dict[str, Any] = {
            "mblog_id": mblog_id,
            "uid": uid,
            "screen_name": user.get("screen_name") or name,
            "created_at": created,
            "created_ts": to_timestamp(created),
            "text": text,
            "source": mblog.get("source") or "",
            "url": f"https://weibo.com/{uid}/{mblogid}",
            "is_retweet": 1 if isinstance(retweeted, dict) else 0,
            "pic_count": 0 if pic_count is None else int(pic_count),
            "video_count": 0 if video_count is None else int(video_count),
            "dir": post_dir,
            "saved_at": saved_at or time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        post.update(extract_meta(mblog, uid))
        try:
            post["raw_json"] = json.dumps(mblog, ensure_ascii=False)
        except Exception:  # noqa: BLE001
            post["raw_json"] = ""
        return post

    def save_post(self, mblog: Dict[str, Any], uid: str, name: str) -> Dict[str, Any]:
        """归档单条微博（正文 + 图片 + 视频）。"""
        mblog_id = str(mblog.get("id") or mblog.get("mblogid") or "")
        mblogid = str(mblog.get("mblogid") or mblog_id)
        # 删掉的微博不允许复活：新账号回溯会把历史整页重新拉一遍，
        # 没有这道闸，删掉的内容和媒体会原样回来、白白再占一次空间
        if self.store.is_deleted(mblog_id):
            self.log("DEBUG", f"[{name}] {mblog_id} 已被删除，跳过")
            return {"skipped": True}

        # User-deleted originals also block future reposts, regardless of dedupe settings.
        ancestor = mblog.get("retweeted_status")
        for _ in range(5):
            if not isinstance(ancestor, dict):
                break
            parent_id = str(ancestor.get("id") or ancestor.get("mblogid") or "")
            if parent_id and self.store.is_deleted_root(parent_id):
                self.log("DEBUG", f"[{name}] 原博 {parent_id} 已删除，跳过转发 {mblog_id}")
                return {"skipped": True, "reason": "deleted_root"}
            ancestor = ancestor.get("retweeted_status")

        # 会员专属 / 付费内容：接口只下发 blur 占位图（所有尺寸都指向同一个
        # /wapb720/ 地址），原图不在响应里。存下来是一屏糊图加一行正文，
        # 在内容流里就是 12 个「已删除」灰块 —— 不如直接不收。
        # 放在建目录之前，跳过就是真的什么都没留下。
        if self.cfg.get("skip_vip_post", True):
            vip = vip_state(mblog)
            vip_from = "本条"
            retweeted_vip = mblog.get("retweeted_status")
            if not vip["locked"] and isinstance(retweeted_vip, dict):
                vip_r = vip_state(retweeted_vip)
                if vip_r["locked"]:
                    vip, vip_from = vip_r, "转发原文"
            if vip["locked"]:
                self.vip_skipped += 1
                self.log("INFO",
                         f"[{name}] 会员专属内容，跳过（{vip['reason']}，"
                         f"{vip_from}）| {extract_text(mblog)[:36]}")
                return {"skipped": True, "reason": "vip_only", "vip": vip}

        # 重复转发去重：同一条原博已经归档过（不管是以原创还是转发的形式），
        # 就不再存第二遍。放在最前面 —— 命中直接返回，不建目录、不抽媒体、
        # 不下文件、不写 post.json、不进自动互动。
        # 不进自动互动是刻意的：转关的幂等键是原博 id、关注的幂等键是原博主
        # uid，第一条早就处理过了，后面这几条本来就会被幂等挡掉；再走一遍
        # 只会让 dry_run 下的演练记录刷屏（上一轮刚修过）。
        retweeted0 = mblog.get("retweeted_status")
        if isinstance(retweeted0, dict) and self.cfg.get("dedupe_retweet", True):
            exempt = {str(u) for u in (self.cfg.get("dedupe_exempt_uids") or [])}
            if str(uid) not in exempt:
                keeper = self.store.find_duplicate_of(str(retweeted0.get("id") or ""),
                                                      self_id=mblog_id)
                if keeper:
                    self.log(
                        "INFO",
                        f"[{name}] 重复转发，跳过：原博 {keeper['root']} 已由"
                        f"「{keeper['screen_name']}」归档（"
                        f"{'原博本身' if keeper['is_original'] else '最早那条转发'}）",
                    )
                    return {"skipped": True, "reason": "duplicate", "keeper": keeper}

        # 纯文本帖子：响应里没有图也没有视频（转发则连原博一起看）。
        # 关掉 archive_text_only 就整条不收 —— 放在建目录之前，跳过就是真的
        # 什么都没留下：不建目录、不写 post.json、不入库。
        #
        # 自动互动照样跑，而且是拿 build_post() 现拼一条不落库的记录去跑：
        # 纯文本是文字最多的一类，关键字命中率恰恰最高，跳过归档不该连带
        # 跳过转关。动作记录自带 detail.preview 文字快照，不依赖 posts 行，
        # 所以审计页不会出现指向空气的链接。
        # 唯一的代价：这类转发不进 posts 表，_retweet_hits 统计「原博主被转发
        # 几次」时会漏掉它们，自动跟踪的阈值可能比真实值低。
        if not self.cfg.get("archive_text_only", True) and not has_media(mblog, self.cfg):
            self.text_skipped += 1
            self.log("INFO", f"[{name}] 纯文本帖子，跳过 | {extract_text(mblog)[:36]}")
            try:
                self.autopilot.on_post(self.build_post(mblog, uid, name))
            except Exception as exc:  # noqa: BLE001
                self.log("WARNING", f"自动互动处理失败（不影响跳过）：{exc}")
            return {"skipped": True, "reason": "text_only"}

        created = parse_created_at(mblog.get("created_at") or "")
        date_str = (created[:10] or time.strftime("%Y-%m-%d")).replace("-", "")
        text = extract_text(mblog)

        title = safe_name(text.split("\n")[0], 40)
        rel_dir = (self.cfg.get("post_dir_template") or "{name}_{uid}/{date}_{mblogid}").format(
            name=safe_name(name, 30), uid=uid, date=date_str,
            mblogid=mblogid, title=title, id=mblog_id,
        )
        rel_dir = rel_dir.replace("\\", os.sep).replace("/", os.sep)
        post_dir = os.path.join(self.archive_dir, rel_dir)
        os.makedirs(post_dir, exist_ok=True)

        images, videos = extract_media(mblog, self.cfg)

        # 转发微博：可选带原博媒体
        retweeted = mblog.get("retweeted_status")
        if isinstance(retweeted, dict):
            if self.cfg.get("save_retweet_media", True):
                r_img, r_vid = extract_media(retweeted, self.cfg)
                images += r_img
                videos += r_vid
            r_text = extract_text(retweeted)
            if r_text:
                text = f"{text}\n\n【转发原文】{r_text}".strip()

        # 原文与转发可能包含同一视频，归档前按地址去重。
        seen_video_urls = set()
        unique_videos = []
        for item in videos:
            url = item.get("url")
            if url in seen_video_urls:
                continue
            seen_video_urls.add(url)
            unique_videos.append(item)
        videos = unique_videos

        self.log("INFO", f"[{name}] 新内容 {created or ''} | 图片 {len(images)} 视频 {len(videos)} | {text[:40]}")

        # 数量在去重前先取：去重只是不重复下载文件，这条微博"有几张图"不变
        pic_n, vid_n = len(images), len(videos)

        # 媒体去重：同一张图在原博和转发里 URL 完全相同。把原博主加入监控后
        # 会重新抓到这些图，不查一次就会原样再下一遍（库里 575 条属于这种情况）。
        if self.cfg.get("media_dedupe", True):
            kind_by_url = {m.get("url"): m.get("kind", "image") for m in images + videos}
            reused = self.store.find_media_paths(list(kind_by_url))
            if reused:
                images = [m for m in images if m.get("url") not in reused]
                videos = [m for m in videos if m.get("url") not in reused]
                for url, path in reused.items():
                    kind = kind_by_url.get(url, "image")
                    local_path, status = self.store.link_reused_media(path, post_dir, url, kind)
                    self.store.save_media(mblog_id, kind, url, local_path,
                                          os.path.getsize(local_path), status)
                self.log("INFO", f"  媒体去重：{len(reused)} 个文件库里已有，不再重复下载")

        if (images or videos) and self.cfg.get("encrypt_downloads") and not self.downloader.vault.unlocked:
            raise RuntimeError("文件加密未解锁；本轮暂不归档含媒体内容")
        results = self.downloader.download_all(mblog_id, images, videos, post_dir)
        for item in results:
            self.store.save_media(mblog_id, item["kind"], item["url"], item["path"],
                                  item["size"], item["status"])

        # 数据库是唯一真源：正文、互动数据、话题、外链、原始 JSON 全部入库
        post = self.build_post(mblog, uid, name, post_dir=post_dir,
                               pic_count=pic_n, video_count=vid_n)
        self.store.save_post(post)

        # 原博可能比其他账号的转发更晚入库。此时保留原博，并将先前归档的
        # 转发副本移入回收站；否则只在转发入库时判重，最终仍会留下两份。
        if not isinstance(retweeted, dict) and self.cfg.get("dedupe_retweet", True):
            for duplicate_id in self.store.retweets_of(mblog_id):
                if duplicate_id == mblog_id:
                    continue
                try:
                    self.store.delete_post(duplicate_id, tombstone=False)
                    self.log("INFO", f"[{name}] 原博已归档，清理重复转发 {duplicate_id}")
                except Exception as exc:  # noqa: BLE001
                    self.log("WARNING", f"[{name}] 清理重复转发 {duplicate_id} 失败：{exc}")

        # 自动互动：跟踪转发原博主 / 关键字命中。默认全关 + dry_run，不会发包。
        try:
            self.autopilot.on_post(post)
        except Exception as exc:  # noqa: BLE001
            self.log("WARNING", f"自动互动处理失败（不影响归档）：{exc}")

        # 磁盘目录退化为媒体冷存：原始 JSON 短期保留作冗余，正文不再重复写
        try:
            if self.cfg.get("save_raw_json_file", True):
                with open(os.path.join(post_dir, "post.json"), "w", encoding="utf-8") as fp:
                    json.dump(mblog, fp, ensure_ascii=False, indent=2)
            if self.cfg.get("save_content_txt", False):
                with open(os.path.join(post_dir, "content.txt"), "w", encoding="utf-8") as fp:
                    fp.write(f"账号：{post['screen_name']}（{uid}）\n"
                             f"时间：{created}\n"
                             f"链接：{post['url']}\n"
                             f"来源：{post['source']}\n\n{text}\n")
        except Exception as exc:  # noqa: BLE001
            self.log("WARNING", f"写文本文件失败：{exc}")

        return post

    # ------------------------------------------------------------------ #
    # 定时总结
    # ------------------------------------------------------------------ #
    def _account_name(self, uid: str) -> str:
        for account in self.cfg.get("accounts", []):
            if str(account.get("uid")) == str(uid):
                return account.get("name") or uid
        return ""

    def _read_latest_summary(self) -> str:
        """启动时读回最近一份总结，看板重启后也能看到。"""
        try:
            files = sorted(
                (f for f in os.listdir(self.summaries_dir) if f.endswith(".md")),
                reverse=True,
            )
            if files:
                with open(os.path.join(self.summaries_dir, files[0]), encoding="utf-8") as fp:
                    return fp.read()
        except Exception:  # noqa: BLE001
            pass
        return ""

    def generate_summary(self, since_ts: float = 0, write_file: bool = True) -> str:
        """生成指定时间段内的更新总结（Markdown 文本）。"""
        now = time.time()
        if since_ts <= 0:
            since_ts = now - 24 * 3600
        since_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(since_ts))
        now_str = time.strftime("%Y-%m-%d %H:%M:%S")
        posts = self.store.posts_since(since_str)

        head = [
            "# 微博更新总结",
            "",
            f"- 统计区间：{since_str} → {now_str}",
            f"- 新增内容：**{len(posts)}** 条",
            "",
        ]
        if not posts:
            text = "\n".join(head + ["这段时间没有新的内容。"])
        else:
            groups: Dict[str, List[Dict[str, Any]]] = {}
            for post in posts:
                groups.setdefault(str(post.get("uid")), []).append(post)
            body: List[str] = []
            for uid, items in groups.items():
                name = self._account_name(uid) or (items[0].get("screen_name") or uid)
                pics = sum(int(i.get("pic_count") or 0) for i in items)
                vids = sum(int(i.get("video_count") or 0) for i in items)
                body.append(f"## {name}（{uid}）· {len(items)} 条 · 图 {pics} / 视频 {vids}")
                body.append("")
                for item in items:
                    clock = (item.get("created_at") or "")[11:16]
                    first_line = (item.get("text") or "").split("\n")[0][:70]
                    marks = []
                    if int(item.get("pic_count") or 0):
                        marks.append(f"图{int(item.get('pic_count'))}")
                    if int(item.get("video_count") or 0):
                        marks.append(f"视频{int(item.get('video_count'))}")
                    tag = f"[{'/'.join(marks)}] " if marks else ""
                    body.append(f"- `{clock}` {tag}{first_line}")
                    body.append(f"  {item.get('url')}")
                body.append("")
            text = "\n".join(head + body)

        self.latest_summary = text
        if write_file:
            try:
                os.makedirs(self.summaries_dir, exist_ok=True)
                fname = time.strftime("%Y%m%d_%H%M%S") + ".md"
                with open(os.path.join(self.summaries_dir, fname), "w", encoding="utf-8") as fp:
                    fp.write(text)
            except Exception as exc:  # noqa: BLE001
                self.log("WARNING", f"总结写入文件失败：{exc}")
        return text

    def _maybe_summarize(self) -> None:
        """到点就生成一份总结。"""
        interval = int(self.cfg.get("summary_interval_min") or 0)
        if interval <= 0:
            return
        now = time.time()
        if self._last_summary_ts <= 0:
            self._last_summary_ts = now
            return
        if now - self._last_summary_ts < interval * 60:
            return
        try:
            since_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self._last_summary_ts))
            count = len(self.store.posts_since(since_str))
            self.generate_summary(self._last_summary_ts)
            self.log("INFO", f"已生成定时总结：区间内 {count} 条新内容")
        except Exception as exc:  # noqa: BLE001
            self.log("WARNING", f"生成定时总结失败：{exc}")
        self._last_summary_ts = now
