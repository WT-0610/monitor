# -*- coding: utf-8 -*-
"""单实例保护：进程级独占锁 + 把已开的窗口提到最前。

这两件事必须放一起看，因为"前端只能打开一个"是两半：

1. 不起第二个进程 —— 靠操作系统托管的文件锁（Windows msvcrt / POSIX fcntl）。
   选文件锁而不是 PID 文件，是因为 PID 判断有两种骗人的方式：
   - 进程被 taskkill /F 强杀后 pid 文件残留，pid 恰好被系统分给别人
     → 判"还活着" → 双击永远起不来
   - pid 文件被误删 → 起第二个实例，两个守护互相认为对方死了
   文件锁没有这两个问题：进程一没，句柄关闭，锁自动释放。

2. 第二次双击要看得见反馈 —— 把已经开着的窗口提到最前。
   光有锁只能保证"没有第二个进程"，用户双击一下屏幕毫无反应，
   最自然的反应就是"没起来"，然后继续双击。

用法：
    from core.single_instance import acquire, focus_existing

    lock = acquire("supervisor")
    if lock is None:
        focus_existing()      # 已有实例：把它的窗口提到最前
        return 0
    ...                       # 正常跑，退出时 lock.release()
"""

from __future__ import annotations

import os
import time

try:  # 项目里是 core.single_instance，兜底支持相对导入
    from core.config import CONFIG_PATH
except ImportError:  # pragma: no cover
    from .config import CONFIG_PATH


# 界面窗口的标题特征。三个入口的标题都不一样，取最小公共前缀：
#   新界面（Vue）  ：微博监控 · 内容存档
#   旧网页看板     ：微博监控看板
#   旧 Tkinter     ：微博实时监控 · 内容存档
WINDOW_KEYWORDS = ("桔子监控台", "微博监控", "微博实时")

# 能承载前端页面的宿主进程（浏览器 / WebView2 壳）。
#
# 光靠标题判断"前端在不在"会踩坑：老 Tkinter 界面的标题
# 「微博实时监控 · 内容存档」同样命中关键字，于是
# open_app_window() 里的 focus_existing() 返回真 → 直接 return，
# 真正的前端窗口一次都开不出来（用户看到的就是"前端不显示"）。
# 所以"找前端窗口"必须再加一道宿主进程的判定。
FRONTEND_HOST_PROCS = (
    "msedge.exe",
    "chrome.exe",
    "msedgewebview2.exe",
    "firefox.exe",
    "weibo_monitor.exe",       # Tauri 打包版（WebView2 壳）
    "weibo_monitor_app.exe",
)

# 锁文件里什么都不写，锁第 0 字节即可。
#
# 这里踩过一个大坑：最初把 pid 文本和锁放在同一个文件里（pid 写开头、
# 锁放 4096 偏移），以为错开就行。实际上 Windows 的 LockFile 一旦生效，
# 别的进程连 open() 这个文件都可能直接失败（Device or resource busy）。
# 于是 acquire() 里的 os.open() 抛 OSError，掉进最外层兜底 except，
# 返回一个"假锁"（fd=-1）→ 第二个实例以为自己抢到了，实际没有任何锁，
# 结果是每次双击都多起一个守护。
#
# 现在的原则：锁文件只用来锁，pid 另存一个纯文本文件。
LOCK_OFFSET = 0


# --------------------------------------------------------------------------- #
# 路径
# --------------------------------------------------------------------------- #
def data_dir() -> str:
    """锁文件跟存档目录走：output_dir 是可配置的，别写死 data。"""
    from .config import resolve_output_dir
    cfg = {}
    try:
        import json

        with open(CONFIG_PATH, "r", encoding="utf-8") as fp:
            cfg = json.load(fp) or {}
    except Exception:  # noqa: BLE001
        pass
    return resolve_output_dir(cfg)


def lock_path(name: str) -> str:
    return os.path.join(data_dir(), f"{name}.lock")


def pid_path(name: str) -> str:
    """持有者 pid 的纯文本文件（只给人看，不参与加锁）。"""
    return os.path.join(data_dir(), f"{name}.pid")


