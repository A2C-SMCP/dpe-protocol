"""请求构造阶段的本地失败（#207）：非法 ``xn--`` 主机与非法凭证头值。

两侧对等口径：这类失败发生在请求发出之前、由本地判定，客户端 MUST 以 SDK 自己的错误类型
上报、显式标记「确定未发出」、不重发，且消息不回显 URL / 主机 / 凭证。
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from client_helpers import CountingProvider
from dpe_sdk import protocol as protocol_module
from dpe_sdk.errors import ClientError, LocalRequestError
from dpe_sdk.models import Document
from dpe_sdk.protocol import BlobSource, Force, ProtocolCore, Session, adrive, drive
from dpe_sdk.transport import AsyncDpeClient, DpeClient
from engine_helpers import text_doc
from protocol_helpers import CAPABILITIES, H1, caps

pytestmark = pytest.mark.anyio

#: 两侧 URL 库都会拒绝的主机（各自拒绝集合更大，按「不复刻库怪癖」不对齐，见 #207）
INVALID_HOSTS = ["xn--zz.example", "xn--a", "xn--.example"]

#: RFC 9110 field-value 不允许的控制字符（HTAB 除外）：LF、NUL、CR、US（0x1F）、DEL（0x7F）
INVALID_CREDENTIALS = [
    "Bearer abc\ndef",
    "Bearer abc\x00def",
    "Bearer abc\rdef",
    "Bearer abc\x1fdef",
    "Bearer abc\x7fdef",
]

SECRET = "super-secret-token"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class _CountingTransport(httpx.BaseTransport):
    """若请求真的到了传输层就记数；正常情况下一条也不该到达。"""

    def __init__(self) -> None:
        self.requests = 0

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests += 1
        return httpx.Response(200, json=CAPABILITIES)


class _CountingAsyncTransport(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.requests = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests += 1
        return httpx.Response(200, json=CAPABILITIES)


class _LocalProtocolTransport(httpx.BaseTransport):
    """模拟 send 阶段由库抛出的 ``LocalProtocolError``（消息里带凭证原文）。"""

    def __init__(self) -> None:
        self.requests = 0

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests += 1
        raise httpx.LocalProtocolError(f"Illegal header value b'Bearer {SECRET}\\ndef'")


class _LocalProtocolAsyncTransport(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.requests = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests += 1
        raise httpx.LocalProtocolError(f"Illegal header value b'Bearer {SECRET}\\ndef'")


class _FirstTimeoutTransport(httpx.BaseTransport):
    """第一次请求丢响应（结果不确定），之后不应再被调用。"""

    def __init__(self) -> None:
        self.requests = 0

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests += 1
        raise httpx.ReadTimeout("response lost", request=request)


class _RotatingProvider:
    """第一次给合法凭证，之后给含 LF 的非法凭证。"""

    def __init__(self) -> None:
        self.calls = 0

    def authorization(self) -> str | None:
        self.calls += 1
        if self.calls == 1:
            return "Bearer token"
        return f"Bearer {SECRET}\ndef"


def _assert_error(error: BaseException, *, forbidden: str) -> None:
    assert isinstance(error, LocalRequestError)
    assert isinstance(error, ClientError)
    assert protocol_module.outcome_unknown(error) is False
    assert error.retryable is False
    assert forbidden not in str(error)


@pytest.mark.parametrize("host", INVALID_HOSTS)
async def test_invalid_xn_host_is_a_local_request_error_for_reads(host: str) -> None:
    provider = CountingProvider("Bearer token")
    transport = _CountingTransport()
    client = DpeClient(f"http://{host}/r", credential=provider, transport=transport)
    try:
        with pytest.raises(LocalRequestError) as caught:
            client.head("s3://bucket/a.md")
    finally:
        client.close()
    _assert_error(caught.value, forbidden=host)
    assert caught.value.__cause__ is not None  # 库的原始错误只经 cause 保留
    assert provider.calls == 1  # 只取一次凭证：没有重发
    assert transport.requests == 0


@pytest.mark.parametrize("host", INVALID_HOSTS)
async def test_invalid_xn_host_is_a_local_request_error_for_writes(host: str) -> None:
    """写操作经 ``drive`` 直接驱动核心的 delete：确定未发出，不得按响应丢失重发。"""
    provider = CountingProvider("Bearer token")
    transport = _CountingTransport()
    client = DpeClient(f"http://{host}/r", credential=provider, transport=transport)
    core = ProtocolCore(caps())
    try:
        with pytest.raises(LocalRequestError) as caught:
            drive(core.delete("s3://bucket/a.md", H1), client.send)
    finally:
        client.close()
    _assert_error(caught.value, forbidden=host)
    assert provider.calls == 1
    assert transport.requests == 0


async def test_invalid_url_is_a_local_request_error() -> None:
    """URL 本身不可构造（如路径含控制字符）同样是 build 阶段的本地失败（与 Rust 对等）。"""
    provider = CountingProvider("Bearer token")
    transport = _CountingTransport()
    client = DpeClient("http://dpe.test/r", credential=provider, transport=transport)
    request = protocol_module.Request("GET", "capabilities\x00", retry_safe=True)
    try:
        with pytest.raises(LocalRequestError) as caught:
            client.send(request)
    finally:
        client.close()
    _assert_error(caught.value, forbidden="\x00")
    assert caught.value.__cause__ is not None
    assert provider.calls == 1
    assert transport.requests == 0


async def test_valid_alabel_host_still_sends() -> None:
    """remote_url 向量里的合法 A-label 样例照常发出（#207 的判定不得误伤合法主机）。"""
    transport = _CountingTransport()
    client = DpeClient("https://xn--fsqu00a.example/r", transport=transport)
    try:
        assert client.capabilities().protocol == "dpe/1"
    finally:
        client.close()
    assert transport.requests == 1


