"""增量推送器（#14，Part 1）：判定表、缓存只作缓存、冲突上报与意图日志。

协议路径经进程内真实参考服务端（``client_helpers.in_process_client``）；``httpx`` transport
层只做故障注入（响应丢失、竞态写入、重读失败、并发计数），不替代承载契约的那一层。sync 与
async 对同一真实 TCP 服务端的结果对照由文件末尾的驱动场景完成。
"""

from __future__ import annotations

import contextlib
import gzip
import json
import threading
from collections.abc import Awaitable, Callable, Mapping, Sequence
from itertools import chain
from typing import Any, Protocol

import anyio
import anyio.to_thread
import dpe_hash
import httpx
import pytest
from client_helpers import PREFIX, REMOTE, ClientRecorder, in_process_client
from dpe_sdk.credentials import StaticCredentialProvider
from dpe_sdk.errors import (
    AlreadyExistsError,
    ForbiddenError,
    MissingContentError,
    NotFoundError,
    PreconditionFailedError,
    RateLimitedError,
)
from dpe_sdk.models import Document
from dpe_sdk.protocol import BaseHash, BlobSource, IfAbsent
from dpe_sdk.pusher import (
    AsyncStatefulPusher,
    MemoryStateStore,
    PushResult,
    StatefulPusher,
    StateSnapshot,
)
from dpe_sdk.testing import PrefixAuthorizer, create_app
from dpe_sdk.transport import AsyncDpeClient, DpeClient
from engine_helpers import make_engine, text_doc
from server_helpers import spawn_server

pytestmark = pytest.mark.anyio

C = dpe_hash.CONTRACT
URI = "dpe://docs/a.md"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _doc(*texts: str) -> Document:
    """一页、每段文本一个 ``NarrativeText`` 元素的文档。"""
    return Document.model_validate(text_doc([*texts]))


def _pages(*pages: Sequence[str]) -> Document:
    """逐页给出文本列表的多页文档。"""
    return Document.model_validate(text_doc(*pages))


def _view(result: PushResult, base: str = "") -> tuple[Any, ...]:
    """回报的可比较视图（sync/async 对照与请求级断言用；异常按类型名与 code 归约）。

    ``base`` 用于对照场景：两次运行用不同的 uri 前缀，比较前去掉它。
    """
    return (
        result.op,
        result.file_uri.removeprefix(base),
        result.status,
        type(result.precondition).__name__,
        getattr(result.precondition, "value", None),
        None
        if result.delta is None
        else (result.delta.added, result.delta.removed, result.delta.retained),
        None if result.remote is None else result.remote.doc_hash,
        None if result.to_uri is None else result.to_uri.removeprefix(base),
        result.side,
        None
        if result.error is None
        else (type(result.error).__name__, getattr(result.error, "code", None)),
        None if result.remote_error is None else type(result.remote_error).__name__,
        result.unknown,
    )


def _decode(headers: Mapping[str, str], body: bytes) -> bytes:
    return gzip.decompress(body) if headers.get("content-encoding") == "gzip" else body


def _commits(recorder: ClientRecorder) -> list[dict[str, Any]]:
    """实际发出的 commit（documents PUT）请求体，按顺序。"""
    return [
        json.loads(_decode(headers, body))
        for request, headers, body in zip(
            recorder.requests, recorder.headers, recorder.bodies, strict=True
        )
        if request.method == "PUT" and request.url.path.endswith("/documents")
    ]


def _traffic(recorder: ClientRecorder) -> list[tuple[str, str, bytes]]:
    """(方法, 路径, 解码后的请求体)：两次运行做请求集合级比较用。"""
    return [
        (request.method, request.url.path, _decode(headers, body))
        for request, headers, body in zip(
            recorder.requests, recorder.headers, recorder.bodies, strict=True
        )
    ]


def _staging(recorder: ClientRecorder) -> list[tuple[str, str]]:
    """暂存上传的 (kind, 路径中的 hash)，按顺序；kind 为 ``page`` 或 ``object``。"""
    out: list[tuple[str, str]] = []
    for request in recorder.requests:
        if request.method != "PUT" or "/staging/" not in request.url.path:
            continue
        segments = request.url.path.split("/")
        out.append(("page" if segments[-2] == "pages" else "object", segments[-1]))
    return out


def _hashes(document: Document) -> tuple[set[str], set[str]]:
    """(页 hash 集合, 元素 content_hash 集合)。"""
    data = document.hashes(C)
    return (
        {page["page_hash"] for page in data["pages"]},
        set(chain.from_iterable(page["elements"] for page in data["pages"])),
    )


async def _state(store: MemoryStateStore) -> StateSnapshot:
    return store.load(REMOTE) or StateSnapshot()


# ---------------------------------------------------------------------- 判定表


async def test_push_creates_then_achieved_without_writes() -> None:
    """首推 created；同内容再推为 achieved（R == N），且**零写入**。"""
    store = MemoryStateStore()
    async with in_process_client() as (client, recorder):
        pusher = AsyncStatefulPusher(client, state=store)
        first = await pusher.push({URI: _doc("hello")})
        assert [(r.status, r.delta is not None) for r in first] == [("created", True)]
        before = len(recorder.requests)
        second = await pusher.push({URI: _doc("hello")})
        assert [r.status for r in second] == ["achieved"]
        assert second[0].remote is not None
        assert second[0].remote.doc_hash == _doc("hello").doc_hash()
        # 第二遍只有筛选读取（POST /heads），没有 documents 写入
        assert [r.method for r in recorder.requests[before:]] == ["POST"]
        assert _commits(recorder)[1:] == []


async def test_push_without_cache_rerun_is_equivalent_and_sends_nothing() -> None:
    """验收①：删除本地缓存后重跑，结果与有缓存时一致，且未变文档不重传（请求集合级断言）。"""
    async with in_process_client() as (client, recorder):
        cached = AsyncStatefulPusher(client, state=MemoryStateStore())
        assert [r.status for r in await cached.push({URI: _doc("hello")})] == ["created"]
        first_traffic_start = len(recorder.requests)
        first_rerun = await cached.push({URI: _doc("hello")})

        # 删缓存：换一个空 store 的推送器（同一客户端、同一 remote）
        uncached = AsyncStatefulPusher(client, state=MemoryStateStore())
        second_rerun = await uncached.push({URI: _doc("hello")})

    assert [_view(r) for r in second_rerun] == [_view(r) for r in first_rerun]
    assert [r.status for r in second_rerun] == ["achieved"]
    # 未变文档不重传：两次重跑各自都没有 documents 写入，且请求序列（方法与路径）相同
    rerun_traffic = _traffic(recorder)[first_traffic_start:]
    assert all(item[0] != "PUT" or not item[1].endswith("/documents") for item in rerun_traffic)
    assert len(rerun_traffic) == 2  # 两次重跑各一个 POST /heads


_PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 4


def _image_doc() -> Document:
    """一页一个 Image 元素的文档，blob 引用 ``_PNG`` 的字节。"""
    element = {"category": "Image", "blob": dpe_hash.blob_ref(_PNG), "mime_type": "image/png"}
    return Document.model_validate(
        {"file_type": "md", "pages": [{"page_metadata": {}, "elements": [element]}]}
    )


def _png_lookup(ref: str) -> BlobSource:
    assert ref == dpe_hash.blob_ref(_PNG)
    return BlobSource.from_bytes(_PNG)


async def test_push_passes_blobs_to_deliver() -> None:
    """``blobs`` 原样交给 ``deliver``：服务端缺的 blob 经它上传后 commit 成功；不给时缺字节由核心
    如实报 ``DPE_MISSING_CONTENT``（不自行解引用任何来源），意图随确定的失败清除。"""
    store = MemoryStateStore()
    async with in_process_client() as (client, recorder):
        pusher = AsyncStatefulPusher(client, state=store)

        missing = await pusher.push({URI: _image_doc()})
        assert [r.status for r in missing] == ["failed"]
        assert isinstance(missing[0].error, MissingContentError)
        assert (await _state(store)).intents == {}

        results = await pusher.push({URI: _image_doc()}, blobs=_png_lookup)
        assert [r.status for r in results] == ["created"]
        uploads = [r for r in recorder.requests if "/blobs/" in r.url.path]
        assert uploads and all(r.url.path.endswith(dpe_hash.blob_ref(_PNG)) for r in uploads)
        assert (await client.head(URI)) == _image_doc().doc_hash()


def test_sync_push_passes_blobs_to_deliver() -> None:
    """同步推送器对真实 TCP 服务端：语义与异步版本相同。"""
    with spawn_server("--prefix", "/r/1") as info, DpeClient(info["url"]) as client:
        pusher = StatefulPusher(client, state=MemoryStateStore())
        results = pusher.push({URI: _image_doc()}, blobs=_png_lookup)
        assert [r.status for r in results] == ["created"]
        assert client.head(URI) == _image_doc().doc_hash()


async def test_mid_page_insert_uploads_only_new_objects() -> None:
    """验收②（北极星 E2E 判据）：文档中间插一页，只上传新页对象与新增元素对象。

    用参考服务端的小 ``max_payload_bytes`` 强制走暂存路径（快路径会把全部对象内联重发，
    判据只对逐层协商有意义）；已有页、已有元素与既有内容一律不重传。
    """
    engine = make_engine(max_payload_bytes=1500)
    doc1 = _pages(["a" * 300, "b" * 300], ["c" * 300], ["d" * 300])
    doc2 = _pages(["a" * 300, "b" * 300], ["NEWPAGE" * 100], ["c" * 300], ["d" * 300])
    async with in_process_client(engine) as (client, recorder):
        pusher = AsyncStatefulPusher(client, state=MemoryStateStore())
        assert [r.status for r in await pusher.push({URI: doc1})] == ["created"]
        assert _staging(recorder), "小限额下首推应走暂存路径"
        # 只留第二遍的流量
        recorder.requests.clear()
        recorder.headers.clear()
        recorder.bodies.clear()

        second = await pusher.push({URI: doc2})

    assert [r.status for r in second] == ["updated"]
    assert second[0].delta is not None
    assert (second[0].delta.added, second[0].delta.removed) == (1, 0)
    old_pages, old_objects = _hashes(doc1)
    new_pages, new_objects = _hashes(doc2)
    assert new_pages - old_pages == {page["page_hash"] for page in doc2.hashes(C)["pages"][1:2]}
    uploaded_pages = {digest for kind, digest in _staging(recorder) if kind == "page"}
    uploaded_objects = {digest for kind, digest in _staging(recorder) if kind == "object"}
    assert uploaded_pages == new_pages - old_pages  # 只有新页
    assert uploaded_objects == new_objects - old_objects  # 只有新增元素
    assert not uploaded_pages & old_pages and not uploaded_objects & old_objects


async def test_old_contract_state_entries_are_treated_as_missing() -> None:
    """契约一致（§7.2）：同步状态中与当前契约不一致的条目按缺失处理，退回以 R 作 base。"""
    store = MemoryStateStore()
    async with in_process_client() as (client, _recorder):
        pusher = AsyncStatefulPusher(client, state=store)
        await pusher.push({URI: _doc("v1")})
        current = await _state(store)
        # 伪造一条旧契约的同步状态：若被当成 L（base=dpe2:…），服务端按值比较必然 412
        stale = "dpe2:" + "9" * 64
        store.save(REMOTE, StateSnapshot(synced={URI: stale}, intents={}))
        # 新建推送器才会从 store 真载入这条旧契约条目（内存态只在构造时载入一次）
        reloaded = AsyncStatefulPusher(client, state=store)

        changed = await reloaded.push({URI: _doc("v2")})

    assert [r.status for r in changed] == ["updated"]
    assert isinstance(changed[0].precondition, BaseHash)
    assert changed[0].precondition.value == current.synced[URI]  # 以读到的 R 作 base，而不是旧条目


