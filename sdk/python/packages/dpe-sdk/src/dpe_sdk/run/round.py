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

**远端失败分层**（§7.1）：401 → 实例级 ``remote_unauthorized``；结果未知、可重试 DPE 错误重试
耗尽、网络失败 → 轮级 ``remote_unavailable``（本轮作废）；其余已确定的 DPE 错误 → 条目级
（source ``remote``，``code`` 与 ``retryable`` 原样）。

**本地状态**：状态目录读写失败是轮级 ``state_unavailable``。本次调用内只要有过一次保存失败
（推送器写前或收尾的 ``save``），退出前都用同一推送器实例有限次补存（``state_retries``，短
退避）；补存仍失败即本轮 ``failed``、游标不推进。补存成功前进程退出（崩溃或补存耗尽）时，
磁盘上可能残留过期意图，下一次调用会读回——风险与 §7.2 接受的崩溃窗口相同。

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
from typing import Any, Literal

import dpe_hash
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


def _cas_code(result: PushResult, fallback: str) -> str:
    """冲突与 move 条目失败的码：有服务端裁决取其原码，写前判定取同义的 DPE 码。"""
    error = result.error
    return error.code if isinstance(error, DpeError) else fallback


def _achieved_on_reread(result: PushResult, document: Document | None) -> bool:
    """冲突后的重读是否已是目标状态（§7.2 冲突处置表，与策略无关）：commit 的重读值等于本次
    提交内容、delete 的重读为「不存在」。"""
    remote = result.remote
    if result.status != "conflict" or remote is None:
        return False
    if result.op == "delete":
        return remote.doc_hash is None
    if result.op != "commit" or remote.doc_hash is None or document is None:
        return False
    try:
        contract, _ = dpe_hash.parse_hash(remote.doc_hash)
    except DpeHashError:
        return False
    return document.doc_hash(contract) == remote.doc_hash


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


#: ``settle`` 的结论：``None`` 为已入账（成功或条目失败）；``RunFailure`` 为终止本轮的失败
_Settled = _Retry | RunFailure | None


class _Round:
    def __init__(
        self,
        *,
        host: PluginHost,
        pusher: AsyncStatefulPusher,
        store: InstanceStore,
        uri_prefix: str,
        policy: ConflictPolicy,
        supports_cursor: bool,
        blob_max_bytes: int | None,
        options: RoundOptions,
        on_event: EventSink | None,
        redact: Callable[[str], str],
    ) -> None:
        self.host = host
        self.pusher = pusher
        self.store = store
        self.prefix = uri_prefix
        self.policy = policy
        self.supports_cursor = supports_cursor
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

    async def call(self, operation: Awaitable[list[PushResult]]) -> list[PushResult]:
        """调用推送器；它抛出的异常（读 remote 失败、状态保存失败）按 §7.1 归类后终止本轮。"""
        try:
            return await operation
        except RunFailure as failure:
            if failure.code == "state_unavailable":
                self.save_failed = True
            raise
        except Exception as exc:
            raise _classify_remote(exc, unknown=True) from None

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
                    self.pusher.push(commits, expected_base=commit_base, blobs=self.stage.lookup)
                ):
                    achieved = _achieved_on_reread(result, commits[result.file_uri])
                    settled = self.settle(result, self.policy.commit, attempts, achieved=achieved)
                    if isinstance(settled, RunFailure):
                        fatal = fatal or settled
                    elif settled is not None:
                        retry_commits[result.file_uri] = commits[result.file_uri]
                        next_commit[result.file_uri] = settled.base
            if deletes:
                for result in await self.call(
                    self.pusher.delete(deletes, expected_base=delete_base)
                ):
                    achieved = _achieved_on_reread(result, None)
                    settled = self.settle(result, self.policy.delete, attempts, achieved=achieved)
                    if isinstance(settled, RunFailure):
                        fatal = fatal or settled
                    elif settled is not None:
                        retry_deletes.append(result.file_uri)
                        next_delete[result.file_uri] = settled.base
            if moves:
                for result in await self.call(self.pusher.move(moves, expected_base=move_base)):
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
        policy: str,
        attempts: dict[str, int],
        *,
        achieved: bool = False,
    ) -> _Settled:
        """一条推送结果入账：已入账返回 ``None``；需要重提时返回 ``_Retry``；终止本轮的失败作为
        返回值交给调用方，在本波全部结果入账后抛出。

        ``achieved``：冲突后的重读已是目标状态（§7.2 的「已达成」行，先于策略）。此时以重读值
        经推送器重提——推送器判定表判已达成、确认同步状态、不发写；期间若又被改写则回到冲突
        路径，同样受重提上限约束。
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
                return self.fail(_classify_remote(result.remote_error, unknown=True), uris)
            code = _cas_code(result, "DPE_PRECONDITION_FAILED")
            if policy == "report" and not achieved:
                return self.fail(
                    ItemFailure(code, "写入冲突（conflict_policy 为 report）", source="remote"),
                    uris,
                )
            used = attempts.get(result.file_uri, 0)
            # 已达成只需一次不发写的确认，可越过重提上限一次（仍有界：确认期间又被改写则失败）
            limit = self.options.conflict_retries + (1 if achieved else 0)
            if used >= limit:
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

    本函数不抛出契约内的失败：一切失败都体现在 ``RoundResult``（``fatal`` 为终止本轮的那一个）。
    """
    options = options or RoundOptions()
    started_at = clock()
    state = _Round(
        host=host,
        pusher=pusher,
        store=store,
        uri_prefix=uri_prefix,
        policy=policy,
        supports_cursor=supports_cursor,
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
    if state.save_failed:
        resave = await state.resave()
        if resave is not None and fatal is None:
            fatal = resave
    if fatal is not None:
        state.record(fatal)

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
