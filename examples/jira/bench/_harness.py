# 临时复制（#237）：与 PR「测试设施提升到 dpe_sdk.testing.harness」中的 harness.py 逐字相同。
# 那个 PR 按发版窗口要等 0.4.0 打标签后才合并；合并后删除本文件，bench 改为
# ``from dpe_sdk.testing.harness import ...``。
"""端到端测试设施（#215、#237）：以独立进程起参考服务端，加一层记录请求的透明代理。

只用标准库、不依赖 pytest，供 connector、示例插件、跑分器自测与基准测试共用（此前各自复制一份）。
它们是测试设施，**不进入 DPE 协议面**。

- ``reference_server``：起 ``python -m dpe_sdk.testing``（需要 extra ``server``），读它公布的
  ``{"url", "hooks"}`` 行；额外参数原样透传（``--prefix``、限额等）；
- ``RecordingProxy``：进程内的透明转发代理，记录每个请求（请求体已解压、带起止时刻），并能在写请求
  被服务端执行之后**扣住响应**——测试据此确定性地制造「写入已生效、确认前崩溃」：等代理报告已
  扣住，再终止运行器，不依赖 sleep 时序。
"""

from __future__ import annotations

import contextlib
import gzip
import json
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import TracebackType
from typing import Any
from urllib.parse import unquote, urlsplit

__all__ = ["RecordingProxy", "ReferenceServer", "Seen", "reference_server"]

#: 转发与扣住响应的超时上限：防止测试卡死
DEFAULT_TIMEOUT = 120.0


def _no_log() -> str:
    return ""


@dataclass(frozen=True)
class ReferenceServer:
    """参考服务端公布的地址：remote URL，以及开启钩子时钩子的根地址。"""

    url: str
    hooks: str | None = None
    #: 读子进程 stderr 至当下（启动告警等）；只在 ``reference_server`` 的 with 块内有效
    _stderr: Callable[[], str] = field(default=_no_log, compare=False, repr=False)

    def stderr(self) -> str:
        return self._stderr()


@contextmanager
def reference_server(*args: str, startup_timeout: float = 20.0) -> Iterator[ReferenceServer]:
    """启动参考服务端子进程，产出它公布的地址；退出时终止子进程。

    ``args`` 透传给 ``python -m dpe_sdk.testing``（端口固定由系统分配）。没有按时公布地址即抛
    ``RuntimeError``，消息里带服务端的 stderr。
    """
    # 追加模式：子进程的每次写入都落在文件末尾，与 ``stderr()`` 读取时移动的共享偏移无关
    with tempfile.TemporaryFile(mode="a+") as log:
        proc = subprocess.Popen(
            [sys.executable, "-m", "dpe_sdk.testing", "--port", "0", *args],
            stdout=subprocess.PIPE,
            stderr=log,
            text=True,
        )
        try:
            stdout = proc.stdout
            assert stdout is not None
            lines: list[str] = []
            reader = threading.Thread(
                target=lambda: lines.append(stdout.readline() or ""), daemon=True
            )
            reader.start()
            reader.join(timeout=startup_timeout)
            if not lines or not lines[0].strip():
                log.seek(0)
                raise RuntimeError(f"参考服务端未公布地址：{log.read()}")
            try:
                info = json.loads(lines[0])
                url = str(info["url"])
            except (ValueError, KeyError, TypeError):
                log.seek(0)
                raise RuntimeError(
                    f"参考服务端的公告行无效：{lines[0]!r}；stderr：{log.read()}"
                ) from None

            def stderr() -> str:
                log.seek(0)
                return log.read()

            hooks = info.get("hooks")
            yield ReferenceServer(url, hooks if isinstance(hooks, str) else None, stderr)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)


@dataclass(frozen=True)
class Seen:
    """记录代理看到的一个请求：方法、remote 之后的路径（含查询串，已解码）、解压后的请求体与
    状态码。"""

    method: str
    path: str
    body: bytes
    status: int
    #: 请求头（名字小写）：断言 CAS 前置条件（If-Match / If-None-Match）
    headers: dict[str, str] = field(default_factory=dict)
    #: 代理收到请求、拿到上游响应的时刻（``time.monotonic()``）：统计耗时与变更可见延迟
    started: float = 0.0
    finished: float = 0.0

    @property
    def target(self) -> str:
        """路径去掉查询串：``documents``、``negotiate``、``heads``、``staging/…`` 等。"""
        return self.path.split("?", 1)[0]

    def json(self) -> Any:
        return json.loads(self.body) if self.body else None

    @property
    def is_write(self) -> bool:
        return (self.method in ("PUT", "DELETE") and self.target == "documents") or (
            self.method == "POST" and self.target == "move"
        )


_HOP = {"connection", "keep-alive", "transfer-encoding", "host"}
#: 上游连接空闲超过这么久就换新的再发请求：参考服务端在空闲 ``KEEP_ALIVE_SECONDS``（5 秒）后关闭
#: keep-alive 连接，赶在它之前换掉，就不会出现「请求已发出、连接被对端关闭」——那时写请求是否已
#: 生效不确定，代理不能替调用方重发
_UPSTREAM_IDLE = 2.0