# --------------------------------------------------------------------------- #
# 独占锁
# --------------------------------------------------------------------------- #
class InstanceLock:
    """持有一个独占文件锁。进程退出（正常或崩溃）时系统会自动释放。"""

    def __init__(self, path: str, fd: int, windows: bool, pidfile: str = "") -> None:
        self.path = path
        self.fd = fd
        self._windows = windows
        self.pidfile = pidfile
        self.pid = os.getpid()

    def release(self) -> None:
        if self.fd is None:
            return
        try:
            if self.fd >= 0:
                if self._windows:
                    import msvcrt

                    os.lseek(self.fd, LOCK_OFFSET, os.SEEK_SET)
                    msvcrt.locking(self.fd, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self.fd, fcntl.LOCK_UN)
        except Exception:  # noqa: BLE001
            pass
        try:
            if self.fd >= 0:
                os.close(self.fd)
        except OSError:
            pass
        self.fd = -1
        if self.pidfile:
            try:
                os.remove(self.pidfile)
            except OSError:
                pass

    def __enter__(self) -> "InstanceLock":
        return self

    def __exit__(self, *exc) -> None:
        self.release()


def acquire(name: str) -> InstanceLock | None:
    """拿锁。拿不到返回 None（说明已经有一个实例在跑）。

    任何异常都按"拿不到"处理 —— 宁可一次起不来（有 WM_FORCE=1 逃生），
    也不能悄悄放行起第二个守护：两个守护会互相 kill 服务。
    """
    path = lock_path(name)
    pidfile = pid_path(name)
    windows = os.name == "nt"

    try:
        if windows:
            import msvcrt

            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
            try:
                os.lseek(fd, LOCK_OFFSET, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)  # 非阻塞，抢不到直接抛
            except OSError:
                os.close(fd)
                return None
        else:
            import fcntl

            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(fd)
                return None
    except Exception as exc:  # noqa: BLE001
        # 锁文件都打不开（权限、被独占）→ 无法判断，一律当"已有实例"。
        # WM_FORCE=1 是逃生口：锁机制本身坏掉时不至于永远启不来。
        if os.environ.get("WM_FORCE") == "1":
            return InstanceLock(path, -1, windows=windows, pidfile="")
        print(f"[single_instance] 无法获取 {name} 锁（{exc}），按“已有实例在运行”处理")
        return None

    _write_pid(pidfile)
    return InstanceLock(path, fd, windows=windows, pidfile=pidfile)


def _write_pid(pidfile: str) -> None:
    """写入持有者 pid + 时间，纯粹是给人看的（--status 会读）。"""
    try:
        with open(pidfile, "w", encoding="utf-8") as fp:
            fp.write(f"{os.getpid()} {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
    except OSError:
        pass


def holder_pid(name: str) -> int:
    """锁当前的持有者 pid（0 = 没人持有）。"""
    path = lock_path(name)
    pid = 0
    try:
        with open(pid_path(name), "r", encoding="utf-8") as fp:
            pid = int((fp.read().split() or ["0"])[0])
    except Exception:  # noqa: BLE001
        pid = 0
    # 锁文件能抢到 = 没人持有，pid 文件是上次残留的
    if _try_lock_once(path):
        return 0
    return max(pid, 0)


def _try_lock_once(path: str) -> bool:
    """能抢到锁 = 没人持有。抢到立刻放掉。"""
    try:
        if os.name == "nt":
            import msvcrt

            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
            try:
                os.lseek(fd, LOCK_OFFSET, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError:
                os.close(fd)
                return False
            try:
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            os.close(fd)
            return True
        import fcntl

        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
        return True
    except Exception:  # noqa: BLE001
        return False


def held(name: str) -> bool:
    return holder_pid(name) > 0


# --------------------------------------------------------------------------- #
# 窗口置前
# --------------------------------------------------------------------------- #
def _proc_name(pid: int) -> str:
    """进程的可执行文件名（小写）。取不到返回空串。"""
    if os.name != "nt" or pid <= 0:
        return ""
    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = ctypes.windll.kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION, False, pid
    )
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        ok = ctypes.windll.kernel32.QueryFullProcessImageNameW(
            handle, 0, buf, ctypes.byref(size)
        )
        if ok:
            return os.path.basename(buf.value or "").lower()
    except Exception:  # noqa: BLE001
        pass
    finally:
        try:
            ctypes.windll.kernel32.CloseHandle(handle)
        except Exception:  # noqa: BLE001
            pass
    return ""


def find_windows(keywords=WINDOW_KEYWORDS, browser_only=False):
    """返回 [(hwnd, title, pid)]，按 Z 序从上到下。非 Windows 返回空。

    browser_only=True 时只保留浏览器/WebView2 的窗口 —— 用来判断
    "前端界面到底开没开"。默认 False 是给老 Tkinter 入口置前自己用的。
    """
    if os.name != "nt":
        return []
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    enum_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    found: list[tuple[int, str, int]] = []

    def _cb(hwnd, _lparam):
        if not hwnd or not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if not length:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value or ""
        if not any(k in title for k in keywords):
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        owner = int(pid.value)
        if browser_only and _proc_name(owner) not in FRONTEND_HOST_PROCS:
            return True
        found.append((int(hwnd), title, owner))
        return True

    try:
        user32.EnumWindows(enum_proc(_cb), 0)
    except Exception:  # noqa: BLE001
        return []
    return found


def _flash(hwnd: int) -> None:
    """实在抢不到前台就闪任务栏，至少让用户知道窗口在哪。"""
    import ctypes
    from ctypes import wintypes

    class FLASHWINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", ctypes.c_uint),
            ("hwnd", wintypes.HWND),
            ("dwFlags", ctypes.c_uint),
            ("uCount", ctypes.c_uint),
            ("dwTimeout", ctypes.c_uint),
        ]

    info = FLASHWINFO()
    info.cbSize = ctypes.sizeof(info)
    info.hwnd = wintypes.HWND(hwnd)
    info.dwFlags = 3          # FLASHW_ALL：标题栏 + 任务栏一起闪
    info.uCount = 3
    info.dwTimeout = 0
    try:
        ctypes.windll.user32.FlashWindowEx(ctypes.byref(info))
    except Exception:  # noqa: BLE001
        pass


