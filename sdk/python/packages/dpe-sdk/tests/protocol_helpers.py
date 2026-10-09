"""sans-IO 协议核心测试的辅助：能力、按顺序回放的假服务端与响应构造（经 pytest pythonpath 导入）。

响应按 HTTP 绑定 §4、§5 的报文形状手工构造。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

from dpe_sdk.protocol import ProtocolCore, Request, Response, drive
from dpe_sdk.wire import Capabilities

H1 = "dpe1:" + "1" * 64
H2 = "dpe1:" + "2" * 64
H_DRILL = "dpe2:" + "3" * 64

CAPABILITIES: dict[str, Any] = {
    "protocol": "dpe/1",
    "hash_contracts": ["dpe1"],
    "limits": {
        "max_payload_bytes": 8388608,
        "page_max_bytes": 268435456,
        "staging_ttl_seconds": 86400,
        "blob_max_bytes": 104857600,
        "blob_chunk_bytes": 8388608,
        "batch_head_max": 2,
        "list_page_max": 1000,
    },
    "content_encodings": ["gzip"],
    "features": ["move"],
}


def caps(**overrides: Any) -> Capabilities:
    return Capabilities.model_validate({**CAPABILITIES, **overrides})


def core(contract: str = "dpe1", **overrides: Any) -> ProtocolCore:
    return ProtocolCore(caps(**overrides), contract)


def ok(body: Any = None, status: int = 200, headers: dict[str, str] | None = None) -> Response:
    hdrs = dict(headers or {})
    if body is None:
        return Response(status, hdrs)
    return Response(status, {"Content-Type": "application/json", **hdrs}, json.dumps(body).encode())


def problem(code: str, status: int, **extra: Any) -> Response:
    body = {"type": "urn:dpe:error:x", "title": "t", "status": status, "code": code, **extra}
    return Response(
        status,
        {"Content-Type": "application/problem+json", "DPE-Error-Code": code},
        json.dumps(body).encode(),
    )


class Server:
    """按顺序回放响应，并记录收到的请求。"""

    def __init__(self, *responses: Response) -> None:
        self.responses: Iterator[Response] = iter(responses)
        self.requests: list[Request] = []

    def __call__(self, request: Request) -> Response:
        self.requests.append(request)
        return next(self.responses)


def run(op: Any, *responses: Response) -> tuple[Any, Server]:
    server = Server(*responses)
    return drive(op, server), server
