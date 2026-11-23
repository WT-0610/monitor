"""Periodically remove high-confidence Weibo duplicates after X is safely archived."""
from __future__ import annotations

import threading
import time

from core.similar_cleanup import scan
from core.store import Store


class CrossPlatformCleanup:
    def __init__(self, db_path, log):
        self.db_path = db_path
        self.log = log
        self._thread = None

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="cross-platform-cleanup", daemon=True)
            self._thread.start()

    def run_once(self):
        result = scan(self.db_path)
        store = Store(self.db_path, platform="weibo", bootstrap=False)
        deleted = 0
        for group in result["groups"]:
            if group.get("grade") != "high":
                continue
            keeper = (group.get("keep") or [None])[0]
            if not keeper or keeper["platform"] != "x":
                continue
            # The scan checks matching full image sets and complete local media.
            # Only delete candidates directly verified against the selected X keeper.
            for item in group.get("auto_delete_image") or []:
                if item["platform"] != "weibo":
                    continue
                try:
                    if store.delete_post(str(item["mblog_id"]), tombstone=True).get("exists"):
                        deleted += 1
                except Exception as exc:
                    self.log("WARNING", f"跨平台重复清理失败 {item['mblog_id']}：{exc}")
        if deleted:
            self.log("INFO", f"跨平台重复清理：{deleted} 条微博记录已移入回收站，保留 X 记录")
        return deleted

    def _run(self):
        # Let both monitors finish startup; the first pass also covers old archives.
        time.sleep(60)
        while True:
            try:
                self.run_once()
            except Exception as exc:
                self.log("WARNING", f"跨平台重复校验暂停：{exc}")
            time.sleep(300)
