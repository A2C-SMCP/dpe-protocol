"""HTTP 绑定测试的辅助：把参考服务端挂到 httpx 的 ASGITransport 上，经真实客户端路径驱动。

``ProtocolCore`` 产出的 ``Request`` 由 ``Remote.send`` 原样发出（target 已编码，拼在 remote 之后），
响应还原为 ``Response``——这就是 #13 传输适配要做的事，测试里只保留最小形态。``Recorder`` /
``Lossy`` 是插在 ``Remote.send`` 与操作之间的请求包装器（记录、模拟响应丢失）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx
from dpe_sdk.protocol import Operation, ProtocolCore, Request, Response, adrive, fetch_capabilities
from dpe_sdk.testing import Engine, create_app
from engine_helpers import make_engine

#: 带路径的 remote（HTTP 绑定 §1：remote 是任意 base URL，不使用域名根）
REMOTE = "http://dpe.test/r/1"
PREFIX = "/r/1"

#: 把 ``ProtocolCore`` 的请求描述交给下游（``Remote.send`` 或包装器）的函数形态
Send = Callable[[Request], Awaitable[Response]]


@dataclass
class Remote:
    engine: Engine
    client: httpx.AsyncClient

    async def send(self, request: Request) -> Response:
        reply = await self.client.request(
            request.method,
            f"{REMOTE}/{request.target}",
            headers=dict(request.headers),
            content=request.body_bytes(),
        )
        return Response(reply.status_code, dict(reply.headers), reply.content)

    async def run(self, op: Operation[Any]) -> Any:
        return await adrive(op, self.send)

    async def core(self, contract: str = "dpe1") -> ProtocolCore:
        return ProtocolCore(await self.run(fetch_capabilities()), contract)

    async def raw(
        self,
        method: str,
        target: str,
        *,
        contract: str | None = "dpe1",
        headers: Mapping[str, str] | None = None,
        content: bytes | None = None,
    ) -> httpx.Response:
        """直接发 HTTP 请求（暂存路径与边界用例）；``contract=None`` 不带契约头。"""
        merged = {} if contract is None else {"DPE-Hash-Contract": contract}
        merged.update(headers or {})
        return await self.client.request(
            method, f"{REMOTE}/{target}", headers=merged, content=content
        )


@asynccontextmanager
async def remote(engine: Engine | None = None, **app_options: Any) -> AsyncIterator[Remote]:
    engine = engine or make_engine()
    app = create_app(engine, prefix=PREFIX, **app_options)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport) as client:
        yield Remote(engine, client)


def problem(response: httpx.Response) -> dict[str, Any]:
    """断言是 DPE 错误响应（problem+json，``DPE-Error-Code`` 与体中 code 一致）并返回体。

    顺带钉住 §5 的 ``type`` / ``title`` 规则：``code`` 去掉 ``DPE_`` 前缀转 kebab-case 即
    ``type`` 的末段，``title`` 是它把连接线换成空格的小写短语——所有经这里断言的 code 都覆盖到。
    """
    assert response.headers["content-type"] == "application/problem+json"
    body: dict[str, Any] = response.json()
    assert response.headers["dpe-error-code"] == body["code"]
    assert body["status"] == response.status_code
    slug = body["code"].removeprefix("DPE_").lower().replace("_", "-")
    assert body["type"] == f"urn:dpe:error:{slug}"
    assert body["title"] == slug.replace("-", " ")
    return body


# ----------------------------------------------------------------------
# 请求包装器（插在操作与 ``Remote.send`` 之间）
# ----------------------------------------------------------------------


class Recorder:
    """记录发出的请求，其余原样转发。"""

    def __init__(self, send: Send) -> None:
        self._send = send
        self.sent: list[Request] = []

    async def __call__(self, request: Request) -> Response:
        self.sent.append(request)
        return await self._send(request)


class Lossy(Recorder):
    """命中 ``match`` 的第一次请求照常送达、响应随即丢弃（服务端已生效，客户端收不到结果）。"""

    def __init__(self, send: Send, match: Callable[[Request], bool]) -> None:
        super().__init__(send)
        self._match = match
        self.dropped = 0

    async def __call__(self, request: Request) -> Response:
        response = await super().__call__(request)
        if self.dropped == 0 and self._match(request):
            self.dropped += 1
            raise ConnectionError("响应丢失（引擎已处理）")
        return response


def chunk_spans(requests: list[Request]) -> list[tuple[int, int, int]]:
    """请求里的 ``Content-Range`` 解析为 (起点, 终点, 总量)。"""
    spans = []
    for request in requests:
        value = request.headers.get("Content-Range")
        if value is None:
            continue
        span, _, total = value.removeprefix("bytes ").partition("/")
        first, _, last = span.partition("-")
        spans.append((int(first), int(last), int(total)))
    return spans


def session_ids(requests: list[Request]) -> set[str]:
    """请求里出现过的暂存会话 id（``staging/{sid}/…`` 的第二段）。"""
    return {
        request.target.split("/")[1]
        for request in requests
        if request.target.startswith("staging/")
    }