@pytest.mark.parametrize("value", INVALID_CREDENTIALS)
async def test_invalid_credential_header_value_is_a_local_request_error(value: str) -> None:
    provider = CountingProvider(value)
    transport = _CountingTransport()
    client = DpeClient("http://dpe.test/r", credential=provider, transport=transport)
    try:
        with pytest.raises(LocalRequestError) as caught:
            client.head("s3://bucket/a.md")
    finally:
        client.close()
    _assert_error(caught.value, forbidden=value)
    assert provider.calls == 1
    assert transport.requests == 0


async def test_invalid_credential_header_value_is_not_resent_for_writes() -> None:
    provider = CountingProvider(f"Bearer {SECRET}\ndef")
    transport = _CountingTransport()
    client = DpeClient("http://dpe.test/r", credential=provider, transport=transport)
    core = ProtocolCore(caps())
    try:
        with pytest.raises(LocalRequestError) as caught:
            drive(core.delete("s3://bucket/a.md", H1), client.send)
    finally:
        client.close()
    _assert_error(caught.value, forbidden=SECRET)
    assert provider.calls == 1
    assert transport.requests == 0


@pytest.mark.parametrize("host", INVALID_HOSTS)
async def test_async_client_reports_invalid_xn_host_as_local_request_error(host: str) -> None:
    provider = CountingProvider("Bearer token")
    transport = _CountingAsyncTransport()
    client = AsyncDpeClient(f"http://{host}/r", credential=provider, transport=transport)
    try:
        with pytest.raises(LocalRequestError) as caught:
            await client.head("s3://bucket/a.md")
    finally:
        await client.aclose()
    _assert_error(caught.value, forbidden=host)
    assert provider.calls == 1
    assert transport.requests == 0


async def test_async_client_reports_invalid_credential_as_local_request_error() -> None:
    provider = CountingProvider(f"Bearer {SECRET}\x00def")
    transport = _CountingAsyncTransport()
    client = AsyncDpeClient("http://dpe.test/r", credential=provider, transport=transport)
    try:
        with pytest.raises(LocalRequestError) as caught:
            await client.head("s3://bucket/a.md")
    finally:
        await client.aclose()
    _assert_error(caught.value, forbidden=SECRET)
    assert provider.calls == 1
    assert transport.requests == 0


async def test_async_invalid_credential_is_not_resent_for_writes() -> None:
    provider = CountingProvider(f"Bearer {SECRET}\ndef")
    transport = _CountingAsyncTransport()
    client = AsyncDpeClient("http://dpe.test/r", credential=provider, transport=transport)
    core = ProtocolCore(caps())
    try:
        with pytest.raises(LocalRequestError) as caught:
            await adrive(core.delete("s3://bucket/a.md", H1), client.send)
    finally:
        await client.aclose()
    _assert_error(caught.value, forbidden=SECRET)
    assert provider.calls == 1
    assert transport.requests == 0