class RecordingProxy:
    """透明转发到 ``upstream``，记录全部请求；``hold_next_write`` 后扣住下一个写请求的响应。

    可作上下文管理器使用：退出时 ``close``（释放被扣住的响应、停止监听）。

    同一条客户端连接上的请求复用同一条上游连接（空闲超过 ``_UPSTREAM_IDLE`` 先换新）。前提：上游
    关闭空闲连接的时间大于 ``_UPSTREAM_IDLE``——参考服务端满足；接别的上游时须同样满足。
    """

    def __init__(self, upstream: str, *, timeout: float = DEFAULT_TIMEOUT) -> None:
        parts = urlsplit(upstream)
        if parts.hostname is None or parts.port is None:
            raise ValueError("upstream 必须带主机与端口")
        self._host, self._port, self._base = parts.hostname, parts.port, parts.path
        self._timeout = timeout
        self.seen: list[Seen] = []
        #: 代理自身出错的原因（如 chunked 请求体、连不上上游）：调用方在一轮结束时断言为空
        self.errors: list[str] = []
        self._lock = threading.Lock()
        self._hold = False
        #: 被扣住的那个写请求（不依赖记录顺序：并发请求按完成顺序追加）
        self.held_request: Seen | None = None
        self.held = threading.Event()
        self._release = threading.Event()
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            #: 本条客户端连接对应的上游连接（keep-alive 复用）：每个请求新建一条会让大量连接
            #: 堆在 TIME_WAIT，长跑时耗尽本机临时端口
            _upstream: HTTPConnection | None = None
            #: 上游连接上一次完成请求的时刻（只在 ``_upstream`` 非 None 时有意义）
            _idle_since = 0.0

            def _connection(self) -> HTTPConnection:
                upstream = self._upstream
                if upstream is not None and time.monotonic() - self._idle_since > _UPSTREAM_IDLE:
                    upstream.close()
                    self._upstream = None
                if self._upstream is None:
                    self._upstream = HTTPConnection(
                        proxy._host, proxy._port, timeout=proxy._timeout
                    )
                return self._upstream

            def finish(self) -> None:
                if self._upstream is not None:
                    self._upstream.close()
                super().finish()

            def _forward(self) -> None:
                try:
                    self._relay()
                except Exception as exc:  # 代理自身的故障：记下原因、回 502，由调用方断言
                    proxy.errors.append(f"{self.command} {self.path}: {exc!r}")
                    with contextlib.suppress(OSError):
                        self.send_error(502)

            def _relay(self) -> None:
                started = time.monotonic()
                if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
                    raise RuntimeError("记录代理不支持 chunked 请求体")
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                headers = {k: v for k, v in self.headers.items() if k.lower() not in _HOP}
                upstream = self._connection()
                try:
                    upstream.request(self.command, self.path, body=body or None, headers=headers)
                    reply = upstream.getresponse()
                    data = reply.read()
                except BaseException:
                    upstream.close()  # 状态不明的连接不再复用
                    self._upstream = None
                    raise
                if reply.will_close:
                    upstream.close()
                    self._upstream = None
                self._idle_since = time.monotonic()
                finished = time.monotonic()
                raw = body
                if raw and (self.headers.get("Content-Encoding") or "").lower() == "gzip":
                    raw = gzip.decompress(raw)
                path = unquote(self.path)
                if path.startswith(proxy._base + "/"):
                    path = path[len(proxy._base) + 1 :]
                lowered = {key.lower(): value for key, value in self.headers.items()}
                seen = Seen(self.command, path, raw, reply.status, lowered, started, finished)
                with proxy._lock:
                    proxy.seen.append(seen)
                    hold = proxy._hold and seen.is_write
                    if hold:
                        proxy._hold = False
                        proxy.held_request = seen
                if hold:
                    # 写入已在服务端生效：通知调用方，等它终止运行器后再（向已断开的连接）回响应
                    proxy.held.set()
                    proxy._release.wait(proxy._timeout)
                try:
                    self.send_response(reply.status)
                    skip = _HOP | {"date", "server"}
                    if self.command != "HEAD":  # HEAD 原样保留上游的 Content-Length
                        skip = skip | {"content-length"}
                    for key, value in reply.getheaders():
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

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        """经代理访问 remote 的地址（保留上游的路径前缀）。"""
        return f"http://127.0.0.1:{self._server.server_address[1]}{self._base}"

    def mark(self) -> int:
        """当前已记录的请求数：配合 ``since`` 取一段时间内的请求。"""
        with self._lock:
            return len(self.seen)

    def since(self, mark: int) -> list[Seen]:
        with self._lock:
            return list(self.seen[mark:])

    def hold_next_write(self) -> None:
        """扣住下一个写请求的响应：写入在服务端生效后置位 ``held``，直到 ``release``。"""
        self.held_request = None
        self.held.clear()
        self._release.clear()
        with self._lock:
            self._hold = True

    def release(self) -> None:
        with self._lock:
            self._hold = False
        self._release.set()

    def close(self) -> None:
        self.release()
        self._server.shutdown()
        self._server.server_close()

    def __enter__(self) -> RecordingProxy:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
