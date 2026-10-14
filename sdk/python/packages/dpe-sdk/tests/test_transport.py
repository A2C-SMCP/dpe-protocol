"""httpx 传输适配（#13）：``DpeClient`` / ``AsyncDpeClient`` 的行为测试。

协议路径经真实参考服务端（``create_app`` 挂到 ``httpx.ASGITransport``，零网络）；
``httpx.MockTransport`` 只用来模拟网络层故障（超时、连接失败、429 / 503 与逐次应答），
不替代承载协议契约的那一层。同步客户端对真实 TCP 端口（子进程参考服务端）的测试与
sync / async 一致性见 ``test_transport_server.py``。
"""

from __future__ import annotations

import asyncio
import gzip
import json
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from typing import Any

import dpe_hash
import httpx
import pytest
from client_helpers import PREFIX, REMOTE, ClientRecorder
from client_helpers import in_process_client as in_process
from dpe_sdk import protocol as protocol_module
from dpe_sdk import transport as transport_module
from dpe_sdk.credentials import StaticCredentialProvider
from dpe_sdk.errors import (
    AlreadyExistsError,
    DpeError,
    ForbiddenError,
    NotFoundError,
    PreconditionFailedError,
    RateLimitedError,
    UnexpectedResponseError,
)
from dpe_sdk.models import Document
from dpe_sdk.protocol import (
    BaseHash,
    BlobSource,
    IfAbsent,
    Response,
    adrive,
    error_from_response,
)
from dpe_sdk.testing import PrefixAuthorizer, create_app
from dpe_sdk.transport import AsyncDpeClient, DpeClient
from engine_helpers import make_engine, text_doc
from protocol_helpers import CAPABILITIES, H1

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _doc(*texts: str) -> Document:
    """一页、每段文本一个 ``NarrativeText`` 元素的文档（``text_doc`` 的一个页参数）。"""
    return Document.model_validate(text_doc([*texts]))


def _problem(code: str, status: int, **headers: str) -> httpx.Response:
    return httpx.Response(
        status,
        json={"type": "urn:dpe:error:x", "title": "t", "status": status, "code": code},
        headers={"Content-Type": "application/problem+json", "DPE-Error-Code": code, **headers},
    )


# ---------------------------------------------------------------------- remote


# remote URL 的接受与拒绝由向量逐例固定（tests/test_remote_url_entry_points.py，HTTP 绑定 §1）


async def test_remote_must_be_a_string() -> None:
    with pytest.raises(TypeError):
        DpeClient(123)  # type: ignore[arg-type]


async def test_remote_is_used_verbatim() -> None:
    """合法的 remote 原样保存，路径直接拼在其后（不经 URL 库归一）。"""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json=CAPABILITIES)

    remote = "http://dpe.test:8080/r/a%2Fb"
    with DpeClient(remote, transport=httpx.MockTransport(handler)) as client:
        assert client.remote == remote
        assert client.capabilities().protocol == "dpe/1"
    assert seen == ["http://dpe.test:8080/r/a%2Fb/capabilities"]


async def test_remote_trailing_slash_is_rejected() -> None:
    """尾部 ``/`` 不再归一（HTTP 绑定 §1 第 7 条）。"""
    with pytest.raises(ValueError, match="/ 结尾"):
        DpeClient("http://dpe.test/r/1/")


@pytest.mark.parametrize(("name", "value"), [("max_retries", -1), ("max_retries", True)])
async def test_max_retries_must_be_a_non_negative_int(name: str, value: Any) -> None:
    with pytest.raises(ValueError, match=name):
        DpeClient("http://dpe.test/r/1", **{name: value})


@pytest.mark.parametrize(("name", "value"), [("max_resends", -1), ("max_resends", True)])
async def test_max_resends_is_validated_at_construction(name: str, value: Any) -> None:
    """孪生参数与 ``max_retries`` 一样在构造时报错，不是等首个操作发完 capabilities 才抛。"""
    with pytest.raises(ValueError, match=name):
        DpeClient("http://dpe.test/r/1", **{name: value})


# ---------------------------------------------------------------------- 进程内全流程


