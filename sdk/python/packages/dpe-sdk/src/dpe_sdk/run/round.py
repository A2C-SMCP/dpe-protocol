"""一轮的编排（connector 契约 §6.4–§6.8、§7.1–§7.3）：拉取、校验、落地、上报。

**拉取与背压**（§6.4）：轮首 ``scan``（有游标时带上）→ 逐批处理 → ``next`` 续批；一批的产出
全部落地后才拉下一批。``scan`` 的错误响应、超时与未知 / 过期 ``next`` 都使本轮作废（轮级）。

**产出项**（§6.5–§6.7）：逐条解析与本地校验（``dpe_sdk.run.items``），分段文档拼接完整后读取
blob（``dpe_sdk.run.blobs``）、换成真实引用，再做文档层校验；任何一步出错都只让该条目失败，
本轮继续处理其余条目。

**保序落地**（§6.4、§6.5）：产出项按流中顺序生效，同一 URI 的多个条目按序后写覆盖，跨 URI
可并发。实现是「波次」：按流中顺序收集落地操作，波内 URI 两两不同（document / delete 看
``file_uri``，move 看 ``from_uri`` 与 ``to_uri``），遇到已在本波出现的 URI 即先落地本波、再开
新波。链式或共用目标的 move 因此落在不同波次，满足推送器「同批 from∪to 不得重复」的约束；
运行器不做集合差分，删除与移动只来自显式条目（§6.5）。

**写入冲突**（§7.2）：推送器只回报单次结果，处置在这里——``conflict`` 时以推送器重读到的
remote 值作 ``expected_base`` 重提（文档已不存在即 ``if_absent`` 复活）；commit 与「源被改写」
的 move 按 ``conflict_policy.commit``，delete 按 ``conflict_policy.delete``。同一条目在本轮内
至多重提 ``conflict_retries`` 次，超过即条目失败（``retryable: true``，§7.1）；``report`` 下的
冲突、move 的目标被占与源已不存在为条目失败（``retryable: false``）。**绝不 force**，失败的 move
不退化为「在目标上 commit、再删除源」。

**远端失败分层**（§7.1）：401 → 实例级 ``remote_unauthorized``；协议或契约不兼容 → 实例级
``remote_incompatible``；结果未知、可重试 DPE 错误重试耗尽、网络失败 → 轮级 ``remote_unavailable``
（本轮作废）；写入、``batch_head`` 与冲突后重读得到的其余确定 DPE 错误 → 条目级（source
``remote``，``code`` 原样；只有不可重试的错误落到条目级，``retryable`` 即 false）。能力获取在
轮首、推送器调用之外完成，它与条目无关：其余确定错误归轮级、code 原样、不可重试（§7.1、
§7.4，#137）。

**本地状态**（#137）：状态目录读写失败是轮级 ``state_unavailable``。本次调用内只要推送器有过
一次保存失败（写前或收尾的 ``save``），本轮即失败、游标不推进（此前没有其他终止失败时，
``fatal`` 即 ``state_unavailable``）——收尾失败时推送器整批报错，运行器拿不到该批的逐条结果，
fail-closed 的代价只是重跑一轮（commit 由内容幂等、意图可对账）。结束前照常用同一推送器实例
有限次补存（``state_retries``，短退避），**补存只为让磁盘与内存一致，成败都不改变本轮结果**；
补存失败单独记一条供观测。游标与收尾记录的写入失败同样使本轮 ``failed``，但它写成功才生效、
内存未变，无需补存。补存成功前进程退出（崩溃或补存耗尽）时，磁盘上可能残留过期意图，下一次
调用会读回——风险与 §7.2 接受的崩溃窗口相同。

**读失败**（§7.1，#137）：``batch_head`` 与冲突后重读得到确定、不可重试的错误时归条目级
（code 原样），本轮继续。``batch_head`` 一次覆盖整波：被确定拒绝时逐条 ``head`` 定位，只给读被
拒的条目记失败，其余条目再以一次推送器调用照常落地；每波至多定位一次，定位不了或收窄后
再次被拒才各记一条原错误。每波首次多条目批量读被拒、定位完成后发出一次
``remote.batch_read_rejected``（warn）事件，只带 ``code`` / ``entries`` / ``probes``（实际发出的
``head`` 次数）/ ``localized``，不带远端文本；收窄后的子集再次被拒不再发。

**游标 fail-closed**（§6.8）：收到结束批、全部条目成功落地才 ``succeeded`` 并持久化新游标
（同步状态与意图由推送器先行落盘，游标最后写，不会先于同步状态前进）；有条目失败为
``partial``，轮级 / 协议级 / 实例级失败为 ``failed``，两者都保留旧游标（§7.3）。
"""

