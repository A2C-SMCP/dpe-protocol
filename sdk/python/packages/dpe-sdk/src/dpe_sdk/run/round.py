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

**move 未达成之后的目标 document**（§7.2，#178）：插件在 move 之后紧跟目标的 document（§6.5）。
本轮发出、尚未得到 ``moved`` 的 move 使其目标「未达成」（不从失败结果反推）；涉及未达成 URI 的
写入（document、delete、以它为源或目标的 move）一律不发写、不另计失败——绝不借它们覆盖、删除或
移走占用目标的文档；跳过的写入使它涉及的 URI 都未达成、永不解除，所以跳过总伴随一条未达成 move
的失败，游标必然扣住。每条跳过输出 ``item.skipped``（§7.5，带根因 move，#197）。

唯一的例外：目标上只有一条未达成的 move，且它在判定期得出「源已不存在，目标未达成」（未发写）。
这条 move 先不计失败，其后的目标 document 以 ``if_absent`` 提交：目标由它建出（``created``）
或已等于它（``achieved`` / ``unchanged``）即按已达成计 ``moved``（波末以「确认不存在」清掉源
可能残留的 L）；目标已被他人以别的内容建出则不覆盖、不重提，这条 move 计「目标已被占用」；否则
轮末计失败——目标 document 出现过失败或本轮中止时可重试。

**远端 / 本地请求失败分层**（§7.1）：
- 401 → 实例级 ``remote_unauthorized``；协议或契约不兼容 → 实例级 ``remote_incompatible``；
- SDK 在请求构造阶段确定拒绝的本地失败（如非法 A-label 主机、请求头值不合 http.md §6）→
  实例级 ``request_invalid``（§7.4；运行器不在实例级校验中复现该判定）；
- 响应不是 DPE 报文（无 problem 体也无 ``DPE-Error-Code`` 的非 2xx，含 3xx；或 2xx 报文
  不合规）→ 轮级 ``remote_protocol_error``（只有 5xx 可重试）；
- 结果未知、可重试 DPE 错误重试耗尽、网络失败 → 轮级 ``remote_unavailable``（本轮作废）；
- 写入、``batch_head`` 与冲突后重读得到的其余确定 DPE 错误 → 条目级（source ``remote``，
  ``code`` 原样；只有不可重试的错误落到条目级，``retryable`` 即 false）；
- 能力获取在轮首、推送器调用之外完成，它与条目无关：其余确定错误归轮级、code 原样、不可重试
  （§7.1、§7.4，#137）。

**本地状态**（#137）：状态目录读写失败是轮级 ``state_unavailable``。本次调用内只要推送器有过
一次保存失败（写前或收尾的 ``save``），本轮即失败、游标不推进（此前没有其他终止失败时，
``fatal`` 即 ``state_unavailable``）——收尾失败时推送器整批报错，运行器拿不到该批的逐条结果，
fail-closed 的代价只是重跑一轮（commit 由内容幂等、意图可对账）。结束前照常用同一推送器实例
有限次补存（``state_retries``，短退避），**补存只为让磁盘与内存一致，成败都不改变本轮结果**；
补存失败单独记一条供观测。游标与收尾记录的写入失败同样使本轮 ``failed``，但它写成功才生效、
内存未变，无需补存。补存成功前进程退出（崩溃或补存耗尽）时，磁盘上可能残留过期意图，下一次
调用会读回——风险与 §7.2 接受的崩溃窗口相同。

**读失败**（§7.1，#137）：``batch_head`` 与冲突后重读得到确定、不可重试的错误时归条目级
（code 原样），本轮继续。``batch_head`` 一次覆盖整波：被确定拒绝时由推送器逐 URI 定位，只让读
被拒的条目以 ``failed`` 回报（#145），运行器按普通条目失败入账，其余条目（含逐条读都通过时的全部
条目）按逐条读到的值照常落地。推送器为此记的 warning 由本模块转成 ``remote.batch_read_rejected``
（warn）事件，只带 ``code`` / ``entries`` / ``denied``（读被拒的 URI 数），不带远端文本——对一切
批量读都拒绝的服务端由此可被察觉（connector §7.1）。

**游标 fail-closed**（§6.8）：收到结束批、全部条目成功落地才 ``succeeded`` 并持久化新游标
（同步状态与意图由推送器先行落盘，游标最后写，不会先于同步状态前进）；有条目失败为
``partial``，轮级 / 协议级 / 实例级失败为 ``failed``，两者都保留旧游标（§7.3）。支持游标的
插件在成功轮给出 ``cursor: null`` 时同样保留已缓存的游标（§6.8），本轮 ``cursor_persisted``
为 false。``round.finished`` 不在这里发出：运行器在 outcome 最终确定后按报告发出（§7.5，
``finished_level``）。
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from typing import Any, Literal, TypeVar, cast

import dpe_hash
import httpx
from dpe_hash import DpeHashError, normalize_file_uri

from dpe_sdk.errors import (
    ClientError,
    ContractUnsupportedError,
    DpeError,
    IncompatibleServerError,
    LocalRequestError,
    UnexpectedResponseError,
)
from dpe_sdk.models import Document, _build_document
from dpe_sdk.protocol import PreparedDocument
from dpe_sdk.pusher import AsyncStatefulPusher, PushResult, StateSnapshot
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
    "RoundProgress",
    "RoundResult",
    "finished_level",
    "negotiate_capabilities",
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
    #: 终止本轮的非条目级失败（``failed`` 时必有）
    fatal: RunFailure | None = None
    #: 轮首 ``scan`` 是否带了游标（``round.started`` 的 ``cursor_given``；日志级别据此判断）
    cursor_given: bool = False


@dataclass
class RoundProgress:
    """一轮进行中已经确定的事实，由调用方传入、``run_round`` 就地记下（同 ``counts`` /
    ``failures``）。运行器缺陷使 ``run_round`` 中途抛出时，调用方据此如实报告（§7.3 的例外：
    ``incremental`` 取自结束批，收到即确定，#169）。"""

    #: 结束批的 ``incremental``；未收到结束批为 ``None``
    incremental: bool | None = None