# ---------------------------------------------------------------------- 冲突（验收③）


async def test_conflict_from_read_is_reported_without_overwriting() -> None:
    """验收③之一：R ≠ L 时如实上报冲突（带读到的 remote），不发写、不覆盖他人写入。"""
    app = create_app(make_engine(), prefix=PREFIX)
    store = MemoryStateStore()
    async with in_process_client(transport=httpx.ASGITransport(app=app)) as (client, recorder):
        pusher = AsyncStatefulPusher(client, state=store)
        await pusher.push({URI: _doc("v1")})
        # 他人改写（经另一个客户端，模拟并发写入者）
        async with AsyncDpeClient(REMOTE, transport=httpx.ASGITransport(app=app)) as foreign:
            await foreign.commit(URI, _doc("theirs"), BaseHash(_doc("v1").doc_hash()))
            before = len(recorder.requests)

            results = await pusher.push({URI: _doc("v2")})

            assert await foreign.head(URI) == _doc("theirs").doc_hash()  # 未被覆盖

    assert [r.status for r in results] == ["conflict"]
    assert results[0].remote is not None and results[0].remote.doc_hash == _doc("theirs").doc_hash()
    assert results[0].precondition is None and results[0].unknown is False
    # 本轮没有发写
    assert [r.method for r in recorder.requests[before:]] == ["POST"]


class _ForeignWrite(httpx.AsyncBaseTransport):
    """在第一个命中的写入请求发出之前先执行一段外部动作（模拟并发写入者的竞态）。

    ``match`` 缺省命中 commit（``PUT documents``）；delete / move 各自传入自己的谓词。
    """

    def __init__(
        self,
        inner: httpx.AsyncBaseTransport,
        before_commit: Callable[[], Awaitable[None]],
        match: Callable[[httpx.Request], bool] | None = None,
    ) -> None:
        self._inner = inner
        self._before_commit = before_commit
        self._match = match or _is_commit
        self.done = False

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if not self.done and self._match(request):
            self.done = True
            await self._before_commit()
        return await self._inner.handle_async_request(request)


async def test_race_conflict_is_detected_and_reread() -> None:
    """验收③之二：读写之间的竞态（R 读后、写前他人抢先）→ 412 → 重读后如实上报冲突。"""
    app = create_app(make_engine(), prefix=PREFIX)
    store = MemoryStateStore()
    async with (
        in_process_client(transport=httpx.ASGITransport(app=app)) as (client, _),
        AsyncDpeClient(REMOTE, transport=httpx.ASGITransport(app=app)) as foreign,
    ):
        pusher = AsyncStatefulPusher(client, state=store)
        await pusher.push({URI: _doc("v1")})

        async def sneak() -> None:
            await foreign.commit(URI, _doc("theirs"), BaseHash(_doc("v1").doc_hash()))

        guard = _ForeignWrite(httpx.ASGITransport(app=app), sneak)
        async with in_process_client(transport=guard) as (raced_client, recorder):
            racer = AsyncStatefulPusher(raced_client, state=store)
            results = await racer.push({URI: _doc("v2")})

        assert [r.status for r in results] == ["conflict"]
        assert results[0].remote is not None
        assert results[0].remote.doc_hash == _doc("theirs").doc_hash()  # 重读到的
        assert results[0].unknown is False
        assert recorder.requests  # 竞态确实发生了（写请求已发出）
        # 未被覆盖
        assert await foreign.head(URI) == _doc("theirs").doc_hash()


async def test_conflict_when_the_document_was_deleted_by_someone_else() -> None:
    """判定表：有 L 而 R 不存在（他人删除/移走）→ 冲突，不静默复活。"""
    app = create_app(make_engine(), prefix=PREFIX)
    store = MemoryStateStore()
    async with (
        in_process_client(transport=httpx.ASGITransport(app=app)) as (client, _),
        AsyncDpeClient(REMOTE, transport=httpx.ASGITransport(app=app)) as foreign,
    ):
        pusher = AsyncStatefulPusher(client, state=store)
        await pusher.push({URI: _doc("v1")})
        await foreign.delete(URI, _doc("v1").doc_hash())

        results = await pusher.push({URI: _doc("v2")})

        assert [r.status for r in results] == ["conflict"]
        assert results[0].remote is not None
        assert results[0].remote.doc_hash is None  # R 不存在
        assert results[0].precondition is None  # 未发写
        assert await foreign.head(URI) is None  # 未被复活


async def test_expected_base_override() -> None:
    """调用方传入的 expected base 覆盖同步状态；``None`` 表示确认「不存在」。"""
    app = create_app(make_engine(), prefix=PREFIX)
    store = MemoryStateStore()
    async with (
        in_process_client(transport=httpx.ASGITransport(app=app)) as (client, _),
        AsyncDpeClient(REMOTE, transport=httpx.ASGITransport(app=app)) as foreign,
    ):
        pusher = AsyncStatefulPusher(client, state=store)
        await pusher.push({URI: _doc("v1")})
        await foreign.commit(URI, _doc("theirs"), BaseHash(_doc("v1").doc_hash()))

        # 以重读到的值作 base 重提（source_wins 等策略归上层）
        resubmitted = await pusher.push(
            {URI: _doc("v2")}, expected_base={URI: _doc("theirs").doc_hash()}
        )
        assert [r.status for r in resubmitted] == ["updated"]
        assert isinstance(resubmitted[0].precondition, BaseHash)
        assert resubmitted[0].precondition.value == _doc("theirs").doc_hash()

        # 声明「不存在」而已存在 → 冲突（不静默覆盖）
        conflicted = await pusher.push({URI: _doc("v3")}, expected_base={URI: None})
        assert [r.status for r in conflicted] == ["conflict"]

        # 声明「不存在」但内容已相同：「已达成」先于 expected_base 的矛盾判定（§7.2 表第一行）
        achieved = await pusher.push({URI: _doc("v2")}, expected_base={URI: None})
        assert [r.status for r in achieved] == ["achieved"]

        # 声明「不存在」且确实不存在 → if_absent 新建
        fresh_uri = "dpe://docs/fresh.md"
        fresh = await pusher.push({fresh_uri: _doc("v1")}, expected_base={fresh_uri: None})
        assert [r.status for r in fresh] == ["created"]
        assert isinstance(fresh[0].precondition, IfAbsent)


# ---------------------------------------------------------------------- 意图日志


class _DroppedResponses(httpx.AsyncBaseTransport):
    """命中条件的请求照常送达服务端，但响应丢弃（服务端已生效、客户端收不到结果）。"""

    def __init__(
        self, inner: httpx.AsyncBaseTransport, match: Callable[[httpx.Request], bool]
    ) -> None:
        self._inner = inner
        self._match = match
        self.armed = True

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self._inner.handle_async_request(request)
        if self.armed and self._match(request):
            raise httpx.ReadTimeout("响应丢失（服务端已生效）", request=request)
        return response


def _is_commit(request: httpx.Request) -> bool:
    return request.method == "PUT" and request.url.path.endswith("/documents")


def _is_delete(request: httpx.Request) -> bool:
    return request.method == "DELETE" and request.url.path.endswith("/documents")


def _is_move(request: httpx.Request) -> bool:
    return request.method == "POST" and request.url.path.endswith("/move")


async def test_unknown_outcome_keeps_the_intent_and_reconciles_next_round() -> None:
    """意图日志正例（判别点：对账把 L 推进到意图值，而不是误报冲突）。

    v1 成功（L=N1）→ 掉响应地推 v2（服务端已生效 R=N2、unknown、意图保留）→ 恢复后推 v3：
    对账先把 L 推进为 N2，再以 **L** 作 base 更新；若对账分支被删，L=N1 ≠ R=N2 会误报 conflict。
    """
    store = MemoryStateStore()
    app = create_app(make_engine(), prefix=PREFIX)
    dropper = _DroppedResponses(httpx.ASGITransport(app=app), _is_commit)
    async with in_process_client(transport=dropper) as (client, _):
        pusher = AsyncStatefulPusher(client, state=store)
        await pusher.push({URI: _doc("v1")})  # 确认 L = N1

        unknown = await pusher.push({URI: _doc("v2")})  # 服务端已生效、响应丢失
        assert [r.status for r in unknown] == ["failed"]
        assert unknown[0].unknown is True
        state = await _state(store)
        assert state.intents == {URI: _doc("v2").doc_hash()}  # 意图保留
        assert state.synced == {URI: _doc("v1").doc_hash()}  # 仍是旧的确认值

        dropper.armed = False  # 恢复
        reconciled = await pusher.push({URI: _doc("v3")})

    assert [r.status for r in reconciled] == ["updated"]
    assert isinstance(reconciled[0].precondition, BaseHash)
    # 判别点：base 是「掉响应那次提交的 doc_hash」（L 被对账推进后的值）；
    # 若对账分支被删，L=N1 ≠ R=N2 会误报 conflict
    assert reconciled[0].precondition.value == _doc("v2").doc_hash()
    state = await _state(store)
    assert state.intents == {}
    assert state.synced == {URI: _doc("v3").doc_hash()}


class _ConnectFailures(httpx.AsyncBaseTransport):
    """对命中的写入请求一律抛 ``ConnectError``（确定未发出）；``match`` 缺省命中 commit。"""

    def __init__(
        self,
        inner: httpx.AsyncBaseTransport,
        match: Callable[[httpx.Request], bool] | None = None,
    ) -> None:
        self._inner = inner
        self._match = match or _is_commit

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if self._match(request):
            raise httpx.ConnectError("connection refused", request=request)
        return await self._inner.handle_async_request(request)


async def test_not_sent_failures_are_determined_and_clear_the_intent() -> None:
    """**首发**即连接失败（确定未发出）：结果已确定，意图 MUST 清除（§7.2）。"""
    store = MemoryStateStore()
    app = create_app(make_engine(), prefix=PREFIX)
    failures = _ConnectFailures(httpx.ASGITransport(app=app))
    async with in_process_client(transport=failures) as (client, _):
        pusher = AsyncStatefulPusher(client, state=store)
        results = await pusher.push({URI: _doc("v1")})

    assert [r.status for r in results] == ["failed"]
    assert isinstance(results[0].error, httpx.ConnectError)
    assert results[0].unknown is False  # 确定未发出 ≠ 结果未知
    state = await _state(store)
    assert state.intents == {}  # 结果已确定 → 清除
    assert state.synced == {}


class _LostThenFail(httpx.AsyncBaseTransport):
    """首个 commit PUT 照常送达（可能已生效）后丢弃响应；其后的 commit 尝试按 ``after`` 故障处置。

    ``armed=False`` 后恢复正常转发（供下一轮推送用）。
    """

    def __init__(
        self,
        inner: httpx.AsyncBaseTransport,
        after: Callable[[httpx.Request], httpx.Response | Exception],
    ) -> None:
        self._inner = inner
        self._after = after
        self._first = True
        self.armed = True

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if self.armed and _is_commit(request):
            if self._first:
                self._first = False
                await self._inner.handle_async_request(request)  # 送达：服务端可能已生效
                raise httpx.ReadTimeout("首发响应丢失", request=request)
            outcome = self._after(request)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        return await self._inner.handle_async_request(request)