from __future__ import annotations

import asyncio
import copy
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from typing import Any, Literal, TypeVar

import httpx
from dpe_hash import DpeHashError

from dpe_sdk.errors import (
    ClientError,
    ContractUnsupportedError,
    DpeError,
    IncompatibleServerError,
    UnexpectedResponseError,
)
from dpe_sdk.models import Document
from dpe_sdk.pusher import AsyncStatefulPusher, PushResult
from dpe_sdk.run.blobs import BlobStage
from dpe_sdk.run.definition import ConflictPolicy
from dpe_sdk.run.errors import (
    InstanceFailure,
    ItemFailure,
    ProtocolFailure,
    RoundFailure,
    RpcError,
    RunFailure,
)
from dpe_sdk.run.host import PluginHost, RequestTimedOut
from dpe_sdk.run.items import (
    DeleteItem,
    DocumentSegment,
    MoveItem,
    ReadyDocument,
    ScanBatch,
    SegmentAssembler,
    parse_item,
    parse_scan_result,
)
from dpe_sdk.run.state import InstanceStore
from dpe_sdk.transport import AsyncDpeClient

__all__ = [
    "EventSink",
    "LogLevel",
    "Outcome",
    "RoundCounts",
    "RoundOptions",
    "RoundResult",
    "run_round",
    "utc_now",
]

Outcome = Literal["succeeded", "partial", "failed"]
LogLevel = Literal["debug", "info", "warn", "error"]
#: 事件钩子：``(event, level, fields)``，供独立运行器渲染为 NDJSON 日志（§7.5）
EventSink = Callable[[str, LogLevel, dict[str, Any]], None]


def utc_now() -> str:
    """RFC 3339 UTC 时间戳（秒精度，``Z`` 结尾）。"""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class RoundOptions:
    """运行器调优参数（实例定义 ``runner`` 内的键，§4.4）；不改变契约规定的行为。"""

    #: 建议批大小上限（``scan`` 的 ``max_items``）
    max_items: int = 100
    #: 单次 ``scan`` 的超时（秒）
    scan_timeout: float = 300.0
    #: 单次 ``read_blob`` 的超时（秒，§6.7 MUST 有界）
    blob_timeout: float = 60.0
    #: 同一条目在本轮内的冲突重提上限（§7.2 SHOULD ≤ 2）
    conflict_retries: int = 2
    #: 保存失败后的补存次数与退避（秒）
    state_retries: int = 2
    state_retry_delay: float = 0.2


@dataclass
class RoundCounts:
    """§7.4 ``counts``：``unchanged`` 含推送器判定的已达成（R == N）。"""

    created: int = 0
    updated: int = 0
    unchanged: int = 0
    deleted: int = 0
    moved: int = 0
    failed: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "created": self.created,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "deleted": self.deleted,
            "moved": self.moved,
            "failed": self.failed,
        }


@dataclass
class RoundResult:
    """一轮的结果（§7.3、§7.4 的轮报告字段）。"""

    outcome: Outcome
    incremental: bool | None
    cursor_persisted: bool
    cursor_held_since: str | None
    counts: RoundCounts
    started_at: str
    ended_at: str
    #: 全部失败（条目级与终止本轮的那一个），按发生顺序
    failures: list[RunFailure] = field(default_factory=list)
    #: 终止本轮的非条目级失败（``failed`` 时必有）；独立运行器据其层级决定退出码（§7.6）
    fatal: RunFailure | None = None


# ---------------------------------------------------------------------- 远端失败分层


