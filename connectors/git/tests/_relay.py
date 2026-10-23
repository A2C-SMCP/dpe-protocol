"""两个测试代理共用的转发实现（#68）：hop-by-hop 过滤、上游连接、响应回写与脱敏。

``RecordingProxy``（记录请求、扣住写响应）与 ``TLSProxy``（TLS 终结、记录握手次数）只在
「怎么连上游」与「转发之后做什么」上不同；请求读取、gzip 解压、响应头过滤与回写只有这一份。
"""

from __future__ import annotations

import contextlib
import gzip
from collections.abc import Mapping, Sequence
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler
from typing import Any, ClassVar, Protocol
from urllib.parse import unquote

#: 逐跳头（RFC 9110 §7.6.1）与 Host：上游连接按目标地址自行生成 Host
HOP_BY_HOP = frozenset({"connection", "keep-alive", "transfer-encoding", "host"})
#: 脱敏时替换凭证值的固定标记
REDACTED = "[REDACTED]"


def redact(text: str, secrets: Sequence[str]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, REDACTED)
    return text


def redact_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """名字小写、``authorization`` 的值替换为 ``REDACTED``（用于记录，不影响转发）。"""
    return {
        key.lower(): (REDACTED if key.lower() == "authorization" else value)
        for key, value in headers.items()
    }


def relative_path(path: str, base: str) -> str:
    """去掉 remote 的 URL 路径前缀后再解码（base 是原始、可能 pct-encoded 的路径）。"""
    if path.startswith(base + "/"):
        path = path[len(base) + 1 :]
    return unquote(path)


class RelayTarget(Protocol):
    """代理侧的实现：上游连接、转发完成后的处理（可阻塞）、错误记录。"""

    def connect(self) -> HTTPConnection: ...

    def on_relayed(
        self,
        method: str,
        path: str,
        body: bytes,
        headers: Mapping[str, str],
        status: int,
    ) -> None: ...

    def on_error(self, method: str, path: str, exc: BaseException) -> None: ...


class RelayingHandler(BaseHTTPRequestHandler):
    """把请求原样转发到 ``relay_target``，读完上游响应后交给它处理再回写客户端。"""

    protocol_version = "HTTP/1.1"
    relay_target: ClassVar[RelayTarget]

    def _forward(self) -> None:
        try:
            self._relay()
        except Exception as exc:  # 代理自身的故障：记下原因、回 502，由用例断言
            self.relay_target.on_error(self.command, self.path, exc)
            with contextlib.suppress(OSError):
                self.send_error(502)

    def _relay(self) -> None:
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            raise AssertionError("测试代理不支持 chunked 请求体")
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_BY_HOP}
        upstream = self.relay_target.connect()
        try:
            upstream.request(self.command, self.path, body=body or None, headers=headers)
            reply = upstream.getresponse()
            data = reply.read()
        finally:
            upstream.close()
        raw = body
        if raw and (self.headers.get("Content-Encoding") or "").lower() == "gzip":
            raw = gzip.decompress(raw)
        request_headers = {key: value for key, value in self.headers.items()}
        self.relay_target.on_relayed(self.command, self.path, raw, request_headers, reply.status)
        self._respond(reply.status, reply.getheaders(), data)

    def _respond(self, status: int, headers: list[tuple[str, str]], data: bytes) -> None:
        try:
            self.send_response(status)
            skip = HOP_BY_HOP | {"date", "server"}
            if self.command != "HEAD":  # HEAD 原样保留上游的 Content-Length
                skip = skip | {"content-length"}
            for key, value in headers:
                if key.lower() not in skip:
                    self.send_header(key, value)
            if self.command != "HEAD":
                self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)
        except OSError:
            pass  # 运行器已被终止：响应无人接收

    do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = _forward

    def log_message(self, format: str, *args: Any) -> None:
        pass


def handler_for(target: RelayTarget) -> type[RelayingHandler]:
    """为一个代理实例生成绑定了它的 Handler 类。"""

    class Handler(RelayingHandler):
        relay_target = target

    return Handler


__all__ = [
    "HOP_BY_HOP",
    "REDACTED",
    "RelayTarget",
    "RelayingHandler",
    "handler_for",
    "redact",
    "redact_headers",
    "relative_path",
]