async def test_first_attempt_lost_then_never_sent_keeps_the_intent() -> None:
    """首发丢响应（可能已生效）、其后的重发全建连失败：结果未知 → 意图保留（判据按单次尝试）。

    下一轮按意图对账、以 N1 作 base 写入，不报冲突——这正是 §7.2 意图日志要防的
    「把自己的写入误报为他人改写」。
    """
    store = MemoryStateStore()
    app = create_app(make_engine(), prefix=PREFIX)
    guard = _LostThenFail(
        httpx.ASGITransport(app=app),
        lambda request: httpx.ConnectError("refused", request=request),
    )
    async with in_process_client(transport=guard) as (client, _):
        pusher = AsyncStatefulPusher(client, state=store)
        unknown = await pusher.push({URI: _doc("v1")})

        assert [r.status for r in unknown] == ["failed"]
        assert isinstance(unknown[0].error, httpx.ConnectError)
        assert unknown[0].unknown is True  # 首发可能已生效——不能因最后的建连失败判为确定
        state = await _state(store)
        assert state.intents == {URI: _doc("v1").doc_hash()}  # 判别点：意图保留
        assert state.synced == {}

        guard.armed = False  # 服务恢复
        reconciled = await pusher.push({URI: _doc("v2")})

    assert [r.status for r in reconciled] == ["updated"]  # 不报冲突
    assert isinstance(reconciled[0].precondition, BaseHash)
    assert reconciled[0].precondition.value == _doc("v1").doc_hash()  # 对账后以 N1 作 base
    state = await _state(store)
    assert state.intents == {}
    assert state.synced == {URI: _doc("v2").doc_hash()}


async def test_first_attempt_lost_then_rate_limited_keeps_the_intent() -> None:
    """首发丢响应、重发得到 429 耗尽：普通 DpeError 也按核心标记归类 → 结果未知、意图保留。"""

    def rate_limited(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            json={
                "type": "urn:dpe:error:rate-limited",
                "title": "rate limited",
                "status": 429,
                "code": "DPE_RATE_LIMITED",
                "retryable": True,
            },
            headers={
                "Content-Type": "application/problem+json",
                "DPE-Error-Code": "DPE_RATE_LIMITED",
                "Retry-After": "0",
            },
        )

    store = MemoryStateStore()
    app = create_app(make_engine(), prefix=PREFIX)
    guard = _LostThenFail(httpx.ASGITransport(app=app), rate_limited)
    async with in_process_client(transport=guard) as (client, _):
        pusher = AsyncStatefulPusher(client, state=store)
        results = await pusher.push({URI: _doc("v1")})

    assert [r.status for r in results] == ["failed"]
    assert isinstance(results[0].error, RateLimitedError)
    assert results[0].unknown is True  # 首发可能已生效 → 不能按「服务端明确裁决」清意图
    state = await _state(store)
    assert state.intents == {URI: _doc("v1").doc_hash()}


async def test_determined_conflict_clears_the_intent_so_later_writes_are_not_masked() -> None:
    """意图日志反例（判别点）：结果确定的冲突清除意图后，他人写下「同值」必须走正常判定表。

    若冲突后残留意图，下一轮会把他人写下的同一内容误认为自身写入并以它作 base 静默覆盖；
    正确行为是如实上报冲突。
    """
    attempted = _doc("v2")
    app = create_app(make_engine(), prefix=PREFIX)
    foreign = AsyncDpeClient(REMOTE, transport=httpx.ASGITransport(app=app))
    store = MemoryStateStore()
    async with in_process_client(transport=httpx.ASGITransport(app=app)) as (client, _):
        pusher = AsyncStatefulPusher(client, state=store)
        await pusher.push({URI: _doc("v1")})

        # 竞态：我们的 v2 写入被他人抢先（确定冲突，意图清除）
        async def sneak() -> None:
            await foreign.commit(URI, _doc("theirs"), BaseHash(_doc("v1").doc_hash()))

        guard = _ForeignWrite(httpx.ASGITransport(app=app), sneak)
        async with in_process_client(transport=guard) as (raced_client, _):
            raced = AsyncStatefulPusher(raced_client, state=store)
            assert [r.status for r in await raced.push({URI: attempted})] == ["conflict"]
        state = await _state(store)
        assert state.intents == {}  # 结果确定 → 清除

        # 他人随后写下的内容与我们的 v2 逐字节相同（同值）
        await foreign.commit(URI, attempted, BaseHash(_doc("theirs").doc_hash()))

        # 本地再改（v3）：必须走正常判定表 —— L = v1、R = v2 → 冲突，而不是误认自身写入
        final = await pusher.push({URI: _doc("v3")})

        assert [r.status for r in final] == ["conflict"]
        assert final[0].remote is not None and final[0].remote.doc_hash == attempted.doc_hash()
        assert await foreign.head(URI) == attempted.doc_hash()  # 未被静默覆盖


# ---------------------------------------------------------------------- 错误路径与并发


async def test_forbidden_failure_clears_the_intent() -> None:
    engine = make_engine(
        authorizer=PrefixAuthorizer({"Bearer full": ("dpe://allowed/",)}, force=frozenset())
    )
    async with in_process_client(engine, credential=StaticCredentialProvider("Bearer full")) as (
        client,
        _,
    ):
        store = MemoryStateStore()
        pusher = AsyncStatefulPusher(client, state=store)
        denied_uri = "dpe://docs/denied.md"
        results = await pusher.push({denied_uri: _doc("v1")})

        assert [r.status for r in results] == ["failed"]
        assert isinstance(results[0].error, ForbiddenError)
        assert results[0].unknown is False
        state = await _state(store)
        assert state.intents == {}  # 结果确定（服务端已裁决）→ 清除


async def test_reread_failure_is_reported_without_masking_the_conflict() -> None:
    """冲突后的重读失败：仍是 conflict（服务端已裁决），remote 未读到、error 为裁决它的
    CAS 错误、remote_error 为重读异常。"""
    app = create_app(make_engine(), prefix=PREFIX)

    class _FailReRead(httpx.AsyncBaseTransport):
        def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
            self._inner = inner
            self.armed = False

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            if self.armed and request.method == "HEAD":
                raise httpx.ReadTimeout("重读失败", request=request)
            response = await self._inner.handle_async_request(request)
            if response.status_code == 412:
                self.armed = True  # 之后的重读一律失败
            return response

    foreign = AsyncDpeClient(REMOTE, transport=httpx.ASGITransport(app=app))
    async with in_process_client(transport=httpx.ASGITransport(app=app)) as (client, _):
        store = MemoryStateStore()
        pusher = AsyncStatefulPusher(client, state=store)
        await pusher.push({URI: _doc("v1")})

        async def sneak() -> None:
            await foreign.commit(URI, _doc("theirs"), BaseHash(_doc("v1").doc_hash()))

        guard = _FailReRead(_ForeignWrite(httpx.ASGITransport(app=app), sneak))
        async with in_process_client(transport=guard) as (guarded_client, _):
            racer = AsyncStatefulPusher(guarded_client, state=store)
            results = await racer.push({URI: _doc("v2")})

    assert [r.status for r in results] == ["conflict"]
    assert results[0].remote is None  # 未读到
    assert isinstance(results[0].error, PreconditionFailedError)  # 裁决冲突的 CAS 错误不丢
    assert isinstance(results[0].remote_error, httpx.ReadTimeout)  # 重读失败单独带出
    assert results[0].unknown is False


class _InFlight(httpx.AsyncBaseTransport):
    """统计并发在飞的请求数（记录峰值）。"""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner
        self.peak = 0
        self._now = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self._now += 1
        self.peak = max(self.peak, self._now)
        try:
            return await self._inner.handle_async_request(request)
        finally:
            self._now -= 1


async def test_concurrency_is_bounded_and_order_is_preserved() -> None:
    docs = {f"dpe://docs/{index}.md": _doc(f"doc-{index}") for index in range(6)}
    tracker = _InFlight(httpx.ASGITransport(app=create_app(make_engine(), prefix=PREFIX)))
    async with in_process_client(transport=tracker) as (client, _):
        pusher = AsyncStatefulPusher(client, state=MemoryStateStore(), concurrency=4)
        results = await pusher.push(docs)
    assert [r.status for r in results] == ["created"] * 6
    assert [r.file_uri for r in results] == list(docs)  # 与输入同序
    assert tracker.peak <= 4

    # 判别对照：concurrency=1 时峰值必须恰为 1
    serial_tracker = _InFlight(httpx.ASGITransport(app=create_app(make_engine(), prefix=PREFIX)))
    async with in_process_client(transport=serial_tracker) as (client, _):
        pusher = AsyncStatefulPusher(client, state=MemoryStateStore(), concurrency=1)
        await pusher.push(docs)
    assert serial_tracker.peak == 1


class _SyncInFlight(httpx.BaseTransport):
    """同步侧的并发计数（记录峰值）：``StatefulPusher`` 的多写线程池路径用。"""

    def __init__(self, inner: httpx.BaseTransport) -> None:
        self._inner = inner
        self.peak = 0
        self._now = 0
        self._lock = threading.Lock()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        # 线程池并发调用：计数加锁（异步版 _InFlight 由单事件循环串行执行，不需要）
        with self._lock:
            self._now += 1
            self.peak = max(self.peak, self._now)
        try:
            return self._inner.handle_request(request)
        finally:
            with self._lock:
                self._now -= 1


async def test_sync_pusher_multiple_writes_go_through_the_pool_in_order() -> None:
    """sync 推送器的多写分支（线程池）对真实服务端跑通：结果与输入同序、并发不超上限。"""
    docs = {f"dpe://pool/{index}.md": _doc(f"doc-{index}") for index in range(6)}
    with spawn_server("--prefix", "/r/1") as info:
        tracker = _SyncInFlight(httpx.HTTPTransport())
        with DpeClient(info["url"], transport=tracker) as client:
            pusher = StatefulPusher(client, state=MemoryStateStore(), concurrency=4)
            results = pusher.push(docs)
    assert [r.status for r in results] == ["created"] * 6
    assert [r.file_uri for r in results] == list(docs)  # 与输入同序
    assert tracker.peak <= 4