async def test_round_trip_through_the_reference_server() -> None:
    async with in_process() as (client, _):
        document = _doc("hello", "world")
        created = await client.commit("s3://bucket/a.md", document, IfAbsent())
        assert (created.status, created.doc_hash) == ("created", document.doc_hash())
        assert await client.head("s3://bucket/a.md") == document.doc_hash()
        assert await client.batch_head(["s3://bucket/a.md", "s3://bucket/gone"]) == [
            document.doc_hash(),
            None,
        ]
        skeleton = await client.get_skeleton("s3://bucket/a.md")
        assert skeleton is not None
        assert (skeleton.file_uri, skeleton.doc_hash) == ("s3://bucket/a.md", document.doc_hash())

        changed = _doc("hello")
        updated = await client.commit("s3://bucket/a.md", changed, BaseHash(created.doc_hash))
        assert (updated.status, updated.doc_hash) == ("updated", changed.doc_hash())

        page = await client.list_page(prefix="s3://bucket/")
        assert [(e.file_uri, e.doc_hash) for e in page.documents] == [
            ("s3://bucket/a.md", changed.doc_hash())
        ]
        assert page.next_cursor is None

        moved = await client.move("s3://bucket/a.md", "s3://bucket/b.md", updated.doc_hash)
        assert moved.doc_hash == updated.doc_hash
        assert await client.head("s3://bucket/a.md") is None
        assert await client.head("s3://bucket/b.md") == updated.doc_hash

        await client.delete("s3://bucket/b.md", updated.doc_hash)
        assert await client.head("s3://bucket/b.md") is None


async def test_precondition_errors_are_typed() -> None:
    async with in_process() as (client, _):
        document = _doc("hello")
        await client.commit("s3://bucket/a.md", document, IfAbsent())
        # 同样内容重复提交命中「unchanged 先于条件求值」（HTTP 绑定 §3.2），不算冲突
        assert (await client.commit("s3://bucket/a.md", document, IfAbsent())).status == "unchanged"
        with pytest.raises(AlreadyExistsError):
            await client.commit("s3://bucket/a.md", _doc("different"), IfAbsent())
        with pytest.raises(PreconditionFailedError):
            await client.commit("s3://bucket/a.md", _doc("other"), BaseHash(H1))
        with pytest.raises(NotFoundError):
            await client.delete("s3://bucket/gone.md", H1)


async def test_capabilities_are_snapshotted_once() -> None:
    async with in_process() as (client, recorder):
        first = await client.capabilities()
        assert await client.capabilities() == first
        assert (await client.core()).capabilities == first
    assert sum(1 for r in recorder.requests if r.url.path.endswith("/capabilities")) == 1


async def test_concurrent_first_use_negotiates_capabilities_once() -> None:
    """同步客户端可跨线程：并发首次访问 ``core()`` 只协商一次能力、得到同一实例。"""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        time.sleep(0.05)  # 放大窗口：其他线程应等在引导锁上，而不是各自再协商
        return httpx.Response(200, json=CAPABILITIES)

    with (
        DpeClient(REMOTE, transport=httpx.MockTransport(handler)) as client,
        ThreadPoolExecutor(max_workers=8) as pool,
    ):
        cores = list(pool.map(lambda _: client.core(), range(8)))
    assert len(requests) == 1
    assert len({id(core) for core in cores}) == 1


async def test_async_concurrent_first_use_negotiates_capabilities_once() -> None:
    # 能力请求在参考服务端的引擎工作线程里执行，事件循环保持可交错，竞态窗口是真实的
    async with in_process() as (client, recorder):
        cores = await asyncio.gather(*(client.core() for _ in range(8)))
    assert len({id(core) for core in cores}) == 1
    assert sum(1 for r in recorder.requests if r.url.path.endswith("/capabilities")) == 1


async def test_send_is_usable_as_a_drive_callback() -> None:
    """``send`` 是公开的驱动缝：自定义操作可以直接经它执行。"""
    async with in_process() as (client, _):
        document = _doc("hello")
        await client.commit("s3://bucket/a.md", document, IfAbsent())
        core = await client.core()
        assert await adrive(core.head("s3://bucket/a.md"), client.send) == document.doc_hash()


