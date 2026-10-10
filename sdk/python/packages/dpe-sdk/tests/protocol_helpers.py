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
SESSION_EXPIRES = "2026-10-01T00:00:00Z"

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
    """能力；``limits={...}`` 合并进 ``limits``，其余键覆盖顶层。"""
    limits = {**CAPABILITIES["limits"], **overrides.pop("limits", {})}
    return Capabilities.model_validate({**CAPABILITIES, **overrides, "limits": limits})


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


class Script:
    """按脚本逐次给出响应或抛出异常（模拟响应丢失），并记录收到的请求。"""

    def __init__(self, *steps: Response | Exception) -> None:
        self.steps: Iterator[Response | Exception] = iter(steps)
        self.requests: list[Request] = []

    def __call__(self, request: Request) -> Response:
        self.requests.append(request)
        step = next(self.steps)
        if isinstance(step, Exception):
            raise step
        return step


# ---------------------------------------------------------------------- 暂存路径（#49）


def session_body(sid: str = "st-1", expires_at: str = SESSION_EXPIRES) -> dict[str, str]:
    return {"id": sid, "expires_at": expires_at}


def negotiated(
    sid: str = "st-1",
    *,
    missing_pages: list[str] | None = None,
    missing_content_hashes: list[str] | None = None,
    expires_at: str = SESSION_EXPIRES,
) -> Response:
    """``POST negotiate`` 的成功响应（HTTP 绑定 §4.5）。"""
    return ok(
        {
            "missing_pages": missing_pages or [],
            "missing_content_hashes": missing_content_hashes or [],
            "staging_session": session_body(sid, expires_at),
        }
    )


def uploaded(
    missing: Any = None,
    *,
    status: int = 201,
    offset: int | None = None,
    expires_at: str | None = SESSION_EXPIRES,
) -> Response:
    """upload 的成功响应：201 新写入 / 200 重复（带缺失清单）/ 202 中间块（无体 + 偏移）。"""
    headers: dict[str, str] = {}
    if expires_at is not None:
        headers["DPE-Session-Expires"] = expires_at
    if offset is not None:
        headers["DPE-Upload-Offset"] = str(offset)
    if status == 202:
        return Response(202, headers)
    return ok({} if missing is None else missing, status, headers)


def resume(offset: int) -> Response:
    """断点查询（HEAD，HTTP 绑定 §4.7）的 200：本会话已收字节数与会话过期时间。"""
    return Response(200, {"DPE-Upload-Offset": str(offset), "DPE-Session-Expires": SESSION_EXPIRES})


def gateway(status: int = 504) -> Response:
    """无 DPE 错误码的网关响应（502/504）：写操作按「结果未知」处理。"""
    return Response(status, {"Content-Type": "text/html"}, b"<h1>gateway</h1>")


def offset_problem(offset: int) -> Response:
    """分块偏移不连续的 400：``DPE_VALIDATION`` + ``DPE-Upload-Offset``（免 HEAD 重同步）。"""
    body = {
        "type": "urn:dpe:error:validation",
        "title": "validation",
        "status": 400,
        "code": "DPE_VALIDATION",
    }
    return Response(
        400,
        {
            "Content-Type": "application/problem+json",
            "DPE-Error-Code": "DPE_VALIDATION",
            "DPE-Upload-Offset": str(offset),
        },
        json.dumps(body).encode(),
    )


def chunk_ranges(total: int, size: int) -> list[tuple[int, int]]:
    """``(起点, 长度)`` 的分块序列（块的顺序与边界）。"""
    return [(start, min(size, total - start)) for start in range(0, total, size)]
