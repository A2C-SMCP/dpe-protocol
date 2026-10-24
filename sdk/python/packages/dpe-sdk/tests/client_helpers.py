"""客户端级测试辅助（#13、#14 共用）：httpx transport 层的请求记录与进程内客户端工厂。

协议路径经真实参考服务端（``create_app`` 挂到 ``httpx.ASGITransport``，零网络）。记录器插在
httpx transport 这一层——比 ``http_helpers`` 的 sans-IO 包装器低一层——同步与异步客户端共用
同一形态，既能看头部也能看逐字节的请求体。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from dpe_sdk.credentials import CredentialProvider
from dpe_sdk.testing import Engine, create_app
from dpe_sdk.transport import AsyncDpeClient
from engine_helpers import make_engine

#: 带路径的 remote（HTTP 绑定 §1：remote 是任意 base URL，不使用域名根）
REMOTE = "http://dpe.test/r/1"
PREFIX = "/r/1"


class CountingProvider:
    """计数凭证 provider：记录取用次数，固定返回一个值（``None`` 表示不带凭证）。"""

    def __init__(self, value: str | None) -> None:
        self.value = value
        self.calls = 0

    def authorization(self) -> str | None:
        self.calls += 1
        return self.value


class ClientRecorder(httpx.AsyncBaseTransport):
    """记录每个请求（头与体）后转发给内层 transport。"""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner
        self.requests: list[httpx.Request] = []
        self.headers: list[dict[str, str]] = []
        self.bodies: list[bytes] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.headers.append(dict(request.headers))
        self.bodies.append(request.read())
        return await self._inner.handle_async_request(request)


@asynccontextmanager
async def in_process_client(
    engine: Engine | None = None,
    *,
    credential: CredentialProvider | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    **app_options: Any,
) -> AsyncIterator[tuple[AsyncDpeClient, ClientRecorder]]:
    """异步客户端对真实参考服务端（ASGI 应用，进程内）。

    给出 ``transport`` 时以它为准（调用方自行构造并包裹参考服务端，做故障注入）；
    此时 ``engine`` / ``app_options`` 不再使用。
    """
    inner: httpx.AsyncBaseTransport = transport or httpx.ASGITransport(
        app=create_app(engine or make_engine(), prefix=PREFIX, **app_options)
    )
    recorder = ClientRecorder(inner)
    client = AsyncDpeClient(REMOTE, credential=credential, transport=recorder)
    try:
        yield client, recorder
    finally:
        await client.aclose()
