"""网络错误的分类，以及「本机代理端口到底开没开」的快速探测。

为什么单独一个模块：X 抓取失败时的原始异常是三层嵌套的 requests / urllib3
文本 —— `ProxyError: HTTPSConnectionPool(host='x.com', port=443): Max retries
exceeded with url: /foo (Caused by ProxyError('Unable to connect to proxy',
NewConnectionError("HTTPConnection(host='127.0.0.1', port=10801): Failed to
establish a new connection: [WinError 10061] 由于目标计算机积极拒绝…")))`。
直接把这段写进日志和数据库 last_error，人看不出「其实是代理没开」。

这里只做两件事：

1. `explain(exc)` 把异常压成一句中文，带上能动手的地方（端口号 / 是否被墙）。
2. `node_down(exc)` 判断「重试没有意义」—— 代理端口拒绝连接时，等 1.5s 再试
   三次也不会变通，纯属把一轮轮询拖成几分钟。

`port_open()` 是同一思路的前置版：本机 127.0.0.1 的端口拿 socket 连一下就知道，
比发一个 HTTPS 请求（还可能 10 秒超时）快两个数量级。
"""
from __future__ import annotations

import socket
import time
import urllib.parse as _up
from typing import Any, Optional, Tuple

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0", "::ffff:127.0.0.1"}
DEFAULT_PORTS = {"http": 80, "https": 443, "socks5": 1080, "socks5h": 1080,
                 "socks": 1080, "socks4": 1080}


def split_proxy(proxy: str) -> Tuple[str, int, bool]:
    """`http://127.0.0.1:10801` → ('127.0.0.1', 10801, True)。

    没有 scheme 的（`127.0.0.1:10801`）也认，requests 同样能接受这种写法。
    """
    raw = (proxy or "").strip()
    if not raw:
        return "", 0, False
    if "://" not in raw:
        raw = "http://" + raw
    try:
        parsed = _up.urlparse(raw)
    except ValueError:
        return "", 0, False
    host = (parsed.hostname or "").strip()
    try:
        port = int(parsed.port or DEFAULT_PORTS.get((parsed.scheme or "").lower(), 0))
    except (TypeError, ValueError):
        port = 0
    return host, port, host.lower() in LOCAL_HOSTS


def tcp_open(host: str, port: int, timeout: float = 1.2) -> bool:
    """TCP 能不能连上。**只测三次握手**，不代表 TLS/协议可用。"""
    return tcp_probe(host, port, timeout) == "open"


def tcp_probe(host: str, port: int, timeout: float = 1.2) -> str:
    """同 tcp_open，但把失败原因分类出来。

    区分 `refused` 和 `timeout` 有实际用处：端口拒绝说明主机活着、只是没起服务
    （多半是本机代理没开），超时说明这台机器整个不可达（订阅里的机场挂了）。
    后者可以按**主机**做一次负缓存，省掉同一台机器上几十个端口的重复等待。
    """
    if not host or not port:
        return "error"
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return "open"
    except ConnectionRefusedError:
        return "refused"
    except (TimeoutError, socket.timeout):
        return "timeout"
    except OSError:
        return "error"


def port_open(proxy: str, timeout: float = 0.5) -> Optional[bool]:
    """本机代理端口在监听吗？

    返回值刻意是三态：
    - True：端口开着（不代表能转发，只代表进程在）
    - False：端口关着 —— 代理客户端没启动 / 改了端口 / 连的是别的机器
    - None：不是本机地址，探不了（远端主机好不好只能真发请求）
    """
    host, port, is_local = split_proxy(proxy)
    if not is_local or not port:
        return None
    return tcp_open(host, port, timeout)


def _chain_texts(exc: BaseException, depth: int = 4) -> str:
    """把异常自身与 cause/context 链上的文本拼一起，用来做关键字判定。"""
    parts = []
    cur: Optional[BaseException] = exc
    for _ in range(depth):
        if cur is None:
            break
        parts.append(type(cur).__name__)
        parts.append(str(cur))
        cur = cur.__cause__ or cur.__context__
    return " | ".join(parts).lower()


def classify(exc: BaseException) -> str:
    """错误种类。前端/日志只认这些短标签，不认原始栈。"""
    text = _chain_texts(exc)
    name = type(exc).__name__.lower()

    is_proxy = "proxyerror" in text or "unable to connect to proxy" in text
    refused = ("10061" in text or "积极拒绝" in text or "connection refused" in text
               or "actively refused" in text)
    reset = ("10054" in text or "connectionreset" in text or "connection aborted" in text
             or "远程主机强迫关闭" in text or "reset by peer" in text)
    dns = ("nameresolution" in text or "getaddrinfo" in text
           or "名称或服务" in text or "nodename nor servname" in text)
    tls = "ssl" in name or "sslerror" in text or "certificate" in text or "tls" in text
    timeout = "timeout" in type(exc).__name__.lower() or "timed out" in text

    if is_proxy:
        if refused:
            return "proxy_refused"
        if dns:
            return "proxy_dns"
        if timeout:
            return "proxy_timeout"
        return "proxy_error"
    if dns:
        return "dns"
    if reset:
        return "reset"
    if tls:
        return "tls"
    if timeout:
        return "timeout"
    return "other"