# ---------------------------------------------------------------------- 暂存路径与分块


async def test_deliver_uploads_missing_blobs_in_chunks_off_the_event_loop() -> None:
    engine = make_engine(max_payload_bytes=4096, blob_chunk_bytes=1024)
    # 5120 字节必须分块：单块上限 min(blob_chunk_bytes, max_payload_bytes)
    data = bytes(range(256)) * 20
    ref = dpe_hash.blob_ref(data)
    document = Document.model_validate(
        {
            "file_type": "png",
            "pages": [{"elements": [{"category": "Image", "blob": ref, "mime_type": "image/png"}]}],
        }
    )
    idents: set[int] = set()

    def read(offset: int, length: int) -> bytes:
        idents.add(threading.get_ident())
        return data[offset : offset + length]

    source = BlobSource(ref, len(data), read)
    async with in_process(engine) as (client, recorder):
        result = await client.deliver(
            "s3://bucket/img.png", document, IfAbsent(), blobs=lambda _: source
        )
        assert result.status == "created"
        # 去重范围内已有 blob：同一篇文档重新投递不再传字节
        again = await client.deliver("s3://bucket/img.png", document, BaseHash(result.doc_hash))
        assert again.status == "unchanged"

    chunks = [
        (headers, body)
        for headers, body in zip(recorder.headers, recorder.bodies, strict=True)
        if "content-range" in headers
    ]
    assert len(chunks) > 1
    assert b"".join(body for _, body in chunks) == data
    # 分块请求不走解码路径（HTTP 绑定 §7）：MUST NOT 压缩，否则服务端 DPE_VALIDATION
    assert all("content-encoding" not in headers for headers, _ in chunks)
    # BlobRange 是文件读取（阻塞 I/O）：异步层放进工作线程，不在事件循环线程里读源
    assert idents and threading.main_thread().ident not in idents


class _FlakyChunks(httpx.AsyncBaseTransport):
    """对第一个分块 PUT（带 ``Content-Range``）抛读超时一次，其余转发给内层 transport。

    模拟「分块请求可能已发出、响应丢失」：传输层 MUST NOT 盲目重发；先断点查询再续传的
    客户端义务由核心实现（HTTP 绑定 §4.7）。
    """

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner
        self.failed = False

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if not self.failed and "content-range" in request.headers:
            self.failed = True
            raise httpx.ReadTimeout("chunk response lost", request=request)
        return await self._inner.handle_async_request(request)


async def test_chunk_put_failure_resumes_via_the_head_query() -> None:
    """分块 PUT 的响应丢失：传输层不重发（``retry_safe`` 为假），恢复由核心的断点查询承担。"""
    engine = make_engine(max_payload_bytes=4096, blob_chunk_bytes=1024)
    data = bytes(range(256)) * 20  # 5120 字节 → 5 块
    ref = dpe_hash.blob_ref(data)
    document = Document.model_validate(
        {
            "file_type": "png",
            "pages": [{"elements": [{"category": "Image", "blob": ref, "mime_type": "image/png"}]}],
        }
    )
    source = BlobSource(ref, len(data), lambda offset, length: data[offset : offset + length])
    app = create_app(engine, prefix=PREFIX)
    recorder = ClientRecorder(_FlakyChunks(httpx.ASGITransport(app=app)))
    async with AsyncDpeClient(REMOTE, transport=recorder) as client:
        result = await client.deliver(
            "s3://bucket/flaky.png", document, IfAbsent(), blobs=lambda _: source
        )
    assert result.status == "created"
    ranges = [h["content-range"] for h in recorder.headers if "content-range" in h]
    # 首块恰好出现两次 = 原发 1 次 + 断点查询后按已收偏移续传 1 次
    assert ranges.count("bytes 0-1023/5120") == 2
    # 判别点：恢复必须先断点查询（传输层盲重发则直接重发、不会出现 HEAD）
    assert sum(1 for r in recorder.requests if r.method == "HEAD") == 1


# ---------------------------------------------------------------------- gzip


