# -*- coding: utf-8 -*-
"""轻量日志总线：支持控制台输出 + 多订阅者（GUI / 网页看板）。"""

from __future__ import annotations

import os
import threading
import time
from typing import Callable, List

LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40}


class LogBus:
    """线程安全日志广播。"""

    def __init__(self, level: str = "INFO", log_file: str = "", echo: bool = True):
        self.level = LEVELS.get(str(level).upper(), 20)
        self.echo = echo
        self.log_file = log_file
        self._subs: List[Callable[[str, str], None]] = []
        self._lock = threading.Lock()
        self._buffer: List[str] = []
        self._max_buffer = 500
        if log_file:
            os.makedirs(os.path.dirname(log_file), exist_ok=True)

    def subscribe(self, fn: Callable[[str, str], None]) -> None:
        with self._lock:
            self._subs.append(fn)

    def unsubscribe(self, fn: Callable[[str, str], None]) -> None:
        with self._lock:
            if fn in self._subs:
                self._subs.remove(fn)

    def recent(self, count: int = 200) -> List[str]:
        with self._lock:
            return self._buffer[-count:]

    def emit(self, level: str, message: str) -> None:
        if LEVELS.get(level, 20) < self.level:
            return
        stamp = time.strftime("%H:%M:%S")
        line = f"[{stamp}] {level:<7} {message}"
        with self._lock:
            self._buffer.append(line)
            if len(self._buffer) > self._max_buffer:
                del self._buffer[: len(self._buffer) - self._max_buffer]
            subs = list(self._subs)
        if self.echo:
            try:
                print(line, flush=True)
            except Exception:  # noqa: BLE001
                pass
        if self.log_file:
            try:
                with open(self.log_file, "a", encoding="utf-8") as fp:
                    fp.write(f"{time.strftime('%Y-%m-%d ')}{line}\n")
            except Exception:  # noqa: BLE001
                pass
        for fn in subs:
            try:
                fn(level, message)
            except Exception:  # noqa: BLE001
                pass

    def __call__(self, level: str, message: str) -> None:
        self.emit(level, message)
