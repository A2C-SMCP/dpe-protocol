"""httpx 传输适配对真实 TCP 端口的参考服务端（#13）：同步客户端全流程与 sync / async 一致性。

子进程参考服务端（``python -m dpe_sdk.testing``）是真实设施；同步与异步客户端各经真实 HTTP
访问同一实例，跑同一场景并逐项比较结果——这是 #13 的验收：「sync 与 async 两套接口对同一
参考服务端（``dpe_sdk.testing`` 的 ASGI 应用）跑出一致结果」。
"""

from __future__ import annotations

from collections.abc import Awaitable, Iterator
from typing import Any, Protocol

import dpe_hash
import pytest
from dpe_sdk.credentials import StaticCredentialProvider
from dpe_sdk.errors import DpeError, ForbiddenError, PreconditionFailedError
from dpe_sdk.models import Document
from dpe_sdk.protocol import (
    BaseHash,
    BlobSource,
    CommitPrecondition,
    IfAbsent,
    Session,
)
from dpe_sdk.transport import AsyncDpeClient, DpeClient
from dpe_sdk.wire import Capabilities, CommitResult, ListPage, MoveResult, Skeleton
from engine_helpers import text_doc
from protocol_helpers import H1
from server_helpers import spawn_server

pytestmark = pytest.mark.anyio

#: 授权表里的小限额服务端：blob 超过 max_payload_bytes 必须分块，写操作按凭证前缀授权
SERVER_ARGS = (
    "--prefix", "/r/1",
    "--max-payload-bytes", "65536",
    "--blob-chunk-bytes", "16384",
    "--grant", "Bearer limited", "test://scope/",
    "--grant", "Bearer full", "*",
)  # fmt: skip

_FULL = StaticCredentialProvider("Bearer full")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="module")
def server() -> Iterator[str]:
    with spawn_server(*SERVER_ARGS) as info:
        yield info["url"]


def _doc(*texts: str) -> Document:
    """一页、每段文本一个 ``NarrativeText`` 元素的文档（``text_doc`` 的一个页参数）。"""
    return Document.model_validate(text_doc([*texts]))


# ---------------------------------------------------------------------- 同步客户端（真实端口）


async def test_sync_client_full_flow_over_tcp(server: str) -> None:
    uri = "test://sync-flow/round.md"
    with DpeClient(server, credential=_FULL) as client:
        document = _doc("hello", "world")
        created = client.commit(uri, document, IfAbsent())
        assert (created.status, created.doc_hash) == ("created", document.doc_hash())
        assert client.head(uri) == document.doc_hash()
        skeleton = client.get_skeleton(uri)
        # 线上页对象的 elements 是 content_hash 列表（展开视图才内联元素）
        assert skeleton is not None and [len(p.elements) for p in skeleton.pages] == [2]
        with pytest.raises(PreconditionFailedError):
            client.commit(uri, _doc("other"), BaseHash(H1))
        page = client.list_page(prefix="test://sync-flow/")
        assert [e.file_uri for e in page.documents] == [uri]
        client.delete(uri, created.doc_hash)
        assert client.head(uri) is None


async def test_sync_client_uploads_a_large_blob_in_chunks(server: str) -> None:
    """blob 超过 ``max_payload_bytes``：同步客户端经分块与断点续传协议（HTTP 绑定 §4.7）跑通。"""
    data = bytes(range(256)) * 400  # 102400 字节，单块上限 min(blob_chunk_bytes, max_payload_bytes)
    ref = dpe_hash.blob_ref(data)
    document = Document.model_validate(
        {
            "file_type": "png",
            "pages": [{"elements": [{"category": "Image", "blob": ref, "mime_type": "image/png"}]}],
        }
    )
    uri = "test://sync-flow/image.png"
    with DpeClient(server, credential=_FULL) as client:
        result = client.deliver(
            uri, document, IfAbsent(), blobs=lambda _: BlobSource.from_bytes(data)
        )
        assert (result.status, result.doc_hash) == ("created", document.doc_hash())
        assert client.head(uri) == document.doc_hash()


async def test_credential_scoping_over_tcp(server: str) -> None:
    """凭据可插拔：同一 remote 上，不同 ``CredentialProvider`` 得到不同的授权结果（#45 多身份）。"""
    with DpeClient(server, credential=StaticCredentialProvider("Bearer limited")) as client:
        assert client.commit("test://scope/ok.md", _doc("x"), IfAbsent()).status == "created"
        with pytest.raises(ForbiddenError):
            client.commit("test://other/denied.md", _doc("x"), IfAbsent())
    with DpeClient(server, credential=_FULL) as client:
        assert client.commit("test://other/allowed.md", _doc("x"), IfAbsent()).status == "created"
    with (  # 无凭证：不在授权表内
        DpeClient(server) as client,
        pytest.raises(ForbiddenError),
    ):
        client.commit("test://other/anonymous.md", _doc("x"), IfAbsent())