async def test_send_stage_local_protocol_error_message_is_redacted() -> None:
    """send 阶段的 ``LocalProtocolError`` 判定不变，但消息不得回显凭证（原错误进 cause）。"""
    provider = CountingProvider(f"Bearer {SECRET}")  # 凭证本身合法，失败发生在 send 阶段
    transport = _LocalProtocolTransport()
    client = DpeClient("http://dpe.test/r", credential=provider, transport=transport)
    try:
        with pytest.raises(httpx.LocalProtocolError) as caught:
            client.head("s3://bucket/a.md")
    finally:
        client.close()
    assert SECRET not in str(caught.value)
    assert isinstance(caught.value.__cause__, httpx.LocalProtocolError)
    assert SECRET in str(caught.value.__cause__)
    assert protocol_module.outcome_unknown(caught.value) is True  # 判定不变：仍按可能已发出
    assert provider.calls == 1
    assert transport.requests == 1


async def test_async_send_stage_local_protocol_error_message_is_redacted() -> None:
    provider = CountingProvider(f"Bearer {SECRET}")
    transport = _LocalProtocolAsyncTransport()
    client = AsyncDpeClient("http://dpe.test/r", credential=provider, transport=transport)
    try:
        with pytest.raises(httpx.LocalProtocolError) as caught:
            await client.head("s3://bucket/a.md")
    finally:
        await client.aclose()
    assert SECRET not in str(caught.value)
    assert isinstance(caught.value.__cause__, httpx.LocalProtocolError)
    assert SECRET in str(caught.value.__cause__)
    assert protocol_module.outcome_unknown(caught.value) is True
    assert provider.calls == 1
    assert transport.requests == 1


async def test_htab_in_credential_is_allowed() -> None:
    """RFC 9110 field-value 允许 HTAB：不得误判为本地失败（请求照常发出）。"""
    provider = CountingProvider("Bearer\ttoken")
    transport = _CountingTransport()
    client = DpeClient("http://dpe.test/r", credential=provider, transport=transport)
    try:
        assert client.capabilities().protocol == "dpe/1"
    finally:
        client.close()
    assert transport.requests == 1


async def test_force_first_attempt_local_failure_is_not_confirmed() -> None:
    """force 首次提交即本地失败：不得进入 head 确认，也不得报「可能已生效」。"""
    provider = CountingProvider("Bearer token")
    transport = _CountingTransport()
    client = DpeClient("http://xn--zz.example/r", credential=provider, transport=transport)
    core = ProtocolCore(caps())
    document = Document.model_validate(text_doc(["hello"]))
    try:
        with pytest.raises(LocalRequestError) as caught:
            drive(core.commit("s3://bucket/a.md", document, Force()), client.send)
    finally:
        client.close()
    _assert_error(caught.value, forbidden="xn--zz.example")
    assert provider.calls == 1
    assert transport.requests == 0


async def test_chunked_upload_local_failure_does_not_resume_or_resend() -> None:
    """分块上传的本地构造失败：不得先发断点查询 HEAD 再重发该块。"""
    provider = CountingProvider(f"Bearer {SECRET}\ndef")
    transport = _CountingTransport()
    client = DpeClient("http://dpe.test/r", credential=provider, transport=transport)
    core = ProtocolCore(
        caps(limits={"max_payload_bytes": 64, "blob_chunk_bytes": 64, "blob_max_bytes": 1024})
    )
    source = BlobSource(
        ref="sha256:" + "0" * 64,
        size=128,
        # 首块请求体必须合法：失败要发生在取请求头（凭证校验）阶段，而不是读源
        read=lambda offset, length: b"\0" * length,
    )
    session = Session(id="session-1", uri="s3://bucket/a.md", expires_at=datetime.now(UTC))
    try:
        with pytest.raises(LocalRequestError) as caught:
            drive(core.upload_blob(session, source), client.send)
    finally:
        client.close()
    _assert_error(caught.value, forbidden=SECRET)
    assert provider.calls == 1
    assert transport.requests == 0


async def test_local_failure_after_an_uncertain_attempt_keeps_outcome_unknown() -> None:
    """首次尝试丢响应、续发时凭证非法：整体仍是「可能已生效」，不能降级为确定未生效。"""
    provider = _RotatingProvider()
    transport = _FirstTimeoutTransport()
    client = DpeClient("http://dpe.test/r", credential=provider, transport=transport)
    core = ProtocolCore(caps())
    try:
        with pytest.raises(LocalRequestError) as caught:
            drive(core.delete("s3://bucket/a.md", H1), client.send)
    finally:
        client.close()
    assert protocol_module.outcome_unknown(caught.value) is True
    assert SECRET not in str(caught.value)
    assert provider.calls == 2
    assert transport.requests == 1
