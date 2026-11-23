"""代理节点管理：配置、存活探测、延迟实测、流量回收。

两类节点：

- `external`：外部已经在跑的代理（比如常驻的 trojan 客户端 / Clash），
  后端只探测和使用，不负责它的生死 —— 硬去 kill 别人的进程属于越界。
- `trojan`：凭据填在配置里，后端自己在本进程起一个 CONNECT 代理
  （见 core/trojan.py），这才是"接管生命周期"。

节点状态（alive / latency）**必须实测**：页面上要是显示"在线"但其实连不上，
X 抓取会静默失败，只看日志还以为账号没更新。
"""
from __future__ import annotations

import threading
import time
import hashlib
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional

import requests

from . import neterr
from .trojan import TrojanProxy

PROBE_URL = "https://x.com/"
PROBE_TTL = 60.0          # 探测结果缓存 60 秒，别每次刷新页面都打一次外网
# 探不通的节点缓存时间要短得多：代理客户端可能下一秒就被打开，
# 缓存 60 秒会让"我明明开了代理，页面还显示离线"变成一个小型悬案。
PROBE_TTL_DOWN = 12.0
PROBE_TIMEOUT = 6         # 真发一次 HTTPS 的上限，比 requests 默认的没边好
UPSTREAM_TTL = 12.0       # trojan 上游服务器的 TCP 预检缓存
DEFAULT_NODES: List[Dict[str, Any]] = [
    {"id": "local-10801", "name": "本地代理 10801", "region": "—", "type": "external",
     "proxy": "http://127.0.0.1:10801", "enabled": True},
]


def parse_link(link: str) -> Optional[Dict[str, Any]]:
    """解析 `trojan://password@host:port?sni=xxx#备注`，返回节点配置。

    只认 trojan：别的协议（ss / vmess / hysteria）就算解析出来，后端也没有
    对应的出站实现，硬加进来只会得到一个"永远连不上"的节点。
    """
    link = (link or "").strip()
    if not link.lower().startswith("trojan://"):
        return None
    import urllib.parse as _up

    parsed = _up.urlparse(link)
    if not parsed.hostname or not parsed.username:
        return None
    query = {k: v[0] for k, v in _up.parse_qs(parsed.query).items()}
    name = _up.unquote(parsed.fragment or "") or parsed.hostname
    return {
        "id": f"t-{parsed.hostname}-{parsed.port or 443}",
        "name": name[:24],
        "region": (query.get("region") or "—")[:6],
        "type": "trojan",
        "server": parsed.hostname,
        "port": int(parsed.port or 443),
        "password": _up.unquote(parsed.username),
        "sni": query.get("sni") or query.get("peer") or parsed.hostname,
        # 监听口从 10811 起递增，跟外部代理（常见 10801）错开
        "listen_port": int(query.get("listen") or 10811),
        "enabled": True,
    }


def parse_subscription(text: str) -> List[Dict[str, Any]]:
    """订阅内容可能是 base64 也可能是明文，里面一行一个链接。"""
    import base64

    raw = (text or "").strip()
    if raw and "://" not in raw:
        try:
            padded = raw + "=" * (-len(raw) % 4)
            raw = base64.b64decode(padded).decode("utf-8", "ignore")
        except Exception:  # noqa: BLE001
            pass
    out: List[Dict[str, Any]] = []
    used_ports = set()
    for line in raw.splitlines():
        node = parse_link(line)
        if not node:
            continue
        # 同一份订阅里服务器可能同名同端口，多节点要错开监听口
        while node["listen_port"] in used_ports:
            node["listen_port"] += 1
        used_ports.add(node["listen_port"])
        node["id"] = f"{node['id']}-{node['listen_port']}"
        out.append(node)
    return out


def apply_host_overrides(nodes: List[Dict[str, Any]], overrides: Dict[str, str]
                         ) -> int:
    """只改托管节点的上游入口；保留订阅给出的端口、密码和 SNI。"""
    changed = 0
    for node in nodes:
        if node.get("type") != "trojan":
            continue
        source = str(node.get("server") or "")
        target = str(overrides.get(source) or "").strip()
        if not target or target == source:
            continue
        node["subscription_server"] = source
        node["server"] = target
        node["id"] = f"t-{target}-{node['port']}-{node['listen_port']}"
        changed += 1
    return changed