#: 来自 remote 或其传输的异常；其余异常（ValueError、KeyError 等）是运行器 / 推送器自身的
#: 缺陷，原样抛出、以非契约退出码暴露（§7.6），不伪装成可重试的远端故障
_REMOTE_ERRORS = (DpeError, ClientError, httpx.HTTPError, OSError)


def _classify_remote(exc: BaseException, *, unknown: bool) -> RunFailure:
    """把推送器回报或抛出的远端异常归入 §7.1 的层级（不含条目 URI）。"""
    if isinstance(exc, RunFailure):
        return exc  # 状态目录失败（state_unavailable）等已归类的失败
    if not isinstance(exc, _REMOTE_ERRORS):
        raise exc
    if isinstance(exc, IncompatibleServerError | ContractUnsupportedError):
        # remote 不支持运行器可用的协议或 hash 契约：不改动实例就无法恢复（§7.1 实例级）
        return InstanceFailure("remote_incompatible", f"remote 不兼容：{exc}", source="remote")
    if isinstance(exc, UnexpectedResponseError) and exc.status == 401:
        return InstanceFailure(
            "remote_unauthorized", "remote 拒绝凭证（HTTP 401）", source="remote"
        )
    if isinstance(exc, DpeError) and not unknown and not exc.retryable:
        return ItemFailure(exc.code, str(exc), source="remote", retryable=False)
    return RoundFailure(
        "remote_unavailable", f"remote 不可用：{exc}", source="remote", retryable=True
    )


async def _negotiate(client: AsyncDpeClient) -> None:
    """取得 remote 能力（§7.1）：401 / 不兼容为实例级，网络失败与可重试错误耗尽为轮级可重试；
    其余确定错误（如凭证缺权限得 403）与任何条目无关，归轮级、code 原样、不可重试。"""
    try:
        await client.core()
    except Exception as exc:
        failure = _classify_remote(exc, unknown=False)
        if isinstance(failure, ItemFailure):
            raise RoundFailure(
                failure.code, failure.message, source="remote", retryable=False
            ) from None
        raise failure from None


def _cas_code(result: PushResult, fallback: str) -> str:
    """冲突与 move 条目失败的码：有服务端裁决取其原码，写前判定取同义的 DPE 码。"""
    error = result.error
    return error.code if isinstance(error, DpeError) else fallback


# ---------------------------------------------------------------------- 落地波次


@dataclass
class _Wave:
    """一波 URI 两两不同的落地操作（按流中顺序收集）。"""

    commits: dict[str, Document] = field(default_factory=dict)
    deletes: list[str] = field(default_factory=list)
    moves: list[tuple[str, str]] = field(default_factory=list)
    uris: set[str] = field(default_factory=set)

    def __bool__(self) -> bool:
        return bool(self.uris)


_Op = tuple[Literal["commit"], str, Document] | DeleteItem | MoveItem


def _op_uris(op: _Op) -> set[str]:
    if isinstance(op, DeleteItem):
        return {op.file_uri}
    if isinstance(op, MoveItem):
        return {op.from_uri, op.to_uri}
    return {op[1]}


@dataclass(frozen=True)
class _Retry:
    """按 ``source_wins`` 重提：``base`` 为重读到的 remote 值（``None`` 即文档不存在）。"""

    base: str | None


def _file_uri(uri: str) -> dict[str, str | None]:
    return {"file_uri": uri}


def _move_uris(pair: tuple[str, str]) -> dict[str, str | None]:
    return {"from_uri": pair[0], "to_uri": pair[1]}


#: 批量读被拒后逐条定位的并发上限
_PROBE_CONCURRENCY = 8

#: 一次推送器调用的条目键：commit / delete 为 file_uri，move 为 (from_uri, to_uri)
_K = TypeVar("_K", str, tuple[str, str])

#: ``settle`` 的结论：``None`` 为已入账（成功或条目失败）；``RunFailure`` 为终止本轮的失败
_Settled = _Retry | RunFailure | None


