"""Trojan 出站代理：在本进程内起一个本地 HTTP CONNECT 代理，并统计隧道流量。

跟 `_proxy/trojan_proxy.py` 的协议部分一样（trojan 握手是固定的那几个字节），
区别是这里跑成线程而不是独立脚本：

- 生命周期归后端管：`NodeManager` 起停，后端退出自动收摊，不用去任务管理器里找进程
- **流量在隧道层就数清楚了**：每个连接收发多少字节是 pump 循环里数出来的，
  比在应用层按 Content-Length 估准确（TLS 头、分块、重试都算进去了）
"""
from __future__ import annotations

import hashlib
import socket
import socketserver
import ssl
import struct
import threading

from typing import Optional


class Counter:
    """极简收发字节计数。conns 是当前活跃连接数（看节点忙不忙）。"""

    def __init__(self) -> None:
        self.up = 0
        self.down = 0
        self.conns = 0
        self.total_conns = 0
        self._lock = threading.Lock()

    def add(self, up: int = 0, down: int = 0) -> None:
        with self._lock:
            self.up += up
            self.down += down

    def drain(self) -> tuple:
        """取走累计值并清零（定期落库用，避免重复记账）。"""
        with self._lock:
            up, down = self.up, self.down
            self.up = self.down = 0
        return up, down


class TrojanProxy:
    """一个监听在 127.0.0.1:<listen_port> 的 CONNECT 代理。"""

    def __init__(self, server: str, port: int, password: str, sni: str,
                 listen_port: int, verify: bool = False, log=None):
        self.server = server
        self.port = int(port)
        self.sni = sni
        self.listen_port = int(listen_port)
        self.verify = verify
        self.log = log or (lambda msg: None)
        self.key = hashlib.sha224(password.encode()).hexdigest().encode()
        self.counter = Counter()
        self._server: Optional[socketserver.ThreadingTCPServer] = None
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------ #
    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.listen_port}"

    @property
    def alive(self) -> bool:
        return self._server is not None

    def _context(self) -> ssl.SSLContext:
        ctx = ssl.create_default_context()
        if not self.verify:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        return ctx

    def _tunnel(self, host: str, dport: int) -> ssl.SSLSocket:
        raw = socket.create_connection((self.server, self.port), timeout=20)
        sock = self._context().wrap_socket(raw, server_hostname=self.sni)
        addr = host.encode()
        req = (self.key + b"\r\n" + b"\x01" + b"\x03" + bytes([len(addr)]) + addr
               + struct.pack(">H", dport) + b"\r\n")
        sock.sendall(req)
        return sock

    def _pump(self, src, dst, up: bool) -> None:
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                dst.sendall(data)
                self.counter.add(up=len(data) if up else 0,
                                 down=0 if up else len(data))
        except OSError:
            pass
        finally:
            for s in (src, dst):
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def _make_handler(self):
        outer = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self) -> None:      # noqa: D102
                line = self.rfile.readline(65535).decode("latin-1").strip()
                if not line:
                    return
                parts = line.split()
                if len(parts) < 2 or parts[0].upper() != "CONNECT":
                    self.wfile.write(b"HTTP/1.1 501 Only CONNECT\r\n\r\n")
                    return
                host, _, port = parts[1].rpartition(":")
                port = int(port or 443)
                while True:
                    h = self.rfile.readline(65535)
                    if h in (b"\r\n", b"\n", b""):
                        break
                try:
                    upstream = outer._tunnel(host, port)
                except Exception as exc:  # noqa: BLE001
                    outer.log(f"隧道失败 {host}:{port} -> {type(exc).__name__}: {exc}")
                    self.wfile.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                    return
                outer.counter.conns += 1
                outer.counter.total_conns += 1
                self.wfile.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                self.wfile.flush()
                t = threading.Thread(target=outer._pump,
                                     args=(self.connection, upstream, True), daemon=True)
                t.start()
                outer._pump(upstream, self.connection, False)
                t.join(timeout=1)
                outer.counter.conns -= 1
                try:
                    upstream.close()
                except OSError:
                    pass

        return Handler

    # ------------------------------------------------------------------ #
    def start(self) -> bool:
        if self._server:
            return True
        try:
            class Server(socketserver.ThreadingTCPServer):
                allow_reuse_address = True
                daemon_threads = True

            self._server = Server(("127.0.0.1", self.listen_port), self._make_handler())
        except OSError as exc:
            self.log(f"监听 127.0.0.1:{self.listen_port} 失败：{exc}")
            return False
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        name=f"trojan-{self.listen_port}", daemon=True)
        self._thread.start()
        self.log(f"trojan 代理已启动 127.0.0.1:{self.listen_port} → {self.sni}@{self.server}:{self.port}")
        return True

    def stop(self) -> None:
        if not self._server:
            return
        try:
            self._server.shutdown()
            self._server.server_close()
        except Exception:  # noqa: BLE001
            pass
        self._server = None
        self._thread = None

    # 这里故意不提供「只测握手」的 latency()：trojan 握手没有响应帧，
    # 服务器不开 trojan、密码不对，TCP+TLS 一样连得上。
    # 判活一律走真实业务请求，见 NodeManager._probe_proxy。