async def test_large_json_requests_are_gzipped() -> None:
    async with in_process() as (client, recorder):
        await client.commit("s3://bucket/big.md", _doc("x" * 4096), IfAbsent())
    index = next(i for i, r in enumerate(recorder.requests) if r.url.path.endswith("/documents"))
    assert recorder.headers[index]["content-encoding"] == "gzip"
    payload = json.loads(gzip.decompress(recorder.bodies[index]))
    assert payload["document"]["file_type"] == "md"
    # capabilities 请求没有体，也不需要压缩
    assert "content-encoding" not in recorder.headers[0]


async def test_small_json_requests_stay_identity() -> None:
    async with in_process() as (client, recorder):
        await client.commit("s3://bucket/small.md", _doc("hi"), IfAbsent())
    assert all("content-encoding" not in headers for headers in recorder.headers)


async def test_no_gzip_when_the_server_does_not_offer_it() -> None:
    # 服务端不声明 gzip 时客户端必须用 identity：错误压缩会被服务端 415 拒绝
    engine = make_engine(content_encodings=())
    async with in_process(engine) as (client, recorder):
        result = await client.commit("s3://bucket/big.md", _doc("x" * 4096), IfAbsent())
        assert result.status == "created"
    assert all("content-encoding" not in headers for headers in recorder.headers)


# ---------------------------------------------------------------------- 凭证


class _CountingProvider:
    def __init__(self, value: str | None) -> None:
        self.value = value
        self.calls = 0

    def authorization(self) -> str | None:
        self.calls += 1
        return self.value


async def test_credential_is_injected_into_every_request() -> None:
    provider = _CountingProvider("Bearer full")
    async with in_process(credential=provider) as (client, recorder):
        await client.commit("s3://bucket/a.md", _doc("hello"), IfAbsent())
    assert provider.calls == len(recorder.requests)
    assert all(headers.get("authorization") == "Bearer full" for headers in recorder.headers)


class _RotatingProvider:
    """每次取凭证都换一个值：钉住「重发前重新取凭证」的语义。"""

    def __init__(self) -> None:
        self.calls = 0

    def authorization(self) -> str | None:
        self.calls += 1
        return f"Bearer token-{self.calls}"


async def test_credential_is_refreshed_before_every_retry() -> None:
    provider = _RotatingProvider()
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization"))
        if len(seen) == 1:
            return _problem("DPE_RATE_LIMITED", 429, **{"Retry-After": "0"})
        return httpx.Response(200, json=CAPABILITIES)

    with DpeClient(REMOTE, credential=provider, transport=httpx.MockTransport(handler)) as client:
        assert client.capabilities().protocol == "dpe/1"
    assert seen == ["Bearer token-1", "Bearer token-2"]
    assert provider.calls == 2


def _authenticate(value: str | None) -> str | None:
    """#45 的多身份钩子：``Authorization`` 头的取值即身份，未列出的一律 401。"""
    return value if value in ("Bearer full", "Bearer limited") else None


async def test_multi_identity_authorization() -> None:
    engine = make_engine(
        authorizer=PrefixAuthorizer(
            {"Bearer full": ("",), "Bearer limited": ("dpe://docs/a/",)},
            force=frozenset({"Bearer full"}),
        )
    )
    async with in_process(
        engine, credential=StaticCredentialProvider("Bearer full"), authenticate=_authenticate
    ) as (client, _):
        assert (await client.commit("dpe://docs/b", _doc("x"), IfAbsent())).status == "created"
    async with in_process(
        engine,
        credential=StaticCredentialProvider("Bearer limited"),
        authenticate=_authenticate,
    ) as (client, _):
        with pytest.raises(ForbiddenError):
            await client.commit("dpe://docs/b", _doc("x"), IfAbsent())
        assert (await client.commit("dpe://docs/a/ok", _doc("x"), IfAbsent())).status == "created"
    async with in_process(engine, authenticate=_authenticate) as (client, _):
        # 无凭证：401 不是 DPE 错误（无 problem 体、无 DPE-Error-Code），如实上报不猜 code
        with pytest.raises(UnexpectedResponseError) as excinfo:
            await client.capabilities()
        assert excinfo.value.status == 401


# ---------------------------------------------------------------------- 重试