class _Round:
    def __init__(
        self,
        *,
        host: PluginHost,
        pusher: AsyncStatefulPusher,
        uri_prefix: str,
        policy: ConflictPolicy,
        blob_max_bytes: int | None,
        options: RoundOptions,
        on_event: EventSink | None,
        redact: Callable[[str], str],
    ) -> None:
        self.host = host
        self.pusher = pusher
        self.prefix = uri_prefix
        self.policy = policy
        self.options = options
        self.on_event = on_event
        self.redact = redact
        self.stage = BlobStage(host, blob_max_bytes=blob_max_bytes, timeout=options.blob_timeout)
        self.assembler = SegmentAssembler()
        self.counts = RoundCounts()
        self.failures: list[RunFailure] = []
        self.item_failed = False
        #: 本次调用内是否有过保存失败（退出前须补存，见模块说明）
        self.save_failed = False

    # -------------------------------------------------------------- 事件与失败

    def emit(self, event: str, level: LogLevel, fields: dict[str, Any]) -> None:
        if self.on_event is not None:
            self.on_event(event, level, fields)

    def record(self, failure: RunFailure) -> None:
        failure.redact(self.redact)
        self.failures.append(failure)
        if isinstance(failure, ItemFailure):
            self.item_failed = True
            self.counts.failed += 1
            self.emit("item.failed", "warn", failure.to_report())
        else:
            self.emit("item.failed", "error", failure.to_report())

    def item_failure(self, failure: RunFailure, **uris: str | None) -> None:
        """记录一条条目级失败；``failure`` 不是条目级（如结果未知）时向上抛出、终止本轮。"""
        if not isinstance(failure, ItemFailure):
            raise failure
        for key, value in uris.items():
            if value is not None and getattr(failure, key) is None:
                setattr(failure, key, value)
        self.record(failure)

    # -------------------------------------------------------------- 拉取

    async def scan(self, params: dict[str, Any], *, continued: bool) -> ScanBatch:
        try:
            raw = await self.host.request("scan", params, timeout=self.options.scan_timeout)
        except RpcError as exc:
            # 未知 / 过期的 next：本轮作废，以原游标重开即可（§6.4），所以可重试
            retryable = True if continued and exc.name == "invalid_params" else exc.retryable
            raise RoundFailure(
                exc.name, f"scan 失败：{exc.message}", source="plugin", retryable=retryable
            ) from None
        except RequestTimedOut:
            raise RoundFailure(
                "timeout", f"scan 在 {self.options.scan_timeout} 秒内未完成", retryable=True
            ) from None
        return parse_scan_result(raw)

    async def run(self, cursor: str | None) -> ScanBatch:
        """跑完整轮的拉取与落地，返回结束批。"""
        params: dict[str, Any] = {"max_items": self.options.max_items}
        if cursor is not None:
            params["cursor"] = cursor
        continued = False
        while True:
            batch = await self.scan(params, continued=continued)
            ops = self.collect(batch)
            await self.land(ops)
            self.stage.close()  # 本批已全部落地：暂存不跨批累积
            if batch.final:
                return batch
            assert batch.next is not None
            params = {"next": batch.next}
            continued = True

    def collect(self, batch: ScanBatch) -> list[_Op | ReadyDocument]:
        """解析一批产出项，按流中顺序给出待落地的操作（失败即时记录）。"""
        ops: list[_Op | ReadyDocument] = []

        def take(events: list[ReadyDocument | ItemFailure]) -> None:
            for event in events:
                if isinstance(event, ItemFailure):
                    self.record(event)
                else:
                    ops.append(event)

        for raw in batch.items:
            item = parse_item(raw, self.prefix)
            if isinstance(item, DocumentSegment):
                take(self.assembler.feed(item))
                continue
            take(self.assembler.interrupt())
            if isinstance(item, ItemFailure):
                self.record(item)
            else:
                ops.append(item)
        if batch.final:
            take(self.assembler.finish())
        return ops

    # -------------------------------------------------------------- 落地

    async def prepare(self, ready: ReadyDocument) -> Document | None:
        """读取 blob、换成真实引用，再做文档层校验（§6.6 第 3–4 步）。"""
        pages = copy.deepcopy(ready.pages)
        for slot in ready.blobs:
            ref = await self.stage.resolve(slot.spec)
            if isinstance(ref, ItemFailure):
                self.item_failure(ref, file_uri=ready.file_uri)
                return None
            pages[slot.page]["elements"][slot.element]["blob"] = ref
        try:
            return Document.model_validate({**ready.document, "pages": pages})
        except DpeHashError as exc:
            self.item_failure(
                ItemFailure(exc.code, str(exc), source="validation"), file_uri=ready.file_uri
            )
            return None

    async def land(self, ops: list[_Op | ReadyDocument]) -> None:
        wave = _Wave()
        for entry in ops:
            op: _Op
            if isinstance(entry, ReadyDocument):
                document = await self.prepare(entry)
                if document is None:
                    continue
                op = ("commit", entry.file_uri, document)
            else:
                op = entry
            uris = _op_uris(op)
            if uris & wave.uris:
                await self.flush_wave(wave)
                wave = _Wave()
            wave.uris |= uris
            if isinstance(op, DeleteItem):
                wave.deletes.append(op.file_uri)
            elif isinstance(op, MoveItem):
                wave.moves.append((op.from_uri, op.to_uri))
            else:
                wave.commits[op[1]] = op[2]
        if wave:
            await self.flush_wave(wave)

    async def call(
        self,
        run: Callable[[list[_K]], Awaitable[list[PushResult]]],
        keys: list[_K],
        uris: Callable[[_K], dict[str, str | None]],
        *,
        localize: bool = True,
    ) -> list[PushResult]:
        """以 ``run(keys)`` 调用推送器。写入的失败都在回报里；能力已在轮首取得，推送器只会因
        ``batch_head`` 或状态保存而抛出，所以按读来归类（``unknown=False``——「写入结果未知」
        对读没有意义）：网络失败与可重试错误耗尽终止本轮（轮级）。

        批量读被确定、不可重试地拒绝时**逐条定位**（§7.1，#137）：对每个条目涉及的 URI 各
        ``head`` 一次（有界并发），只给读被拒的条目记条目级失败，其余条目再以**一次**推送器调用
        照常落地。每波至多定位一次：逐条读都通过（拒绝与具体 URI 无关、定位不了）或收窄后的
        子集再次被拒时，每个条目各记一条原错误。定位只发生在失败路径上，且在任何写入之前完成，
        不会丢失已落地的结果。定位后发 ``remote.batch_read_rejected`` 事件（只带码与计数，
        不带远端文本）。
        """
        try:
            return await run(keys)
        except RunFailure as failure:
            if failure.code == "state_unavailable":
                self.save_failed = True
            raise
        except Exception as exc:
            rejected = _classify_remote(exc, unknown=False)
        if not isinstance(rejected, ItemFailure):
            raise rejected
        # TODO(#145): 推送器在 batch_head 被确定拒绝时自行逐 URI
        # head、把读被拒的条目作为 failed 结果返回（两个 SDK 一起改）之后，删除这里的运行器层
        # 定位（_probe / _record_item 的降级分支），call 随之简化为只处理状态保存与轮级失败。
        if len(keys) > 1 and localize:
            denials, probes = await self._probe_all([uris(key) for key in keys])
            readable = [key for key, denied in zip(keys, denials, strict=True) if denied is None]
            self.emit(
                "remote.batch_read_rejected",
                "warn",
                {
                    "code": rejected.code,
                    "entries": len(keys),
                    "probes": probes,
                    "localized": len(keys) - len(readable),
                },
            )
            if 0 < len(readable) < len(keys):
                for key, denied in zip(keys, denials, strict=True):
                    if denied is not None:
                        self._record_item(denied, uris(key))
                return await self.call(run, readable, uris, localize=False)
            if not readable:
                for key, denied in zip(keys, denials, strict=True):
                    assert denied is not None
                    self._record_item(denied, uris(key))
                return []
        for key in keys:  # 单个条目，或拒绝与具体 URI 无关、定位不了：各记一条原错误
            self._record_item(rejected, uris(key))
        return []

    async def _probe_all(
        self, targets: list[dict[str, str | None]]
    ) -> tuple[list[ItemFailure | None], int]:
        """有界并发地探测每个条目，返回各条目的结论与实际发出的 ``head`` 次数。

        网络类失败终止本轮（此时尚未写入）：第一个失败出现即取消其余探测（``TaskGroup``），
        不留未等待的任务，也不再向不可用的 remote 发请求。探测的结论本身可能是
        ``ItemFailure``（它也是异常类型），所以结论作为返回值、与探测中抛出的异常区分开。
        """
        limit = asyncio.Semaphore(_PROBE_CONCURRENCY)

        async def one(uris: dict[str, str | None]) -> tuple[ItemFailure | None, int]:
            async with limit:
                return await self._probe(uris)

        try:
            async with asyncio.TaskGroup() as group:
                tasks = [group.create_task(one(uris)) for uris in targets]
        except* Exception as failures:
            raise failures.exceptions[0] from None
        outcomes = [task.result() for task in tasks]
        return [denied for denied, _ in outcomes], sum(count for _, count in outcomes)

    async def _probe(self, uris: dict[str, str | None]) -> tuple[ItemFailure | None, int]:
        """逐个 ``head`` 条目涉及的 URI：返回（读被确定拒绝的失败或 ``None``，实际 head 次数）；
        网络类失败上抛。"""
        sent = 0
        for uri in uris.values():
            if uri is None:
                continue
            sent += 1
            try:
                await self.pusher.client.head(uri)
            except Exception as exc:
                failure = _classify_remote(exc, unknown=False)
                if not isinstance(failure, ItemFailure):
                    raise failure from None
                return failure, sent
        return None, sent

    def _record_item(self, failure: ItemFailure, uris: dict[str, str | None]) -> None:
        self.record(
            ItemFailure(
                failure.code,
                failure.message,
                source=failure.source,
                retryable=failure.retryable,
                **uris,
            )
        )

    def _push_subset(
        self, documents: dict[str, Document], bases: dict[str, str | None], subset: list[str]
    ) -> Awaitable[list[PushResult]]:
        return self.pusher.push(
            {uri: documents[uri] for uri in subset},
            expected_base={uri: bases[uri] for uri in subset if uri in bases},
            blobs=self.stage.lookup,
        )

    def _delete_subset(
        self, bases: dict[str, str | None], subset: list[str]
    ) -> Awaitable[list[PushResult]]:
        return self.pusher.delete(
            subset, expected_base={uri: bases[uri] for uri in subset if uri in bases}
        )

    def _move_subset(
        self, bases: dict[str, str | None], subset: list[tuple[str, str]]
    ) -> Awaitable[list[PushResult]]:
        return self.pusher.move(
            subset, expected_base={src: bases[src] for src, _ in subset if src in bases}
        )

    async def flush_wave(self, wave: _Wave) -> None:
        """落地一波；冲突按策略重提，直到全部有结论。本波出现的终止性失败在全部结果入账后抛出。"""
        attempts: dict[str, int] = {}
        commits, deletes, moves = dict(wave.commits), list(wave.deletes), list(wave.moves)
        commit_base: dict[str, str | None] = {}
        delete_base: dict[str, str | None] = {}
        move_base: dict[str, str | None] = {}
        fatal: RunFailure | None = None
        while commits or deletes or moves:
            retry_commits: dict[str, Document] = {}
            retry_deletes: list[str] = []
            retry_moves: list[tuple[str, str]] = []
            next_commit: dict[str, str | None] = {}
            next_delete: dict[str, str | None] = {}
            next_move: dict[str, str | None] = {}
            if commits:
                for result in await self.call(
                    partial(self._push_subset, commits, commit_base),
                    list(commits),
                    _file_uri,
                ):
                    settled = self.settle(result, self.policy.commit, attempts)
                    if isinstance(settled, RunFailure):
                        fatal = fatal or settled
                    elif settled is not None:
                        retry_commits[result.file_uri] = commits[result.file_uri]
                        next_commit[result.file_uri] = settled.base
            if deletes:
                for result in await self.call(
                    partial(self._delete_subset, delete_base), list(deletes), _file_uri
                ):
                    settled = self.settle(result, self.policy.delete, attempts)
                    if isinstance(settled, RunFailure):
                        fatal = fatal or settled
                    elif settled is not None:
                        retry_deletes.append(result.file_uri)
                        next_delete[result.file_uri] = settled.base
            if moves:
                for result in await self.call(
                    partial(self._move_subset, move_base), list(moves), _move_uris
                ):
                    settled = self.settle(result, self.policy.commit, attempts)
                    if isinstance(settled, RunFailure):
                        fatal = fatal or settled
                    elif settled is not None:
                        assert result.to_uri is not None
                        retry_moves.append((result.file_uri, result.to_uri))
                        next_move[result.file_uri] = settled.base
            if fatal is not None:
                raise fatal
            commits, deletes, moves = retry_commits, retry_deletes, retry_moves
            commit_base, delete_base, move_base = next_commit, next_delete, next_move

    def settle(
        self,
        result: PushResult,
        policy: Literal["source_wins", "report"],
        attempts: dict[str, int],
    ) -> _Settled:
        """一条推送结果入账：已入账返回 ``None``；需要重提时返回 ``_Retry``；终止本轮的失败作为
        返回值交给调用方，在本波全部结果入账后抛出。

        §7.2 冲突处置表中与策略无关的「已达成」行（commit 重读等于本次内容、delete 重读不存在）
        由推送器在写后冲突重读时判定（#140），这里收到的 ``conflict`` 都是真正的冲突。
        """
        uris: dict[str, str | None] = {"file_uri": result.file_uri}
        if result.op == "move":
            uris = {"from_uri": result.file_uri, "to_uri": result.to_uri}
        status = result.status
        if status in ("created", "updated", "unchanged", "achieved"):
            if status == "created":
                self.counts.created += 1
            elif status == "updated":
                self.counts.updated += 1
            else:
                self.counts.unchanged += 1
            return None
        if status == "deleted":
            self.counts.deleted += 1
            return None
        if status == "moved":
            self.counts.moved += 1
            return None
        if status == "conflict":
            if result.remote is None:
                # 冲突后重读失败：无从按策略重判定
                assert result.remote_error is not None
                # 重读是读操作：确定错误落到该条目，网络失败与可重试错误耗尽才是轮级
                return self.fail(_classify_remote(result.remote_error, unknown=False), uris)
            code = _cas_code(result, "DPE_PRECONDITION_FAILED")
            if policy == "report":
                return self.fail(
                    ItemFailure(code, "写入冲突（conflict_policy 为 report）", source="remote"),
                    uris,
                )
            used = attempts.get(result.file_uri, 0)
            if used >= self.options.conflict_retries:
                return self.fail(
                    ItemFailure(
                        code,
                        f"写入冲突在本轮重提 {used} 次后仍未解决",
                        source="remote",
                        retryable=True,
                    ),
                    uris,
                )
            attempts[result.file_uri] = used + 1
            return _Retry(result.remote.doc_hash)
        # status == "failed"
        if result.op == "move" and result.side is not None and not result.unknown:
            # 目标被占、源已不存在且目标未达成：重跑不会自行消失（§7.2 永久冲突），不退化
            fallback = "DPE_ALREADY_EXISTS" if result.side == "to" else "DPE_NOT_FOUND"
            message = "move 目标已被占用" if result.side == "to" else "move 源已不存在，目标未达成"
            return self.fail(
                ItemFailure(_cas_code(result, fallback), message, source="remote"), uris
            )
        assert result.error is not None
        return self.fail(_classify_remote(result.error, unknown=result.unknown), uris)

    def fail(self, failure: RunFailure, uris: Mapping[str, str | None]) -> RunFailure | None:
        if isinstance(failure, ItemFailure):
            for key, value in uris.items():
                if value is not None:
                    setattr(failure, key, value)
            self.record(failure)
            return None
        if failure.code == "state_unavailable":
            self.save_failed = True
        return failure

    # -------------------------------------------------------------- 收尾

    async def resave(self) -> RunFailure | None:
        """有过保存失败时，用同一推送器实例有限次补存（模块说明）；仍失败返回该失败。"""
        last: RunFailure | None = None
        for attempt in range(self.options.state_retries):
            if attempt:
                await asyncio.sleep(self.options.state_retry_delay)
            try:
                self.pusher.flush()
            except RunFailure as failure:
                last = failure
                continue
            return None
        return last


