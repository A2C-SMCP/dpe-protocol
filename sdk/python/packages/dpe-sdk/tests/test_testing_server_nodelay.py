"""参考服务端接受连接时开启 ``TCP_NODELAY``（#241）。

uvicorn 把响应头与响应体分两次写；accepted socket 默认开 Nagle，Linux 的延迟确认会把第二段
（响应体）扣住约 40ms——带响应体的请求都会慢。这里用真实 TCP socket + 假 transport 断言
``connection_made`` 之后 ``TCP_NODELAY=1``，并断言传给 ``uvicorn.Config`` 的仍是 auto 选择的
协议子类（httptools / h11）。
"""

from __future__ import annotations

import asyncio
import errno
import socket
import statistics
import sys
import time
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock

import pytest
import uvicorn
from dpe_sdk.models import Document
from dpe_sdk.protocol import IfAbsent
from dpe_sdk.testing.__main__ import (
    _enable_tcp_nodelay,
    _http_protocol_class,
    _uvicorn_config,
)
from dpe_sdk.transport import DpeClient
from server_helpers import spawn_server
from uvicorn.protocols.http.auto import AutoHTTPProtocol
from uvicorn.server import ServerState


@pytest.fixture
def accepted_socket() -> Iterator[socket.socket]:
    """一条已建立的真实 TCP 连接里服务端一侧的 socket。"""
    listener = socket.create_server(("127.0.0.1", 0))
    client = socket.create_connection(listener.getsockname())
    server_side, _ = listener.accept()
    try:
        yield server_side
    finally:
        server_side.close()
        client.close()
        listener.close()


def _fake_transport(sock: socket.socket) -> Any:
    transport = MagicMock()
    transport.get_extra_info.side_effect = lambda name, default=None: (
        sock if name == "socket" else default
    )
    return transport


def test_connection_made_enables_tcp_nodelay(accepted_socket: socket.socket) -> None:
    async def app(scope: Any, receive: Any, send: Any) -> None:  # pragma: no cover - 不会被调用
        return None

    protocol_class = _http_protocol_class()
    config = uvicorn.Config(app, lifespan="off", log_level="warning")
    # 显式传 loop：整套用例跑下来时当前线程可能没有事件循环（其他用例已清理）
    loop = asyncio.new_event_loop()
    try:
        protocol = protocol_class(config, ServerState(), {}, loop)
        protocol.connection_made(_fake_transport(accepted_socket))
    finally:
        loop.close()
    # macOS 的 getsockopt 在开启时返回 4（Darwin 的编码），Linux 返回 1；只断言「非零」
    assert accepted_socket.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY) != 0


def test_nodelay_helper_ignores_non_tcp_sockets() -> None:
    """不是 TCP socket（Unix socketpair）时忽略 ENOPROTOOPT/EOPNOTSUPP，不把参考服务端起不来。"""
    left, right = socket.socketpair()
    try:
        _enable_tcp_nodelay(_fake_transport(left))
    finally:
        left.close()
        right.close()


def test_nodelay_helper_ignores_missing_socket() -> None:
    """transport 没有 socket（如某些测试替身）时直接返回，不调用 setsockopt。"""
    transport = MagicMock()
    transport.get_extra_info.return_value = None
    _enable_tcp_nodelay(transport)


def test_nodelay_helper_reraises_unexpected_oserror() -> None:
    """非 ENOPROTOOPT/EOPNOTSUPP 的 socket 错误必须原样抛出，不能静默吞掉。"""
    sock = MagicMock()
    sock.setsockopt.side_effect = OSError(errno.EBADF, "bad file descriptor")
    with pytest.raises(OSError):
        _enable_tcp_nodelay(_fake_transport(sock))


def test_protocol_class_preserves_auto_selection() -> None:
    """返回值来自 uvicorn 的 auto 别名。

    CI 的 plain uvicorn 没装 httptools，``AutoHTTPProtocol`` 就是 ``H11Protocol``，本用例只能
    保证返回值是该别名的子类；httptools 分支由 uvicorn 自己的 alias 选择保证（装
    ``uvicorn[standard]`` 的环境会覆盖到）。
    """
    protocol_class = _http_protocol_class()
    assert issubclass(protocol_class, AutoHTTPProtocol)


def test_uvicorn_config_carries_the_nodelay_protocol_class() -> None:
    """`main()` 用的配置必须把 nodelay 协议类接到 uvicorn（#241 的接线回归）。"""

    async def app(scope: Any, receive: Any, send: Any) -> None:  # pragma: no cover - 不会被调用
        return None

    config = _uvicorn_config(app, "warning")
    config.load()
    assert config.http_protocol_class is _http_protocol_class()


@pytest.mark.skipif(sys.platform != "linux", reason="延迟确认只在 Linux 上把第二段扣住约 40ms")
def test_small_response_commits_are_fast_on_linux() -> None:
    """带响应体的 commit 不应再被拖到约 40ms（#241 的回归检查；阈值留足余量，不测绝对性能）。"""

    def doc(i: int) -> Document:
        return Document.model_validate(
            {
                "file_type": "md",
                "pages": [{"elements": [{"category": "NarrativeText", "text": f"x{i}"}]}],
            }
        )

    with spawn_server() as info, DpeClient(info["url"]) as client:
        for i in range(5):
            client.commit(f"jira://probe/nodelay-warm-{i}", doc(i), IfAbsent())
        samples: list[float] = []
        for i in range(10):
            started = time.perf_counter()
            client.commit(f"jira://probe/nodelay-{i}", doc(i), IfAbsent())
            samples.append((time.perf_counter() - started) * 1000)
    median = statistics.median(samples)
    # 修复前约 43ms；修复后约 1ms。15ms 留出 15 倍余量，避免 CI 抖动误报。
    assert median < 15, f"commit 中位耗时 {median:.1f}ms"