def focus_window(hwnd: int) -> bool:
    """把窗口提到最前。最小化的先还原，抢不到前台就闪任务栏。

    Windows 的前台锁：不是前台进程（也没有被前台进程启动）时，
    SetForegroundWindow 会直接失败、只闪一下任务栏。这在静默启动
    （pythonw 无窗口）的场景里几乎必现，所以兜底要一层层来。
    """
    if os.name != "nt":
        return False
    import ctypes

    user32 = ctypes.windll.user32
    VK_MENU = 0x12
    KEYEVENTF_KEYUP = 0x0002

    try:
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)     # SW_RESTORE
        else:
            user32.ShowWindow(hwnd, 5)     # SW_SHOW

        if user32.SetForegroundWindow(hwnd):
            return True

        # 发一个 Alt 按键能临时解除前台锁，是 Win32 里唯一可靠的公共做法：
        # 按下 → 置前 → 抬起。单独 keybd_event 不会真的触发菜单。
        try:
            user32.keybd_event(VK_MENU, 0, 0, 0)
            ok = user32.SetForegroundWindow(hwnd)
            user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
            if ok:
                return True
        except Exception:  # noqa: BLE001
            pass

        try:
            user32.SwitchToThisWindow(hwnd, True)   # 已废弃但仍有效
        except Exception:  # noqa: BLE001
            pass
        if int(user32.GetForegroundWindow() or 0) == int(hwnd):
            return True
    except Exception:  # noqa: BLE001
        pass

    _flash(hwnd)
    return False


def focus_existing(keywords=WINDOW_KEYWORDS, browser_only=False) -> bool:
    """把已开着的界面窗口提到最前。

    返回值只回答"有没有已开着的窗口"，**不回答置前是否成功**。
    调用方拿它来决定"要不要再开一个"，而置前失败绝不能变成"再开一个"——
    否则前台锁抢不到时就会每点一次多一个窗口（这个坑实测踩到过）。

    browser_only=True 时只认浏览器窗口：老 Tkinter 界面标题也带关键字，
    把它当成"前端已开"会让前端永远起不来。
    """
    windows = find_windows(keywords, browser_only=browser_only)
    if not windows:
        return False
    focus_window(windows[0][0])
    return True


def close_app_windows() -> int:
    """关闭本应用的浏览器窗口，不影响其它浏览器标签页。"""
    if os.name != "nt":
        return 0
    import ctypes

    windows = find_windows(keywords=("桔子监控台",), browser_only=True)
    for hwnd, _title, _pid in windows:
        ctypes.windll.user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
    return len(windows)