def finished_level(outcome: Outcome, *, cursor_given: bool, incremental: bool | None) -> LogLevel:
    """``round.finished`` 的日志级别：失败为 error；部分成功，或给了游标插件却回退为全量轮
    （§6.4 MUST 可观测）为 warn；其余为 info。

    ``round.finished`` 由调用方在本轮 outcome 最终确定后发出（§7.5：每次同步调用恰好一条，
    字段与轮报告一致）——``run_round`` 之后运行器还可能改变 outcome（如轮后才出的运行器
    缺陷，§7.3），所以本函数只给出级别，不在 ``run_round`` 里发事件。
    """
    if outcome == "failed":
        return "error"
    if outcome == "partial" or (cursor_given and incremental is False):
        return "warn"
    return "info"


# ---------------------------------------------------------------------- 远端失败分层


#: 来自 remote 或其传输的异常；其余异常（ValueError、KeyError 等）是运行器 / 推送器自身的
#: 缺陷，原样抛出、以非契约退出码暴露（§7.6），不伪装成可重试的远端故障
_REMOTE_ERRORS = (DpeError, ClientError, httpx.HTTPError, OSError)


def _classify_remote(exc: BaseException, *, unknown: bool) -> RunFailure:
    """把推送器回报或抛出的远端异常、以及 SDK 的本地请求构造失败归入 §7.1 的层级（不含条目
    URI）。"""
    if isinstance(exc, RunFailure):
        return exc  # 状态目录失败（state_unavailable）等已归类的失败
    if not isinstance(exc, _REMOTE_ERRORS):
        raise exc
    if isinstance(exc, IncompatibleServerError | ContractUnsupportedError):
        # remote 不支持运行器可用的协议或 hash 契约：不改动实例就无法恢复（§7.1 实例级）
        return InstanceFailure("remote_incompatible", f"remote 不兼容：{exc}", source="remote")
    if isinstance(exc, LocalRequestError):
        # 判定锚定在异常类型，不看「确定未发出」标记：该标记还被 provider 异常、意图持久化
        # 失败、_save_after(wrote=False)、建连失败共用，按标记判定会把暂时性失败误报为不可
        # 重试（#256）。结果是否未知只由推送器决定意图是否保留，本分支忽略 unknown。
        # LocalRequestError 保留给 SDK 的请求构造失败；provider 自己抛出的异常应使用自己的
        # 类型，不应复用本类型。
        return InstanceFailure(
            "request_invalid",
            "remote 请求在本地构造阶段被拒绝，确定未发出",
            source="runner",
        )
    if isinstance(exc, UnexpectedResponseError):
        if exc.status == 401:
            return InstanceFailure(
                "remote_unauthorized", "remote 拒绝凭证（HTTP 401）", source="remote"
            )
        # 响应不是 DPE 报文（§7.1、§7.4，#137）：非 2xx 且既无 problem 体也无 DPE-Error-Code
        # 头（含不跟随的 3xx 重定向），或 2xx 报文不合规。只有 5xx 可重试。写请求遇到它时结果
        # 未知，推送器已按 outcome_unknown 保留意图；这里只决定本轮如何终止
        return RoundFailure(
            "remote_protocol_error",
            # 只带状态码：SDK 的说明里可能内嵌远端响应体片段（如校验回显），不进报告与日志
            f"remote 的响应不是 DPE 报文（HTTP {exc.status}）",
            source="remote",
            retryable=500 <= exc.status < 600,
        )
    if isinstance(exc, DpeError) and not unknown and not exc.retryable:
        return ItemFailure(exc.code, str(exc), source="remote", retryable=False)
    return RoundFailure(
        "remote_unavailable", f"remote 不可用：{exc}", source="remote", retryable=True
    )


async def negotiate_capabilities(client: AsyncDpeClient) -> None:
    """取得 remote 能力（§7.1）：401 / 不兼容为实例级；SDK 的本地请求构造失败
    （``LocalRequestError``）为实例级 ``request_invalid``；响应不是 DPE 报文为轮级
    ``remote_protocol_error``；网络失败与可重试错误耗尽为轮级可重试；其余 remote 返回的确定
    错误（如凭证缺权限得 403）与任何条目无关，归轮级、code 原样、不可重试。"""
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


#: 波内操作的种类，也是 ``item.skipped`` 的 ``kind``（§7.5）
_SkipKind = Literal["document", "delete", "move"]


@dataclass
class _Wave:
    """一波 URI 两两不同的落地操作（按流中顺序收集）。"""

    commits: dict[str, tuple[Document, PreparedDocument]] = field(default_factory=dict)
    deletes: list[str] = field(default_factory=list)
    moves: list[tuple[str, str]] = field(default_factory=list)
    uris: set[str] = field(default_factory=set)
    #: 各操作在流中的顺序：(种类, 涉及的 URI)，供 ``item.skipped`` 按流序输出（§7.5）
    order: list[tuple[_SkipKind, dict[str, str]]] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.uris)


_Op = tuple[Literal["commit"], str, Document, PreparedDocument] | DeleteItem | MoveItem


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


#: 一次推送器调用的条目键：commit / delete 为 file_uri，move 为 (from_uri, to_uri)
_K = TypeVar("_K", str, tuple[str, str])

#: ``settle`` 的结论：``None`` 为已入账（成功或条目失败）；``RunFailure`` 为终止本轮的失败
_Settled = _Retry | RunFailure | None