async def test_local_validation() -> None:
    async with in_process_client() as (client, _):
        pusher = AsyncStatefulPusher(client, state=MemoryStateStore())
        with pytest.raises(ValueError):
            await pusher.push({"relative": _doc("x")})  # 非合法 file_uri（core §1.1）
        with pytest.raises(ValueError, match="重复"):
            # 归一化后相同（scheme / host 大小写）
            await pusher.push({"dpe://docs/a.md": _doc("x"), "DPE://DOCS/a.md": _doc("y")})
        with pytest.raises(ValueError, match="expected_base"):
            await pusher.push({URI: _doc("x")}, expected_base={"dpe://docs/other.md": None})
        with pytest.raises(ValueError, match="契约"):
            await pusher.push({URI: _doc("x")}, expected_base={URI: "dpe9:" + "0" * 64})
        with pytest.raises(TypeError):
            await pusher.push({URI: "not a document"})  # type: ignore[dict-item]
        assert await pusher.push({}) == []

        with pytest.raises(TypeError, match="序列"):
            await pusher.delete(URI)  # 单个字符串不是序列
        with pytest.raises(ValueError, match="重复"):
            await pusher.delete([URI, "DPE://DOCS/a.md"])
        with pytest.raises(ValueError, match="同一篇"):
            await pusher.move([(URI, "DPE://DOCS/a.md")])
        b, c = "dpe://docs/b.md", "dpe://docs/c.md"
        for overlapping in (
            [(URI, b), ("DPE://DOCS/b.md", c)],  # 链式（归一化后 b 同时是目标与源）
            [(URI, c), (b, c)],  # 共用目标
            [(URI, b), ("dpe://DOCS/a.md", c)],  # 重复源
        ):
            with pytest.raises(ValueError, match="保序拆批"):
                await pusher.move(overlapping)
        with pytest.raises(TypeError, match="二元组"):
            await pusher.move([(URI, "dpe://docs/b.md", "extra")])  # type: ignore[list-item]
        with pytest.raises(ValueError, match="expected_base"):
            await pusher.delete([URI], expected_base={"dpe://docs/other.md": None})
        with pytest.raises(ValueError, match="expected_base"):
            await pusher.move(
                [(URI, "dpe://docs/b.md")], expected_base={"dpe://docs/other.md": None}
            )
        assert await pusher.delete([]) == []
        assert await pusher.move([]) == []
    with DpeClient(REMOTE) as client, pytest.raises(ValueError, match="concurrency"):
        StatefulPusher(client, concurrency=0)


# ---------------------------------------------------------------------- 删除（Part 2）


async def test_delete_success_removes_the_entry() -> None:
    """删除成功：记 ``deleted``、删条目；同内容再推送按「无 L」退化（if_absent 新建）。"""
    store = MemoryStateStore()
    app = create_app(make_engine(), prefix=PREFIX)
    async with (
        in_process_client(transport=httpx.ASGITransport(app=app)) as (client, recorder),
        AsyncDpeClient(REMOTE, transport=httpx.ASGITransport(app=app)) as foreign,
    ):
        pusher = AsyncStatefulPusher(client, state=store)
        await pusher.push({URI: _doc("v1")})
        before = len(recorder.requests)

        results = await pusher.delete([URI])

        assert [r.status for r in results] == ["deleted"]
        assert isinstance(results[0].precondition, BaseHash)
        state = await _state(store)
        assert state.synced == {} and state.intents == {}
        assert await foreign.head(URI) is None
        assert sum(1 for r in recorder.requests[before:] if r.method == "DELETE") == 1

        again = await pusher.push({URI: _doc("v1")})  # 缓存已删：无 L、R 不存在 → if_absent
    assert [r.status for r in again] == ["created"]


async def test_delete_already_absent_is_achieved_without_writes() -> None:
    """R 不存在＝目标状态已达成（``deleted``），零写入。"""
    async with in_process_client() as (client, recorder):
        pusher = AsyncStatefulPusher(client, state=MemoryStateStore())
        results = await pusher.delete(["dpe://docs/never.md"])

    assert [r.status for r in results] == ["deleted"]
    assert results[0].remote is not None and results[0].remote.doc_hash is None
    assert results[0].precondition is None
    assert [r.method for r in recorder.requests] == ["GET", "POST"]  # 能力 + 筛选读取，无写


async def test_delete_conflict_is_reported_without_writing() -> None:
    """L ≠ R（他人改写）→ ``conflict``（带读到的 remote），不发写、不删他人内容。"""
    store = MemoryStateStore()
    app = create_app(make_engine(), prefix=PREFIX)
    async with (
        in_process_client(transport=httpx.ASGITransport(app=app)) as (client, recorder),
        AsyncDpeClient(REMOTE, transport=httpx.ASGITransport(app=app)) as foreign,
    ):
        pusher = AsyncStatefulPusher(client, state=store)
        await pusher.push({URI: _doc("v1")})
        await foreign.commit(URI, _doc("theirs"), BaseHash(_doc("v1").doc_hash()))
        before = len(recorder.requests)

        results = await pusher.delete([URI])

        assert [r.status for r in results] == ["conflict"]
        assert results[0].remote is not None
        assert results[0].remote.doc_hash == _doc("theirs").doc_hash()
        assert results[0].precondition is None
        assert all(r.method != "DELETE" for r in recorder.requests[before:])
        assert await foreign.head(URI) == _doc("theirs").doc_hash()  # 未被删


async def test_delete_race_conflict_is_detected_and_reread() -> None:
    """竞态：读后、写前他人抢先改写 → 412 → 重读后如实上报冲突（source_wins 归上层）。"""
    app = create_app(make_engine(), prefix=PREFIX)
    store = MemoryStateStore()
    async with (
        in_process_client(transport=httpx.ASGITransport(app=app)) as (client, _),
        AsyncDpeClient(REMOTE, transport=httpx.ASGITransport(app=app)) as foreign,
    ):
        pusher = AsyncStatefulPusher(client, state=store)
        await pusher.push({URI: _doc("v1")})

        async def sneak() -> None:
            await foreign.commit(URI, _doc("theirs"), BaseHash(_doc("v1").doc_hash()))

        guard = _ForeignWrite(httpx.ASGITransport(app=app), sneak, match=_is_delete)
        async with in_process_client(transport=guard) as (raced_client, _):
            racer = AsyncStatefulPusher(raced_client, state=store)
            results = await racer.delete([URI])

        assert [r.status for r in results] == ["conflict"]
        assert isinstance(results[0].error, PreconditionFailedError)
        assert results[0].remote is not None
        assert results[0].remote.doc_hash == _doc("theirs").doc_hash()
        assert await foreign.head(URI) == _doc("theirs").doc_hash()


async def test_delete_race_deleted_first_is_achieved() -> None:
    """竞态：读后、写前他人先删 → 服务端裁决「不存在」＝目标状态已达成（``deleted``）。"""
    app = create_app(make_engine(), prefix=PREFIX)
    store = MemoryStateStore()
    async with (
        in_process_client(transport=httpx.ASGITransport(app=app)) as (client, _),
        AsyncDpeClient(REMOTE, transport=httpx.ASGITransport(app=app)) as foreign,
    ):
        pusher = AsyncStatefulPusher(client, state=store)
        await pusher.push({URI: _doc("v1")})

        async def sweep() -> None:
            await foreign.delete(URI, _doc("v1").doc_hash())

        guard = _ForeignWrite(httpx.ASGITransport(app=app), sweep, match=_is_delete)
        async with in_process_client(transport=guard) as (raced_client, recorder):
            racer = AsyncStatefulPusher(raced_client, state=store)
            results = await racer.delete([URI])

        assert [r.status for r in results] == ["deleted"]
        assert isinstance(results[0].error, NotFoundError)  # 服务端原样的裁决
        assert results[0].unknown is False
        assert sum(1 for r in recorder.requests if _is_delete(r)) == 1
        assert await foreign.head(URI) is None
    state = await _state(store)
    assert state.synced == {} and state.intents == {}  # 已达成：条目删除


async def test_delete_expected_base_override() -> None:
    """expected_base 覆盖 L：以重读值重删成功；声明「不存在」而存在 → conflict。"""
    app = create_app(make_engine(), prefix=PREFIX)
    store = MemoryStateStore()
    async with (
        in_process_client(transport=httpx.ASGITransport(app=app)) as (client, _),
        AsyncDpeClient(REMOTE, transport=httpx.ASGITransport(app=app)) as foreign,
    ):
        pusher = AsyncStatefulPusher(client, state=store)
        await pusher.push({URI: _doc("v1")})
        await foreign.commit(URI, _doc("theirs"), BaseHash(_doc("v1").doc_hash()))

        forced = await pusher.delete([URI], expected_base={URI: _doc("theirs").doc_hash()})
        assert [r.status for r in forced] == ["deleted"]
        assert await foreign.head(URI) is None

        await pusher.push({URI: _doc("v9")})
        conflicted = await pusher.delete([URI], expected_base={URI: None})
        assert [r.status for r in conflicted] == ["conflict"]
        fresh = "dpe://docs/fresh-delete.md"
        gone = await pusher.delete([fresh], expected_base={fresh: None})
        assert [r.status for r in gone] == ["deleted"]


async def test_delete_intent_reconciles_after_lost_response() -> None:
    """delete 响应丢失：意图 ``{uri: None}`` 保留；下一轮 R 不存在 → 对账为自身写入 → deleted。"""
    store = MemoryStateStore()
    app = create_app(make_engine(), prefix=PREFIX)
    dropper = _DroppedResponses(httpx.ASGITransport(app=app), _is_delete)
    async with in_process_client(transport=dropper) as (client, _):
        pusher = AsyncStatefulPusher(client, state=store)
        await pusher.push({URI: _doc("v1")})

        unknown = await pusher.delete([URI])
        assert [r.status for r in unknown] == ["failed"]
        assert unknown[0].unknown is True
        state = await _state(store)
        assert state.intents == {URI: None}  # 意图保留：预期「不存在」
        assert state.synced == {URI: _doc("v1").doc_hash()}  # 未确认

        dropper.armed = False
        reconciled = await pusher.delete([URI])

    assert [r.status for r in reconciled] == ["deleted"]
    state = await _state(store)
    assert state.intents == {} and state.synced == {}


async def test_delete_not_sent_failure_clears_the_intent() -> None:
    """delete 首发即「确定未发出」：结果已确定（未生效）→ 意图清除。"""
    store = MemoryStateStore()
    app = create_app(make_engine(), prefix=PREFIX)
    async with in_process_client(transport=httpx.ASGITransport(app=app)) as (client, _):
        pusher = AsyncStatefulPusher(client, state=store)
        await pusher.push({URI: _doc("v1")})

    failures = _ConnectFailures(httpx.ASGITransport(app=app), match=_is_delete)
    async with in_process_client(transport=failures) as (client, _):
        pusher = AsyncStatefulPusher(client, state=store)
        results = await pusher.delete([URI])

    assert [r.status for r in results] == ["failed"]
    assert isinstance(results[0].error, httpx.ConnectError)
    assert results[0].unknown is False
    state = await _state(store)
    assert state.intents == {}
    assert state.synced == {URI: _doc("v1").doc_hash()}  # 未生效：条目保持


async def test_delete_forbidden_failure_is_reported_with_code() -> None:
    """服务端的明确拒绝（409/403 等）按原错误对象上报（含 code），意图清除、条目保持。"""
    engine = make_engine(authorizer=PrefixAuthorizer({"Bearer full": ("dpe://allowed/",)}))
    app = create_app(engine, prefix=PREFIX)
    store = MemoryStateStore()
    async with (
        in_process_client(
            transport=httpx.ASGITransport(app=app),
            credential=StaticCredentialProvider("Bearer full"),
        ) as (client, _),
    ):
        pusher = AsyncStatefulPusher(client, state=store)
        await pusher.push({"dpe://allowed/x.md": _doc("v1")})

    other_app = httpx.ASGITransport(app=app)
    async with in_process_client(
        transport=other_app, credential=StaticCredentialProvider("Bearer other")
    ) as (client, _):
        other = AsyncStatefulPusher(client, state=store)
        results = await other.delete(["dpe://allowed/x.md"])

    assert [r.status for r in results] == ["failed"]
    assert isinstance(results[0].error, ForbiddenError)
    assert results[0].error.code == "DPE_FORBIDDEN"  # 服务端原始错误对象
    state = await _state(store)
    assert state.intents == {}
    assert state.synced == {"dpe://allowed/x.md": _doc("v1").doc_hash()}


