# -*- coding: utf-8 -*-
"""本地缩略图缓存。

为什么要这个模块（2026-10-29）：内容流滑动卡顿的根因是**列表直接加载原图**。
存档目录里 1196 个媒体共 4.6 GB，平均 3.96 MB，最大单图 28 MB —— 浏览器要把
这些图解码成几千×几千的位图，几十张同时活着就是几百 MB 内存，滑动必然掉帧。
`<img loading="lazy">` 只推迟了"什么时候下载"，没减少"下多少、解多大"。

所以这里把原图缩成两档 WebP 落盘缓存：
    320  —— 列表卡片（约 20~40 KB）
    1280 —— 灯箱预览（秒开，不再直连 28 MB 原图）
原图只留给「打开目录 / 下载」。

设计取舍：
- **按需生成 + 一键预生成**：滑到哪生成哪（单张几十毫秒，无感），设置页有按钮
  后台跑完全库。不在下载时同步生成 —— 那会拖慢抓取，且存量数据仍要补跑一次。
- **缓存失效按源文件 mtime + size**，直接编进文件名，重下/改名会自动重建，
  不需要额外维护索引文件。
- **任何失败都降级**：Pillow 缺失、文件损坏、HEIC 不支持…一律返回 None，
  由调用方回退到原图，绝不让列表开天窗。
"""

from __future__ import annotations

import glob
import hashlib
import os
import threading
import time
from typing import Any, Callable, Dict, List, Optional

# 允许请求的档位，防止被拿来当任意尺寸的图片处理器用
ALLOWED_WIDTHS = (320, 1280)
DEFAULT_WIDTH = 320

try:  # Pillow 是可选依赖：没有就整体降级，不影响监控主流程
    from PIL import Image, ImageOps

    Image.MAX_IMAGE_PIXELS = None  # 存档里有超大图，别让 Pillow 拒绝解压
    _PIL_OK = True
    _PIL_ERR = ""
except Exception as exc:  # noqa: BLE001
    Image = None  # type: ignore[assignment]
    ImageOps = None  # type: ignore[assignment]
    _PIL_OK = False
    _PIL_ERR = str(exc)


def _fingerprint(src: str) -> str:
    """把源文件的 mtime + size 编成短指纹，用于判断缓存是否过期。"""
    try:
        st = os.stat(src)
        return f"{st.st_mtime_ns}_{st.st_size}"
    except OSError:
        return "0_0"


