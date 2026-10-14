"""测试用的本地 HTTP 代理（#132、#194）：两个包的代理用例共用（本目录在 pytest 的 ``pythonpath``）。

三种模式：``forward`` 把明文请求转发给上游（绝对形式的请求目标改写为原始形式）；``bad_gateway``
对一切请求回 502；任何模式下 ``CONNECT`` 一律以 403 拒绝。``seen`` 按到达顺序记录请求行
（``方法 目标``），用来断言请求是否经过了代理。
"""

from __future__ import annotations

import socket
import socketserver
import threading
from collections.abc import Iterator
from urllib.parse import urlsplit

__all__ = ["ProxyServer", "start_proxy"]


class _Handler(socketserver.StreamRequestHandler):
    server: ProxyServer

    def handle(self) -> None:
        while True:
            line = self.rfile.readline()
            if not line:
                return
            headers: list[bytes] = []
            while (header := self.rfile.readline()) not in (b"\r\n", b"\n", b""):
                headers.append(header)
            method, target, _ = line.decode("latin-1").split(" ", 2)
            self.server.seen.append(f"{method} {target}")
            if method == "CONNECT":  # 隧道一律拒绝：请求随后才会经隧道发出
                self.wfile.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                return
            length = 0
            for header in headers:
                name, _, value = header.decode("latin-1").partition(":")
                if name.strip().lower() == "content-length":
                    length = int(value)
            body = self.rfile.read(length)
            if self.server.mode == "bad_gateway":
                self.wfile.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
                continue
            self._forward(method, target, headers, body)
            return

    def _forward(self, method: str, target: str, headers: list[bytes], body: bytes) -> None:
        """明文转发：绝对形式的请求目标改写为原始形式，带 ``Connection: close`` 交给上游。"""
        parts = urlsplit(target)
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        kept = [
            h
            for h in headers
            if h.split(b":", 1)[0].strip().lower() not in (b"connection", b"proxy-connection")
        ]
        request = (
            f"{method} {path} HTTP/1.1\r\n".encode()
            + b"".join(kept)
            + b"Connection: close\r\n\r\n"
            + body
        )
        assert parts.hostname is not None and parts.port is not None
        with socket.create_connection((parts.hostname, parts.port), timeout=10) as upstream:
            upstream.sendall(request)
            while chunk := upstream.recv(65536):
                self.wfile.write(chunk)


class ProxyServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, mode: str) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.mode = mode
        self.seen: list[str] = []

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"


def start_proxy(mode: str) -> Iterator[ProxyServer]:
    server = ProxyServer(mode)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
