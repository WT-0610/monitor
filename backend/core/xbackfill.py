"""X 一键回溯：把某个推主的历史时间线往回翻，按选项归档。

和日常轮询的分工：

- 轮询只会**往前**看（`since_id` 之后的新内容），历史根本不在它的视野里；
- 回溯只会**往回**看，而且永远不改 `since_id` —— 基线是"最新一条"，
  让回溯写旧推文时顺手把基线往回改，下一轮增量会把一整片内容重抓一遍。

三条硬约束（都不是可选的）：

1. **一页一停**。`XClient.fetch_timeline_page` 只拉一页并把 cursor 交出来，
   worker 自己控制节奏。这样 100 页的长任务能在页边界被打断、能从断点续跑。
2. **429 要退避**。X 的 GraphQL 速率窗口按 15 分钟算，连续请求必然撞上；
   撞了继续硬发只会把窗口拖得更长。这里等到窗口重置再接着翻，进度条上会写
   "等待限流"。
3. **墓碑不复活**。回溯会把翻到的每一条都往库里写，而 X 侧的 `save_post`
   原本不查删除黑名单（增量抓取天然碰不到旧推文，问题一直没暴露）。
   任务层在写之前先问一次 `store.is_deleted()`，你亲手删过的不会再回来。

进度落 `data/x_backfill.json`，进程重启后任务标为「已中断」，cursor 仍在，
点「继续回溯」从断点接着翻，不浪费已经花掉的请求。
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from .xclient import XError

STATE_FILE = "x_backfill.json"
# 429 之后等多久再试（秒）。X 的窗口是 15 分钟，但真等到 900 秒会让用户以为卡死，
# 所以先短等再长等：多数限流只是瞬时的并发限制。
RATE_WAITS = (60, 180, 300, 600, 900)
DEFAULT_PAGES = 100
PAGE_SIZE = 20

# 任务字段的完整默认值。老版本落盘的 data/x_backfill.json 里没有后来才加的字段
# （比如 failover），读回来必须补齐：少一个键，前端拿 task.failover 就是 undefined，
# 后端格式化日志也会直接 KeyError。
TASK_DEFAULTS: Dict[str, Any] = {
    "handle": "",
    "name": "",
    "status": "done",
    "options": {"keep_text": True, "keep_retweets": False, "media_only": False},
    "pages_limit": DEFAULT_PAGES,
    "pages_done": 0,
    "cursor": "",
    "added": 0,
    "skipped_dup": 0,
    "skipped_deleted": 0,
    "skipped_text": 0,
    "skipped_retweet": 0,
    "failed": 0,
    "failover": 0,
    "oldest_time": "",
    "oldest_id": "",
    "has_more": True,
    "note": "",
    "error": "",
    "wait_until": 0,
    "rate_waits": 0,
    "started_at": "",
    "finished_at": "",
    "seconds": 0,
}


def _now_text() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


class BackfillManager:
    """每账号一个回溯 worker。任务状态以 json 落盘，跨进程重启可续。"""

    def __init__(self, cfg: Dict[str, Any], xmon: Any,
                 logger: Optional[Callable[[str, str], None]] = None):
        self.cfg = cfg
        self.xmon = xmon
        self.store = xmon.store
        self.logger = logger
        self.root = xmon.output_dir
        self.path = os.path.join(self.root, STATE_FILE)
        self._lock = threading.RLock()
        self._tasks: Dict[str, Dict[str, Any]] = self._load()
        self._threads: Dict[str, threading.Thread] = {}
        self._stops: Dict[str, threading.Event] = {}

    # ---- 日志与落盘 ------------------------------------------------------ #
    def _log(self, level: str, message: str) -> None:
        emit = self.logger
        if callable(emit):
            try:
                emit(level, message)
                return
            except Exception:  # noqa: BLE001
                pass
        print(f"[{level}] {message}")

    def _load(self) -> Dict[str, Dict[str, Any]]:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return {}
        tasks = data.get("tasks") if isinstance(data, dict) else None
        if not isinstance(tasks, dict):
            return {}
        for key, task in tasks.items():
            # 老版本落盘的状态可能缺字段，先补齐再判状态
            task = {**TASK_DEFAULTS, **task}
            task["handle"] = str(task.get("handle") or key)
            if not isinstance(task.get("options"), dict):
                task["options"] = dict(TASK_DEFAULTS["options"])
            tasks[key] = task
            # 上次进程被杀时任务还是 running：标成中断，保留 cursor 供续跑。
            # 不标的话界面会一直显示"回溯中"，而实际上根本没有线程在跑。
            if str(task.get("status")) in ("running", "waiting"):
                task["status"] = "stopped"
                task["note"] = "进程重启，已中断（进度已保留，可继续）"
                task["wait_until"] = 0
        return tasks

    def _save(self) -> None:
        """原子写：先写临时文件再 replace，杀进程时不会留下半个 JSON。"""
        with self._lock:
            payload = {"tasks": self._tasks, "at": _now_text()}
        tmp = self.path + ".tmp"
        try:
            os.makedirs(self.root, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except OSError as exc:
            self._log("WARNING", f"回溯进度写盘失败：{exc}")

    # ---- 对外状态 -------------------------------------------------------- #
    def status(self) -> Dict[str, Any]:
        with self._lock:
            tasks = []
            for key, task in self._tasks.items():
                item = dict(task)
                item["key"] = key
                done = int(item.get("pages_done") or 0)
                limit = max(1, int(item.get("pages_limit") or DEFAULT_PAGES))
                item["percent"] = min(100, round(done * 100 / limit))
                tasks.append(item)
        tasks.sort(key=lambda t: str(t.get("started_at") or ""), reverse=True)
        return {
            "tasks": tasks,
            "running": sum(1 for t in tasks if t.get("status") in ("running", "waiting")),
        }

    def task_of(self, handle: str) -> Optional[Dict[str, Any]]:
        return self._tasks.get(self._key(handle))

    # ---- 启停 ------------------------------------------------------------ #
    @staticmethod
    def _key(handle: str) -> str:
        return str(handle or "").lstrip("@").strip().lower()

    def _account(self, key: str) -> Dict[str, Any]:
        for acc in self.cfg.get("accounts", []):
            if (acc.get("platform") or "weibo") != "x":
                continue
            if self._key(str(acc.get("uid") or "")) == key:
                return acc
        return {}

    def start(self, handle: str, body: Optional[Dict[str, Any]] = None,
              resume: bool = False) -> Dict[str, Any]:
        """开始（或继续）一个账号的回溯。返回任务快照。"""
        key = self._key(handle)
        if not key:
            raise ValueError("缺少账号 handle")
        acc = self._account(key)
        if not acc:
            raise ValueError(f"@{key} 不在 X 监控列表里")

        with self._lock:
            old = self._tasks.get(key) or {}
            if old.get("status") in ("running", "waiting"):
                raise ValueError(f"@{key} 已经在回溯了")
            # 并发上限：几个号同时全量回溯会在同一批出口上叠请求，429 概率翻倍。
            # 默认 2 个，够用又不至于把节点压垮。
            busy = [t for t in self._tasks.values()
                    if t.get("status") in ("running", "waiting")]
            limit = max(1, int(self.cfg.get("x_backfill_concurrency") or 2))
            if len(busy) >= limit:
                raise ValueError(f"同时最多 {limit} 个回溯任务在跑，等一个结束再开始")

        body = body or {}
        options = self._options(body)
        try:
            pages = int(body.get("pages") or self.cfg.get("x_backfill_pages") or DEFAULT_PAGES)
        except (TypeError, ValueError):
            pages = DEFAULT_PAGES
        pages = max(1, min(1000, pages))

        # 续跑：从上次的 cursor 接着往回翻，计数累加；
        # 重开：cursor 清空，计数归零，从最新一条重新往回翻（已入库的靠判重跳过）。
        prev = old if resume else {}
        cursor = str(prev.get("cursor") or "") if resume else ""
        pages_done = int(prev.get("pages_done") or 0) if resume else 0
        task: Dict[str, Any] = {
            # 先铺默认值，再接本次任务的实参：以后加字段只用改 TASK_DEFAULTS
            **TASK_DEFAULTS,
            "handle": key,
            "name": acc.get("name") or key,
            "status": "running",
            "options": options,
            "pages_limit": pages + pages_done if resume else pages,
            "pages_done": pages_done,
            "cursor": cursor,
            "added": int(prev.get("added") or 0) if resume else 0,
            "skipped_dup": int(prev.get("skipped_dup") or 0) if resume else 0,
            "skipped_deleted": int(prev.get("skipped_deleted") or 0) if resume else 0,
            "skipped_text": int(prev.get("skipped_text") or 0) if resume else 0,
            "skipped_retweet": int(prev.get("skipped_retweet") or 0) if resume else 0,
            "failed": int(prev.get("failed") or 0) if resume else 0,
            "failover": int(prev.get("failover") or 0) if resume else 0,
            "oldest_time": str(prev.get("oldest_time") or "") if resume else "",
            "oldest_id": str(prev.get("oldest_id") or "") if resume else "",
            "has_more": True,
            "note": "",
            "error": "",
            "wait_until": 0,
            "started_at": _now_text(),
            "finished_at": "",
            "seconds": 0,
        }
        with self._lock:
            self._tasks[key] = task
        self._save()

        stop = threading.Event()
        self._stops[key] = stop
        thread = threading.Thread(target=self._run, args=(key, stop),
                                  name=f"x-backfill-{key}", daemon=True)
        self._threads[key] = thread
        thread.start()

        mode = []
        mode.append("含纯文本" if options["keep_text"] else "不含纯文本")
        mode.append("含转推" if options["keep_retweets"] else "不含转推")
        if options["media_only"]:
            mode.append("仅带图")
        self._log("INFO", f"[{task['name']}] 开始回溯历史：{' / '.join(mode)}，"
                         f"上限 {pages} 页（约 {pages * PAGE_SIZE} 条）"
                         + ("，从断点续翻" if resume else ""))
        return dict(task)

    def stop(self, handle: str) -> bool:
        key = self._key(handle)
        with self._lock:
            task = self._tasks.get(key)
            stop = self._stops.get(key)
        if not task or not stop:
            return False
        stop.set()
        self._log("INFO", f"[{task.get('name') or key}] 回溯已请求停止（当前页跑完即停）")
        return True

    def shutdown(self) -> None:
        """进程退出时用：让所有 worker 尽快收手，把 cursor 写下去。"""
        with self._lock:
            stops = list(self._stops.values())
        for stop in stops:
            stop.set()

    # ---- 选项 ------------------------------------------------------------ #
    @staticmethod
    def _options(body: Dict[str, Any]) -> Dict[str, bool]:
        """三个勾选框。默认：带纯文本、不带转推、不限制带图。

        「仅带图」一旦勾上就等于把纯文本排除掉（界面上也会把纯文本置灰），
        否则两个条件互相打架 —— 三个都勾的结果会是"只看带图的转推"。
        """
        media_only = bool(body.get("media_only"))
        keep_text = bool(body.get("keep_text", True)) and not media_only
        return {
            "keep_text": keep_text,
            "keep_retweets": bool(body.get("keep_retweets", False)),
            "media_only": media_only,
        }

    # ---- worker ---------------------------------------------------------- #
    def _run(self, key: str, stop: threading.Event) -> None:
        task = self._tasks.get(key) or {}
        handle = str(task.get("handle") or key)
        name = str(task.get("name") or handle)
        acc = self._account(key)
        opts = task.get("options") or self._options({})
        client = None
        self.xmon.backfill_paused.add(key)
        started = time.time()
        try:
            client = self.xmon.new_client(acc)
            self._remember_user_id(acc, client)
            while True:
                if stop.is_set():
                    self._finish(key, "stopped", "已停止")
                    break
                if int(task.get("pages_done") or 0) >= int(task.get("pages_limit") or 1):
                    self._finish(key, "done", "已到本次页数上限，还有更早的内容")
                    task["has_more"] = True
                    break

                pulled = self._fetch_page(key, task, acc, handle, client, stop)
                if pulled is None:
                    break       # 已停止 / 已记错误，收尾在 _fetch_page 里做过了
                rows, next_cursor, client = pulled

                if not rows:
                    # 没有条目 = 时间线到头（或这条时间线不给 cursor）
                    self._finish(key, "done", "已经翻到时间线尽头")
                    task["has_more"] = False
                    break

                # 拉这一页时可能顺手纠正了绑错的 x_user_id，落盘省下次一次请求
                self._remember_user_id(acc, client)
                task["pages_done"] = int(task.get("pages_done") or 0) + 1
                self._archive_rows(key, task, rows, client, opts)

                tail = rows[-1]
                task["oldest_time"] = str(tail.get("time") or task.get("oldest_time") or "")
                task["oldest_id"] = str(tail.get("id") or "")
                task["cursor"] = str(next_cursor or "")
                task["has_more"] = bool(next_cursor)
                task["status"] = "running"
                task["wait_until"] = 0
                task["rate_waits"] = 0
                self._save()

                if not next_cursor:
                    self._finish(key, "done", "已经翻到时间线尽头")
                    task["has_more"] = False
                    break

                self._sleep(key, stop, self._delay())
                if stop.is_set():
                    self._finish(key, "stopped", "已停止")
                    break
        except Exception as exc:  # noqa: BLE001  线程里不能抛出去
            task["error"] = str(exc)
            self._finish(key, "error", f"异常：{exc}")
            self._log("ERROR", f"[{name}] 回溯异常：{exc}")
        finally:
            self.xmon.backfill_paused.discard(key)
            task["seconds"] = int(time.time() - started)
            with self._lock:
                self._stops.pop(key, None)
                self._threads.pop(key, None)
            self._save()

    def _remember_user_id(self, acc: Dict[str, Any], client: Any) -> None:
        try:
            self.xmon._remember_user_id(acc, client)
        except Exception:  # noqa: BLE001
            pass

    def _fetch_page(self, key: str, task: Dict[str, Any], acc: Dict[str, Any],
                    handle: str, client: Any, stop: threading.Event) -> Any:
        """拉一页；节点挂了就换一个出口重试**同一页**。

        长任务必然会遇到节点失效（实测第 4 页时 10870 端口关了）。日常轮询可以
        下一轮再来，回溯不行：一停就整任务失败，用户得手动再点一次。这里换成
        别的节点重试，页边界之前的进度都还在 cursor 上。

        返回 `(rows, next_cursor, client)`；返回 None 表示已停止或已记错误。
        """
        limit = max(1, int(self.cfg.get("x_backfill_retries") or 3))
        node_tries = 0
        while True:
            if stop.is_set():
                self._finish(key, "stopped", "已停止")
                return None
            try:
                rows, cursor = client.fetch_timeline_page(
                    handle, user_id=str(acc.get("x_user_id") or ""),
                    cursor=str(task.get("cursor") or ""))
                return rows, cursor, client
            except XError as exc:
                if self._rate_limited(exc):
                    if self._wait_429(key, task, stop):
                        continue
                    self._finish(key, "stopped", "已停止")
                    return None
                if getattr(exc, "fatal", False):
                    task["error"] = str(exc)
                    self._finish(key, "error", f"失败：{exc}")
                    self._log("ERROR", f"[{task.get('name') or key}] 回溯中断：{exc}")
                    return None
                node_tries += 1
                if node_tries >= limit:
                    task["failed"] = int(task.get("failed") or 0) + 1
                    task["error"] = str(exc)
                    self._finish(key, "error", f"失败：{exc}")
                    self._log("ERROR", f"[{task.get('name') or key}] 回溯中断：{exc}")
                    return None
                failed = getattr(client, "node_id", "")
                task["failover"] = int(task.get("failover") or 0) + 1
                self._log("WARNING", f"[{task.get('name') or key}] 本页失败（{exc}），"
                                     f"换节点重试 {node_tries}/{limit}")
                client = self.xmon.new_client(acc, exclude={failed} if failed else set())
                self._save()

    def _archive_rows(self, key: str, task: Dict[str, Any], rows: List[Dict[str, Any]],
                      client: Any, opts: Dict[str, bool]) -> None:
        handle = str(task.get("handle") or key)
        name = str(task.get("name") or handle)
        for row in rows:
            tid = str(row.get("id") or "")
            if not tid:
                continue
            try:
                if self.store.has_post(tid):
                    task["skipped_dup"] = int(task.get("skipped_dup") or 0) + 1
                    continue
                if self.store.is_deleted(tid):
                    task["skipped_deleted"] = int(task.get("skipped_deleted") or 0) + 1
                    continue
                media = row.get("media") or []
                has_media = bool(media)
                is_rt = bool(row.get("is_retweet")) or bool(row.get("retweet_of"))
                if is_rt and not opts.get("keep_retweets"):
                    task["skipped_retweet"] = int(task.get("skipped_retweet") or 0) + 1
                    continue
                if opts.get("media_only") and not has_media:
                    task["skipped_text"] = int(task.get("skipped_text") or 0) + 1
                    continue
                if not has_media and not opts.get("keep_text"):
                    task["skipped_text"] = int(task.get("skipped_text") or 0) + 1
                    continue
                # 过滤已经在上面做完，这里必须显式允许纯文本 ——
                # 全局 archive_text_only 可能是 False（日常只收带图），
                # 不能让它把回溯里用户明确勾选的纯文本吞掉。
                if self.xmon.save_post(row, handle, name, client, opts={"keep_text": True}):
                    task["added"] = int(task.get("added") or 0) + 1
                else:
                    task["skipped_dup"] = int(task.get("skipped_dup") or 0) + 1
            except Exception as exc:  # noqa: BLE001  单条失败不能拖垮整个任务
                task["failed"] = int(task.get("failed") or 0) + 1
                self._log("WARNING", f"[{name}] 回溯归档 {tid} 失败：{exc}")

    # ---- 429 与节奏 ------------------------------------------------------ #
    @staticmethod
    def _rate_limited(exc: Exception) -> bool:
        text = str(exc)
        return "429" in text or "限流" in text

    def _wait_429(self, key: str, task: Dict[str, Any], stop: threading.Event) -> bool:
        """撞 429 后等窗口重置。返回 False 表示放弃（等太多次了）。"""
        waits = int(task.get("rate_waits") or 0)
        if waits >= len(RATE_WAITS):
            return False
        seconds = RATE_WAITS[waits]
        task["rate_waits"] = waits + 1
        task["status"] = "waiting"
        task["wait_until"] = time.time() + seconds
        self._save()
        self._log("WARNING", f"[{task.get('name') or key}] 被 X 限流（429），"
                             f"等待 {seconds} 秒后继续（第 {waits + 1} 次）")
        self._sleep(key, stop, seconds)
        task["wait_until"] = 0
        return not stop.is_set()

    def _delay(self) -> float:
        try:
            return max(0.5, float(self.cfg.get("x_backfill_delay_sec") or 2.5))
        except (TypeError, ValueError):
            return 2.5

    @staticmethod
    def _sleep(key: str, stop: threading.Event, seconds: float) -> None:
        """可被停止打断的 sleep：按秒等，别让「停止」按钮要等一整段退避。"""
        end = time.time() + max(0.0, seconds)
        while time.time() < end:
            if stop.wait(min(1.0, max(0.05, end - time.time()))):
                return

    def _finish(self, key: str, status: str, note: str) -> None:
        task = self._tasks.get(key) or {}
        task["status"] = status
        task["note"] = note
        task["finished_at"] = _now_text()
        task["wait_until"] = 0
        self._save()
        if status != "error":
            self._log("INFO", f"[{task.get('name') or key}] 回溯结束：{note}；"
                             f"新增 {task.get('added') or 0} 条，"
                             f"跳过重复 {task.get('skipped_dup') or 0} / "
                             f"已删 {task.get('skipped_deleted') or 0} / "
                             f"纯文本 {task.get('skipped_text') or 0} / "
                             f"转推 {task.get('skipped_retweet') or 0}，"
                             f"共 {task.get('pages_done') or 0} 页")