# ---------------------------------------------------------------------- 移动（Part 2）


async def test_move_success_migrates_the_entry() -> None:
    """移动成功：记 ``moved``、条目从源迁到目标；对目标再 push 同内容为 achieved（不重传）。"""
    store = MemoryStateStore()
    target = "dpe://docs/moved.md"
    async with in_process_client() as (client, recorder):
        pusher = AsyncStatefulPusher(client, state=store)
        await pusher.push({URI: _doc("v1")})
        before = len(recorder.requests)

        results = await pusher.move([(URI, target)])

        assert [r.status for r in results] == ["moved"]
        assert isinstance(results[0].precondition, BaseHash)
        assert results[0].side is None
        state = await _state(store)
        assert state.synced == {target: _doc("v1").doc_hash()}
        assert state.intents == {}
        posts = [r for r in recorder.requests[before:] if _is_move(r)]
        assert len(posts) == 1
        achieved = await pusher.push({target: _doc("v1")})
    assert [r.status for r in achieved] == ["achieved"]


async def test_move_replay_after_own_success_is_achieved() -> None:
    """自己的 move 成功后重放（同一推送器、同一 store 的新推送器）：迁移后的条目即本实例完成过的
    move，判已达成记 ``moved``，不发写——否则 §6.8 整轮重跑会把它变成永久失败、扣住游标。"""
    store = MemoryStateStore()
    target = "dpe://docs/moved.md"
    async with in_process_client() as (client, recorder):
        pusher = AsyncStatefulPusher(client, state=store)
        await pusher.push({URI: _doc("v1")})
        assert [r.status for r in await pusher.move([(URI, target)])] == ["moved"]

        for replayer in (pusher, AsyncStatefulPusher(client, state=store)):
            before = len(recorder.requests)

            replayed = await replayer.move([(URI, target)])

            assert [r.status for r in replayed] == ["moved"]
            assert replayed[0].precondition is None  # 未发写
            assert all(not _is_move(r) for r in recorder.requests[before:])
            state = await _state(store)
            assert state.synced == {target: _doc("v1").doc_hash()}
            assert state.intents == {}


async def test_move_already_achieved_without_writes() -> None:
    """源已不存在而目标等于 base：已达成，记 ``moved``、迁条目、零写入。"""
    store = MemoryStateStore()
    target = "dpe://docs/elsewhere.md"
    app = create_app(make_engine(), prefix=PREFIX)
    async with (
        in_process_client(transport=httpx.ASGITransport(app=app)) as (client, recorder),
        AsyncDpeClient(REMOTE, transport=httpx.ASGITransport(app=app)) as foreign,
    ):
        pusher = AsyncStatefulPusher(client, state=store)
        await pusher.push({URI: _doc("v1")})
        await foreign.move(URI, target, _doc("v1").doc_hash())  # 他人已完成这次移动
        before = len(recorder.requests)

        results = await pusher.move([(URI, target)])

        assert [r.status for r in results] == ["moved"]
        assert results[0].precondition is None  # 未发写
        assert results[0].remote is not None
        assert results[0].remote.doc_hash is None  # 源侧读到不存在
        assert all(not _is_move(r) for r in recorder.requests[before:])
        state = await _state(store)
        assert state.synced == {target: _doc("v1").doc_hash()}


async def test_move_source_gone_and_target_differs_is_failed_side_from() -> None:
    """源已不存在、目标存在但内容不同：条目失败（``side="from"``）——不是目标侧被占。"""
    store = MemoryStateStore()
    target = "dpe://docs/occupied.md"
    app = create_app(make_engine(), prefix=PREFIX)
    async with (
        in_process_client(transport=httpx.ASGITransport(app=app)) as (client, recorder),
        AsyncDpeClient(REMOTE, transport=httpx.ASGITransport(app=app)) as foreign,
    ):
        pusher = AsyncStatefulPusher(client, state=store)
        await pusher.push({URI: _doc("v1")})
        await pusher.push({target: _doc("someone-else-content")})
        await foreign.delete(URI, _doc("v1").doc_hash())  # 源被他人删走（未迁到目标）
        before = len(recorder.requests)

        results = await pusher.move([(URI, target)])

        assert [r.status for r in results] == ["failed"]
        assert results[0].side == "from"  # 源已不在，失败原因在源侧
        assert isinstance(results[0].error, NotFoundError)
        assert results[0].error.code == "DPE_NOT_FOUND"
        assert all(not _is_move(r) for r in recorder.requests[before:])  # 未发写


async def test_move_source_rewritten_is_conflict_side_from() -> None:
    """源被改写（L ≠ R）→ ``conflict``、``side="from"``，不发写。"""
    store = MemoryStateStore()
    target = "dpe://docs/target.md"
    app = create_app(make_engine(), prefix=PREFIX)
    async with (
        in_process_client(transport=httpx.ASGITransport(app=app)) as (client, recorder),
        AsyncDpeClient(REMOTE, transport=httpx.ASGITransport(app=app)) as foreign,
    ):
        pusher = AsyncStatefulPusher(client, state=store)
        await pusher.push({URI: _doc("v1")})
        await foreign.commit(URI, _doc("theirs"), BaseHash(_doc("v1").doc_hash()))
        before = len(recorder.requests)

        results = await pusher.move([(URI, target)])

        assert [r.status for r in results] == ["conflict"]
        assert results[0].side == "from"
        assert results[0].remote is not None
        assert results[0].remote.doc_hash == _doc("theirs").doc_hash()
        assert all(not _is_move(r) for r in recorder.requests[before:])


async def test_move_target_occupied_is_failed_side_to() -> None:
    """源未变、目标已被占 → 条目失败、``side="to"``，错误为服务端原样的 ALREADY_EXISTS。"""
    store = MemoryStateStore()
    target = "dpe://docs/occupied.md"
    async with in_process_client() as (client, recorder):
        pusher = AsyncStatefulPusher(client, state=store)
        await pusher.push({URI: _doc("v1")})
        await pusher.push({target: _doc("occupier")})
        before = len(recorder.requests)

        results = await pusher.move([(URI, target)])

        assert [r.status for r in results] == ["failed"]
        assert results[0].side == "to"
        assert isinstance(results[0].error, AlreadyExistsError)
        assert results[0].error.code == "DPE_ALREADY_EXISTS"
        assert results[0].remote is not None  # 重读目标侧
        assert results[0].remote.doc_hash == _doc("occupier").doc_hash()
        assert sum(1 for r in recorder.requests[before:] if _is_move(r)) == 1
        state = await _state(store)  # 条目不变
        assert state.synced == {URI: _doc("v1").doc_hash(), target: _doc("occupier").doc_hash()}
        assert state.intents == {}


async def test_move_onto_target_holding_same_content_deletes_source() -> None:
    """目标已持有与源相同的内容（如失败轮已建出目标，#165）：不发 move（会得 ALREADY_EXISTS、
    重放永不收敛），以 base 为前置条件删除源 → ``moved``，条目迁到目标，意图清空。"""
    store = MemoryStateStore()
    target = "dpe://docs/same.md"
    app = create_app(make_engine(), prefix=PREFIX)
    async with (
        in_process_client(transport=httpx.ASGITransport(app=app)) as (client, recorder),
        AsyncDpeClient(REMOTE, transport=httpx.ASGITransport(app=app)) as foreign,
    ):
        pusher = AsyncStatefulPusher(client, state=store)
        await pusher.push({URI: _doc("v1"), target: _doc("v1")})
        before = len(recorder.requests)

        results = await pusher.move([(URI, target)])

        assert [r.status for r in results] == ["moved"]
        assert results[0].error is None
        assert results[0].to_uri == target
        assert results[0].remote is not None  # 源侧 R
        assert results[0].remote.doc_hash == _doc("v1").doc_hash()
        assert results[0].precondition == BaseHash(_doc("v1").doc_hash())
        writes = recorder.requests[before:]
        assert not any(_is_move(r) for r in writes)  # 不发 move
        assert sum(1 for r in writes if _is_delete(r)) == 1  # 只删源
        assert await foreign.head(URI) is None
        assert await foreign.head(target) == _doc("v1").doc_hash()  # 目标保留、未被改写
    state = await _state(store)
    assert state.synced == {target: _doc("v1").doc_hash()}
    assert state.intents == {}


async def test_move_onto_same_content_without_sync_state_deletes_source() -> None:
    """同步状态缺失（无 L）时以读到的源 R 为 base：目标同内容同样删源、判 ``moved``。"""
    target = "dpe://docs/same.md"
    app = create_app(make_engine(), prefix=PREFIX)
    async with (
        in_process_client(transport=httpx.ASGITransport(app=app)) as (client, recorder),
        AsyncDpeClient(REMOTE, transport=httpx.ASGITransport(app=app)) as foreign,
    ):
        await AsyncStatefulPusher(client).push({URI: _doc("v1"), target: _doc("v1")})
        store = MemoryStateStore()  # 状态丢失
        before = len(recorder.requests)

        results = await AsyncStatefulPusher(client, state=store).move([(URI, target)])

        assert [r.status for r in results] == ["moved"]
        assert not any(_is_move(r) for r in recorder.requests[before:])
        assert await foreign.head(URI) is None
    state = await _state(store)
    assert state.synced == {target: _doc("v1").doc_hash()}


async def test_move_source_rewritten_onto_target_with_their_content_is_conflict() -> None:
    """源被他人改写（R ≠ L）时，即使目标恰好等于改写后的值，也是冲突（不删源、不发写）：
    删源路径只在源可写入（R == base）时成立（#165）。"""
    store = MemoryStateStore()
    target = "dpe://docs/same.md"
    app = create_app(make_engine(), prefix=PREFIX)
    async with (
        in_process_client(transport=httpx.ASGITransport(app=app)) as (client, recorder),
        AsyncDpeClient(REMOTE, transport=httpx.ASGITransport(app=app)) as foreign,
    ):
        pusher = AsyncStatefulPusher(client, state=store)
        await pusher.push({URI: _doc("v1")})
        await foreign.commit(URI, _doc("theirs"), BaseHash(_doc("v1").doc_hash()))
        await foreign.commit(target, _doc("theirs"), IfAbsent())
        before = len(recorder.requests)

        results = await pusher.move([(URI, target)])

        assert [r.status for r in results] == ["conflict"]
        assert results[0].side == "from"
        assert not any(_is_move(r) or _is_delete(r) for r in recorder.requests[before:])