async def run_round(
    *,
    host: PluginHost,
    pusher: AsyncStatefulPusher,
    store: InstanceStore,
    uri_prefix: str,
    policy: ConflictPolicy,
    supports_cursor: bool,
    blob_max_bytes: int | None,
    options: RoundOptions | None = None,
    on_event: EventSink | None = None,
    redact: Callable[[str], str] = lambda text: text,
    clock: Callable[[], str] = utc_now,
) -> RoundResult:
    """执行一轮（§6.4–§6.8）：``host`` 已完成 initialize，``pusher`` 以 ``store`` 为状态存储。

    轮首先经推送器自己的客户端（``pusher.client``）取得 remote 能力（已缓存则不发请求）：
    它与任何条目无关，失败按轮级 / 实例级归类、不拆成条目失败；此后推送器抛出的异常只可能
    来自 ``batch_head`` 或状态保存。

    本函数不抛出契约内的失败：一切失败都体现在 ``RoundResult``（``fatal`` 为终止本轮的那一个）。
    """
    options = options or RoundOptions()
    started_at = clock()
    state = _Round(
        host=host,
        pusher=pusher,
        uri_prefix=uri_prefix,
        policy=policy,
        blob_max_bytes=blob_max_bytes,
        options=options,
        on_event=on_event,
        redact=redact,
    )
    cursor = store.cursor if supports_cursor else None
    state.emit("round.started", "info", {"cursor_given": cursor is not None})
    fatal: RunFailure | None = None
    final: ScanBatch | None = None
    try:
        await _negotiate(pusher.client)
        final = await state.run(cursor)
        if final.cursor is not None and not supports_cursor:
            raise ProtocolFailure("插件声明不支持游标，却在结束批给出了游标（§6.8）")
        if final.incremental and cursor is None:
            raise ProtocolFailure("运行器未给出游标，结束批却标记 incremental: true（§6.4）")
    except RunFailure as failure:
        fatal = failure
        if failure.code == "state_unavailable":
            state.save_failed = True
    finally:
        state.stage.close()
    if fatal is not None:
        state.record(fatal)
    if state.save_failed:
        # 有过保存失败的轮已经以 state_unavailable 失败（游标不推进）；补存只为让磁盘与内存
        # 一致，成败都不改变本轮结果。补存失败单独记一条，供观测
        resave = await state.resave()
        if resave is not None:
            state.record(
                RoundFailure(
                    "state_unavailable",
                    f"保存失败后的补存也失败：{resave.message}",
                    retryable=True,
                )
            )

    # §7.3：取自结束批；未收到结束批为 null
    incremental = final.incremental if final is not None else None
    outcome: Outcome = (
        "failed" if fatal is not None else "partial" if state.item_failed else "succeeded"
    )
    new_cursor = final.cursor if final is not None and supports_cursor else None
    try:
        held_since = store.finish_round(
            succeeded=outcome == "succeeded", cursor=new_cursor, started_at=started_at
        )
    except RunFailure as failure:
        # 游标与收尾记录写不进去：游标不推进，本轮 failed（§6.8 fail-closed）
        state.record(failure)
        fatal = fatal or failure
        outcome = "failed"
        held_since = store.held_since or started_at
    cursor_persisted = outcome == "succeeded" and new_cursor is not None

    level: LogLevel = "info"
    if outcome == "failed":
        level = "error"
    elif outcome == "partial" or (cursor is not None and incremental is False):
        level = "warn"
    result = RoundResult(
        outcome=outcome,
        incremental=incremental,
        cursor_persisted=cursor_persisted,
        cursor_held_since=held_since,
        counts=state.counts,
        started_at=started_at,
        ended_at=clock(),
        failures=state.failures,
        fatal=fatal,
    )
    state.emit(
        "round.finished",
        level,
        {
            "outcome": outcome,
            "incremental": incremental,
            "cursor_persisted": cursor_persisted,
            "cursor_held_since": held_since,
            "counts": state.counts.as_dict(),
        },
    )
    return result