def subscription_stats(text: str) -> Dict[str, Any]:
    """订阅里各协议各有几条 —— 导入后必须把这个数字告诉用户。

    只支持 trojan，直接把 hysteria2 / anytls 静默丢掉的话，用户会以为
    "订阅里 74 个节点全导进来了"，然后对着一堆连不上的节点查半天。
    顺带把**服务器主机**也数出来：几十个节点共用一个入口时，
    "这台机器到入口不通"才是真因，换端口没用。
    """
    import base64
    import collections
    import urllib.parse as _up

    raw = (text or "").strip()
    if raw and "://" not in raw:
        try:
            padded = raw + "=" * (-len(raw) % 4)
            raw = base64.b64decode(padded).decode("utf-8", "ignore")
        except Exception:  # noqa: BLE001
            pass
    protos: Dict[str, int] = collections.Counter()
    hosts: Dict[str, int] = collections.Counter()
    for line in raw.splitlines():
        line = line.strip()
        if "://" not in line:
            continue
        protos[line.split("://", 1)[0].lower()] += 1
        try:
            host = _up.urlparse(line).hostname or ""
        except ValueError:
            host = ""
        if host:
            hosts[host] += 1
    return {"total": int(sum(protos.values())), "protocols": dict(protos),
            "hosts": dict(hosts)}


def describe_stats(stats: Dict[str, Any]) -> str:
    """把统计压成一句人话，进日志和接口返回。"""
    protos = stats.get("protocols") or {}
    supported = int(protos.get("trojan", 0))
    others = {k: v for k, v in protos.items() if k != "trojan"}
    parts = [f"{supported} 个 trojan 已导入"]
    if others:
        parts.append("、".join(f"{k} {v} 个暂不支持" for k, v in sorted(others.items())))
    hosts = stats.get("hosts") or {}
    text = f"订阅共 {stats.get('total', 0)} 条：" + "，".join(parts)
    if hosts:
        top = sorted(hosts.items(), key=lambda kv: -kv[1])[:2]
        text += "；入口 " + "、".join(f"{h}（{c} 个）" for h, c in top)
    return text