# ---------------------------------------------------------------------- sync / async 一致性


class Driver(Protocol):
    """场景用的最小操作面：``AsyncDpeClient`` 结构上就满足它，同步客户端由 ``_SyncDriver`` 包平。"""

    async def capabilities(self) -> Capabilities: ...

    async def commit(
        self,
        uri: str,
        document: Document,
        precondition: CommitPrecondition,
        *,
        session: Session | None = None,
    ) -> CommitResult: ...

    async def head(self, uri: str) -> str | None: ...

    async def batch_head(self, uris: list[str]) -> list[str | None]: ...

    async def get_skeleton(self, uri: str) -> Skeleton | None: ...

    async def list_page(
        self, prefix: str = "", cursor: str | None = None, limit: int | None = None
    ) -> ListPage: ...

    async def delete(self, uri: str, base_hash: str) -> None: ...

    async def move(self, from_uri: str, to_uri: str, base_hash: str) -> MoveResult: ...


class _SyncDriver:
    """把同步客户端包成与 ``AsyncDpeClient`` 同签名的协程接口（测试进程内直接调用，阻塞可忽略）。"""

    def __init__(self, client: DpeClient) -> None:
        self._client = client

    async def capabilities(self) -> Capabilities:
        return self._client.capabilities()

    async def commit(
        self,
        uri: str,
        document: Document,
        precondition: CommitPrecondition,
        *,
        session: Session | None = None,
    ) -> CommitResult:
        return self._client.commit(uri, document, precondition, session=session)

    async def head(self, uri: str) -> str | None:
        return self._client.head(uri)

    async def batch_head(self, uris: list[str]) -> list[str | None]:
        return self._client.batch_head(uris)

    async def get_skeleton(self, uri: str) -> Skeleton | None:
        return self._client.get_skeleton(uri)

    async def list_page(
        self, prefix: str = "", cursor: str | None = None, limit: int | None = None
    ) -> ListPage:
        return self._client.list_page(prefix, cursor, limit)

    async def delete(self, uri: str, base_hash: str) -> None:
        self._client.delete(uri, base_hash)

    async def move(self, from_uri: str, to_uri: str, base_hash: str) -> MoveResult:
        return self._client.move(from_uri, to_uri, base_hash)


async def _error_of(awaitable: Awaitable[Any]) -> tuple[str, str]:
    try:
        await awaitable
    except DpeError as exc:
        return (type(exc).__name__, exc.code)
    raise AssertionError("期望协议错误，实际成功")


async def _scenario(driver: Driver, base: str) -> list[Any]:
    """同一场景在两套接口上各跑一遍；返回值逐项比较（URI 前缀的差异已归一化）。"""
    out: list[Any] = [await driver.capabilities()]
    created = await driver.commit(f"{base}/a.md", _doc("alpha", "beta"), IfAbsent())
    out.append(created)
    out.append(await driver.head(f"{base}/a.md"))
    out.append(await driver.batch_head([f"{base}/a.md", f"{base}/gone.md"]))
    skeleton = await driver.get_skeleton(f"{base}/a.md")
    assert skeleton is not None
    out.append((skeleton.doc_hash, skeleton.document, skeleton.pages))
    updated = await driver.commit(f"{base}/a.md", _doc("alpha"), BaseHash(created.doc_hash))
    out.append(updated)
    page = await driver.list_page(prefix=f"{base}/")
    out.append([(e.file_uri.removeprefix(base), e.doc_hash) for e in page.documents])
    out.append(
        await _error_of(driver.commit(f"{base}/a.md", _doc("conflict"), BaseHash(created.doc_hash)))
    )
    out.append(await driver.move(f"{base}/a.md", f"{base}/b.md", updated.doc_hash))
    out.append(await driver.head(f"{base}/a.md"))
    out.append(await driver.head(f"{base}/b.md"))
    await driver.delete(f"{base}/b.md", updated.doc_hash)
    out.append(await driver.head(f"{base}/b.md"))
    return out


async def test_sync_and_async_agree_against_the_same_server(server: str) -> None:
    with DpeClient(server, credential=_FULL) as sync_client:
        sync_results = await _scenario(_SyncDriver(sync_client), "test://parity/sync")
    async with AsyncDpeClient(server, credential=_FULL) as async_client:
        async_results = await _scenario(async_client, "test://parity/async")
    assert sync_results == async_results
