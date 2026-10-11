"""增量推送器（#14，Part 1）：判定表、缓存只作缓存、冲突上报与意图日志。

协议路径经进程内真实参考服务端（``client_helpers.in_process_client``）；``httpx`` transport
层只做故障注入（响应丢失、竞态写入、重读失败、并发计数），不替代承载契约的那一层。sync 与
async 对同一真实 TCP 服务端的结果对照由文件末尾的驱动场景完成。
"""

from __future__ import annotations

import gzip
import json
import threading
from collections.abc import Awaitable, Callable, Mapping, Sequence
from itertools import chain
from typing import Any, Protocol

import dpe_hash
import httpx
import pytest
from client_helpers import PREFIX, REMOTE, ClientRecorder, in_process_client
from dpe_sdk.credentials import StaticCredentialProvider
from dpe_sdk.errors import ForbiddenError, PreconditionFailedError, RateLimitedError
from dpe_sdk.models import Document
from dpe_sdk.protocol import BaseHash, IfAbsent
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
        store.save(REMOTE, StateSnapshot(synced={URI: stale}, intents=current.intents))

        changed = await pusher.push({URI: _doc("v2")})

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
    """在第一个 commit 写入发出之前先执行一段外部动作（模拟并发写入者的竞态）。"""

    def __init__(
        self, inner: httpx.AsyncBaseTransport, before_commit: Callable[[], Awaitable[None]]
    ) -> None:
        self._inner = inner
        self._before_commit = before_commit
        self.done = False

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if not self.done and request.method == "PUT" and request.url.path.endswith("/documents"):
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
    """对 commit 写入一律抛 ``ConnectError``（确定未发出）。"""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if _is_commit(request):
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
    with DpeClient(REMOTE) as client, pytest.raises(ValueError, match="concurrency"):
        StatefulPusher(client, concurrency=0)


# ---------------------------------------------------------------------- sync / async 一致性


class _PushDriver(Protocol):
    async def push(
        self,
        documents: Mapping[str, Document],
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
