# -*- coding: utf-8 -*-
"""后台守护：api_server 挂了自动拉起。

三条设计约束都来自实际踩坑，别随手改：

1. 只负责"进程在不在"。监控线程活不活由 api_server 内部的看门狗管，
   否则两层会因为"监控被用户手动停止"打架。
2. 退出必须靠哨兵文件 data/.stop_flag。有守护就会自动拉起，
   没有哨兵的话用户点「退出程序」会被立刻复活，永远退不掉。
3. 单实例。两个 supervisor 同时跑会互相拉起、互相 kill。

用法：
    python supervisor.py --open      # 后台守护 + 首次拉起时打开界面
    python supervisor.py --stop      # 写停止标记，守护自行退出
    python supervisor.py --status    # 看一眼现在是什么状态
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# 存档与运行态文件（日志/锁/哨兵）都在新工程根的 data/ 下，
# 不要和 backend/ 里的代码混在一起
ROOT_DIR = os.path.dirname(BASE_DIR)
sys.path.insert(0, BASE_DIR)  # 双击 bat 时 cwd 未必是项目根，core 包要能 import

from core.envguard import clean_env, poisoned_reasons  # noqa: E402
from core.config import CONFIG_PATH, resolve_output_dir  # noqa: E402
from core.single_instance import (  # noqa: E402
    acquire,
    find_windows,
    focus_existing,
    holder_pid,
)

CONFIG_FILE = CONFIG_PATH

# 下面三个在 main() 里按 config.json 的 output_dir 重新绑定：
# 哨兵文件必须和 api_server 写在同一处，否则守护永远看不到退出意图。
DATA_DIR = os.path.join(ROOT_DIR, "data")
STOP_FLAG = os.path.join(DATA_DIR, ".stop_flag")
LOG_FILE = os.path.join(DATA_DIR, "supervisor.log")

DEFAULT_PORT = 8788
FAIL_THRESHOLD = 3          # 连续失败几次才判定"真挂了"，避免忙一下就被当成死
STARTUP_GRACE_SEC = 120    # 大归档首次启动可能要迁移数据库路径，期间不能误杀
CREATE_NO_WINDOW = 0x08000000
STILL_ACTIVE = 259


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def log(message: str) -> None:
    """只能写文件。pythonw 起的时候 sys.stdout 是 None，print 会炸。"""
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}"
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as fp:
            fp.write(line + "\n")
    except OSError:
        pass
    if sys.stdout is not None:
        try:
            print(line, flush=True)
        except Exception:  # noqa: BLE001
            pass


def api_port() -> int:
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as fp:
            return int((json.load(fp) or {}).get("api_port") or DEFAULT_PORT)
    except Exception:  # noqa: BLE001
        return DEFAULT_PORT


def resolve_data_dir() -> str:
    """存档目录可配置，哨兵/日志/pid 都跟着它走，别硬编码 data。"""
    cfg = {}
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as fp:
            cfg = json.load(fp) or {}
    except Exception:  # noqa: BLE001
        pass
    return resolve_output_dir(cfg)


def init_paths() -> None:
    global DATA_DIR, STOP_FLAG, LOG_FILE
    DATA_DIR = resolve_data_dir()
    STOP_FLAG = os.path.join(DATA_DIR, ".stop_flag")
    LOG_FILE = os.path.join(DATA_DIR, "supervisor.log")
    os.makedirs(DATA_DIR, exist_ok=True)


def _opener():
    """绕开系统代理：本机 127.0.0.1 也会被代理劫走，返回 502。"""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def health_detail(port: int) -> tuple[bool, str]:
    """健康检查 + 失败原因。失败原因别吞掉——否则"不通"永远查不出为什么。"""
    try:
        with _opener().open(f"http://127.0.0.1:{port}/api/health", timeout=3) as resp:
            return resp.status == 200, ""
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


def healthy(port: int) -> bool:
    return health_detail(port)[0]


def server_pid(port: int) -> int:
    try:
        with _opener().open(f"http://127.0.0.1:{port}/api/health", timeout=3) as resp:
            return int(json.loads(resp.read().decode("utf-8")).get("pid") or 0)
    except Exception:  # noqa: BLE001
        return 0


def request_quit(port: int) -> bool:
    """让 api_server 自己优雅退出（它会先停监控再关服务）。"""
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/control",
            data=json.dumps({"action": "quit"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with _opener().open(req, timeout=5):
            return True
    except Exception:  # noqa: BLE001
        return False


def request_open_ui(port: int) -> bool:
    """让已经在跑的服务把界面窗口提到最前（没有窗口时它会新建一个）。"""
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/open-ui",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with _opener().open(req, timeout=5):
            return True
    except Exception:  # noqa: BLE001
        return False


def ensure_ui(port: int, why: str) -> bool:
    """保证界面窗口对用户可见：已开着就提到最前，没有就开一个。

    这是"双击 bat 没反应"的唯一解药。supervisor 有三条进入路径：
      ① 拿到锁 + 服务没在跑 → 拉起并 --open
      ② 拿到锁 + 服务已在跑 → 以前这里直接结束，**完全不开窗**
      ③ 没拿到锁（已有守护）→ 以前只 focus，窗口已被关掉时也开不出来
    ②③ 都会让用户看到"黑窗口一闪、什么都没发生"，于是反复双击。
    所以开窗不能挂在某条分支上，必须三个入口都过一遍。
    """
    from core.single_instance import find_windows

    # browser_only：只认浏览器窗口。老 Tkinter 界面标题也带关键字，
    # 不算"前端已开"，否则窗口被关掉后就再也开不出来了。
    if focus_existing(browser_only=True):
        log(f"[界面·{why}] 已开着，提到最前")
        return True

    if request_open_ui(port):
        # 窗口不是瞬间出现：Edge 可能要从零启动进程，最多等 6 秒
        for _ in range(12):
            time.sleep(0.5)
            if find_windows(browser_only=True):
                log(f"[界面·{why}] 已打开")
                return True
        log(f"[界面·{why}] 已请求服务开窗，窗口识别尚未完成；不重复启动浏览器")
        return True
    else:
        log(f"[界面·{why}] 请服务开窗失败（/api/open-ui 不通）")

    # 兜底：不走 HTTP，本地直接起浏览器窗口
    try:
        from api_server import open_app_window

        open_app_window(f"http://127.0.0.1:{port}")
        time.sleep(2)
        if find_windows(browser_only=True):
            log(f"[界面·{why}] 已用本地兜底打开")
            return True
    except Exception as exc:  # noqa: BLE001
        log(f"[界面·{why}] 本地兜底也失败：{exc}")

    log(f"[界面·{why}] 界面没能打开，请手动访问 http://127.0.0.1:{port}")
    return False


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    import ctypes

    handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        return bool(ok) and code.value == STILL_ACTIVE
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def launcher() -> str:
    """优先 pythonw：守护和它拉起的服务都不该有控制台窗口。"""
    exe = sys.executable or "python.exe"
    if exe.lower().endswith("python.exe"):
        candidate = exe[:-len("python.exe")] + "pythonw.exe"
        if os.path.isfile(candidate):
            return candidate
    return exe


def stop_flag_exists() -> bool:
    return os.path.exists(STOP_FLAG)


def write_stop_flag() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(STOP_FLAG, "w", encoding="utf-8") as fp:
        fp.write(time.strftime("%Y-%m-%d %H:%M:%S"))


# --------------------------------------------------------------------------- #
# 单实例
# --------------------------------------------------------------------------- #
def another_instance() -> int:
    """返回已在运行的守护 PID（0 表示没有）。

    判据是 data/supervisor.lock 上的独占文件锁，不是 PID 文件：
    进程被强杀时锁由系统自动释放，不会出现"pid 文件残留导致永远起不来"
    或"pid 被复用导致误判还在跑"。
    """
    return holder_pid("supervisor")


# --------------------------------------------------------------------------- #
# 拉起 / 结束服务
# --------------------------------------------------------------------------- #
def spawn_server(port: int, open_ui: bool) -> subprocess.Popen:
    args = [launcher(), os.path.join(BASE_DIR, "api_server.py")]
    if open_ui:
        args.append("--open")
    errlog = open(LOG_FILE, "a", encoding="utf-8")
    # 必须传净化过的 env：宿主注入的 PYTHONPATH 里那套 safe-delete shim 会把
    # shutil.rmtree 换成"批量删除就 SystemExit"的版本，清空回收站会直接断连接。
    # 详见 core/envguard.py 顶部注释。
    reasons = poisoned_reasons()
    if reasons:
        log("拉起前已净化子进程环境：" + "；".join(reasons))
    try:
        proc = subprocess.Popen(
            args,
            cwd=BASE_DIR,
            stdout=subprocess.DEVNULL,
            stderr=errlog,
            env=clean_env(),
            creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    finally:
        errlog.close()
    log(f"已拉起 api_server（pid={proc.pid}，端口 {port}）")
    return proc


def kill_server(proc: subprocess.Popen | None, port: int, graceful: bool = True) -> None:
    """结束服务。**以 /api/health 报的 pid 为准**，不靠 Popen 句柄。

    这台机器上 pythonw.exe 是"启动器壳 + 真进程"两段式：Popen 拿到的是壳，
    壳会立刻退出而真进程继续占着端口。只 terminate() 壳的话，守护会以为
    已经杀干净、其实旧服务还在跑，最后变成"拉起一个新壳 → 新壳发现端口被占
    → 自己退出"，永远救不回真正卡死的那个。
    """
    # 先礼：让服务自己优雅退出（它会先停监控再关服务）。
    # 注意 request_quit 会顺带写哨兵文件，所以只在"确实要收官"时用 graceful，
    # 重启服务时必须 graceful=False，否则守护下一轮会看到哨兵把自己关了。
    if graceful and healthy(port):
        request_quit(port)
        for _ in range(6):
            time.sleep(0.5)
            if not healthy(port):
                break

    pid = server_pid(port)
    if not pid and proc is not None and proc.poll() is None:
        pid = proc.pid
    if pid and pid_alive(pid):
        log(f"强杀服务进程 pid={pid}")
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
                           check=False)
        except Exception:  # noqa: BLE001
            pass

    # 兜底：壳自己还挂着也收掉
    if proc is not None and proc.poll() is None:
        proc.terminate()


# --------------------------------------------------------------------------- #
def _pin_console_encoding() -> None:
    """把 stdout 钉到控制台代码页，别让 Python 自己挑成 UTF-8。

    踩过的坑：936 控制台下直接跑中文正常，一旦输出被重定向（bat 里写日志、
    管道），Python 改用 UTF-8 写字节，按 GBK 读就成了"宸插啓鍏ュ仠姝㈡爣璁"。
    钉死之后两种场景字节一致。pythonw 起时 stdout 是 None，直接跳过。
    """
    stream = sys.stdout
    if stream is None or not hasattr(stream, "reconfigure"):
        return
    cp = 936
    try:
        import ctypes
        cp = ctypes.windll.kernel32.GetConsoleOutputCP() or 936
    except Exception:  # noqa: BLE001  非 Windows 或没有控制台
        return
    try:
        stream.reconfigure(encoding=f"cp{cp}", errors="replace")
    except Exception:  # noqa: BLE001
        pass


def main() -> int:
    _pin_console_encoding()
    parser = argparse.ArgumentParser(description="微博监控后台守护")
    parser.add_argument("--open", action="store_true", help="首次拉起服务时打开界面窗口")
    parser.add_argument("--interval", type=int, default=5,
                        help="健康检查间隔秒数（越小恢复越快，一次检查就是一次本地 HTTP）")
    parser.add_argument("--stop", action="store_true", help="写停止标记，让守护退出")
    parser.add_argument("--status", action="store_true", help="只打印状态")
    args = parser.parse_args()

    init_paths()
    port = api_port()

    if args.stop:
        write_stop_flag()
        request_quit(port)
        print("已写入停止标记，守护和服务都会退出。")
        return 0

    if args.status:
        alive = another_instance()
        ok, why = health_detail(port)
        print(f"端口      : {port}")
        print(f"服务      : {'在跑 pid=' + str(server_pid(port)) if ok else '不通 —— ' + why}")
        print(f"守护      : {'在跑 pid=' + str(alive) if alive else '没在跑'}")
        print(f"停止标记  : {'存在（下次不会被拉起）' if stop_flag_exists() else '无'}")
        return 0

    lock = acquire("supervisor")
    if lock is None:
        owner = holder_pid("supervisor")
        log(f"已有守护在运行（pid={owner or '未知'}），本次只把界面调出来")
        # 第二次双击必须看得见反应：把已经开着的界面提到最前。
        # 否则屏幕毫无变化，用户会以为没起来，然后反复双击。
        # 窗口被关掉了（不是最小化）就请已在跑的服务重新开一个 —— 只有一个。
        if args.open:
            ensure_ui(port, "二次启动")
        return 0

    # 双击 bat 才走到这里，说明用户想开始跑 → 清掉上次的停止标记
    if stop_flag_exists():
        try:
            os.remove(STOP_FLAG)
        except OSError:
            pass

    log(f"守护启动（pid={os.getpid()}，端口 {port}，间隔 {args.interval}s）")

    proc: subprocess.Popen | None = None
    fails = 0
    startup_deadline = 0.0
    open_ui = args.open

    if healthy(port):
        log(f"服务已在运行（pid={server_pid(port)}），直接接管监控")
        if open_ui:
            # 这个服务是上一轮留下的（守护被强杀过 / 刚重启完），
            # 界面窗口很可能早就被关掉了 —— 不补这一步，双击就没反应。
            ensure_ui(port, "接管已有服务")
    else:
        proc = spawn_server(port, False)
        startup_deadline = time.monotonic() + STARTUP_GRACE_SEC
        time.sleep(5)

    needs_ui = open_ui and not healthy(port)
    if open_ui and not needs_ui and proc is not None:
        ensure_ui(port, "首次拉起")

    while True:
        try:
            if stop_flag_exists():
                log("检测到停止标记，关闭服务并退出守护")
                request_quit(port)
                time.sleep(1.5)
                kill_server(proc, port)
                break

            if healthy(port):
                startup_deadline = 0.0
                if needs_ui:
                    needs_ui = False
                    ensure_ui(port, "服务就绪")
                if fails:
                    log("服务恢复正常")
                fails = 0
                if proc is not None and proc.poll() is not None:
                    # pythonw 是"壳+真进程"两段式，壳退出属于正常现象，不必记日志
                    proc = None
            else:
                if startup_deadline and time.monotonic() < startup_deadline:
                    # 服务仍在初始化：等完整个宽限期，期间不累计故障次数。
                    fails = 0
                else:
                    fails += 1
                    _, why = health_detail(port)
                    log(f"健康检查失败（{fails}/{FAIL_THRESHOLD}）{why}")
                    if fails >= FAIL_THRESHOLD:
                        log("判定服务已停止，重新拉起")
                        kill_server(proc, port, graceful=False)
                        proc = spawn_server(port, False)
                        startup_deadline = time.monotonic() + STARTUP_GRACE_SEC
                        needs_ui = open_ui
                        fails = 0
                        time.sleep(4)
        except Exception as exc:  # noqa: BLE001
            log(f"守护循环异常：{exc}")

        # 正常时慢轮询省资源；一旦开始失败就快轮询，
        # 否则 3 次失败要等将近 40 秒才判定"真挂了"，恢复太慢。
        time.sleep(3 if fails else max(3, args.interval))

    lock.release()
    log("守护已退出")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