class ThumbCache:
    """按「源路径 hash + 宽度 + 指纹」命名的磁盘缩略图缓存。"""

    def __init__(self, output_dir: str, logger: Optional[Callable[[str, str], None]] = None):
        self.root = os.path.join(os.path.abspath(output_dir), ".thumbs")
        self.logger = logger
        self._lock = threading.Lock()
        os.makedirs(self.root, exist_ok=True)

    # ------------------------------------------------------------------ #
    def _log(self, level: str, msg: str) -> None:
        if self.logger:
            try:
                self.logger(level, msg)
            except Exception:  # noqa: BLE001
                pass

    def _key(self, src: str) -> str:
        norm = os.path.normcase(os.path.abspath(src)).encode("utf-8", "ignore")
        return hashlib.sha1(norm).hexdigest()[:16]

    def _name(self, src: str, width: int) -> str:
        return f"{self._key(src)}_{width}_{_fingerprint(src)}.webp"

    # ------------------------------------------------------------------ #
    def exists(self, src: str, width: int = DEFAULT_WIDTH) -> bool:
        return os.path.isfile(os.path.join(self.root, self._name(src, width)))

    def get(self, src: str, width: int = DEFAULT_WIDTH) -> Optional[str]:
        """返回缩略图文件路径；没有现成的就现生成。失败一律返回 None。"""
        width = width if width in ALLOWED_WIDTHS else DEFAULT_WIDTH
        if not src or not os.path.isfile(src):
            return None
        if not _PIL_OK:
            return None

        dest = os.path.join(self.root, self._name(src, width))
        if os.path.isfile(dest) and os.path.getsize(dest) > 0:
            return dest

        with self._lock:  # 同一张图并发请求时只解码一次
            if os.path.isfile(dest) and os.path.getsize(dest) > 0:
                return dest
            return self._generate(src, width, dest)

    # ------------------------------------------------------------------ #
    def _generate(self, src: str, width: int, dest: str) -> Optional[str]:
        tmp = dest + f".tmp{os.getpid()}{threading.get_ident()}"
        try:
            with Image.open(src) as im:
                im = ImageOps.exif_transpose(im)  # 手机竖拍图的旋转信息别丢
                if getattr(im, "n_frames", 1) > 1:
                    im.seek(0)  # 动图只取第一帧
                # draft 让 JPEG 直接按目标尺寸解码，大图能快好几倍
                try:
                    im.draft(im.mode if im.mode in ("RGB", "L") else "RGB", (width, width))
                except Exception:  # noqa: BLE001
                    pass
                if im.width > width:
                    im = im.resize(
                        (width, max(1, round(im.height * width / im.width))),
                        Image.LANCZOS,
                    )
                if im.mode not in ("RGB", "L"):
                    im = im.convert("RGB")
                try:
                    im.save(tmp, "WEBP", quality=80, method=4)
                except Exception:  # noqa: BLE001
                    im.save(tmp, "JPEG", quality=82, optimize=True)
            os.replace(tmp, dest)
            self._purge_stale(src, width, keep=dest)
            return dest
        except Exception as exc:  # noqa: BLE001
            # 生成失败不能影响浏览：删掉半截文件，让调用方回退原图
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            self._log("DEBUG", f"缩略图生成失败，回退原图：{os.path.basename(src)}（{exc}）")
            return None

    def _purge_stale(self, src: str, width: int, keep: str) -> None:
        """删掉同一张图同一档位的旧指纹文件，避免缓存目录无限膨胀。"""
        pattern = os.path.join(self.root, f"{self._key(src)}_{width}_*.webp")
        for old in glob.glob(pattern):
            if os.path.abspath(old) != os.path.abspath(keep):
                try:
                    os.remove(old)
                except OSError:
                    pass

    # ------------------------------------------------------------------ #
    def discard(self, src: str) -> int:
        """清掉某个源文件的全部缩略图（删除微博时同步调用，别在缓存里留死文件）。

        只按「路径 hash 前缀」匹配，不看 mtime/size 指纹 ——
        调用时源文件往往已经被移进回收站了，stat 拿不到。
        """
        if not src:
            return 0
        key = self._key(src)
        removed = 0
        try:
            for name in os.listdir(self.root):
                if not name.startswith(key + "_"):
                    continue
                try:
                    os.remove(os.path.join(self.root, name))
                    removed += 1
                except OSError:
                    continue
        except OSError:
            pass
        return removed

    def discard_many(self, paths: List[str]) -> int:
        """批量清理，返回删掉的缓存文件数。"""
        return sum(self.discard(p) for p in (paths or []) if p)

    def stats(self) -> Dict[str, Any]:
        try:
            files = os.listdir(self.root)
        except OSError:
            files = []
        size = 0
        for name in files:
            try:
                size += os.path.getsize(os.path.join(self.root, name))
            except OSError:
                pass
        return {
            "root": self.root,
            "files": len(files),
            "bytes": size,
            "pillow": _PIL_OK,
            "pillow_error": _PIL_ERR,
            "widths": list(ALLOWED_WIDTHS),
        }


# ---------------------------------------------------------------------- #
# 预生成：设置页按钮用它后台跑完全库。模块级单例，进度可被轮询。
# ---------------------------------------------------------------------- #
_PRE_STATE: Dict[str, Any] = {
    "running": False,
    "done": 0,
    "total": 0,
    "failed": 0,
    "started_at": 0.0,
    "finished_at": 0.0,
    "message": "",
}
_PRE_LOCK = threading.Lock()


def pregenerate_state() -> Dict[str, Any]:
    with _PRE_LOCK:
        state = dict(_PRE_STATE)
    if state["running"]:
        state["elapsed"] = round(time.time() - state["started_at"], 1)
    return state


def _pre_worker(cache: ThumbCache, paths: List[str]) -> None:
    done = failed = 0
    for i, path in enumerate(paths):
        for width in ALLOWED_WIDTHS:
            if cache.get(path, width) is None:
                failed += 1
        done += 1
        with _PRE_LOCK:
            _PRE_STATE["done"] = done
            _PRE_STATE["failed"] = failed
        if i % 50 == 0:  # 让出 GIL，别把 HTTP 请求饿死
            time.sleep(0.01)
    with _PRE_LOCK:
        _PRE_STATE["running"] = False
        _PRE_STATE["finished_at"] = time.time()
        _PRE_STATE["message"] = f"完成：{done} 张，失败 {failed} 张"


def pregenerate_start(cache: ThumbCache, paths: List[str]) -> Dict[str, Any]:
    """后台批量生成缩略图。已经在跑就返回当前进度，不重复启动。"""
    with _PRE_LOCK:
        if _PRE_STATE["running"]:
            return dict(_PRE_STATE)
        if not _PIL_OK:
            _PRE_STATE.update({"running": False, "message": f"缺少 Pillow：{_PIL_ERR}"})
            return dict(_PRE_STATE)
        _PRE_STATE.update({
            "running": True,
            "done": 0,
            "total": len(paths),
            "failed": 0,
            "started_at": time.time(),
            "finished_at": 0.0,
            "message": "",
        })
    threading.Thread(
        target=_pre_worker, args=(cache, paths), name="thumb-pregen", daemon=True
    ).start()
    return dict(_PRE_STATE)
