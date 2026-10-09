"""HTTP 绑定测试的辅助：把参考服务端挂到 httpx 的 ASGITransport 上，经真实客户端路径驱动。

``ProtocolCore`` 产出的 ``Request`` 由 ``Remote.send`` 原样发出（target 已编码，拼在 remote 之后），
响应还原为 ``Response``——这就是 #13 传输适配要做的事，测试里只保留最小形态。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
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


@dataclass
class Remote:
    engine: Engine
    client: httpx.AsyncClient

    async def send(self, request: Request) -> Response:
        reply = await self.client.request(
            request.method,
            f"{REMOTE}/{request.target}",
            headers=dict(request.headers),
            content=request.body,
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
    """断言是 DPE 错误响应（problem+json，``DPE-Error-Code`` 与体中 code 一致）并返回体。"""
    assert response.headers["content-type"] == "application/problem+json"
    body: dict[str, Any] = response.json()
    assert response.headers["dpe-error-code"] == body["code"]
    assert body["status"] == response.status_code
    return body