async def test_move_race_conflict_rereads_source() -> None:
    """竞态：源在写前被他人改写 → 412 → 重读源侧 → ``conflict``、``side="from"``。"""
    app = create_app(make_engine(), prefix=PREFIX)
    store = MemoryStateStore()
    target = "dpe://docs/raced.md"
    async with (
        in_process_client(transport=httpx.ASGITransport(app=app)) as (client, _),
        AsyncDpeClient(REMOTE, transport=httpx.ASGITransport(app=app)) as foreign,
    ):
        pusher = AsyncStatefulPusher(client, state=store)
        await pusher.push({URI: _doc("v1")})

        async def sneak() -> None:
            await foreign.commit(URI, _doc("theirs"), BaseHash(_doc("v1").doc_hash()))

        guard = _ForeignWrite(httpx.ASGITransport(app=app), sneak, match=_is_move)
        async with in_process_client(transport=guard) as (raced_client, _):
            racer = AsyncStatefulPusher(raced_client, state=store)
            results = await racer.move([(URI, target)])

        assert [r.status for r in results] == ["conflict"]
        assert results[0].side == "from"
        assert isinstance(results[0].error, PreconditionFailedError)
        assert results[0].remote is not None
        assert results[0].remote.doc_hash == _doc("theirs").doc_hash()


async def test_move_intents_record_both_sides_and_reconcile_after_lost_response() -> None:
    """move 响应丢失：两侧意图一起保留（``from → 不存在``、``to → base``）；下一轮对账判已达成。"""
    store = MemoryStateStore()
    target = "dpe://docs/lost.md"
    app = create_app(make_engine(), prefix=PREFIX)
    dropper = _DroppedResponses(httpx.ASGITransport(app=app), _is_move)
    async with in_process_client(transport=dropper) as (client, recorder):
        pusher = AsyncStatefulPusher(client, state=store)
        await pusher.push({URI: _doc("v1")})

        unknown = await pusher.move([(URI, target)])
        assert [r.status for r in unknown] == ["failed"]
        assert unknown[0].unknown is True
        state = await _state(store)
        assert state.intents == {URI: None, target: _doc("v1").doc_hash()}  # 两侧一起记
        assert state.synced == {URI: _doc("v1").doc_hash()}  # 未确认

        dropper.armed = False
        before = len(recorder.requests)
        reconciled = await pusher.move([(URI, target)])

    assert [r.status for r in reconciled] == ["moved"]  # 对账后判已达成，不是冲突/失败
    assert all(not _is_move(r) for r in recorder.requests[before:])  # 零写入
    state = await _state(store)
    assert state.intents == {}
    assert state.synced == {target: _doc("v1").doc_hash()}


async def test_move_expected_base_override() -> None:
    """expected_base 以 from_uri 为键：值覆盖 L；``None``（确认源不存在）无 base 可核 → failed。"""
    store = MemoryStateStore()
    target = "dpe://docs/target.md"
    app = create_app(make_engine(), prefix=PREFIX)
    async with (
        in_process_client(transport=httpx.ASGITransport(app=app)) as (client, _),
        AsyncDpeClient(REMOTE, transport=httpx.ASGITransport(app=app)) as foreign,
    ):
        pusher = AsyncStatefulPusher(client, state=store)
        await pusher.push({URI: _doc("v1")})
        await foreign.commit(URI, _doc("theirs"), BaseHash(_doc("v1").doc_hash()))

        forced = await pusher.move([(URI, target)], expected_base={URI: _doc("theirs").doc_hash()})
        assert [r.status for r in forced] == ["moved"]
        assert isinstance(forced[0].precondition, BaseHash)
        assert forced[0].precondition.value == _doc("theirs").doc_hash()

        declared_absent = await pusher.move([(URI, target)], expected_base={URI: None})
        assert [r.status for r in declared_absent] == ["failed"]
        assert declared_absent[0].side == "from"


async def test_move_declared_absent_but_present_is_conflict() -> None:
    """调用方确认源「不存在」而源实际存在：与确认矛盾，判 ``conflict``（同 commit/delete 口径）。"""
    store = MemoryStateStore()
    target = "dpe://docs/target.md"
    async with in_process_client() as (client, recorder):
        pusher = AsyncStatefulPusher(client, state=store)
        await pusher.push({URI: _doc("v1")})
        before = len(recorder.requests)

        results = await pusher.move([(URI, target)], expected_base={URI: None})

        assert [r.status for r in results] == ["conflict"]
        assert results[0].side == "from"
        assert results[0].remote is not None
        assert results[0].remote.doc_hash == _doc("v1").doc_hash()
        assert all(not _is_move(r) for r in recorder.requests[before:])  # 不发写


async def test_move_post_write_not_found_is_failed_side_from() -> None:
    """源在读后、写前被他人删除 → 服务端裁决「源已不存在、目标未达成」→ 条目失败、原错误保留。"""
    app = create_app(make_engine(), prefix=PREFIX)
    store = MemoryStateStore()
    target = "dpe://docs/target.md"
    async with (
        in_process_client(transport=httpx.ASGITransport(app=app)) as (client, _),
        AsyncDpeClient(REMOTE, transport=httpx.ASGITransport(app=app)) as foreign,
    ):
        pusher = AsyncStatefulPusher(client, state=store)
        await pusher.push({URI: _doc("v1")})

        async def sweep() -> None:
            await foreign.delete(URI, _doc("v1").doc_hash())

        guard = _ForeignWrite(httpx.ASGITransport(app=app), sweep, match=_is_move)
        async with in_process_client(transport=guard) as (raced_client, recorder):
            racer = AsyncStatefulPusher(raced_client, state=store)
            results = await racer.move([(URI, target)])

        assert [r.status for r in results] == ["failed"]
        assert results[0].side == "from"
        assert isinstance(results[0].error, NotFoundError)  # 服务端原样的裁决
        assert results[0].error.code == "DPE_NOT_FOUND"
        assert results[0].unknown is False
        assert isinstance(results[0].precondition, BaseHash)  # 发过写（区别于写前判定失败）
        assert sum(1 for r in recorder.requests if _is_move(r)) == 1
        assert results[0].to_uri == target
    state = await _state(store)
    assert state.intents == {}  # 结果确定 → 清除


async def test_move_target_pending_intents_count_only_after_reconcile() -> None:
    """to 侧的残留意图只有经对账确认（R == 意图值）并入 L 后才参与判定：不进入写入路径，
    未对上的不当 base；对上的即为目标侧 L，源不存在且无 L 时据此判已达成。"""
    store = MemoryStateStore()
    target = "dpe://docs/b.md"
    app = create_app(make_engine(), prefix=PREFIX)
    async with (
        in_process_client(transport=httpx.ASGITransport(app=app)) as (client, recorder),
        AsyncDpeClient(REMOTE, transport=httpx.ASGITransport(app=app)) as foreign,
    ):
        # 场景一（假冲突）：源 A 未被本推送器追踪但有远端内容；target 有他次 move 的残留意图
        await foreign.commit(URI, _doc("v1"), IfAbsent())
        residual = _doc("stale").doc_hash()
        store.save(
            REMOTE,
            StateSnapshot(synced={}, intents={"dpe://docs/x.md": None, target: residual}),
        )
        # 新建推送器才会从 store 真载入这些残留意图（内存态只在构造时载入一次）
        reloaded = AsyncStatefulPusher(client, state=store)

        results = await reloaded.move([(URI, target)])

        # 按表：无 L、R 存在 → 以 R 作 base 写入（不是被残留意图带进 conflict）
        assert [r.status for r in results] == ["moved"]
        assert isinstance(results[0].precondition, BaseHash)
        assert results[0].precondition.value == _doc("v1").doc_hash()

        # 场景二（假 moved）：源不存在且无 L，target 的 L 与残留意图都不等于 R(target)——
        # 回退取到的目标侧 L 与 R(target) 不等、未对上的残留意图不当 base，即判失败。
        # 另放一条本轮读不到的待定意图，验证写前判定失败不触碰既有意图。
        ghost = "dpe://docs/ghost.md"
        unrelated = "dpe://docs/unrelated.md"
        stale = {target: _doc("older").doc_hash()}
        store.save(
            REMOTE,
            StateSnapshot(synced=stale, intents={target: residual, unrelated: None}),
        )
        second = await AsyncStatefulPusher(client, state=store).move([(ghost, target)])

        assert [r.status for r in second] == ["failed"]  # 目标未达成：不得报 moved
        assert second[0].side == "from"
        assert isinstance(second[0].error, NotFoundError)
        status = await _state(store)
        assert status.intents == {target: residual, unrelated: None}  # 未对上的意图原样保留
        assert status.synced == stale

        # 场景三：target 的残留意图恰等于 R(target) → 按 §7.2 对账确认为本实例已生效的写入、并入 L；
        # 源不存在且无 L，回退以它作 base → 已达成（moved），不发写，条目落在 target
        store.save(REMOTE, StateSnapshot(synced={}, intents={target: _doc("v1").doc_hash()}))
        before = len(recorder.requests)
        third = await AsyncStatefulPusher(client, state=store).move([(ghost, target)])

        assert [r.status for r in third] == ["moved"]
        assert third[0].precondition is None
        assert all(not _is_move(r) for r in recorder.requests[before:])
        status = await _state(store)
        assert status.intents == {}
        assert status.synced == {target: _doc("v1").doc_hash()}


# ---------------------------------------------------------------------- sync / async 一致性


class _PushDriver(Protocol):
    async def push(
        self,
        documents: Mapping[str, Document],
        *,
        expected_base: Mapping[str, str | None] | None = None,
    ) -> list[PushResult]: ...

    async def delete(
        self,
        uris: Sequence[str],
        *,
        expected_base: Mapping[str, str | None] | None = None,
    ) -> list[PushResult]: ...

    async def move(
        self,
        moves: Sequence[tuple[str, str]],
        *,
        expected_base: Mapping[str, str | None] | None = None,
    ) -> list[PushResult]: ...


class _SyncDriver:
    """把同步推送器包成与 ``AsyncStatefulPusher`` 同签名的协程接口。"""

    def __init__(self, pusher: StatefulPusher) -> None:
        self._pusher = pusher

    async def push(
        self,
        documents: Mapping[str, Document],
        *,
        expected_base: Mapping[str, str | None] | None = None,
    ) -> list[PushResult]:
        return self._pusher.push(documents, expected_base=expected_base)

    async def delete(
        self,
        uris: Sequence[str],
        *,
        expected_base: Mapping[str, str | None] | None = None,
    ) -> list[PushResult]:
        return self._pusher.delete(uris, expected_base=expected_base)

    async def move(
        self,
        moves: Sequence[tuple[str, str]],
        *,
        expected_base: Mapping[str, str | None] | None = None,
    ) -> list[PushResult]:
        return self._pusher.move(moves, expected_base=expected_base)


