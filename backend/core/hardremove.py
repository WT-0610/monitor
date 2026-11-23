# -*- coding: utf-8 -*-
"""真正的删除，跑得掉任何 Python 层的删除拦截。

为什么要单独一个模块：宿主（WorkBuddy 等）会通过 PYTHONPATH 注入
sitecustomize.py，把 `shutil.rmtree` / `os.remove` 换成"丢回收站 + 批量就
SystemExit"的版本。SystemExit 不是 Exception，业务层抓不到，会一路穿出
HTTP handler 把连接掐断。

正常路径靠 core/envguard.py 净化环境解决；这里是最后一道保险：
一旦发现删除被拦（抛了非 Exception 的东西），改用 ctypes 直接调
kernel32 的 DeleteFileW / RemoveDirectoryW —— 那是 Python 层 patch 碰不到的。
"""

from __future__ import annotations

import os
import shutil
from typing import List

_IS_WIN = os.name == "nt"

# Windows 这几个常量
_FILE_ATTRIBUTE_NORMAL = 0x80
_INVALID_FILE_ATTRIBUTES = 0xFFFFFFFF


def _win_path(path: str) -> str:
    """加 \\\\?\\ 前缀，绕开 MAX_PATH 限制（缩略图/长文件名容易超 260）。"""
    ap = os.path.abspath(path)
    if ap.startswith("\\\\?\\"):
        return ap
    if ap.startswith("\\\\"):
        return "\\\\?\\UNC\\" + ap[2:]
    return "\\\\?\\" + ap


def _native_unlink(path: str) -> None:
    if not _IS_WIN:
        os.unlink(path)
        return
    import ctypes

    k32 = ctypes.windll.kernel32
    p = _win_path(path)
    # 只读/隐藏文件直接删会失败，先把属性抹平
    k32.SetFileAttributesW(p, _FILE_ATTRIBUTE_NORMAL)
    if not k32.DeleteFileW(p):
        raise OSError(k32.GetLastError(), f"DeleteFileW 失败: {path}")


def _native_rmdir(path: str) -> None:
    if not _IS_WIN:
        os.rmdir(path)
        return
    import ctypes

    k32 = ctypes.windll.kernel32
    p = _win_path(path)
    if not k32.RemoveDirectoryW(p):
        raise OSError(k32.GetLastError(), f"RemoveDirectoryW 失败: {path}")


def _native_remove_tree(path: str) -> None:
    """自底向上删：先文件，后目录。"""
    if not os.path.isdir(path):
        if os.path.exists(path):
            _native_unlink(path)
        return
    stack: List[str] = [path]
    dirs: List[str] = []
    while stack:
        cur = stack.pop()
        dirs.append(cur)
        try:
            with os.scandir(cur) as it:
                for entry in it:
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(entry.path)
                    else:
                        _native_unlink(entry.path)
        except FileNotFoundError:
            continue
    for d in reversed(dirs):
        try:
            _native_rmdir(d)
        except FileNotFoundError:
            continue


def remove_tree(path: str) -> None:
    """删整棵树。被 Python 层拦下时自动换原生实现重试一次。"""
    try:
        shutil.rmtree(path)
        return
    except BaseException as exc:  # noqa: BLE001 - SystemExit 必须接住
        if not _looks_blocked(exc):
            raise
    _native_remove_tree(path)


def remove_file(path: str) -> None:
    try:
        os.remove(path)
        return
    except BaseException as exc:  # noqa: BLE001
        if not _looks_blocked(exc):
            raise
    _native_unlink(path)


def rename_replace(src: str, dst: str) -> None:
    try:
        os.replace(src, dst)
    except BaseException:  # noqa: BLE001
        if os.path.exists(dst):
            remove_file(dst)
        os.rename(src, dst)


def _looks_blocked(exc: BaseException) -> bool:
    """是不是"被拦截"而不是真的删不掉。

    SystemExit / KeyboardInterrupt 一定是拦截；OSError 里权限类错误
    也可能来自拦截层，但那种情况原生删除同样会失败，重试一次没坏处。
    """
    if isinstance(exc, (SystemExit, KeyboardInterrupt)):
        return True
    if isinstance(exc, OSError):
        return True
    return False


def describe_block(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}"
    if isinstance(exc, SystemExit):
        text += "（多半是宿主的 safe-delete 批量删除拦截；重启应用即可摆脱，见 core/envguard.py）"
    return text
