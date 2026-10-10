"""暂存路径对真实参考服务端（#49）：页分块、blob 断点续传、会话过期与缺失恢复。

``ProtocolCore`` 产出的请求经 ``ASGITransport`` 发给 ``dpe_sdk.testing`` 的参考服务端；
「响应丢失」用包装器模拟：请求照常到达引擎（状态已变），客户端拿不到结果。
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

import dpe_hash
import pytest
from dpe_sdk import Document, errors
from dpe_sdk.protocol import BaseHash, BlobSource, IfAbsent, Request, Response, adrive
from engine_helpers import FakeClock, make_engine
from http_helpers import remote

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


URI = "test://docs/staged"
TARGET = "documents?uri=test%3A%2F%2Fdocs%2Fstaged"
IMAGE = b"\x89PNG" + b"x" * 246  # 250 字节
REF = dpe_hash.blob_ref(IMAGE)

Send = Callable[[Request], Awaitable[Response]]


def image_doc() -> Document:
    return Document.model_validate(
        {
            "file_type": "png",
            "title": "图片",
            "pages": [{"elements": [{"category": "Image", "blob": REF, "mime_type": "image/png"}]}],
        }
    )


def many_elements_doc(count: int = 10) -> Document:
    """一页多元素：页对象超过 ``max_payload_bytes`` 而每个元素不超（走页分块）。"""
    return Document.model_validate(
        {
            "file_type": "md",
            "pages": [
                {
                    "elements": [
                        {"category": "NarrativeText", "text": f"sentence {i} " * 4}
                        for i in range(count)
                    ]
                }
            ],
        }
    )


def small_engine(**overrides: Any) -> Any:
    """小限额的参考服务端：``max_payload_bytes=200``、``blob_chunk_bytes=100``。"""
    limits: dict[str, Any] = {
        "max_payload_bytes": 200,
        "blob_chunk_bytes": 100,
        "page_max_bytes": 4096,
        "blob_max_bytes": 4096,
    }
    limits.update(overrides)
    return make_engine(**limits)


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


class Expire(Recorder):
    """命中 ``match`` 的第一次请求转发前拨快时钟：会话在服务端立即过期。"""

    def __init__(
        self, send: Send, match: Callable[[Request], bool], clock: FakeClock, seconds: float
    ) -> None:
        super().__init__(send)
        self._match = match
        self._clock = clock
        self._seconds = seconds
        self.expired = 0

    async def __call__(self, request: Request) -> Response:
        if self.expired == 0 and self._match(request):
            self.expired += 1
            self._clock.advance(self._seconds)
        return await super().__call__(request)


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
    return {
        request.target.split("/")[1]
        for request in requests
        if request.target.startswith("staging/")
    }


async def test_deliver_chunks_a_large_page_against_the_reference_server() -> None:
    document = many_elements_doc()
    async with remote(small_engine()) as r:
        core = await r.core()
        recorder = Recorder(r.send)
        result = await adrive(core.deliver(URI, document, IfAbsent()), recorder)
        assert result.status == "created"
        assert (result.delta.added, result.delta.removed) == (10, 0)
        skeleton = await r.run(core.get_skeleton(URI))
        assert skeleton is not None
        assert skeleton.doc_hash == document.doc_hash()
        assert len(skeleton.pages) == 1 and len(skeleton.pages[0].elements) == 10

        spans = chunk_spans(recorder.sent)
        assert spans, "页对象应当分块上传"
        total = spans[0][2]
        assert all(item[2] == total for item in spans)
        assert spans[0][0] == 0 and spans[-1][1] + 1 == total
        assert all(span[0] == spans[i - 1][1] + 1 for i, span in enumerate(spans) if i)
        for request in recorder.sent:
            body = request.body_bytes()
            if "Content-Range" in request.headers:  # 分块请求：单块不超过 blob_chunk_bytes
                assert body is not None and len(body) <= 100


async def test_deliver_resumes_a_lost_blob_chunk_against_the_reference_server() -> None:
    """某一块的响应丢失：先断点查询、按已收偏移续传（core §3.4 的客户端义务）。"""
    document = image_doc()

    def drop_second_chunk(request: Request) -> bool:
        return (
            request.method == "PUT"
            and "/blobs/" in request.target
            and request.headers.get("Content-Range", "").startswith("bytes 100-")
        )

    async with remote(small_engine()) as r:
        core = await r.core()
        lossy = Lossy(r.send, drop_second_chunk)
        result = await adrive(
            core.deliver(URI, document, IfAbsent(), blobs=lambda ref: BlobSource.from_bytes(IMAGE)),
            lossy,
        )
        assert result.status == "created"
        assert lossy.dropped == 1
        dropped_at = next(i for i, request in enumerate(lossy.sent) if drop_second_chunk(request))
        head = lossy.sent[dropped_at + 1]
        assert (head.method, head.target) == ("HEAD", lossy.sent[dropped_at].target)
        skeleton = await r.run(core.get_skeleton(URI))
        assert skeleton is not None and skeleton.doc_hash == document.doc_hash()


async def test_deliver_renegotiates_when_the_session_expires_mid_flow() -> None:
    document = many_elements_doc()
    clock = FakeClock()
    engine = small_engine(clock=clock, staging_ttl_seconds=3600)
    async with remote(engine) as r:
        core = await r.core()
        expiring = Expire(
            r.send,
            lambda request: request.method == "PUT" and "/pages/" in request.target,
            clock,
            3601,
        )
        result = await adrive(core.deliver(URI, document, IfAbsent()), expiring)
        assert result.status == "created"
        assert expiring.expired == 1
        assert len(session_ids(expiring.sent)) == 2  # 过期后重开：旧会话的请求被拒、新会话完成投递
        last_session = json.loads(expiring.sent[-1].body_bytes() or b"")["staging_session"]
        assert last_session in session_ids(expiring.sent)
        skeleton = await r.run(core.get_skeleton(URI))
        assert skeleton is not None and skeleton.doc_hash == document.doc_hash()


async def test_deliver_reports_a_server_side_blob_hash_mismatch() -> None:
    """声明了正确的 ref 但字节不符：服务端到齐校验失败，如实上报（带会话句柄）。"""
    document = image_doc()
    wrong = b"y" * len(IMAGE)
    source = BlobSource(REF, len(IMAGE), lambda offset, length: wrong[offset : offset + length])
    async with remote(small_engine()) as r:
        core = await r.core()
        with pytest.raises(errors.HashMismatchError) as info:
            await adrive(core.deliver(URI, document, IfAbsent(), blobs=lambda ref: source), r.send)
        assert info.value.session is not None


async def test_deliver_recovers_a_missing_blob_via_a_session() -> None:
    """快路径缺 blob（blob 永远不能内联）：开会话补传后以「内联 + 会话」重提（core §6）。"""
    document = image_doc()
    async with remote() as r:
        core = await r.core()
        recorder = Recorder(r.send)
        result = await adrive(
            core.deliver(URI, document, IfAbsent(), blobs=lambda ref: IMAGE), recorder
        )
        assert result.status == "created"
        methods = [(request.method, request.target) for request in recorder.sent]
        assert methods[0] == ("PUT", TARGET)  # 先快路径
        assert methods[1][0] == "POST" and methods[1][1] == "negotiate"
        assert any("/blobs/" in target for _, target in methods)
        body = json.loads(recorder.sent[-1].body_bytes() or b"")
        assert body["staging_session"] and body["pages"] and body["objects"]  # 内联 + 会话
        skeleton = await r.run(core.get_skeleton(URI))
        assert skeleton is not None and skeleton.doc_hash == document.doc_hash()


async def test_deliver_reuses_the_session_after_a_cas_failure() -> None:
    """CAS 冲突后以新前置条件引用同一会话重提，已上传内容不重传（core §3.4）。"""
    document = many_elements_doc()
    changed = many_elements_doc(9)
    async with remote(small_engine()) as r:
        core = await r.core()
        assert (await adrive(core.deliver(URI, document, IfAbsent()), r.send)).status == "created"
        # 前置条件不满足（如他人已写入）：冲突原样上报，异常带仍可复用的会话
        with pytest.raises(errors.PreconditionFailedError) as info:
            await adrive(core.deliver(URI, changed, BaseHash(changed.doc_hash())), r.send)
        session = info.value.session
        assert session is not None
        # 重新读取后引用同一会话重提：只 commit，不再 negotiate、不重传页与元素
        recorder = Recorder(r.send)
        result = await adrive(
            core.deliver(URI, changed, BaseHash(document.doc_hash()), session=session), recorder
        )
        assert result.status == "updated"
        assert [request.method for request in recorder.sent] == ["PUT"]
        skeleton = await r.run(core.get_skeleton(URI))
        assert skeleton is not None and skeleton.doc_hash == changed.doc_hash()