def _scripted(
    *steps: httpx.Response | Exception,
) -> tuple[httpx.MockTransport, list[httpx.Request]]:
    """按脚本逐次应答或抛出的 MockTransport 处理器，与收到的请求列表。"""
    requests: list[httpx.Request] = []
    remaining = list(steps)

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        step = remaining.pop(0) if len(remaining) > 1 else remaining[0]
        if isinstance(step, Exception):
            raise step
        return step

    return httpx.MockTransport(handler), requests


async def test_retryable_errors_wait_for_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """429 / 503 按 ``Retry-After``（秒数与 HTTP-date 两种格式）等待后原样重发。"""
    waits: list[float] = []
    monkeypatch.setattr(transport_module, "_sleep", waits.append)
    when = format_datetime(datetime.now(UTC) + timedelta(seconds=30), usegmt=True)
    transport, requests = _scripted(
        _problem("DPE_RATE_LIMITED", 429, **{"Retry-After": "7"}),
        _problem("DPE_UNAVAILABLE", 503, **{"Retry-After": when}),
        httpx.Response(200, json=CAPABILITIES),
    )
    with DpeClient(REMOTE, transport=transport) as client:
        assert client.capabilities().protocol == "dpe/1"
    assert len(requests) == 3
    assert waits[0] == 7.0
    assert 25.0 <= waits[1] <= 30.0  # HTTP-date 只有秒级分辨率


async def test_retryable_error_without_retry_after_uses_the_spec_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    waits: list[float] = []
    monkeypatch.setattr(transport_module, "_sleep", waits.append)
    transport, requests = _scripted(
        _problem("DPE_RATE_LIMITED", 429), httpx.Response(200, json=CAPABILITIES)
    )
    with DpeClient(REMOTE, transport=transport) as client:
        assert client.capabilities().protocol == "dpe/1"
    assert (len(requests), waits) == (2, [1.0])


async def test_retryable_error_budget_exhausted_is_reported() -> None:
    transport, requests = _scripted(_problem("DPE_RATE_LIMITED", 429, **{"Retry-After": "0"}))
    with (
        DpeClient(REMOTE, transport=transport, max_retries=2) as client,
        pytest.raises(RateLimitedError) as excinfo,
    ):
        client.capabilities()
    assert (len(requests), excinfo.value.retryable, excinfo.value.retry_after) == (3, True, 0.0)


async def test_absurd_retry_after_is_not_retried() -> None:
    """超过等待上限的 Retry-After（含大到无法表示的 ``inf``）不吸收：如实抛出且值不丢。"""
    for header, expected in (("9" * 30, float("9" * 30)), ("9" * 5000, math.inf)):
        transport, requests = _scripted(
            _problem("DPE_RATE_LIMITED", 429, **{"Retry-After": header})
        )
        with (
            DpeClient(REMOTE, transport=transport) as client,
            pytest.raises(RateLimitedError) as excinfo,
        ):
            client.capabilities()
        assert len(requests) == 1
        assert excinfo.value.retry_after == expected


async def test_wait_at_the_ceiling_is_still_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    waits: list[float] = []
    monkeypatch.setattr(transport_module, "_sleep", waits.append)
    transport, requests = _scripted(
        _problem("DPE_RATE_LIMITED", 429, **{"Retry-After": "300"}),
        httpx.Response(200, json=CAPABILITIES),
    )
    with DpeClient(REMOTE, transport=transport) as client:
        assert client.capabilities().protocol == "dpe/1"
    assert (len(requests), waits) == (2, [300.0])