async def _scenario(
    driver: _PushDriver, store: MemoryStateStore, foreign: AsyncDpeClient, remote: str, base: str
) -> list[Any]:
    uri = f"{base}/a.md"
    out: list[Any] = []
    out.append([_view(r, base) for r in await driver.push({uri: _doc("one")})])
    out.append([_view(r, base) for r in await driver.push({uri: _doc("one")})])  # achieved
    out.append([_view(r, base) for r in await driver.push({uri: _doc("two")})])  # 以 L 作 base
    # 他人改写 → 冲突；再以重读值作 base 重提
    snapshot = store.load(remote)
    assert snapshot is not None
    current = snapshot.synced[uri]
    await foreign.commit(uri, _doc("theirs"), BaseHash(current))
    conflicted = await driver.push({uri: _doc("three")})
    out.append([_view(r, base) for r in conflicted])
    remote_state = conflicted[0].remote
    assert remote_state is not None and remote_state.doc_hash is not None
    out.append(
        [
            _view(r, base)
            for r in await driver.push(
                {uri: _doc("three")}, expected_base={uri: remote_state.doc_hash}
            )
        ]
    )
    # move：成功（迁条目）→ 目标已存在时再 push 同内容应 achieved（证明条目已迁移）
    moved_uri = f"{base}/moved.md"
    out.append([_view(r, base) for r in await driver.move([(uri, moved_uri)])])
    replayed = await driver.move([(uri, moved_uri)])  # 重放自己的 move：已达成、不发写
    assert [(r.status, r.precondition) for r in replayed] == [("moved", None)]
    out.append([_view(r, base) for r in replayed])
    out.append([_view(r, base) for r in await driver.push({moved_uri: _doc("three")})])
    # 目标被占（源未变、目标内容不同）→ 条目失败 side=to
    occupied = f"{base}/occupied.md"
    out.append([_view(r, base) for r in await driver.push({occupied: _doc("occupy")})])
    out.append([_view(r, base) for r in await driver.move([(moved_uri, occupied)])])
    # 目标已持有与源相同的内容（#165）→ 不发 move、以 base 删源 → moved；源删除、目标保留
    same_from, same_to = f"{base}/same-from.md", f"{base}/same-to.md"
    pair = {same_from: _doc("same"), same_to: _doc("same")}
    out.append([_view(r, base) for r in await driver.push(pair)])
    out.append([_view(r, base) for r in await driver.move([(same_from, same_to)])])
    assert await foreign.head(same_from) is None
    assert await foreign.head(same_to) == _doc("same").doc_hash()
    # delete：成功 → 已达成（再删同 URI 零写入）
    out.append([_view(r, base) for r in await driver.delete([moved_uri])])
    out.append([_view(r, base) for r in await driver.delete([moved_uri])])
    return out


async def test_sync_and_async_agree_against_the_same_server() -> None:
    with spawn_server("--prefix", "/r/1") as info:
        url = info["url"]
        async with AsyncDpeClient(url) as foreign:
            sync_store = MemoryStateStore()
            with DpeClient(url) as sync_client:
                sync_results = await _scenario(
                    _SyncDriver(StatefulPusher(sync_client, state=sync_store)),
                    sync_store,
                    foreign,
                    url,
                    "dpe://parity/sync",
                )
            async_store = MemoryStateStore()
            async with AsyncDpeClient(url) as async_client:
                async_results = await _scenario(
                    AsyncStatefulPusher(async_client, state=async_store),
                    async_store,
                    foreign,
                    url,
                    "dpe://parity/async",
                )
    assert sync_results == async_results


# ---------------------------------------------------------------------- 意图存盘失败（#134）


class _FailingStore(MemoryStateStore):
    """``failing`` 为真时 ``save`` 抛 ``OSError``（模拟落盘失败），不改动已存内容。"""

    def __init__(self) -> None:
        super().__init__()
        self.failing = False

    def save(self, remote: str, snapshot: StateSnapshot) -> None:
        if self.failing:
            raise OSError("模拟的状态落盘失败")
        super().save(remote, snapshot)


async def _save_failure_keeps_reconcile_drops_new_intents(
    driver: _PushDriver, store: _FailingStore, foreign: AsyncDpeClient, remote: str, base: str
) -> None:
    """上一轮 v2 结果未知（意图 A→v2 保留）→ 本轮 push(v3)：对账把 L 推进到 v2，持久化 A→v3 时
    ``save`` 失败。本批意图不进入内存、未发写；对账结果（来自真实读到的 R）保留，
    在下次保存时落盘。"""
    uri, other = f"{base}/a.md", f"{base}/b.md"
    store.failing = True
    with pytest.raises(OSError):
        await driver.push({uri: _doc("v3")})
    assert await foreign.head(uri) == _doc("v2").doc_hash()  # 未发写

    store.failing = False
    assert [r.status for r in await driver.push({other: _doc("b")})] == ["created"]
    state = store.load(remote)
    assert state is not None
    assert state.intents == {}  # A→v3 从未进入内存，A→v2 已由对账消费
    assert state.synced == {uri: _doc("v2").doc_hash(), other: _doc("b").doc_hash()}


async def _save_failure_does_not_mask_foreign_same_value_write(
    driver: _PushDriver, store: _FailingStore, foreign: AsyncDpeClient, base: str
) -> None:
    """#134 复现序列：push(v2) 时意图存盘失败 → 他人写下同值 v2 → push(v3) 必须判冲突、不发写。

    若失败的意图残留在内存并随下次保存落盘，R == v2 会被误认为自身写入，以 base=v2 静默覆盖。
    """
    uri = f"{base}/repro.md"
    assert [r.status for r in await driver.push({uri: _doc("v1")})] == ["created"]
    store.failing = True
    with pytest.raises(OSError):
        await driver.push({uri: _doc("v2")})
    store.failing = False
    await foreign.commit(uri, _doc("v2"), BaseHash(_doc("v1").doc_hash()))

    final = await driver.push({uri: _doc("v3")})

    assert [r.status for r in final] == ["conflict"]  # L = v1、R = v2
    assert final[0].precondition is None  # 未发写
    assert await foreign.head(uri) == _doc("v2").doc_hash()  # 未被静默覆盖


async def _save_failure_keeps_both_move_sides_out_of_memory(
    driver: _PushDriver, store: _FailingStore, foreign: AsyncDpeClient, remote: str, base: str
) -> None:
    """move 两侧意图（``from → 不存在``、``to → base``）在写前 ``save`` 失败时都不进入内存、未发写；
    之后的成功保存不会把任一侧写进 store，同一实例重放 move 照常完成。"""
    source, target = f"{base}/from.md", f"{base}/to.md"
    assert [r.status for r in await driver.push({source: _doc("m")})] == ["created"]
    store.failing = True
    with pytest.raises(OSError):
        await driver.move([(source, target)])
    assert await foreign.head(source) == _doc("m").doc_hash()  # 未发写
    assert await foreign.head(target) is None

    store.failing = False
    assert [r.status for r in await driver.push({f"{base}/other.md": _doc("o")})] == ["created"]
    state = store.load(remote)
    assert state is not None and state.intents == {}  # 两侧意图都未落盘
    assert [r.status for r in await driver.move([(source, target)])] == ["moved"]


@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize("scenario", ["reconcile", "repro", "move"])
async def test_intent_save_failure_does_not_leak_into_memory(mode: str, scenario: str) -> None:
    """意图存盘失败（#134）：先存、存成功才生效——同步与异步推送器对同一真实 TCP 服务端。"""
    base = f"dpe://save-failure/{mode}"
    store = _FailingStore()
    with spawn_server("--prefix", "/r/1") as info:
        url = info["url"]
        async with contextlib.AsyncExitStack() as stack:
            foreign = await stack.enter_async_context(AsyncDpeClient(url))
            if scenario == "reconcile":
                # 上一轮：L = v1，v2 的写入结果未知（意图 A→v2 保留），而它实际已生效（R = v2）
                uri = f"{base}/a.md"
                await foreign.commit(uri, _doc("v1"), IfAbsent())
                await foreign.commit(uri, _doc("v2"), BaseHash(_doc("v1").doc_hash()))
                store.save(
                    url,
                    StateSnapshot(
                        synced={uri: _doc("v1").doc_hash()}, intents={uri: _doc("v2").doc_hash()}
                    ),
                )
            driver: _PushDriver
            if mode == "sync":
                client = stack.enter_context(DpeClient(url))
                driver = _SyncDriver(StatefulPusher(client, state=store))
            else:
                async_client = await stack.enter_async_context(AsyncDpeClient(url))
                driver = AsyncStatefulPusher(async_client, state=store)
            if scenario == "reconcile":
                await _save_failure_keeps_reconcile_drops_new_intents(
                    driver, store, foreign, url, base
                )
            elif scenario == "repro":
                await _save_failure_does_not_mask_foreign_same_value_write(
                    driver, store, foreign, base
                )
            else:
                await _save_failure_keeps_both_move_sides_out_of_memory(
                    driver, store, foreign, url, base
                )


# ------------------------------------------------------------ 写后冲突重读即已达成（#140）


class _SyncForeignWrite(httpx.BaseTransport):
    """``_ForeignWrite`` 的同步版本（对真实 TCP 服务端）。"""

    def __init__(self, race: _Race) -> None:
        self._inner = httpx.HTTPTransport()
        self._race = race

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        hit = self._race.enter(request)
        if hit:
            self._race.before()
        response = self._inner.handle_request(request)
        if hit and self._race.after is not None:
            response.read()
            self._race.after()
        return response


class _AsyncForeignWrite(httpx.AsyncBaseTransport):
    """异步版本：外部动作在工作线程执行（它们用同步客户端写入另一个进程中的服务端）。"""

    def __init__(self, race: _Race) -> None:
        self._inner = httpx.AsyncHTTPTransport()
        self._race = race

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        hit = self._race.enter(request)
        if hit:
            await anyio.to_thread.run_sync(self._race.before)
        response = await self._inner.handle_async_request(request)
        if hit and self._race.after is not None:
            await response.aread()
            await anyio.to_thread.run_sync(self._race.after)
        return response


class _Race:
    """一次竞态的脚本：命中的第一个写请求发出前执行 ``before``、响应后执行 ``after``；
    ``cut_reads`` 时此后的 HEAD（冲突后的重读）一律断网。``writes`` 计命中的写请求数。"""

    def __init__(
        self,
        match: Callable[[httpx.Request], bool],
        before: Callable[[], object],
        after: Callable[[], object] | None = None,
        *,
        cut_reads: bool = False,
    ) -> None:
        self.match, self.before, self.after = match, before, after
        self.cut_reads = cut_reads
        self.writes = 0

    def enter(self, request: httpx.Request) -> bool:
        """返回本请求是否是第一个命中的写请求；重读被切断时直接抛传输错误。"""
        if self.cut_reads and self.writes and request.method == "HEAD":
            raise httpx.ConnectError("重读断网", request=request)
        if not self.match(request):
            return False
        self.writes += 1
        return self.writes == 1


def _raced(
    mode: str,
    url: str,
    store: MemoryStateStore,
    race: _Race,
    operation: Callable[[Any], Any],
) -> list[PushResult]:
    """以 ``mode`` 的推送器在注入 ``race`` 的客户端上执行 ``operation(pusher)``。"""
    if mode == "sync":
        with DpeClient(url, transport=_SyncForeignWrite(race)) as client:
            results: list[PushResult] = operation(StatefulPusher(client, state=store))
        return results

    async def run() -> list[PushResult]:
        async with AsyncDpeClient(url, transport=_AsyncForeignWrite(race)) as client:
            done: list[PushResult] = await operation(AsyncStatefulPusher(client, state=store))
            return done

    return anyio.run(run)


def _commit_race(variant: str, foreign: DpeClient, uri: str) -> _Race:
    """commit 写后冲突的三种裁决（412 / 409 / 404），重读之前他人都已写下与我们相同的内容 v2。"""
    v1, theirs, v2 = _doc("v1").doc_hash(), _doc("theirs").doc_hash(), _doc("v2").doc_hash()
    if variant == "precondition":  # L = v1：他人改写 → 412；随后改成 v2
        return _Race(
            _is_commit,
            lambda: foreign.commit(uri, _doc("theirs"), BaseHash(v1)),
            lambda: foreign.commit(uri, _doc("v2"), BaseHash(theirs)),
        )
    if variant == "already_exists":  # 无 L、R 不存在 → if_absent：他人先建 → 409；随后改成 v2
        return _Race(
            _is_commit,
            lambda: foreign.commit(uri, _doc("theirs"), IfAbsent()),
            lambda: foreign.commit(uri, _doc("v2"), BaseHash(theirs)),
        )
    assert variant == "not_found"  # L = v1：他人删除 → 404（不属于已达成）；随后重建为 v2
    del v2
    return _Race(
        _is_commit,
        lambda: foreign.delete(uri, v1),
        lambda: foreign.commit(uri, _doc("v2"), IfAbsent()),
    )


