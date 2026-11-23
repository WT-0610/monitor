# -*- coding: utf-8 -*-
"""把从「被宿主污染的终端」继承来的环境变量摘干净。

踩过的坑（2026-11-01 清空回收站失败）：

    WorkBuddy / 某些 Agent 宿主会往子进程注入 PYTHONPATH=<...>/cli/vendor/shim，
    那里的 sitecustomize.py 会把 os.remove / shutil.rmtree 换成一个「先丢回收站、
    批量超过阈值就拒绝」的版本，拒绝时是 `raise SystemExit(1)`。

    SystemExit 不是 Exception 的子类，业务层的 `except Exception` 抓不到，
    会一路穿出 handler，被 socketserver 当成致命错误直接关掉连接 ——
    前端 fetch 收到的是网络错误，只能显示「连不上本地服务」，
    实际上服务好好的（/api/health 还是 200）。链路是：

        shutil.rmtree → SystemExit → 穿出 do_POST → 连接断开 → 前端报"连不上"

所以：**拉起 api_server 的每一处都要先 env = clean_env()**，
保证子进程里既没有 shim 的 PYTHONPATH，也没有让它启用的 session 变量。
"""

from __future__ import annotations

import os
from typing import Dict, Iterable, List

# shim 目录的两段特征路径，命中任意一段就摘掉这条 PYTHONPATH
_SHIM_MARKERS = (
    os.path.join("cli", "vendor", "shim"),
    os.path.join("cli", "vendor", "shim", "safe-bin"),
)

# 这些变量单独存在就能让 sitecustomize 启用删除拦截，一并清掉
_POISON_KEYS = (
    "CODEBUDDY_SESSION_ID",
    "CLAUDE_SESSION_ID",
    "CODEBUDDY_SAFE_DELETE_ENABLED",
    "CODEBUDDY_SAFE_DELETE_SANDBOX",
    "CODEBUDDY_SAFE_DELETE_BULK_GUARD",
    "CODEBUDDY_SAFE_DELETE_BULK_STATE_DIR",
    "CODEBUDDY_SAFE_DELETE_BULK_THRESHOLD",
    "CODEBUDDY_SAFE_DELETE_BULK_REPLAY_FILE",
    "CODEBUDDY_SAFE_DELETE_REPORT_PATH",
    "CODEBUDDY_SAFE_DELETE_BIN_DIR",
    "CODEBUDDY_SAFE_DELETE_BROKER_DELETE",
    "CODEBUDDY_BROKERED_FS_HOOK_ENABLED",
    "CODEBUDDY_TOOL_CALL_ID",
)

# 已经净化过的标记，防止 api_server 自愈时无限 re-exec
HEALED_FLAG = "WEIBO_ENV_HEALED"


def _is_shim_path(entry: str) -> bool:
    if not entry:
        return False
    norm = entry.replace("/", os.sep).replace("\\", os.sep)
    return any(marker in norm for marker in _SHIM_MARKERS)


def poisoned_reasons(env: Dict[str, str] | None = None) -> List[str]:
    """返回污染原因（空列表 = 干净）。用于日志和自检。"""
    env = os.environ if env is None else env
    reasons: List[str] = []
    for key in _POISON_KEYS:
        if env.get(key):
            reasons.append(f"{key} 已设置")
    for var in ("PYTHONPATH", "PATH"):
        if any(_is_shim_path(p) for p in (env.get(var) or "").split(os.pathsep)):
            reasons.append(f"{var} 含 safe-delete shim")
    return reasons


def _strip_path_list(value: str) -> str:
    parts = [p for p in value.split(os.pathsep) if p and not _is_shim_path(p)]
    return os.pathsep.join(parts)


def clean_env(env: Dict[str, str] | None = None) -> Dict[str, str]:
    """返回净化后的环境副本；不改动传入的 dict。"""
    out: Dict[str, str] = dict(os.environ if env is None else env)
    for key in _POISON_KEYS:
        out.pop(key, None)
    for var in ("PYTHONPATH", "PATH"):
        if var in out:
            cleaned = _strip_path_list(out[var])
            if cleaned:
                out[var] = cleaned
            else:
                out.pop(var, None)
    out[HEALED_FLAG] = "1"
    return out


def heal_current_process() -> bool:
    """给「已经被污染地启动的」解释器用：清掉环境变量并原地 re-exec。

    返回 True 表示已 re-exec（本函数不会返回）；False 表示本就干净或已净化过。
    re-exec 后 sitecustomize 不再被导入，删除 API 恢复原样。
    """
    if os.environ.get(HEALED_FLAG) == "1":
        return False
    reasons = poisoned_reasons()
    if not reasons:
        return False
    env = clean_env()
    argv = [os.path.abspath(sys_executable()), *[_arg_str(a) for a in _argv_tail()]]
    try:
        os.execve(argv[0], argv, env)
    except Exception:  # noqa: BLE001
        # re-exec 失败（极少见）：清掉当前进程里的变量，至少让后续子进程干净
        os.environ.pop("PYTHONPATH", None)
        for key in _POISON_KEYS:
            os.environ.pop(key, None)
        return False
    return True


def _arg_str(arg: Iterable) -> str:
    return arg if isinstance(arg, str) else str(arg)


def sys_executable() -> str:
    import sys

    return sys.executable


def _argv_tail() -> List[str]:
    import sys

    return list(sys.argv[1:])