async def test_async_retryable_error_awaits_the_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    waits: list[float] = []

    async def record(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(transport_module, "_asleep", record)
    transport, requests = _scripted(
        _problem("DPE_UNAVAILABLE", 503, **{"Retry-After": "3"}),
        httpx.Response(200, json=CAPABILITIES),
    )
    async with AsyncDpeClient(REMOTE, transport=transport) as client:
        assert (await client.capabilities()).protocol == "dpe/1"
    assert (len(requests), waits) == (2, [3.0])


async def test_read_requests_resent_network_failures() -> None:
    transport, requests = _scripted(
        httpx.ReadTimeout("lost"),
        httpx.ReadTimeout("lost"),
        httpx.Response(200, json=CAPABILITIES),
    )
    with DpeClient(REMOTE, transport=transport) as client:
        assert client.capabilities().protocol == "dpe/1"
    assert len(requests) == 3


async def test_async_read_requests_resent_network_failures() -> None:
    transport, requests = _scripted(
        httpx.ReadTimeout("lost"),
        httpx.ReadTimeout("lost"),
        httpx.Response(200, json=CAPABILITIES),
    )
    async with AsyncDpeClient(REMOTE, transport=transport) as client:
        assert (await client.capabilities()).protocol == "dpe/1"
    assert len(requests) == 3


async def test_a_bare_429_is_not_retried_and_not_guessed() -> None:
    """无 problem 体、无 ``DPE-Error-Code`` 的 429 不是 DPE 错误：不猜 code、也不重试（§5）。"""
    transport, requests = _scripted(httpx.Response(429, headers={"Retry-After": "0"}))
    with (
        DpeClient(REMOTE, transport=transport) as client,
        pytest.raises(UnexpectedResponseError) as excinfo,
    ):
        client.capabilities()
    assert len(requests) == 1
    assert excinfo.value.status == 429


async def test_retryable_errors_are_resent_on_writes_too() -> None:
    """写请求（``retry_safe`` 为假）遇到按 ``code`` 判定的可重试错误时，传输层原样重发——
    这是写路径上唯一允许传输层自行重发的情形（core §6：``retryable`` 即请求未被处理）。"""
    documents: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/capabilities"):
            return httpx.Response(200, json=CAPABILITIES)
        documents.append(request.read())
        if len(documents) == 1:
            return _problem("DPE_RATE_LIMITED", 429, **{"Retry-After": "0"})
        document = _doc("hello")
        return httpx.Response(
            201,
            json={
                "status": "created",
                "doc_hash": document.doc_hash(),
                "delta": {"added": 1, "removed": 0, "retained": 0},
            },
        )

    with DpeClient(REMOTE, transport=httpx.MockTransport(handler)) as client:
        result = client.commit("s3://bucket/a.md", _doc("hello"), IfAbsent())
    assert (len(documents), result.status) == (2, "created")
    assert documents[0] == documents[1]  # 原样重发：两次的请求体逐字节相同


async def test_a_bare_503_on_a_write_is_not_resent() -> None:
    """无 ``DPE-Error-Code`` 的 503 不是可重试的 DPE 错误：不猜 code、写请求也不重发（§5）。"""
    documents: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/capabilities"):
            return httpx.Response(200, json=CAPABILITIES)
        documents.append(request.read())
        return httpx.Response(503, headers={"Retry-After": "0"})

    with (
        DpeClient(REMOTE, transport=httpx.MockTransport(handler)) as client,
        pytest.raises(UnexpectedResponseError) as excinfo,
    ):
        client.commit("s3://bucket/a.md", _doc("hello"), IfAbsent())
    assert len(documents) == 1
    assert excinfo.value.status == 503


async def test_batch_head_is_retry_safe_despite_being_a_post() -> None:
    """重发安全性来自 ``retry_safe``，不按 HTTP 方法推断：batch_head 走 POST 但只读。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/capabilities"):
            return httpx.Response(200, json=CAPABILITIES)
        requests.append(request)
        if len(requests) == 1:
            raise httpx.ReadTimeout("lost")
        return httpx.Response(200, json={"heads": [{"doc_hash": H1}]})

    requests: list[httpx.Request] = []
    with DpeClient(REMOTE, transport=httpx.MockTransport(handler)) as client:
        assert client.batch_head(["s3://bucket/a.md"]) == [H1]
    assert len(requests) == 2


async def test_write_timeout_is_left_to_the_core_to_handle() -> None:
    """写请求（``retry_safe=False``）可能已发出后的读超时不被传输层重发：原样抛回，由核心
    按 core §5.2 的重试规则处理（这里核心把它当响应丢失、原样重发至多 ``max_resends`` 次）。"""
    puts: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/capabilities"):
            return httpx.Response(200, json=CAPABILITIES)
        puts.append(request)
        raise httpx.ReadTimeout("possibly delivered")

    document = _doc("hello")
    with (
        DpeClient(REMOTE, transport=httpx.MockTransport(handler)) as client,
        pytest.raises(httpx.ReadTimeout),
    ):
        client.commit("s3://bucket/a.md", document, IfAbsent())
    # 3 次 = 首发 1 次 + 核心的 2 次重发（max_resends）；传输层没有自行翻倍
    assert len(puts) == 3


async def test_write_timeout_resent_by_the_core_succeeds() -> None:
    puts: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/capabilities"):
            return httpx.Response(200, json=CAPABILITIES)
        puts.append(request)
        if len(puts) == 1:
            raise httpx.ReadTimeout("lost")
        document = _doc("hello")
        return httpx.Response(
            201,
            json={
                "status": "created",
                "doc_hash": document.doc_hash(),
                "delta": {"added": 1, "removed": 0, "retained": 0},
            },
        )

    with DpeClient(REMOTE, transport=httpx.MockTransport(handler)) as client:
        result = client.commit("s3://bucket/a.md", _doc("hello"), IfAbsent())
    assert (len(puts), result.status) == (2, "created")


async def test_connect_failures_are_resent_also_for_writes() -> None:
    """确定未发出的建连失败可以安全重发：写请求也不例外（对端连字节都没收到）。"""
    puts: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/capabilities"):
            return httpx.Response(200, json=CAPABILITIES)
        puts.append(request)
        if len(puts) <= 2:
            raise httpx.ConnectError("refused", request=request)
        document = _doc("hello")
        return httpx.Response(
            201,
            json={
                "status": "created",
                "doc_hash": document.doc_hash(),
                "delta": {"added": 1, "removed": 0, "retained": 0},
            },
        )

    with DpeClient(REMOTE, transport=httpx.MockTransport(handler)) as client:
        result = client.commit("s3://bucket/a.md", _doc("hello"), IfAbsent())
    assert (len(puts), result.status) == (3, "created")


async def test_negotiate_timeout_is_not_resent() -> None:
    """negotiate 会开出会话、不是幂等的只读请求：``retry_safe=False``，传输层不重发。"""
    negotiations: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/capabilities"):
            return httpx.Response(200, json=CAPABILITIES)
        negotiations.append(request)
        raise httpx.ReadTimeout("possibly delivered")

    with (
        DpeClient(REMOTE, transport=httpx.MockTransport(handler)) as client,
        pytest.raises(httpx.ReadTimeout),
    ):
        client.negotiate("s3://bucket/a.md", _doc("hello"))
    assert len(negotiations) == 1


async def test_error_from_response_maps_problem_bodies() -> None:
    body = json.dumps({"code": "DPE_PRECONDITION_FAILED"}).encode()
    response = Response(412, {"Content-Type": "application/problem+json"}, body)
    assert isinstance(error_from_response(response), PreconditionFailedError)
    assert not isinstance(error_from_response(Response(500, {}, b"<html>")), DpeError)


async def test_not_sent_classifies_connect_failures() -> None:
    """公开的「确定未发出」判定：建连失败为真（写操作调用方据此清除待定意图），其余为假。"""
    assert transport_module.not_sent(httpx.ConnectError("refused"))
    assert transport_module.not_sent(httpx.ConnectTimeout("timed out"))
    assert not transport_module.not_sent(httpx.ReadTimeout("lost"))
    assert not transport_module.not_sent(httpx.RemoteProtocolError("closed"))
    assert not transport_module.not_sent(ValueError("local"))


async def test_not_sent_failures_carry_the_base_mark() -> None:
    """传输层放弃发送时给「确定未发出」的异常打底标（写操作结果标记协议的一半）。"""

    def refused(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with (
        DpeClient(REMOTE, transport=httpx.MockTransport(refused)) as client,
        pytest.raises(httpx.ConnectError) as excinfo,
    ):
        client.capabilities()
    assert protocol_module._attempt_not_sent(excinfo.value)

    def lost(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("lost", request=request)

    with (
        DpeClient(REMOTE, transport=httpx.MockTransport(lost)) as client,
        pytest.raises(httpx.ReadTimeout) as timeout_info,
    ):
        client.capabilities()
    assert not protocol_module._attempt_not_sent(timeout_info.value)