class _BatchReadRejected(logging.Handler):
    """把本轮推送器的「批量读被拒」warning（logger ``dpe_sdk.pusher``）转成事件（§7.1、§7.5）。

    只转发本轮推送器发出的记录（``dpe_pusher`` 为同一实例），同一进程里的其他推送器不串扰。
    """

    def __init__(self, state: _Round) -> None:
        super().__init__(logging.WARNING)
        self._state = state

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(record, "dpe_pusher", None) is not self._state.pusher:
            return
        self._state.emit(
            "remote.batch_read_rejected",
            "warn",
            {
                "code": getattr(record, "dpe_code", None),
                "entries": getattr(record, "dpe_entries", None),
                "denied": getattr(record, "dpe_denied", None),
            },
        )


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
        counts: RoundCounts | None = None,
        failures: list[RunFailure] | None = None,
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
        # 计数与失败可由调用方提供：本轮中途因运行器缺陷中断时，调用方仍能报告已发生的部分
        self.counts = RoundCounts() if counts is None else counts
        self.failures: list[RunFailure] = [] if failures is None else failures
        #: 本轮请求声明的契约（能力协商后由 core.contract 覆盖）
        self.contract = dpe_hash.CONTRACT
        self.item_failed = False
        #: 本次调用内是否有过保存失败（退出前须补存，见模块说明）
        self.save_failed = False
        #: 本轮未达成的 move 数（按 to_uri；发出后尚未得到 moved 的，以及因涉及未达成目标而
        #: 跳过的）：涉及这些目标的写入不发写（§7.2「move 未达成之后的目标 document」，#178）
        self.open_moves: Counter[str] = Counter()
        #: 其中判定期得出「源已不存在，目标未达成」的：to_uri → (失败, 涉及的 URI)。目标上只有
        #: 这一条未达成的 move 时，其后的目标 document 以 if_absent 提交，以它的结果下结论
        self.awaiting_target: dict[str, tuple[ItemFailure, dict[str, str | None]]] = {}
        #: 未达成 URI → 使它未达成的那条发出过的 move（from_uri, to_uri）：跳过链一律追溯到它，
        #: 作为 ``item.skipped`` 的 ``cause``（§7.5，#197）
        self.unachieved_cause: dict[str, tuple[str, str]] = {}
        #: 当前这次扫描产出过的 file_uri（document、delete、move 两侧，含条目 error）：写入集合的
        #: 对账以它判断「未产出」（§6.8「写入集合与刷新」，#213）
        self.produced: set[str] = set()
        #: 每个 file_uri（move 取 from_uri）在本次调用中最后计入的计数类别（统计对账删除的结局）
        self.tallied: dict[str, str] = {}
        #: 刷新的全量扫描只落地这些 URI（写入集合中增量扫描未产出的，W'）；``None`` 即不在刷新中
        self.refresh_scope: set[str] | None = None
        #: 刷新中只作为被忽略的 move 的目标出现的 W' 成员：没有 document 就无法对账（枚举不完整）
        self.move_targets: set[str] = set()
        #: 无 L 的推断删除：运行器读到的 R 作为删除的预期值（推送器重读不同即判冲突）
        self.foreign_base: dict[str, str | None] = {}
        #: 其中判冲突的 URI（从来不是本实例的值）：记一次失败，由调用方移出写入集合
        self.foreign_hits: list[str] = []

    # -------------------------------------------------------------- 事件与失败

    def emit(self, event: str, level: LogLevel, fields: dict[str, Any]) -> None:
        if self.on_event is not None:
            self.on_event(event, level, fields)

    def record(self, failure: RunFailure) -> None:
        failure.redact(self.redact)
        self.failures.append(failure)
        if isinstance(failure, ItemFailure):
            self.item_failed = True
            self.tally(failure.file_uri or failure.from_uri, "failed")
            self.emit("item.failed", "warn", {"failure": failure.to_report()})
        else:
            self.emit("item.failed", "error", {"failure": failure.to_report()})

    def tally(self, key: str | None, category: str) -> None:
        """计入一个结局（§7.4 ``counts``）。刷新的全量扫描只落地增量扫描未产出的 URI，两者不
        重叠，计数直接相加。"""
        setattr(self.counts, category, getattr(self.counts, category) + 1)
        if key is not None:
            self.tallied[key] = category

    def begin_refresh(self, scope: set[str]) -> None:
        """开始刷新的全量扫描（§6.8「写入集合与刷新」）：只落地 ``scope``（W'）中的 URI，产出
        集合重新累计。"""
        self.refresh_scope = scope
        self.produced = set()

    async def reconcile(
        self,
        written: Mapping[str, str | None],
        scope: list[str],
        state: StateSnapshot,
        forget: Callable[[list[str]], None],
        *,
        incremental: bool,
    ) -> None:
        """对账写入集合（§6.8「写入集合与刷新」）。``scope`` 是要对账的 URI（增量轮为 W'，全量
        轮为 W 中本轮未产出的）；全量扫描产出了的已按普通条目落地，其余在完整枚举中未产出、即
        不在源中，以**本实例确认过的值**为前置条件删除：

        - 有 L：与插件显式产出的 delete 同等对待——交给推送器的 delete（以 L 为前置条件，含意图
          对账），冲突遵从 ``conflict_policy.delete``（``report`` 即永久冲突，改 ``source_wins``
          即以重读到的 R 重提）；
        - 没有 L：先读 R。R 不存在即已达成（有待定意图时仍交给推送器清除它，不会删除任何
          内容）；R 等于本实例的待定意图值时，它是本实例自己的写入，交给推送器（按意图确认后
          删除）；其余 R 存在的情形——这个值从来不是本实例的——判冲突、不发写，记一次条目失败
          （``DPE_PRECONDITION_FAILED``，不可重试），并经 ``forget`` 移出写入集合（不再对账，
          下一轮即可成功；在落地之前移出，落地中途失败也只上报这一次）。交给推送器的无 L 条目
          以运行器读到的 R 为预期值：两次读之间 R 被他人改写时推送器判冲突，同样按此处理，
          不按策略重提。

        B 存在的（游标那一刻存在、插件本应产出其删除）只做观测，计入 ``violations``：运行器
        无法区分插件违约与过期的 L，后者若扣住游标会使实例永久卡住。只在枚举完整时调用。
        """
        unproduced = [uri for uri in scope if uri not in self.produced]
        confirmed = [uri for uri in unproduced if uri in state.synced]
        unknown = [uri for uri in unproduced if uri not in state.synced]
        heads = await self.read_heads(unknown)
        achieved = 0
        for uri in unknown:
            if uri not in heads:
                continue  # 读被确定拒绝：已记条目失败
            head = heads[uri]
            if head is None:
                if uri in state.intents:
                    confirmed.append(uri)  # 已不存在：交给推送器清除残留的意图
                    self.foreign_base[uri] = None
                else:
                    achieved += 1  # 已不存在、也没有本实例的记录：已达成，不发写
                    self.tally(uri, "deleted")
            elif uri in state.intents and state.intents[uri] == head:
                confirmed.append(uri)  # remote 正是本实例的待定写入
                self.foreign_base[uri] = head
            else:
                self.foreign_conflict(uri)
        if self.foreign_hits:
            forget(list(self.foreign_hits))  # 落地之前移出：只上报这一次
            self.foreign_hits.clear()
        try:
            await self.land([DeleteItem(file_uri=uri) for uri in confirmed])
        finally:
            self.foreign_base.clear()  # 只作用于这次 land（对账是本次调用的最后一次落地）
            if self.foreign_hits:  # 推送器重读判冲突的（两次读之间 R 被改写）
                forget(list(self.foreign_hits))
                self.foreign_hits.clear()
        self.emit(
            "round.refreshed",
            "warn",
            {
                "uris": len(scope),
                "deleted": achieved
                + sum(1 for uri in confirmed if self.tallied.get(uri) == "deleted"),
                "violations": sum(1 for uri in unproduced if written[uri] is not None)
                if incremental
                else 0,
            },
        )

    def foreign_conflict(self, uri: str) -> None:
        """无 L 的推断删除判冲突（§6.8）：remote 上的值从来不是本实例的，不发写、不按策略重提，
        记一次条目失败，并由调用方把它移出写入集合。"""
        self.foreign_hits.append(uri)
        self.record(
            ItemFailure(
                "DPE_PRECONDITION_FAILED",
                "对账删除：本实例没有确认过该文档的值，remote 上的内容可能属于他人，不删除（§6.8）",
                source="remote",
                file_uri=uri,
            )
        )

    async def read_heads(self, uris: list[str]) -> dict[str, str | None]:
        """读取 R（推断删除的无 L 一支）。失败按推送器调用的口径归类（§7.1、§7.3）：轮级失败
        终止本轮；批量读被确定拒绝时逐条重读定位，读被拒的条目记失败、不在返回值中（#145）。"""
        if not uris:
            return {}
        client = self.pusher.client
        try:
            return dict(zip(uris, await client.batch_head(uris), strict=True))
        except Exception as exc:
            rejected = _classify_remote(exc, unknown=False)
            if not isinstance(rejected, ItemFailure):
                raise rejected from None
        heads: dict[str, str | None] = {}
        denied = 0
        for uri in uris:
            try:
                heads[uri] = await client.head(uri)
            except Exception as exc:
                failure = _classify_remote(exc, unknown=False)
                if not isinstance(failure, ItemFailure):
                    raise failure from None
                failure.file_uri = uri
                self.record(failure)
                denied += 1
        self.emit(
            "remote.batch_read_rejected",
            "warn",
            {"code": rejected.code, "entries": len(uris), "denied": denied},
        )
        return heads

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

        scope = self.refresh_scope

        def kept(failure: ItemFailure) -> bool:
            # 刷新中只忽略能证明在 W' 之外的失败（越界，或 URI 已规范化、在前缀内且不在 W' 中）：
            # 归属不了 URI 的失败可能就是 W' 中的某篇，照记——它使枚举不完整、跳过推断删除。
            # 插件的 error 条目一律照记（如读不了的目录，可能掩盖其下 W' 中的 URI）
            if scope is None or failure.source == "plugin":
                return True
            if failure.source == "prefix":
                return False
            uri = failure.file_uri
            return not (
                isinstance(uri, str)
                and uri.startswith(self.prefix)
                and _is_normalized(uri)
                and uri not in scope
            )

        def take(events: list[ReadyDocument | ItemFailure]) -> None:
            for event in events:
                if isinstance(event, ItemFailure):
                    if kept(event):
                        self.record(event)
                elif scope is None or event.file_uri in scope:
                    ops.append(event)

        for raw in batch.items:
            item = parse_item(raw, self.prefix, contract=self.contract)
            if scope is None or not isinstance(item, MoveItem):
                self.mark_produced(item)
            elif item.to_uri in scope:
                # 刷新中被忽略的 move：from_uri 不算产出（照常进入推断删除）；W' 中的 to_uri 若此后
                # 没有它的 document，无法对账，视为枚举不完整（见 ``check_move_targets``）
                self.move_targets.add(item.to_uri)
            if isinstance(item, DocumentSegment):
                take(self.assembler.feed(item))
                continue
            take(self.assembler.interrupt())
            if isinstance(item, ItemFailure):
                if kept(item):
                    self.record(item)
            elif scope is None or (isinstance(item, DeleteItem) and item.file_uri in scope):
                ops.append(item)  # 刷新中只落地 W' 内的 delete；move 一律由增量扫描负责
        if batch.final:
            take(self.assembler.finish())
        return ops

    def check_move_targets(self) -> None:
        """刷新的全量扫描结束：W' 中只作为被忽略的 move 的目标出现、没有 document 的 URI 无法
        对账（它在源中，但内容未知）——记条目失败，使枚举不完整、跳过推断删除（§6.8）。"""
        for uri in sorted(self.move_targets - self.produced):
            self.record(
                ItemFailure(
                    "DPE_VALIDATION",
                    "刷新的全量扫描只以 move 的目标给出它、没有给出其 document，无法对账（§6.8）",
                    source="validation",
                    retryable=True,
                    file_uri=uri,
                )
            )
        self.move_targets.clear()

    def mark_produced(self, item: object) -> None:
        """记下产出项涉及的 file_uri（含条目 error 与无法落地的产出：本轮因此不会成功，不影响
        对账的结论）。"""
        for key in ("file_uri", "from_uri", "to_uri"):
            uri = getattr(item, key, None)
            if isinstance(uri, str):
                self.produced.add(uri)

    # -------------------------------------------------------------- 落地

    async def prepare(self, ready: ReadyDocument) -> tuple[Document, PreparedDocument] | None:
        """读取 blob、换成真实引用，补算含 blob 的页 hash，再做文档层校验（§6.6 第 3–4 步）。"""
        pages = ready.pages
        page_hashes = ready.hashes
        dirty_pages: set[int] = set()
        for slot in ready.blobs:
            ref = await self.stage.resolve(slot.spec)
            if isinstance(ref, ItemFailure):
                self.item_failure(ref, file_uri=ready.file_uri)
                return None
            element = pages[slot.page]["elements"][slot.element]
            element["blob"] = ref
            at = f"/pages/{slot.page}/elements/{slot.element}"
            try:
                page_hashes[slot.page]["elements"][slot.element] = dpe_hash.content_hash(
                    element, self.contract
                )
            except DpeHashError as exc:
                self.item_failure(
                    ItemFailure(
                        exc.code,
                        f"{exc.message}（位置 {at}{exc.path}）",
                        source="validation",
                    ),
                    file_uri=ready.file_uri,
                )
                return None
            dirty_pages.add(slot.page)
        for index in sorted(dirty_pages):
            try:
                page_hashes[index]["page_hash"] = dpe_hash.page_hash(
                    cast(
                        dpe_hash.PageFields,
                        {key: value for key, value in pages[index].items() if key != "elements"},
                    ),
                    page_hashes[index]["elements"],
                    self.contract,
                )
            except DpeHashError as exc:
                self.item_failure(
                    ItemFailure(
                        exc.code,
                        f"{exc.message}（位置 /pages/{index}{exc.path}）",
                        source="validation",
                    ),
                    file_uri=ready.file_uri,
                )
                return None
        try:
            document_hash = dpe_hash.doc_hash(
                cast(dpe_hash.DocumentFields, ready.document),
                [page_hash_item["page_hash"] for page_hash_item in page_hashes],
                self.contract,
            )
        except DpeHashError as exc:
            self.item_failure(
                ItemFailure(exc.code, str(exc), source="validation"), file_uri=ready.file_uri
            )
            return None
        document = _build_document(ready.document, pages)
        prepared = PreparedDocument(
            self.contract,
            {"doc_hash": document_hash, "pages": page_hashes},
            {**ready.document, "pages": pages},
        )
        return document, prepared

    async def land(self, ops: list[_Op | ReadyDocument]) -> None:
        wave = _Wave()
        for entry in ops:
            op: _Op
            if isinstance(entry, ReadyDocument):
                prepared = await self.prepare(entry)
                if prepared is None:
                    continue
                document, prepared_document = prepared
                op = ("commit", entry.file_uri, document, prepared_document)
            else:
                op = entry
            uris = _op_uris(op)
            if uris & wave.uris:
                await self.flush_wave(wave)
                wave = _Wave()
            wave.uris |= uris
            if isinstance(op, DeleteItem):
                wave.deletes.append(op.file_uri)
                wave.order.append(("delete", {"file_uri": op.file_uri}))
            elif isinstance(op, MoveItem):
                wave.moves.append((op.from_uri, op.to_uri))
                wave.order.append(("move", {"from_uri": op.from_uri, "to_uri": op.to_uri}))
            else:
                wave.commits[op[1]] = (op[2], op[3])
                wave.order.append(("document", {"file_uri": op[1]}))
        if wave:
            await self.flush_wave(wave)

    async def call(
        self, run: Callable[[list[_K]], Awaitable[list[PushResult]]], keys: list[_K]
    ) -> list[PushResult]:
        """以 ``run(keys)`` 调用推送器。读写的确定失败都在逐条回报里——批量读被确定拒绝时，推送器
        自己逐 URI 定位、只让读被拒的条目 ``failed``（#145）；能力已在轮首取得，推送器只会因网络
        失败、可重试错误耗尽、响应不是 DPE 报文或状态保存而整批抛出，这些都终止本轮（轮级）。
        """
        try:
            return await run(keys)
        except RunFailure as failure:
            if failure.code == "state_unavailable":
                # 因持久化失败而中止的这次调用：推送器整批报错，其涉及的全部条目（含判定阶段已
                # 得出结论的）都不计入 counts、也不列入 failures；此前已完成的调用照常计入（§7.2）
                self.save_failed = True
            raise
        except Exception as exc:
            classified = _classify_remote(exc, unknown=False)
            if isinstance(classified, ItemFailure):
                # 确定的 DPE 错误由推送器逐条返回（#145），整批抛出即违背其契约：按运行器内部
                # 缺陷处理，不猜测归属到哪些条目
                raise RuntimeError(f"推送器整批抛出了确定的 DPE 错误：{classified.code}") from exc
            raise classified from None

    def _push_subset(
        self,
        commits: dict[str, tuple[Document, PreparedDocument]],
        bases: dict[str, str | None],
        subset: list[str],
    ) -> Awaitable[list[PushResult]]:
        return self.pusher._push_prepared(
            {uri: commits[uri][0] for uri in subset},
            {uri: commits[uri][1] for uri in subset},
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
        # 无 L 的推断删除以运行器读到的 R 为预期值（§6.8），推送器重读不同即判冲突
        delete_base: dict[str, str | None] = {
            uri: self.foreign_base[uri] for uri in deletes if uri in self.foreign_base
        }
        move_base: dict[str, str | None] = {}
        # 策略重提及其 base（#214）：与 commit_base 分开记——后者还装着 guarded 目标的
        # if_absent（#178），复用会把目标建出误报成复活
        retried: dict[str, str | None] = {}
        fatal: RunFailure | None = None
        # 涉及未达成 URI 的写入不发写；跳过的写入使它涉及的 URI 都未达成（永不解除），在等待
        # 的 move 因此不再能由目标 document 解除、轮末计失败——跳过必然扣住游标。目标上只有
        # 一条在等待目标 document 的 move 时，该 document 以 if_absent 提交、不按策略重提
        # （§7.2，#178）
        guarded = {
            uri for uri in commits if self.open_moves[uri] == 1 and uri in self.awaiting_target
        }
        skipped: set[str] = set()
        for kind, uris in wave.order:  # 按流序（§7.5）
            if kind == "document" and uris["file_uri"] in guarded:
                continue
            if any(self.open_moves[uri] for uri in uris.values()):
                skipped |= self.skip(kind, uris)
        commits = {uri: doc for uri, doc in commits.items() if uri not in skipped}
        deletes = [uri for uri in deletes if uri not in skipped]
        moves = [move for move in moves if move[0] not in skipped]
        for move in moves:
            self.mark_cause(move[1], move)
        self.open_moves.update([to_uri for _, to_uri in moves] + sorted(skipped))
        commit_base.update({uri: None for uri in guarded})
        while commits or deletes or moves:
            cleared: list[str] = []
            retry_commits: dict[str, tuple[Document, PreparedDocument]] = {}
            retry_deletes: list[str] = []
            retry_moves: list[tuple[str, str]] = []
            next_commit: dict[str, str | None] = {}
            next_delete: dict[str, str | None] = {}
            next_move: dict[str, str | None] = {}
            if commits:
                landed = await self.call(
                    partial(self._push_subset, commits, commit_base),
                    list(commits),
                )
                for result in landed:
                    target = result.file_uri in guarded
                    if target and result.status == "conflict" and result.remote is not None:
                        # if_absent 被拒、重读成功：目标仍被占用则不覆盖；重读已不存在（他人建出
                        # 又删掉）则留到轮末、可重试。都不按策略重提
                        if result.remote.doc_hash is not None:
                            self.reject_target(result)
                        else:
                            self.awaiting_target[result.file_uri][0].retryable = True
                        continue
                    settled = self.settle(result, self.policy.commit, attempts)
                    if isinstance(settled, RunFailure):
                        fatal = fatal or settled
                    elif settled is not None:
                        retry_commits[result.file_uri] = commits[result.file_uri]
                        next_commit[result.file_uri] = settled.base
                        retried[result.file_uri] = settled.base
                    else:
                        self.overwritten(
                            result, retried, commits[result.file_uri][1].hashes["doc_hash"]
                        )
                        if target and result.status in ("created", "achieved", "unchanged"):
                            cleared.append(self.resolve_move_onto(result.file_uri))
            if deletes:
                for result in await self.call(
                    partial(self._delete_subset, delete_base), list(deletes)
                ):
                    if result.status == "conflict" and result.file_uri in self.foreign_base:
                        # 无 L：R 在两次读之间变了——不是本实例的值，不按策略重提（§6.8）
                        self.foreign_conflict(result.file_uri)
                        continue
                    settled = self.settle(result, self.policy.delete, attempts)
                    if isinstance(settled, RunFailure):
                        fatal = fatal or settled
                    elif settled is not None:
                        retry_deletes.append(result.file_uri)
                        next_delete[result.file_uri] = settled.base
                        retried[result.file_uri] = settled.base
                    else:
                        self.overwritten(result, retried, None)
            if moves:
                for result in await self.call(partial(self._move_subset, move_base), list(moves)):
                    settled = self.settle(result, self.policy.commit, attempts)
                    if isinstance(settled, RunFailure):
                        fatal = fatal or settled
                    elif settled is not None:
                        assert result.to_uri is not None
                        retry_moves.append((result.file_uri, result.to_uri))
                        next_move[result.file_uri] = settled.base
                    elif result.status == "moved":
                        assert result.to_uri is not None
                        self.open_moves[result.to_uri] -= 1
            if fatal is not None:
                raise fatal
            if cleared:
                # 全部结果入账之后才发（它可能抛出轮级失败）
                await self.clear_absent_sources(sorted(set(cleared)))
            commits, deletes, moves = retry_commits, retry_deletes, retry_moves
            commit_base, delete_base, move_base = next_commit, next_delete, next_move

    def mark_cause(self, uri: str, cause: tuple[str, str]) -> None:
        """``uri`` 即将未达成：此刻它不是未达成时以 ``cause`` 为根因（此前达成过的旧根因作废），
        已是未达成时保留最先的根因。"""
        if not self.open_moves[uri]:
            self.unachieved_cause[uri] = cause

    def skip(self, kind: _SkipKind, uris: dict[str, str]) -> set[str]:
        """一条涉及未达成 URI 的写入不发写（§7.2）：输出 ``item.skipped``（§7.5，#197），并让它
        涉及的 URI 都追溯到同一条根因 move；返回这些 URI。跳过不是失败，不计数。"""
        blocked = next(uri for uri in uris.values() if self.open_moves[uri])
        cause = self.unachieved_cause[blocked]
        for uri in uris.values():
            self.mark_cause(uri, cause)
        self.emit(
            "item.skipped",
            "warn",
            {"kind": kind, **uris, "cause": {"from_uri": cause[0], "to_uri": cause[1]}},
        )
        return set(uris.values())

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
            self.tally(result.file_uri, "unchanged" if status == "achieved" else status)
            return None
        if status == "deleted":
            self.tally(result.file_uri, "deleted")
            return None
        if status == "moved":
            self.tally(result.file_uri, "moved")
            return None
        if status == "conflict":
            if result.remote is None:
                # 冲突后重读失败：无从按策略重判定
                assert result.remote_error is not None
                # 重读是读操作：确定错误落到该条目；网络失败、可重试错误耗尽与响应不是 DPE 报文
                # 才是轮级
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
            failure = ItemFailure(_cas_code(result, fallback), message, source="remote")
            if result.side == "from" and result.to_uri is not None and result.precondition is None:
                # 判定期失败（未发写）：判定时源不存在、目标不等于源的 base（通常也不存在）。
                # 先不计失败，其后的目标 document 以 if_absent 把目标建出来即按已达成计（§7.2，
                # #178）；写入期得到的「不存在」（判定时源仍在）不属于此列，照常失败
                self.awaiting_target[result.to_uri] = (failure, uris)
                return None
            return self.fail(failure, uris)
        assert result.error is not None
        return self.fail(_classify_remote(result.error, unknown=result.unknown), uris)

    def overwritten(
        self, result: PushResult, retried: dict[str, str | None], written: str | None
    ) -> None:
        """策略重提有结论：那次重提真正落地时输出 ``item.overwritten``（§7.5，#214）。

        ``retried`` 是本次波内策略重提的 ``base_hash``（commit / delete 共用；move 不在其中）。
        只有落地才算覆盖：commit 得到 ``created`` / ``updated``，delete 真正删除成功（``deleted``
        且发了写、没带「文档不存在」的原错误——delete 的已达成同样是这个状态）。重提后已达成、
        再次冲突直到重试耗尽，都什么也没覆盖。``written`` 是本次提交内容的 doc_hash，delete 为
        ``null``。
        """
        if result.file_uri not in retried:
            return
        base = retried.pop(result.file_uri)
        if result.op == "delete":
            # delete 的 ``deleted`` 也含「目标状态已达成」（§7.2 判定表与冲突处置表）：判定时 R
            # 已不存在（未发写，无前置条件），或服务端裁决不存在 / 重读不存在（带原错误对象）。
            # 那时文档不是本实例删除的，什么也没覆盖，不输出
            if (
                result.status != "deleted"
                or result.precondition is None
                or result.error is not None
            ):
                return
            kind = "delete"
        elif result.status in ("created", "updated"):
            # base 为 null 即用 if_absent 复活他人删除的文档，否则是覆盖他人的写入
            kind = "resurrect" if base is None else "overwrite"
        else:
            return
        self.emit(
            "item.overwritten",
            "warn",
            {"kind": kind, "file_uri": result.file_uri, "overwritten": base, "written": written},
        )

    def resolve_move_onto(self, to_uri: str) -> str:
        """目标已由其后的 document 建出（或已等于它）：等待它的 move 按已达成计（§7.2，#178），
        返回要清 L 的源。

        源这时在 remote 上不存在，但同步状态里可能还留着它的 L（源是被他人删除的）：调用方在
        波末以「确认不存在」为前置条件对源做一次 delete——源不存在即已达成、只清掉 L，不发写；
        源在这期间被重新建出则判冲突、什么也不做（绝不删除）。
        """
        _, uris = self.awaiting_target.pop(to_uri)
        self.open_moves[to_uri] -= 1
        source = uris["from_uri"]
        assert source is not None
        self.tally(source, "moved")
        return source

    def reject_target(self, result: PushResult) -> None:
        """目标 document 以 if_absent 提交被拒：目标已被他人建出，等它的 move 计「目标已被
        占用」（§7.2，#178）；目标仍未达成，此后涉及它的写入不发写。"""
        _, uris = self.awaiting_target.pop(result.file_uri)
        failure = ItemFailure(
            _cas_code(result, "DPE_ALREADY_EXISTS"), "move 目标已被占用", source="remote"
        )
        self.fail(failure, uris)

    async def clear_absent_sources(self, sources: list[str]) -> None:
        """以「确认不存在」删除已不存在的源：只清掉同步状态里残留的 L，不发写。

        读被确定拒绝时没能清掉 L：残留的 L 会让源端之后重现同一 URI 时误判冲突，所以给源记
        一条条目失败（可重试与否随 remote 的裁决）、扣住游标——拒绝解除后，下一轮重放 move 时
        再次达成（目标等于源的 base 时推送器直接判已达成，否则经紧跟的目标 document 按 §7.2
        再次达成）并清掉 L。网络等轮级失败与状态保存失败照常终止本轮。
        """
        results = await self.call(
            lambda subset: self.pusher.delete(subset, expected_base=dict.fromkeys(subset)),
            sources,
        )
        for result in results:
            if result.status != "failed":
                continue  # 已达成即清掉 L；源被重新建出则判冲突、什么也不做（绝不删除）
            assert result.error is not None
            classified = _classify_remote(result.error, unknown=result.unknown)
            if not isinstance(classified, ItemFailure):
                raise classified
            self.fail(
                ItemFailure(
                    classified.code,
                    f"未能清除已不存在的源的同步状态：{classified.message}",
                    source="remote",
                    retryable=classified.retryable,
                ),
                {"file_uri": result.file_uri},
            )

    def settle_awaiting_moves(self, *, aborted: bool) -> None:
        """轮末：目标没有由其后的 document 建出的 move，照常计条目失败（§7.2，#178）。可重试
        与否随目标：本轮中止、目标上有可重试的条目失败（目标 document 下一轮可能落地），或写入
        时遇到竞态，记为可重试；否则重跑结果相同，不可重试。"""
        pending, self.awaiting_target = self.awaiting_target, {}
        retryable = {
            failure.file_uri
            for failure in self.failures
            if isinstance(failure, ItemFailure) and failure.retryable
        }
        for to_uri, (failure, uris) in pending.items():
            failure.retryable = failure.retryable or aborted or to_uri in retryable
            self.fail(failure, uris)

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


def _is_normalized(uri: str) -> bool:
    """``uri`` 是否已是 core §1.1 规范化形式（无法规范化的视为否）。"""
    try:
        return normalize_file_uri(uri) == uri
    except DpeHashError:
        return False


def _check_final(final: ScanBatch, *, cursor_given: bool, supports_cursor: bool) -> None:
    """结束批的游标约定（§6.4、§6.8）。"""
    if final.cursor is not None and not supports_cursor:
        raise ProtocolFailure("插件声明不支持游标，却在结束批给出了游标（§6.8）")
    if final.incremental and not cursor_given:
        raise ProtocolFailure("运行器未给出游标，结束批却标记 incremental: true（§6.4）")


async def _reconcile_written(
    state: _Round,
    store: InstanceStore,
    final: ScanBatch,
    supports_cursor: bool,
) -> None:
    """推进游标之前对写入集合 W 对账（§6.8「写入集合与刷新」，#213）。

    只在本轮其余条目都已成功落地时对账（有条目失败的轮本就不推进游标，W 保留）。

    - 增量轮：W 中有本轮未产出的 URI（W'）时，同一次调用里再做一次全量扫描（不带游标），只
      落地 W' 中的 URI，其余产出忽略（由增量扫描的游标负责，两次扫描之间的变化下一轮照常表达）；
      全量扫描也未产出的，以本实例确认过的值删除。游标仍取增量扫描的；
    - 全量轮：扫描本身就是全量的，W 中未产出的同样删除（只限 W）。

    删除要求枚举完整：扫描没有条目级失败。
    """
    state.settle_awaiting_moves(aborted=False)  # 等待中的 move 先下结论（可能使本轮 partial）
    written = store.written
    if not written or state.item_failed:
        return
    missing = sorted(uri for uri in written if uri not in state.produced)
    if not missing:
        return
    if final.incremental:
        state.begin_refresh(set(missing))
        full = await state.run(None)
        _check_final(full, cursor_given=False, supports_cursor=supports_cursor)
        state.check_move_targets()
        state.settle_awaiting_moves(aborted=False)
        state.refresh_scope = None
        if state.item_failed:
            # 全量扫描枚举不完整：不做推断；本轮 partial，游标不推进，W 保留
            state.emit(
                "round.refreshed", "warn", {"uris": len(missing), "deleted": 0, "violations": 0}
            )
            return
    snapshot = store.load("") or StateSnapshot()
    # forget 写失败抛 state_unavailable：本轮失败、游标不推进
    await state.reconcile(
        written, missing, snapshot, store.forget_written, incremental=bool(final.incremental)
    )


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
    counts: RoundCounts | None = None,
    failures: list[RunFailure] | None = None,
    progress: RoundProgress | None = None,
) -> RoundResult:
    """执行一轮（§6.4–§6.8）：``host`` 已完成 initialize，``pusher`` 以 ``store`` 为状态存储。

    轮首先经推送器自己的客户端（``pusher.client``）取得 remote 能力（已缓存则不发请求）：
    它与任何条目无关，失败按轮级 / 实例级归类、不拆成条目失败；此后推送器抛出的异常只可能
    来自 ``batch_head`` 或状态保存。

    本函数不抛出契约内的失败：一切失败都体现在 ``RoundResult``（``fatal`` 为终止本轮的那一个）。
    ``counts`` / ``failures`` 可由调用方传入（必须为空）并就地累计——运行器自身缺陷使本函数
    中途抛出时，调用方据此报告已发生的计数与失败。
    """
    if counts is not None and counts != RoundCounts():
        raise ValueError("counts 必须是空的 RoundCounts（就地累计本轮）")
    if failures:
        raise ValueError("failures 必须是空列表（就地累计本轮）")
    options = options or RoundOptions()
    progress = progress if progress is not None else RoundProgress()
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
        counts=counts,
        failures=failures,
    )
    cursor = store.cursor if supports_cursor else None
    state.emit("round.started", "info", {"cursor_given": cursor is not None})
    fatal: RunFailure | None = None
    final: ScanBatch | None = None
    rejected_reads = _BatchReadRejected(state)
    pusher_log = logging.getLogger("dpe_sdk.pusher")
    pusher_log.addHandler(rejected_reads)
    try:
        await negotiate_capabilities(pusher.client)
        state.contract = (await pusher.client.core()).contract
        final = await state.run(cursor)
        progress.incremental = final.incremental  # 收到结束批即确定（§7.3），唯一来源
        _check_final(final, cursor_given=cursor is not None, supports_cursor=supports_cursor)
        # 推进游标之前对写入集合对账（§6.8「写入集合与刷新」，#213）；游标仍取自增量扫描
        await _reconcile_written(state, store, final, supports_cursor)
    except RunFailure as failure:
        fatal = failure
        if failure.code == "state_unavailable":
            state.save_failed = True
    finally:
        pusher_log.removeHandler(rejected_reads)
        state.stage.close()
    # 等待目标 document 的 move 在轮末下结论，先于轮结果判定（未达成的扣住游标）
    state.settle_awaiting_moves(aborted=fatal is not None)
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
    incremental = progress.incremental
    outcome: Outcome = (
        "failed" if fatal is not None else "partial" if state.item_failed else "succeeded"
    )
    new_cursor = final.cursor if final is not None and supports_cursor else None
    try:
        # 支持游标的插件给 null 时保留已缓存的游标（§6.8）；不支持游标的实例不缓存
        held_since = store.finish_round(
            succeeded=outcome == "succeeded",
            cursor=new_cursor,
            started_at=started_at,
            keep_cached=supports_cursor,
        )
    except RunFailure as failure:
        held_since = store.reported_held_since  # 报告照常取本轮计算出的值（§7.4）
        if store.cursor_lost:
            # 新游标随这次写入持久化：写不进去即游标不推进，本轮 failed（§6.8 fail-closed）
            state.record(failure)
            fatal = fatal or failure
            outcome = "failed"
        else:
            # 只是游标扣住记录（§7.4，含 succeeded 轮的清除）写不进去：不影响轮结果、不计入
            # failures，只记日志（§7.2）
            failure.redact(state.redact)
            state.emit("state.held_write_failed", "warn", {"failure": failure.to_report()})
    # 唯一来源是收尾落盘本身（§7.4）：新游标与已存值相同（未重写）同样算已持久化；结束批
    # cursor 为 null 的轮为 false（保留的旧游标不是本轮持久化的）
    cursor_persisted = store.cursor_persisted  # 非成功轮由 finish_round 给出 false

    return RoundResult(
        outcome=outcome,
        incremental=incremental,
        cursor_persisted=cursor_persisted,
        cursor_held_since=held_since,
        counts=state.counts,
        started_at=started_at,
        ended_at=clock(),
        failures=state.failures,
        fatal=fatal,
        cursor_given=cursor is not None,
    )