# 「等一会儿再试也没用」的种类：端口都没开，重试只是把一轮轮询拖长
_DEAD = {"proxy_refused", "proxy_dns", "proxy_error"}


def node_down(exc: BaseException) -> bool:
    """这次失败是不是「节点根本不在」——重试无意义。"""
    return classify(exc) in _DEAD


_KIND_TEXT = {
    "proxy_refused": "节点代理没在监听（{addr} 拒绝连接）",
    "proxy_dns": "节点代理地址解析不了（{addr}）",
    "proxy_timeout": "节点代理无响应（{addr} 转发超时）",
    "proxy_error": "节点代理不可用（{addr}）",
    "dns": "域名解析失败（DNS 不通）",
    "reset": "连接被重置（多半是直连被墙）",
    "tls": "TLS 握手失败（连接被干扰，或本机时间不对）",
    "timeout": "连接超时（对方无响应，或连接被阻断）",
    "other": "",
}


def explain(exc: BaseException, proxy: str = "", limit: int = 120) -> str:
    """一句话说清失败原因。proxy 只用于把地址写进提示里。"""
    kind = classify(exc)
    host, port, _ = split_proxy(proxy)
    addr = f"{host}:{port}" if host and port else (proxy or "代理")
    text = _KIND_TEXT.get(kind) or ""
    if not text:
        text = f"{type(exc).__name__}: {exc}"
    elif kind in ("proxy_refused", "proxy_dns", "proxy_timeout", "proxy_error"):
        text = text.format(addr=addr)
    if kind == "proxy_refused":
        text += "——代理客户端没启动，或端口改了"
    if len(text) > limit:
        text = text[:limit - 1] + "…"
    return text


# ---- 本机到底能不能出国 ------------------------------------------------ #
# 节点全超时时，用户第一反应是"我的订阅坏了"（因为订阅链接在浏览器里打得开）。
# 但订阅分发站和节点服务器通常是两台机器：前者在国内/CDN 上，后者在境外。
# 所以必须有一个**对照实验**把话说死：境外地址全超时 + 国内站点正常
# = 这台机器现在没有出墙手段，跟订阅好坏无关。
_ABROAD_PROBES = (("1.1.1.1", 443), ("8.8.8.8", 443))
_HOME_PROBES = (("www.baidu.com", 443),)
_EGRESS_TTL = 120.0
_egress_cache: dict = {}


def egress_check(timeout: float = 1.2, force: bool = False) -> dict:
    """本机有无出墙能力。返回 `{"state", "detail", "abroad", "home", "ts"}`。

    - `state="ok"`：境外 TCP 通（本机已在翻墙，或网络本身就能出去）
    - `state="blocked"`：境外全超时、国内站点正常 —— **没有出墙手段**
    - `state="offline"`：连国内站点都不通 —— 是网络断了，不是墙的问题
    - `state="unknown"`：没探出确定结论（比如 DNS 解析失败）

    结果缓存 2 分钟：X 一轮轮询里可能 5 个账号连续走到这个分支，
    每次都打 3 次外网等于自己给自己加延迟。
    """
    now = time.time()
    if not force and _egress_cache and now - _egress_cache["ts"] < _EGRESS_TTL:
        return dict(_egress_cache)

    def probe(pair):
        host, port = pair
        try:
            return tcp_probe(socket.gethostbyname(host), port, timeout)
        except Exception:  # noqa: BLE001
            return "dns"

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=4) as pool:
        abroad = list(pool.map(probe, _ABROAD_PROBES))
        home = list(pool.map(probe, _HOME_PROBES))

    abroad_ok = any(r == "open" for r in abroad)
    home_ok = any(r == "open" for r in home)
    if abroad_ok:
        state, detail = "ok", ""
    elif home_ok:
        state = "blocked"
        detail = ("本机直连境外不通（1.1.1.1:443 等全部超时，国内站点正常）——"
                  "这台机器上没有在跑的翻墙出口，跟订阅本身是否过期无关")
    elif all(r == "dns" for r in abroad + home):
        state, detail = "unknown", "域名解析失败，连不上网"
    else:
        state = "offline"
        detail = "连国内站点也不通：先把网络弄好，再谈代理"

    info = {"state": state, "detail": detail, "abroad": abroad, "home": home,
            "ts": now}
    _egress_cache.clear()
    _egress_cache.update(info)
    return dict(info)


def dead_proxy_message(proxy: str) -> str:
    """端口没开时直接给这句，不用发请求。"""
    host, port, _ = split_proxy(proxy)
    addr = f"{host}:{port}" if host and port else (proxy or "代理")
    return (f"节点代理没在监听（{addr} 拒绝连接）——代理客户端没启动，"
            f"或端口改了")


def describe(proxy: str) -> dict:
    """给诊断页/日志用的一小段画像。"""
    host, port, is_local = split_proxy(proxy)
    opened: Any = port_open(proxy)
    return {"proxy": proxy or "", "host": host, "port": port,
            "local": is_local, "listening": opened}