class NodeManager:
    def __init__(self, cfg: Dict[str, Any], log: Optional[Callable[[str, str], None]] = None,
                 store: Any = None):
        self.cfg = cfg
        self.log = log or (lambda level, msg: None)
        self.store = store
        self._probes: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._trojans: Dict[str, TrojanProxy] = {}
        self._upstream: Dict[str, Dict[str, Any]] = {}
        self._last_drain = 0.0

    # ---- 配置 ------------------------------------------------------------ #
    def nodes(self) -> List[Dict[str, Any]]:
        if self.cfg.get("x_proxy_mode") in ("local", "manual"):
            return []
        raw = self.cfg.get("x_nodes")
        if not isinstance(raw, list) or not raw:
            # 没配过就用默认的"本地代理"，保证 X 抓取开箱可用
            raw = DEFAULT_NODES
        out: List[Dict[str, Any]] = []
        for i, item in enumerate(raw):
            if not isinstance(item, dict):
                continue
            node = dict(item)
            node.setdefault("id", f"n{i + 1}")
            node.setdefault("name", node["id"])
            node.setdefault("region", "—")
            node.setdefault("type", "external")
            node.setdefault("enabled", True)
            out.append(node)
        return out

    def find(self, node_id: str) -> Optional[Dict[str, Any]]:
        for node in self.nodes():
            if node["id"] == node_id:
                return node
        return None

    def proxy_of(self, node: Dict[str, Any]) -> str:
        """这个节点实际可用的代理地址（trojan 节点返回自己监听的地址）。"""
        if (node.get("type") or "external") == "trojan":
            tp = self._trojans.get(node["id"])
            if tp is not None and tp.alive:
                return tp.url
            return ""
        return str(node.get("proxy") or "")

    # ---- 生命周期 -------------------------------------------------------- #
    def ensure_started(self) -> None:
        """旧订阅配置保留，但不再启动内置节点。"""
        if self.cfg.get("x_proxy_mode") in ("local", "manual"):
            return
        for node in self.nodes():
            if (node.get("type") or "external") != "trojan" or not node.get("enabled"):
                continue
            if self._trojans.get(node["id"]) is not None:
                continue
            missing = [k for k in ("server", "password", "sni") if not node.get(k)]
            if missing:
                self.log("WARNING", f"节点 {node['name']} 缺 {', '.join(missing)}，跳过启动")
                continue
            tp = TrojanProxy(
                server=str(node.get("server")),
                port=int(node.get("port") or 443),
                password=str(node.get("password")),
                sni=str(node.get("sni")),
                listen_port=int(node.get("listen_port") or 10811),
                log=lambda msg, n=node["name"]: self.log("INFO", f"[节点 {n}] {msg}"),
            )
            if tp.start():
                self._trojans[node["id"]] = tp

    def stop_all(self) -> None:
        for tp in self._trojans.values():
            tp.stop()
        self._trojans.clear()

    def stop(self, node_id: str) -> None:
        tp = self._trojans.pop(node_id, None)
        if tp:
            tp.stop()

    def reload(self, cfg: Dict[str, Any]) -> None:
        self.cfg = cfg
        # 同 id 的订阅节点也可能轮换密码或入口；旧代理不能继续持有旧凭据。
        active = {n["id"]: n for n in self.nodes()
                  if n.get("enabled") and (n.get("type") or "external") == "trojan"}
        for node_id, tp in list(self._trojans.items()):
            node = active.get(node_id)
            if not node or (
                tp.server != str(node.get("server") or "")
                or tp.port != int(node.get("port") or 443)
                or tp.sni != str(node.get("sni") or "")
                or tp.listen_port != int(node.get("listen_port") or 10811)
                or tp.key != hashlib.sha224(str(node.get("password") or "").encode()).hexdigest().encode()
            ):
                self.stop(node_id)
        self._probes.clear()
        self._upstream.clear()
        self.ensure_started()

    # ---- 探测 ------------------------------------------------------------ #
    def probe(self, node: Dict[str, Any], force: bool = False) -> Dict[str, Any]:
        cached = self._probes.get(node["id"])
        ttl = PROBE_TTL if (cached or {}).get("alive") else PROBE_TTL_DOWN
        if cached and not force and time.time() - cached["ts"] < ttl:
            return cached
        result = self._probe_now(node)
        result["ts"] = time.time()
        with self._lock:
            self._probes[node["id"]] = result
        return result

    def _upstream_ok(self, node: Dict[str, Any]) -> Dict[str, Any]:
        """trojan 节点的上游服务器还活着吗。

        这一层是必须的：`TrojanProxy` 连上游用的是 20 秒超时的
        `socket.create_connection`，59 个订阅节点串行探一遍能把整个
        `/api/nodes` 拖成几分钟（实测 40 秒还没返回）。先拿 1.5 秒的 TCP
        预检挡一道，连不上就直接判死，不发那个注定超时的握手。

        只按 host:port 缓存。一个端口超时不能推断同一入口的其他端口都坏：
        这份订阅实测同一主机的 43 个端口可用、另 15 个超时。
        """
        host = str(node.get("server") or "")
        port = int(node.get("port") or 0)
        key = f"{host}:{port}"
        now = time.time()
        cached = self._upstream.get(key)
        if cached and now - cached["ts"] < UPSTREAM_TTL:
            return cached
        reason = neterr.tcp_probe(host, port, 1.5)
        info = {"ok": reason == "open", "ts": now, "addr": key, "reason": reason}
        self._upstream[key] = info
        return info

    def probe_many(self, nodes: List[Dict[str, Any]], force: bool = False
                   ) -> List[Dict[str, Any]]:
        """并发探测一批节点。选路和节点页都要用，串行会慢到没法用。"""
        if len(nodes) <= 1:
            return [self.probe(node, force=force) for node in nodes]
        with ThreadPoolExecutor(max_workers=min(12, len(nodes))) as pool:
            return list(pool.map(lambda node: self.probe(node, force=force), nodes))

    def _probe_now(self, node: Dict[str, Any]) -> Dict[str, Any]:
        if not node.get("enabled"):
            return {"alive": False, "latency": -1, "note": "已停用",
                    "kind": "disabled", "conns": 0}
        if (node.get("type") or "external") == "trojan":
            tp = self._trojans.get(node["id"])
            if tp is None or not tp.alive:
                return {"alive": False, "latency": -1, "note": "未启动",
                        "kind": "stopped", "conns": 0}
            up = self._upstream_ok(node)
            if not up["ok"]:
                reason = up.get("reason") or ""
                if reason == "refused":
                    note = f"上游端口拒绝连接（{up['addr']}）"
                elif reason == "timeout":
                    note = f"上游服务器连不通（{up['addr']} 超时）"
                else:
                    note = f"上游服务器不可达（{up['addr']}）"
                return {"alive": False, "latency": -1, "kind": "upstream_down",
                        "note": note, "conns": 0}
            # 托管节点也必须走真实请求才能判活：trojan 握手没有响应帧，
            # 哪怕密码是错的、服务器根本没开 trojan，TCP+TLS 也能连上 ——
            # 只测握手会给出一堆"在线"的假节点。
            result = self._probe_proxy(tp.url)
            result["conns"] = tp.counter.conns
            return result
        proxy = self.proxy_of(node)
        if not proxy:
            return {"alive": False, "latency": -1, "note": "未配置代理地址",
                    "kind": "unset", "conns": 0}
        # 本机代理先看端口：没在监听就没必要发那个注定失败的 HTTPS 请求。
        # 这条同时给出最有用的一句话——"代理客户端没开"，而不是一串 ProxyError。
        if neterr.port_open(proxy) is False:
            host, port, _ = neterr.split_proxy(proxy)
            return {"alive": False, "latency": -1, "conns": 0,
                    "kind": "refused",
                    "note": f"端口未监听（{host}:{port}），代理客户端没启动或端口改了"}
        return self._probe_proxy(proxy)

    @staticmethod
    def system_proxy() -> str:
        """读取 Windows 当前手动 HTTP 代理；只接受本机端口。"""
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Internet Settings")
            if not winreg.QueryValueEx(key, "ProxyEnable")[0]:
                return ""
            raw = str(winreg.QueryValueEx(key, "ProxyServer")[0])
            entries = dict(part.split("=", 1) for part in raw.split(";") if "=" in part)
            proxy = entries.get("https") or entries.get("http") or raw
            if "=" in proxy:
                return ""
            if "://" not in proxy:
                proxy = "http://" + proxy
            _, port, local = neterr.split_proxy(proxy)
            return proxy if local and port and neterr.port_open(proxy) else ""
        except (OSError, ValueError):
            return ""

    def probe_proxy(self, proxy: str) -> Dict[str, Any]:
        return self._probe_proxy(proxy)

    def _probe_proxy(self, proxy: str) -> Dict[str, Any]:
        """通过某代理真发一次请求，拿到响应头才算活。"""
        started = time.time()
        try:
            resp = requests.get(PROBE_URL, proxies={"http": proxy, "https": proxy},
                                timeout=PROBE_TIMEOUT, stream=True,
                                headers={"user-agent": "Mozilla/5.0"})
            ms = int((time.time() - started) * 1000)
            ok = resp.status_code < 500
            resp.close()
            return {"alive": ok, "latency": ms, "conns": 0, "kind": "" if ok else "http",
                    "note": "" if ok else f"HTTP {resp.status_code}"}
        except Exception as exc:  # noqa: BLE001
            return {"alive": False, "latency": -1, "conns": 0,
                    "kind": neterr.classify(exc),
                    "note": neterr.explain(exc, proxy, limit=90)}

    # ---- 选节点 ---------------------------------------------------------- #
    def bound_counts(self) -> Dict[str, int]:
        """每个节点上粘了多少个 X 账号。分配新账号时用它做负载均衡。"""
        out: Dict[str, int] = {}
        for acc in (self.cfg.get("accounts") or []):
            if not isinstance(acc, dict):
                continue
            if (acc.get("platform") or "weibo") != "x":
                continue
            nid = str(acc.get("node_id") or "")
            if nid:
                out[nid] = out.get(nid, 0) + 1
        return out

    def pick(self, exclude: Optional[set] = None) -> Optional[Dict[str, Any]]:
        """给账号分一个新节点：先看绑定人数（少的优先），再比延迟。

        只按延迟挑的话所有账号会挤在最快那个节点上，一挂全挂；
        只按人数挑又会让刚启动的账号被分到慢节点。两个一起排。
        """
        exclude = exclude or set()
        counts = self.bound_counts()
        candidates = [n for n in self.nodes()
                      if n.get("enabled") and n["id"] not in exclude]
        best: Optional[Dict[str, Any]] = None
        best_key = (1 << 30, 1 << 30)
        for node, info in zip(candidates, self.probe_many(candidates)):
            if not info.get("alive"):
                continue
            ms = int(info.get("latency") or -1)
            key = (counts.get(node["id"], 0), ms if ms >= 0 else 9999)
            if key < best_key:
                best, best_key = node, key
        return best

    def active(self) -> Optional[Dict[str, Any]]:
        """挑一个可用的节点：启用 + 探测存活，延迟低的优先。"""
        candidates = [n for n in self.nodes() if n.get("enabled")]
        best: Optional[Dict[str, Any]] = None
        best_ms = 1 << 30
        for node, info in zip(candidates, self.probe_many(candidates)):
            if not info.get("alive"):
                continue
            ms = int(info.get("latency") or 0)
            if ms < 0:
                ms = 9999
            if ms < best_ms:
                best, best_ms = node, ms
        return best

    def active_proxy(self) -> tuple:
        node = self.active()
        if node is None:
            return "", ""
        return node["id"], self.proxy_of(node)

    # ---- 流量 ------------------------------------------------------------ #
    # 隧道层字节只作为节点诊断数据（tunnelUp/tunnelDown）展示，**不进 traffic 表**：
    # 走托管节点的请求在应用层也已经记过一次，两边都算就是重复计费。
    # 外部节点的流量更不归我们数 —— 浏览器和其他软件共用一个代理，数出来也是别人的量。

    # ---- 对外状态 -------------------------------------------------------- #
    def status(self, force: bool = False) -> List[Dict[str, Any]]:
        """全量节点画像。**并发探测**：67 个节点串行探一遍要几分钟，
        页面等不起（实测 force=1 时请求 40 秒都没返回）。"""
        counts = self.bound_counts()
        nodes = self.nodes()
        infos = self.probe_many(nodes, force=force)
        out = []
        for node, info in zip(nodes, infos):
            tp = self._trojans.get(node["id"])
            out.append({
                "id": node["id"],
                "name": node["name"],
                "region": node.get("region") or "—",
                "type": node.get("type") or "external",
                "enabled": bool(node.get("enabled")),
                "proxy": self.proxy_of(node),
                "alive": bool(info.get("alive")),
                "latency": int(info.get("latency") or -1),
                "note": info.get("note") or "",
                "kind": info.get("kind") or "",
                "conns": int(info.get("conns") or 0),
                "boundAccounts": counts.get(node["id"], 0),
                "managed": tp is not None,
                "tunnelUp": tp.counter.up if tp else 0,
                "tunnelDown": tp.counter.down if tp else 0,
                "totalConns": tp.counter.total_conns if tp else 0,
            })
        return out

    def summary(self) -> Dict[str, Any]:
        """**只读缓存，不发请求**。给 /api/status 用 —— 那个接口每几秒被轮一次，
        不能因为它去探 67 个节点。还没探过的节点算 unknown，不能算死。"""
        nodes = self.nodes()
        alive = 0
        known = 0
        note = ""
        kind = ""
        for node in nodes:
            info = self._probes.get(node["id"])
            if not info or not node.get("enabled"):
                continue
            known += 1
            if info.get("alive"):
                alive += 1
            elif not note:
                note = str(info.get("note") or "")
                kind = str(info.get("kind") or "")
        enabled = [n for n in nodes if n.get("enabled")]
        if alive:
            note, kind = "", ""
        return {"total": len(nodes), "enabled": len(enabled), "alive": alive,
                "known": known, "note": note, "kind": kind}

    def active_node_note(self) -> str:
        """给「当前没有可用节点」找一个能看的原因。"""
        for node in self.nodes():
            if not node.get("enabled"):
                continue
            info = self._probes.get(node["id"]) or {}
            if info.get("note"):
                return str(info["note"])
        return ""