def _run_commit_race(
    mode: str, variant: str, url: str, base: str
) -> tuple[list[PushResult], int, StateSnapshot | None]:
    uri = f"{base}/{variant}.md"
    store = MemoryStateStore()
    with DpeClient(url) as foreign:
        if variant != "already_exists":
            foreign.commit(uri, _doc("v1"), IfAbsent())
            store.save(url, StateSnapshot(synced={uri: _doc("v1").doc_hash()}))
        race = _commit_race(variant, foreign, uri)
        results = _raced(mode, url, store, race, lambda p: p.push({uri: _doc("v2")}))
    return results, race.writes, store.load(url)


def _run_delete_race(
    mode: str, url: str, base: str, *, cut_reads: bool
) -> tuple[list[PushResult], int, StateSnapshot | None]:
    uri = f"{base}/d.md"
    store = MemoryStateStore()
    with DpeClient(url) as foreign:
        foreign.commit(uri, _doc("v1"), IfAbsent())
        store.save(url, StateSnapshot(synced={uri: _doc("v1").doc_hash()}))
        race = _Race(
            _is_delete,
            lambda: foreign.commit(uri, _doc("theirs"), BaseHash(_doc("v1").doc_hash())),
            lambda: foreign.delete(uri, _doc("theirs").doc_hash()),
            cut_reads=cut_reads,
        )
        results = _raced(mode, url, store, race, lambda p: p.delete([uri]))
    return results, race.writes, store.load(url)


def _run_delete_source_race(
    mode: str, variant: str, url: str, base: str
) -> tuple[list[PushResult], list[str | None], StateSnapshot | None, DpeClient]:
    """#165 删源路径的写后竞态：源与目标同为 v1（目标已持有同一内容），删源请求发出前他人
    改写源（412）、删除源（404），或改写后再删除（412、重读源不存在）。返回删除请求的
    ``If-Match`` 以核对 CAS 用的是 base。"""
    source, target = f"{base}/{variant}-src.md", f"{base}/{variant}-dst.md"
    v1, theirs = _doc("v1").doc_hash(), _doc("theirs").doc_hash()
    store = MemoryStateStore()
    if_matches: list[str | None] = []

    def is_delete(request: httpx.Request) -> bool:
        if _is_delete(request):
            if_matches.append(request.headers.get("if-match"))
            return True
        return False

    foreign = DpeClient(url)
    for uri in (source, target):
        foreign.commit(uri, _doc("v1"), IfAbsent())
    store.save(url, StateSnapshot(synced={source: v1, target: v1}))
    if variant == "rewritten":
        race = _Race(is_delete, lambda: foreign.commit(source, _doc("theirs"), BaseHash(v1)))
    elif variant == "gone":
        race = _Race(is_delete, lambda: foreign.delete(source, v1))
    else:
        assert variant == "rewritten_then_gone"
        race = _Race(
            is_delete,
            lambda: foreign.commit(source, _doc("theirs"), BaseHash(v1)),
            lambda: foreign.delete(source, theirs),
        )
    results = _raced(mode, url, store, race, lambda p: p.move([(source, target)]))
    return results, if_matches, store.load(url), foreign


@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize("variant", ["rewritten", "gone", "rewritten_then_gone"])
def test_move_onto_same_content_delete_source_races(mode: str, variant: str) -> None:
    """#165 删源（以 base 为前置条件，仍经 CAS）的写后结局，同步/异步一致：
    - 源被改写（412、重读源存在）→ 冲突、``side="from"``、带重读值，目标不受影响；
    - 源已被删（404）或 412 后重读源不存在 → move 的目标状态已达成 → ``moved``，迁条目，
      原错误一并带上（同 delete 的已达成口径）。"""
    v1, theirs = _doc("v1").doc_hash(), _doc("theirs").doc_hash()
    with spawn_server() as info:
        url = info["url"]
        results, if_matches, state, foreign = _run_delete_source_race(
            mode, variant, url, f"dpe://delete-source/{mode}"
        )
        with foreign:
            target = results[0].to_uri
            assert target is not None
            assert foreign.head(target) == v1  # 目标只读不写
    assert if_matches == [f'"{v1}"']  # 删源的 CAS 用 base
    assert state is not None and state.intents == {}
    if variant == "rewritten":
        assert [r.status for r in results] == ["conflict"]
        assert results[0].side == "from"
        assert isinstance(results[0].error, PreconditionFailedError)
        assert results[0].remote is not None and results[0].remote.doc_hash == theirs
        assert results[0].file_uri in state.synced  # 条目未迁移
    else:
        assert [r.status for r in results] == ["moved"]
        expected = NotFoundError if variant == "gone" else PreconditionFailedError
        assert isinstance(results[0].error, expected)  # 原错误保留，供归因
        assert state.synced == {target: v1}


async def test_move_onto_same_content_delete_source_other_errors_carry_no_side() -> None:
    """删源时 412 / 404 以外的错误按 delete 的常规归类，**不带侧别**——侧别专指 §7.2 的
    move 条目失败，运行器据此判永久失败；结果未知时两侧意图保留，已确定的失败清除意图。"""
    target = "dpe://docs/same.md"
    app = create_app(make_engine(), prefix=PREFIX)
    # 结果未知：删源请求已送达、响应丢失
    store = MemoryStateStore()
    dropper = _DroppedResponses(httpx.ASGITransport(app=app), _is_delete)
    async with in_process_client(transport=dropper) as (client, _):
        pusher = AsyncStatefulPusher(client, state=store)
        dropper.armed = False
        await pusher.push({URI: _doc("v1"), target: _doc("v1")})
        dropper.armed = True
        unknown = await pusher.move([(URI, target)])
    assert [(r.status, r.side, r.unknown) for r in unknown] == [("failed", None, True)]
    state = await _state(store)
    assert state.intents == {URI: None, target: _doc("v1").doc_hash()}  # 两侧意图保留

    # 已确定的失败：删源得 403（服务端裁决，非 CAS）
    store = MemoryStateStore()
    rejecting = _RejectDeletes(httpx.ASGITransport(app=app))
    async with in_process_client(transport=rejecting) as (client, _):
        pusher = AsyncStatefulPusher(client, state=store)
        await pusher.push({"dpe://docs/b.md": _doc("w"), "dpe://docs/c.md": _doc("w")})
        rejected = await pusher.move([("dpe://docs/b.md", "dpe://docs/c.md")])
    assert [(r.status, r.side, r.unknown) for r in rejected] == [("failed", None, False)]
    assert isinstance(rejected[0].error, ForbiddenError)
    assert (await _state(store)).intents == {}


class _RejectDeletes(httpx.AsyncBaseTransport):
    """``DELETE documents`` 一律以 ``403 DPE_FORBIDDEN`` 应答（服务端裁决的已确定失败）。"""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if not _is_delete(request):
            return await self._inner.handle_async_request(request)
        body = {
            "type": "urn:dpe:error:forbidden",
            "title": "forbidden",
            "status": 403,
            "code": "DPE_FORBIDDEN",
            "retryable": False,
        }
        return httpx.Response(
            403,
            headers={
                "Content-Type": "application/problem+json",
                "DPE-Error-Code": "DPE_FORBIDDEN",
                "Vary": "DPE-Hash-Contract",
            },
            content=json.dumps(body).encode(),
        )


@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize("variant", ["precondition", "already_exists", "not_found"])
def test_commit_conflict_whose_reread_equals_the_content_is_achieved(
    mode: str, variant: str
) -> None:
    """§7.2：写后 CAS 冲突（412 / 409 / 不属于已达成的 404）、重读值等于本次内容 → 已达成：
    确认 L = N、清意图、不重提，原 CAS 错误保留。与策略无关，推送器自己判定。

    服务端对「内容已相同」的提交本就判成功（core §5.2），所以冲突来自他人的不同写入，
    而重读之前他人又写下了与我们相同的内容。"""
    with spawn_server("--prefix", "/r/1") as info:
        results, writes, state = _run_commit_race(
            mode, variant, info["url"], f"dpe://reread/{mode}"
        )
    expected_error = {
        "precondition": PreconditionFailedError,
        "already_exists": AlreadyExistsError,
        "not_found": NotFoundError,
    }[variant]
    assert [r.status for r in results] == ["achieved"]
    assert isinstance(results[0].error, expected_error)
    assert results[0].remote is not None and results[0].remote.doc_hash == _doc("v2").doc_hash()
    assert results[0].precondition is not None  # 发过写（写前判定的已达成为 None）
    assert writes == 1  # 没有重提
    assert state is not None and state.intents == {}
    assert list(state.synced.values()) == [_doc("v2").doc_hash()]


@pytest.mark.parametrize("mode", ["sync", "async"])
def test_delete_conflict_whose_reread_is_absent_is_deleted(mode: str) -> None:
    """§7.2：delete 写后 CAS 冲突、重读不存在 → 已达成（删条目、清意图），原错误保留。"""
    with spawn_server("--prefix", "/r/1") as info:
        results, writes, state = _run_delete_race(
            mode, info["url"], f"dpe://reread/{mode}", cut_reads=False
        )
    assert [r.status for r in results] == ["deleted"]
    assert isinstance(results[0].error, PreconditionFailedError)
    assert results[0].remote is not None and results[0].remote.doc_hash is None
    assert writes == 1
    assert state is not None and state.synced == {} and state.intents == {}


@pytest.mark.parametrize("mode", ["sync", "async"])
def test_delete_conflict_with_failed_reread_stays_a_conflict(mode: str) -> None:
    """重读本身失败时不臆测：仍回报 ``conflict``（带重读异常），不删除同步状态条目。"""
    with spawn_server("--prefix", "/r/1") as info:
        results, _, state = _run_delete_race(
            mode, info["url"], f"dpe://reread-cut/{mode}", cut_reads=True
        )
    assert [r.status for r in results] == ["conflict"]
    assert results[0].remote is None and results[0].remote_error is not None
    assert state is not None and state.synced  # 条目保留（未确认任何结果）


def test_reread_achieved_parity_between_sync_and_async() -> None:
    """同一服务端上同一组竞态：同步与异步推送器的回报逐字段一致（对照场景）。"""
    views: dict[str, list[Any]] = {}
    with spawn_server("--prefix", "/r/1") as info:
        for mode in ("sync", "async"):
            base = f"dpe://reread-parity/{mode}"
            out: list[Any] = []
            for variant in ("precondition", "already_exists", "not_found"):
                results, _, _ = _run_commit_race(mode, variant, info["url"], base)
                out.append([_view(r, base) for r in results])
            results, _, _ = _run_delete_race(mode, info["url"], base, cut_reads=False)
            out.append([_view(r, base) for r in results])
            views[mode] = out
    assert views["sync"] == views["async"]
