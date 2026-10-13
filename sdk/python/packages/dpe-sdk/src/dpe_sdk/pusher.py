"""增量推送器（#14）：把一批文档变成「只推变化的部分」，本地状态只作缓存；删除与移动意图
按 connector 契约 §7.2 执行与回报。

**分层**：``StatefulPusher`` / ``AsyncStatefulPusher`` 建在 ``DpeClient`` / ``AsyncDpeClient``
与核心的 ``deliver`` / ``batch_head`` / ``delete`` / ``move`` 之上，只做编排——CAS、412、
响应丢失重发全部由核心与传输层承担，这里不另写一套，也**绝不自动 force**（move 本就不支持）。

**缓存语义**（connector 契约 §7.2，本模块实现其机制、策略归调用方）：

- **同步状态**：每篇文档最近一次确认的 remote doc_hash（类比 Git 的 remote-tracking ref），
  键为 core §1.1 规范化后的 file_uri；只作缓存，丢失时退化为以即时读到的 R 作 base、
  冲突保护只覆盖读写之间的竞态窗口，结果仍然正确。
- **意图日志**：写入请求发出前先持久化本次写入预期的 remote 状态——commit 记 ``uri → N``、
  delete 记 ``uri → 不存在``、move 同时记两侧 ``from_uri → 不存在``、``to_uri → base_hash``——
  本模块**在 ``StateStore.save`` 返回之后才发出写请求**。结果一旦确定（成功、失败、含冲突）
  即清除对应意图；只有结果未知才保留——判据是核心的 ``outcome_unknown`` 标记：写请求本身
  任何一次尝试结果不确定（传输异常未带「确定未发出」底标、或无 DPE 错误码的任何响应——
  含校验失败的 2xx、未化解的裸 412、无码 4xx/5xx）即未知，**包括「首发丢响应、重发得到
  DPE 错误或建连失败」这类首发可能已生效的序列**。下一轮判定前先对账：R 等于待定意图值
  （含「不存在」）即视为本实例自己已生效的写入，按意图更新同步状态后再查判定表。
- **先存、存成功才生效**：写前那次 ``save`` 失败时，store 的异常原样抛出、整批未发写，**本批
  意图不进入内存**——否则它会随下一次成功的保存落盘，成为「幽灵意图」，他人随后写下同值时被
  误认为自身写入而静默覆盖（#134）。判定阶段按真实读到的远端值完成的对账保留，下次保存时一并
  落盘。收尾那次 ``save`` 失败时同样整批报错（写入已发出）：已确定的结果已并入内存，同一实例
  继续可用；只有结果未知的写入在下一轮按意图对账。
- **store 与内存不一致**：任何一次 ``save`` 失败后，store 都可能与内存不一致——收尾失败时
  store 落后于内存（仍留着内存里已清除的意图，如已确定失败的写入）；落盘实现若在写入完成后才
  抛错（如目录 ``fsync`` 失败），写前那次失败也会让 store 领先于内存（留着从未发出的意图）。
  继续使用同一个推送器实例，直到下一次 ``save`` 成功；在那之前**不要用同一个 store 新建
  实例**——读回的过期意图会在他人恰好写下同值时被误认为自身写入。进程崩溃后重新载入属于
  §7.2 本就接受的崩溃窗口。
- **条目维护**：成功或已达成时——commit 记 ``uri → doc_hash``；delete 删除该条目；move 把
  条目从 ``from_uri`` 迁到 ``to_uri``（值不变）。冲突与失败不更新。
- **按 remote 隔离**：状态经 ``StateStore`` 以 remote 为命名空间存取，同一 store 服务多个
  remote 也不会串；缺省 ``MemoryStateStore`` 只在进程内、**不保证崩溃持久**（持久化实现属于
  运行器的状态目录，connector §4.4）。

**前置条件判定表**（L = 同步状态，R = ``batch_head`` 读到的 remote 值，N = 本地内容 doc_hash；
``expected_base`` 存在时覆盖 L，值为 ``None`` 即调用方确认「不存在」）：

- commit：R == N → 已达成，跳过（``achieved``）——这一行先于 ``expected_base`` 的判定：内容
  已相同，即使调用方声明「不存在」也记 ``achieved``；无 L（含契约不一致的条目按缺失）、
  R 不存在 → ``if_absent``；R 存在 → 以 R 作 base；有 L、R == L → 以 L 作 base；有 L、
  R ≠ L 或 R 不存在；以及 ``expected_base`` 声明「不存在」而 R 存在 → **冲突**，不发写、
  如实回报（不自动重提；上层可用 ``expected_base`` 覆盖后重提）。
- delete / move 用同一张表（move 取 ``from_uri`` 的 L 与 R，并以 ``head(to_uri)`` 判已达成）：
  - delete：R 不存在 → 目标状态已达成（``deleted``，删条目、不发写）；无 L、R 存在 → 以 R
    作 base 删除；有 L、R == L → 以 L 作 base 删除；有 L、R ≠ L → **冲突**；写后服务端报
    「文档不存在」同样计 ``deleted``（已达成）。
  - move：源 R 不存在时读目标——``head(to_uri)`` 等于 base 即 **已达成**（``moved``，迁条目、
    不发写），否则**条目失败**（源已不存在、目标未达成，§7.2）；源 R ≠ base → **冲突**
    （``side="from"``）；写后目标被占（``DPE_ALREADY_EXISTS``）→ **条目失败**（``side="to"``，
    §7.2 列为不可由策略重判定）、源被改写（413 前置条件不符）→ **冲突**（``side="from"``）。
- 比较 MUST 在同一 hash 契约下进行：同步状态中与当前契约不一致的条目按缺失处理。

**写后冲突的重读先套用「已达成」行**（§7.2 冲突处置表，与策略无关，#140）：写入得到 CAS
冲突后推送器重读该 URI——commit 重读值等于本次内容 N 记 ``achieved``（确认 L = N、清意图），
delete 重读不存在记 ``deleted``（删条目、清意图），二者都带重读值与原 CAS 错误、不重提；重读
本身失败时仍回报 ``conflict``（不臆测）。move 不在此列：源被改写时源仍存在。

判定表判出的冲突（未发写）不产生新意图、也**不清除**既有待定意图：那条待定写入的结果仍然
未知（R ≠ 意图值既兼容「未落地」也兼容「落地后被覆盖」），按 §7.2 只有结果确定才清除。

**状态词表**（``PushResult.status``）：``created`` / ``updated`` / ``unchanged``（commit，
``unchanged`` 为服务端转述）/ ``achieved``（commit 的 R == N 跳过，或写后冲突重读等于 N）/
``deleted``（含写后服务端报不存在、写后冲突重读不存在）/
``moved``（含按 core §5.2 视为已达成的情形）/ ``conflict`` / ``failed``。``PushResult.side``
标明 move 的问题发生在源侧还是目标侧（``side="to"`` 的目标被占按 §7.2 记 ``failed``，
仍带 side 供上层处置）。**冲突与失败的 ``error`` 保留服务端原样的错误对象（含 ``code``）**，
供运行器按 §7.1 填 ``remote`` 来源；仅当写前判定（未发写、无服务端裁决）时给出同码的本地
构造（如「源已不存在、目标未达成」给 ``DPE_NOT_FOUND``）。

**边界（交给运行器 #52）**：本模块**不设 ``retryable``、不在内部重试冲突**——§7.1 的
「本可自动重新判定的冲突因超过重试上限而失败时取 true」由运行器按自己的轮内计数决定；
推送器只如实上报单次结果（``conflict`` / ``failed`` 与各自的 ``error``）。
``move`` 写前的已达成判定：``R(from)`` 不存在时查 ``head(to)``——等于 base 判 ``moved``，
否则判 ``failed``（``side="from"``；**即使 to 存在但 hash 不同也不是目标侧被占**——源已不在，
失败原因在源侧）。

**快/暂存分界**：完全复用核心 ``deliver`` 的判据（inline 请求体 ≤
``capabilities.limits.max_payload_bytes`` 走快路径，否则 negotiate → upload → commit），
本模块不引入任何阈值；``concurrency`` 只限制并发度（并发调用写操作，每条互不拖累）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from functools import partial
from typing import Literal, Protocol, TypeAlias

import anyio
import dpe_hash
from dpe_hash import DpeHashError

from dpe_sdk.errors import (
    AlreadyExistsError,
    NotFoundError,
    PreconditionFailedError,
)
from dpe_sdk.models import Document
from dpe_sdk.protocol import BaseHash, BlobSource, IfAbsent, outcome_unknown
from dpe_sdk.transport import AsyncDpeClient, DpeClient
from dpe_sdk.wire import CommitResult, Delta

__all__ = [
    "AsyncStatefulPusher",
    "BlobLookup",
    "MemoryStateStore",
    "PushResult",
    "PushStatus",
    "RemoteState",
    "StateSnapshot",
    "StateStore",
    "StatefulPusher",
]

#: 一条回报的状态；``deleted`` / ``moved`` 含按 core §5.2 视为已达成的情形（§7.3 计数口径）
PushStatus = Literal[
    "created", "updated", "unchanged", "achieved", "deleted", "moved", "conflict", "failed"
]

#: 按 ``sha256:`` 引用给出 blob 字节（同核心 ``deliver`` 的 ``blobs``）
BlobLookup: TypeAlias = Callable[[str], BlobSource | bytes]

#: 一条回报对应的操作
PushOp = Literal["commit", "delete", "move"]

#: move 的问题发生在源侧还是目标侧
ConflictSide = Literal["from", "to"]


@dataclass(frozen=True)
class RemoteState:
    """读到的一篇文档的 remote 状态：``doc_hash`` 为 ``None`` 表示文档不存在。"""

    doc_hash: str | None


@dataclass(frozen=True)
class StateSnapshot:
    """推送器本地状态的一次快照。

    - ``synced``：同步状态（规范化 file_uri → 最近一次确认的 remote doc_hash）；
    - ``intents``：待定意图（规范化 file_uri → doc_hash 或 ``None``＝「不存在」）。
    """

    synced: Mapping[str, str] = field(default_factory=dict)
    intents: Mapping[str, str | None] = field(default_factory=dict)


class StateStore(Protocol):
    """推送器状态的持久化缝（按 remote 命名空间存取）。

    ``save`` 返回即表示**这一批意图已经持久化**（按其实现承诺的强度），推送器保证在它返回
    之后才发出对应的写请求；实现负责在崩溃后仍能读回（落盘实现属运行器状态目录，§4.4）。
    """

    def load(self, remote: str) -> StateSnapshot | None:
        """读回该 remote 的状态；从未保存过时返回 ``None``。"""
        ...

    def save(self, remote: str, snapshot: StateSnapshot) -> None:
        """整批保存该 remote 的状态。"""
        ...


class MemoryStateStore:
    """进程内的缺省实现：**不保证崩溃持久**，仅供单次运行与测试使用。"""

    def __init__(self) -> None:
        self._states: dict[str, StateSnapshot] = {}

    def load(self, remote: str) -> StateSnapshot | None:
        return self._states.get(remote)

    def save(self, remote: str, snapshot: StateSnapshot) -> None:
        self._states[remote] = snapshot


@dataclass(frozen=True)
class PushResult:
    """一条意图/条目的推送结果（按操作回报，connector 契约 §7.2）。

    每条回报对应一个意图/条目，含涉及的 URI：commit 与 delete 以 ``file_uri`` 标识，move 的
    源在 ``file_uri``、目标在 ``to_uri``。

    - ``precondition``：本次写入使用的前置条件（commit 的 ``BaseHash`` / ``IfAbsent``，
      delete / move 的 ``BaseHash``）；未发写时为 ``None``；
    - ``remote``：判定表读到的（写后 CAS 冲突时重读到的）remote 状态——重读即已达成的
      ``achieved`` / ``deleted`` 同样带重读值；``None`` 表示未读到（正常写入成功、未发写的
      判定不重读、或重读本身失败）；move 为源侧；
    - ``delta``：commit 成功时服务端给出的元素级差值；
    - ``to_uri``：move 专用——归一化后的目标 URI（其余操作为 ``None``）；
    - ``side``：move 专用——问题发生在源侧（``"from"``）还是目标侧（``"to"``，如目标已被占用
      的条目失败）；非侧向问题为 ``None``；
    - ``error``：失败/冲突时的异常（``DpeError`` 或传输层异常，按其类型区分；冲突时为裁决它
      的 CAS 错误，其 ``code`` 可原样上报，§7.1）。**已达成也可能携带**：delete 的「服务端报
      文档不存在」、写后 CAS 冲突而重读即目标状态的 ``achieved`` / ``deleted``，都带原服务端
      错误对象供归因——**成败一律看 ``status``，不看 ``error`` 是否为空**；
    - ``remote_error``：冲突后重读失败的异常（此时 ``remote`` 为 ``None``、``error`` 仍是
      CAS 错误）；
    - ``unknown``：结果未确定（响应丢失、请求可能已发出的传输失败等）——对应的待定意图被
      保留，下一轮按意图对账恢复；为 ``False`` 的失败都是已确定的（服务端已裁决、确定未发出、
      或本地校验未发出）。

    ``status`` 的来源：``achieved``＝commit 判定表初判 R == N（推送器自己判定，跳过写入），
    或写后 CAS 冲突、重读值等于本次内容（带重读值与原 CAS 错误）；
    ``unchanged``＝如实转述服务端 commit 的结果（本模块不自行产出）；``created`` /
    ``updated`` 同；``deleted`` / ``moved``＝delete / move 成功，或目标状态已达成（含写后
    服务端报「不存在」的 delete、写后 CAS 冲突且重读不存在的 delete，§7.2）；``conflict``＝
    判定表判定或服务端 CAS 裁决且重读不是目标状态（后者带重读到的 ``remote``）；
    ``failed``＝不可重试的失败（``unknown`` 见上）。
    """

    op: PushOp
    file_uri: str
    status: PushStatus
    precondition: BaseHash | IfAbsent | None = None
    delta: Delta | None = None
    remote: RemoteState | None = None
    to_uri: str | None = None
    side: ConflictSide | None = None
    error: Exception | None = None
    remote_error: Exception | None = None
    unknown: bool = False


@dataclass(frozen=True)
class _Entry:
    """commit 的已校验输入条目（uri 已归一化）。"""

    uri: str
    document: Document
    new_hash: str


@dataclass(frozen=True)
class _Plan:
    """一次写操作的判定结果（commit / delete / move 共用；字段按操作取用）。

    只覆盖「判定」阶段：``achieved``（已达成，跳过）、``write``（发写）、``conflict``
    （判定表判出的冲突，未发写）、``failed``（判定判出的条目失败，未发写，如 move 的
    「源已不存在、目标未达成」——``error`` 带原因）。写后的结局在 ``_Outcome``。
    """

    op: PushOp
    action: Literal["achieved", "write", "conflict", "failed"]
    #: commit / delete 的文档 uri；move 的 from_uri（均归一化）
    uri: str
    #: 判定表读到的 R（move 为源侧）
    remote: RemoteState
    #: 本次写入使用的前置条件；未发写或判定失败为 ``None``（move 的 base 也在这里）
    precondition: BaseHash | IfAbsent | None = None
    #: commit：展开文档（write 时内联投递）与本地内容 hash（已达成 / 成功后确认 L=N）
    document: Document | None = None
    new_hash: str | None = None
    #: move：目标 uri（归一化）与本次使用的 base_hash（已达成 / 迁移条目时也用）
    to_uri: str | None = None
    base: str | None = None
    #: action == "failed" 时的判定错误（未发写）
    error: Exception | None = None


@dataclass(frozen=True)
class _Outcome:
    """一次写入的执行结果（由各驱动的 ``_*_one`` 产出，``_finish_*`` 消费）。"""

    result: CommitResult | None = None
    error: Exception | None = None
    #: 出错时的归类：``conflict``（CAS 裁决）、``achieved``（服务端报「不存在」的 delete）、
    #: ``failed``（其余已确定失败；未确定的另见 ``unknown``）
    kind: Literal["ok", "conflict", "achieved", "failed"] = "ok"
    unknown: bool = False
    #: move 的问题发生在哪一侧
    side: ConflictSide | None = None
    #: 冲突后的重读结果；``None`` 表示重读失败或未重读
    remote: RemoteState | None = None
    remote_error: Exception | None = None


def _matches_contract(value: str, contract: str) -> bool:
    """同步状态中的值是否属于当前契约（旧契约条目按缺失处理）。"""
    try:
        dpe_hash.parse_hash(value, (contract,))
    except DpeHashError:
        return False
    return True


class _PusherBase:
    """两个推送器的共享配置与纯逻辑（校验、判定表、意图对账、状态维护、结果构造），不做 I/O。"""

    def __init__(
        self, remote: str, *, state: StateStore | None = None, concurrency: int = 4
    ) -> None:
        if isinstance(concurrency, bool) or not isinstance(concurrency, int) or concurrency < 1:
            raise ValueError(f"concurrency 必须是正整数，实际为 {concurrency!r}")
        self.remote = remote
        self._store: StateStore = state if state is not None else MemoryStateStore()
        self._concurrency = concurrency
        snapshot = self._store.load(self.remote)
        self._synced: dict[str, str] = dict(snapshot.synced) if snapshot else {}
        self._intents: dict[str, str | None] = dict(snapshot.intents) if snapshot else {}

    # ------------------------------------------------------------------
    # 批准备与判定（纯逻辑）
    # ------------------------------------------------------------------

    def _prepare(
        self,
        documents: Mapping[str, Document],
        expected_base: Mapping[str, str | None] | None,
        contract: str,
    ) -> tuple[list[_Entry], dict[str, str | None]]:
        """本地校验并归一化：uri 文法（core §1.1）、归一化后重复、``expected_base`` 的范围与取值。

        任何一项不合法都在发请求前抛 ``ValueError``（dpe_hash 的错误是其子类），整批拒绝——
        避免部分写入之后才发现拼写错误。
        """
        if not isinstance(documents, Mapping):
            raise TypeError(
                f"documents 必须是 file_uri → Document 的映射，实际为 {type(documents).__name__}"
            )
        entries: list[_Entry] = []
        seen: dict[str, str] = {}
        for uri, document in documents.items():
            if not isinstance(document, Document):
                raise TypeError(
                    f"documents[{uri!r}] 必须是 dpe_sdk.Document，实际为 {type(document).__name__}"
                )
            normalized = dpe_hash.normalize_file_uri(uri)
            if normalized in seen:
                raise ValueError(f"归一化后重复的 file_uri：{seen[normalized]!r} 与 {uri!r}")
            seen[normalized] = uri
            entries.append(_Entry(normalized, document, document.doc_hash(contract)))
        return entries, self._prepare_expected(expected_base, set(seen), contract)

    def _prepare_uris(
        self,
        uris: Sequence[str],
        expected_base: Mapping[str, str | None] | None,
        contract: str,
    ) -> tuple[list[str], dict[str, str | None]]:
        """delete 的输入校验与归一化（与 ``_prepare`` 同一口径：整批拒绝）。"""
        if isinstance(uris, (str, bytes)) or not isinstance(uris, Sequence):
            raise TypeError(f"uris 必须是 file_uri 的序列（不能是单个字符串）：{uris!r}")
        normalized: list[str] = []
        seen: dict[str, str] = {}
        for uri in uris:
            value = dpe_hash.normalize_file_uri(uri)
            if value in seen:
                raise ValueError(f"归一化后重复的 file_uri：{seen[value]!r} 与 {uri!r}")
            seen[value] = uri
            normalized.append(value)
        return normalized, self._prepare_expected(expected_base, set(seen), contract)

    def _prepare_moves(
        self,
        moves: Sequence[tuple[str, str]],
        expected_base: Mapping[str, str | None] | None,
        contract: str,
    ) -> tuple[list[tuple[str, str]], dict[str, str | None]]:
        """move 的输入校验与归一化；``expected_base`` 以 from_uri 为键（§7.2 取源的 L）。

        同一 URI 在本批的 from 与 to 合集中至多出现一次：链式（a→b、b→c）或共用目标（a→c、b→c）
        的生效依赖顺序（connector §6.5），而一批内的写请求并发执行，因此整批拒绝；
        保序拆批归调用方（#52 运行器）。
        """
        if isinstance(moves, (str, bytes)) or not isinstance(moves, Sequence):
            raise TypeError(f"moves 必须是 (from_uri, to_uri) 元组的序列：{moves!r}")
        pairs: list[tuple[str, str]] = []
        sources: set[str] = set()
        seen: dict[str, str] = {}
        for item in moves:
            if not isinstance(item, tuple) or len(item) != 2:
                raise TypeError(f"moves 的每一项必须是 (from_uri, to_uri) 二元组：{item!r}")
            from_uri, to_uri = item
            source = dpe_hash.normalize_file_uri(from_uri)
            target = dpe_hash.normalize_file_uri(to_uri)
            if source == target:
                raise ValueError(f"move 的源与目标是同一篇文档：{source!r}")
            for normalized, raw in ((source, from_uri), (target, to_uri)):
                if normalized in seen:
                    raise ValueError(
                        f"同一批 move 中重复的 URI（归一化后）：{seen[normalized]!r} 与 {raw!r}；"
                        "链式或共用目标须由调用方保序拆批"
                    )
                seen[normalized] = raw
            sources.add(source)
            pairs.append((source, target))
        return pairs, self._prepare_expected(expected_base, sources, contract)

    def _prepare_expected(
        self,
        expected_base: Mapping[str, str | None] | None,
        allowed: set[str],
        contract: str,
    ) -> dict[str, str | None]:
        """校验 ``expected_base``：键必须属于本次操作的目标，值必须是本契约的 hash 或 ``None``。"""
        expected: dict[str, str | None] = {}
        for uri, value in (expected_base or {}).items():
            normalized = dpe_hash.normalize_file_uri(uri)
            if normalized not in allowed:
                raise ValueError(
                    f"expected_base 的 {uri!r} 不在本次操作的目标里（归一化后 {normalized!r}）"
                )
            if value is not None:
                if not isinstance(value, str):
                    raise TypeError(
                        f"expected_base[{uri!r}] 必须是 doc_hash 或 None，"
                        f"实际为 {type(value).__name__}"
                    )
                if not _matches_contract(value, contract):
                    raise ValueError(
                        f"expected_base[{uri!r}] 不是契约 {contract} 的 hash：{value!r}"
                    )
            expected[normalized] = value
        return expected

    # ------------------------------------------------------------------
    # 判定（纯逻辑）：对账 → 解 L → 查表；三个操作共用对账与解 L
    # ------------------------------------------------------------------

    def _reconcile(self, uri: str, head: str | None) -> None:
        """意图对账（§7.2）：R 等于待定意图值（含「不存在」）＝ 本实例自己已生效的写入。"""
        if uri in self._intents and self._intents[uri] == head:
            confirmed = self._intents.pop(uri)
            if confirmed is None:
                self._synced.pop(uri, None)
            else:
                self._synced[uri] = confirmed

    def _base_of(
        self, uri: str, expected: Mapping[str, str | None], contract: str
    ) -> tuple[str | None, bool]:
        """解出 L：``expected_base`` 覆盖同步状态（``None`` = 确认「不存在」）；旧契约条目按缺失。

        返回 ``(base, known_absent)``：``base`` 为 ``None`` 时可能是「无 L」也可能是「确认不存在」，
        由 ``known_absent`` 区分（后者在 R 存在时判冲突，不静默覆盖）。
        """
        if uri in expected:
            base = expected[uri]
            return base, base is None
        base = self._synced.get(uri)
        if base is not None and not _matches_contract(base, contract):
            base = None
        return base, False

    def _plan_commit(
        self,
        entries: Sequence[_Entry],
        heads: Sequence[str | None],
        expected: Mapping[str, str | None],
        contract: str,
    ) -> list[_Plan]:
        """commit 的判定表（§7.2）：只更新内存状态，持久化归 ``_persist`` / ``_save``。"""

        def plan(
            entry: _Entry,
            action: Literal["achieved", "write", "conflict"],
            remote: RemoteState,
            precondition: BaseHash | IfAbsent | None = None,
        ) -> _Plan:
            return _Plan(
                "commit",
                action,
                entry.uri,
                remote,
                precondition=precondition,
                document=entry.document,
                new_hash=entry.new_hash,
            )

        plans: list[_Plan] = []
        for entry, head in zip(entries, heads, strict=True):
            uri = entry.uri
            self._reconcile(uri, head)
            base, known_absent = self._base_of(uri, expected, contract)
            remote = RemoteState(head)
            if head == entry.new_hash:
                plans.append(plan(entry, "achieved", remote))
            elif known_absent or base is None:
                # 无 L（或调用方确认不存在）：R 不存在即 if_absent，否则以 R 作 base；调用方确认
                # 不存在而 R 存在＝与确认矛盾，判冲突（不静默覆盖）
                if head is None:
                    plans.append(plan(entry, "write", remote, IfAbsent()))
                elif known_absent:
                    plans.append(plan(entry, "conflict", remote))
                else:
                    plans.append(plan(entry, "write", remote, BaseHash(head)))
            elif head == base:
                plans.append(plan(entry, "write", remote, BaseHash(base)))
            else:
                plans.append(plan(entry, "conflict", remote))
        return plans

    def _plan_delete(
        self,
        uris: Sequence[str],
        heads: Sequence[str | None],
        expected: Mapping[str, str | None],
        contract: str,
    ) -> list[_Plan]:
        """delete 的判定表（§7.2）：R 不存在＝目标状态已达成（不发写）。"""
        plans: list[_Plan] = []
        for uri, head in zip(uris, heads, strict=True):
            self._reconcile(uri, head)
            base, known_absent = self._base_of(uri, expected, contract)
            remote = RemoteState(head)
            if head is None:
                plans.append(_Plan("delete", "achieved", uri, remote))
            elif known_absent:
                plans.append(_Plan("delete", "conflict", uri, remote))  # 确认不存在而存在
            elif base is None:
                plans.append(_Plan("delete", "write", uri, remote, precondition=BaseHash(head)))
            elif head == base:
                plans.append(_Plan("delete", "write", uri, remote, precondition=BaseHash(base)))
            else:
                plans.append(_Plan("delete", "conflict", uri, remote))
        return plans

    def _plan_move(
        self,
        moves: Sequence[tuple[str, str]],
        from_heads: Sequence[str | None],
        to_heads: Sequence[str | None],
        expected: Mapping[str, str | None],
        contract: str,
    ) -> list[_Plan]:
        """move 的判定表（§7.2「取 from_uri 的 L 与 R」；源不存在时以目标侧判已达成）。

        源已不存在而目标未达成／base 不可知 → 判定失败（条目失败，§7.2 列为不可由策略重判定）；
        调用方确认源「不存在」而实际存在 → 与确认矛盾，判冲突（同 commit / delete 的表口径）；
        源被改写 → 冲突（``side="from"``）。

        源不存在且源无 L 时，以目标侧的 L 作 base 候选：迁移后的条目就是本实例完成过的 move
        （成功后条目已从源迁到目标；未确认的 to 侧意图也已由上面的对账并入），重放据此判已达成，
        与服务端对同一请求的裁决一致（core §5.2）。回退只在源无 L 时生效，
        同 §7.2「无 L」的退化口径。因此「已达成」只表示「源不存在、目标内容等于本实例确认过的值」，
        不担保这次移动由本实例发生（与 core §5.2 第 4 步只保证状态同义）。
        """
        plans: list[_Plan] = []
        for (from_uri, to_uri), from_head, to_head in zip(moves, from_heads, to_heads, strict=True):
            self._reconcile(from_uri, from_head)
            self._reconcile(to_uri, to_head)
            base, known_absent = self._base_of(from_uri, expected, contract)
            if from_head is None and base is None and not known_absent:
                base, _ = self._base_of(to_uri, {}, contract)
            remote = RemoteState(from_head)
            if known_absent and from_head is not None:
                # 调用方确认「不存在」而源实际存在：与确认矛盾，判冲突（不静默覆盖）
                plans.append(_Plan("move", "conflict", from_uri, remote, to_uri=to_uri))
            elif from_head is None or known_absent:
                if base is not None and to_head == base:
                    plans.append(
                        _Plan("move", "achieved", from_uri, remote, to_uri=to_uri, base=base)
                    )
                else:
                    plans.append(
                        _Plan(
                            "move",
                            "failed",
                            from_uri,
                            remote,
                            to_uri=to_uri,
                            error=NotFoundError(
                                f"{from_uri} 已不存在，且目标 {to_uri} 未达成（未发写）"
                            ),
                        )
                    )
            elif base is None:
                plans.append(
                    _Plan(
                        "move",
                        "write",
                        from_uri,
                        remote,
                        precondition=BaseHash(from_head),
                        to_uri=to_uri,
                        base=from_head,
                    )
                )
            elif from_head == base:
                plans.append(
                    _Plan(
                        "move",
                        "write",
                        from_uri,
                        remote,
                        precondition=BaseHash(base),
                        to_uri=to_uri,
                        base=base,
                    )
                )
            else:
                plans.append(_Plan("move", "conflict", from_uri, remote, to_uri=to_uri))
        return plans

    # ------------------------------------------------------------------
    # 意图持久化与状态维护
    # ------------------------------------------------------------------

    def _persist(self, intents: Mapping[str, str | None]) -> None:
        """写前先把这一批意图持久化（delete 记「不存在」、move 两侧一起记）；``save`` 返回之后
        调用方才允许发出写请求（§7.2）。"""
        if not intents:
            return
        # 先存、存成功才生效（#134）：在副本上合并，save 返回后才替换内存中的意图；save 抛出时
        # 内存保持原样（不按键删除——同一个键上可能原本挂着结果未知的待定意图）
        pending = {**self._intents, **intents}
        self._save(pending)
        self._intents = pending

    def flush(self) -> None:
        """把内存中的同步状态与意图整批写入 ``StateStore``（store 的异常原样抛出）。

        某次 ``save`` 失败后 store 可能与内存不一致；调用方（如运行器）用同一实例调用它补存，
        成功后两者重新一致。
        """
        self._save()

    def _save(self, intents: Mapping[str, str | None] | None = None) -> None:
        """整批保存同步状态与意图；``intents`` 缺省为内存中的意图（快照只在这里构造）。"""
        self._store.save(
            self.remote,
            StateSnapshot(
                synced=dict(self._synced),
                intents=dict(self._intents if intents is None else intents),
            ),
        )

    def _move_intents(self, plans: Sequence[_Plan]) -> dict[str, str | None]:
        """move 写前的一批意图：两侧一起记（``from → 不存在``、``to → base``）。"""
        intents: dict[str, str | None] = {}
        for plan in plans:
            if plan.action != "write":
                continue
            assert plan.to_uri is not None and plan.base is not None
            intents[plan.uri] = None
            intents[plan.to_uri] = plan.base
        return intents

    def _confirm(self, uri: str, doc_hash: str | None) -> None:
        """确认一篇文档的同步状态并清意图：``doc_hash`` 为 ``None`` 即删除条目（不存在）。"""
        if doc_hash is None:
            self._synced.pop(uri, None)
        else:
            self._synced[uri] = doc_hash
        self._intents.pop(uri, None)

    def _clear_intents(self, plan: _Plan) -> None:
        """清除一条计划涉及的意图（move 两侧一起）。"""
        self._intents.pop(plan.uri, None)
        if plan.to_uri is not None:
            self._intents.pop(plan.to_uri, None)

    def _migrate(self, plan: _Plan) -> None:
        """move 成功/已达成：条目从 from 迁到 to（值不变），两侧意图清除。"""
        assert plan.to_uri is not None and plan.base is not None
        self._synced.pop(plan.uri, None)
        self._synced[plan.to_uri] = plan.base
        self._clear_intents(plan)

    def _outcome_failure(self, plan: _Plan, outcome: _Outcome) -> PushResult:
        """写后失败的公共归类（三个操作共用）：冲突、未确定（保留意图）、已确定失败（清意图）。"""
        if outcome.kind == "conflict":
            self._clear_intents(plan)
            return PushResult(
                plan.op,
                plan.uri,
                "conflict",
                plan.precondition,
                remote=outcome.remote,
                to_uri=plan.to_uri,
                side=outcome.side,
                error=outcome.error,
                remote_error=outcome.remote_error,
            )
        if outcome.unknown:
            return PushResult(
                plan.op,
                plan.uri,
                "failed",
                plan.precondition,
                remote=outcome.remote,
                to_uri=plan.to_uri,
                side=outcome.side,
                error=outcome.error,
                remote_error=outcome.remote_error,
                unknown=True,
            )
        self._clear_intents(plan)
        return PushResult(
            plan.op,
            plan.uri,
            "failed",
            plan.precondition,
            remote=outcome.remote,
            to_uri=plan.to_uri,
            side=outcome.side,
            error=outcome.error,
            remote_error=outcome.remote_error,
        )

    # ------------------------------------------------------------------
    # 收尾：把执行结果并入状态并构造回报
    # ------------------------------------------------------------------

    def _finish_commit(
        self, plans: Sequence[_Plan], outcomes: Sequence[_Outcome | None]
    ) -> list[PushResult]:
        """commit 的收尾（意图：结果确定即清除，未确定（``unknown``）才保留）。"""
        results: list[PushResult] = []
        for plan, outcome in zip(plans, outcomes, strict=True):
            uri = plan.uri
            if plan.action == "achieved":
                assert plan.new_hash is not None
                self._confirm(uri, plan.new_hash)
                results.append(PushResult("commit", uri, "achieved", remote=plan.remote))
                continue
            if plan.action == "conflict":
                # 未发写：不产生新意图，也不清除既有待定意图（那条写入的结果仍未知，见模块说明）
                results.append(PushResult("commit", uri, "conflict", remote=plan.remote))
                continue
            assert outcome is not None  # write 计划必有执行结果
            if (
                outcome.kind == "conflict"
                and outcome.remote is not None
                and outcome.remote.doc_hash == plan.new_hash
            ):
                # §7.2 冲突处置表 commit 行：重读值等于本次内容＝已达成（与策略无关）；
                # 确认 L = N、清意图，原 CAS 错误一并带上供归因
                assert plan.new_hash is not None
                self._confirm(uri, plan.new_hash)
                results.append(
                    PushResult(
                        "commit",
                        uri,
                        "achieved",
                        plan.precondition,
                        remote=outcome.remote,
                        error=outcome.error,
                    )
                )
                continue
            if outcome.error is not None:
                results.append(self._outcome_failure(plan, outcome))
                continue
            result = outcome.result
            assert result is not None  # 无 error 必有 result
            self._confirm(uri, result.doc_hash)
            results.append(
                PushResult("commit", uri, result.status, plan.precondition, delta=result.delta)
            )
        return results

    def _finish_delete(
        self, plans: Sequence[_Plan], outcomes: Sequence[_Outcome | None]
    ) -> list[PushResult]:
        """delete 的收尾：成功、「文档不存在」（服务端裁决）与写后 CAS 冲突而重读不存在都计
        ``deleted``（已达成）。"""
        results: list[PushResult] = []
        for plan, outcome in zip(plans, outcomes, strict=True):
            uri = plan.uri
            if plan.action == "achieved":
                self._confirm(uri, None)  # 目标状态已达成：删条目、清意图
                results.append(PushResult("delete", uri, "deleted", remote=plan.remote))
                continue
            if plan.action == "conflict":
                results.append(PushResult("delete", uri, "conflict", remote=plan.remote))
                continue
            assert outcome is not None
            if outcome.error is not None:
                reread_absent = (
                    outcome.kind == "conflict"
                    and outcome.remote is not None
                    and outcome.remote.doc_hash is None
                )
                if outcome.kind == "achieved" or reread_absent:
                    # 服务端裁决「文档不存在」、或 CAS 冲突后重读不存在＝目标状态已达成（§7.2
                    # 冲突处置表 delete 行，与策略无关）；原错误对象一并带上（含 code）供归因
                    self._confirm(uri, None)
                    results.append(
                        PushResult(
                            "delete",
                            uri,
                            "deleted",
                            plan.precondition,
                            remote=outcome.remote,
                            error=outcome.error,
                        )
                    )
                else:
                    results.append(self._outcome_failure(plan, outcome))
                continue
            self._confirm(uri, None)  # 删除成功
            results.append(PushResult("delete", uri, "deleted", plan.precondition))
        return results

    def _finish_move(
        self, plans: Sequence[_Plan], outcomes: Sequence[_Outcome | None]
    ) -> list[PushResult]:
        """move 的收尾：成功与已达成都计 ``moved`` 并迁移条目；问题带 ``side`` 标明侧别；
        每条回报都带目标 ``to_uri``（"涉及的 URI"）。"""
        results: list[PushResult] = []
        for plan, outcome in zip(plans, outcomes, strict=True):
            uri = plan.uri
            if plan.action == "achieved":
                self._migrate(plan)
                results.append(
                    PushResult("move", uri, "moved", remote=plan.remote, to_uri=plan.to_uri)
                )
                continue
            if plan.action == "conflict":
                results.append(
                    PushResult(
                        "move", uri, "conflict", remote=plan.remote, to_uri=plan.to_uri, side="from"
                    )
                )
                continue
            if plan.action == "failed":
                # 判定失败（源已不存在、目标未达成）：不产生新意图、不清除既有待定意图
                results.append(
                    PushResult(
                        "move",
                        uri,
                        "failed",
                        remote=plan.remote,
                        to_uri=plan.to_uri,
                        error=plan.error,
                        side="from",
                    )
                )
                continue
            assert outcome is not None
            if outcome.error is not None:
                results.append(self._outcome_failure(plan, outcome))
                continue
            self._migrate(plan)
            results.append(PushResult("move", uri, "moved", plan.precondition, to_uri=plan.to_uri))
        return results


class StatefulPusher(_PusherBase):
    """同步增量推送器：``push`` 只推变化的部分，遇冲突如实回报、绝不自动 force。

    状态在构造时从 store 载入一次、每次 ``push`` 结束时整批保存回去；同一 store 上并发使用
    多个推送器不在支持范围（每个 remote 一个实例）。
    """

    def __init__(
        self, client: DpeClient, *, state: StateStore | None = None, concurrency: int = 4
    ) -> None:
        super().__init__(client.remote, state=state, concurrency=concurrency)
        self._client = client

    def push(
        self,
        documents: Mapping[str, Document],
        *,
        expected_base: Mapping[str, str | None] | None = None,
        blobs: BlobLookup | None = None,
    ) -> list[PushResult]:
        """推送一批文档（file_uri → 展开文档）：只推变化的部分，返回与输入同序的逐条回报。

        ``expected_base`` 覆盖同步状态中的 L（值为 ``None`` 即调用方确认「不存在」）；上层可在
        冲突后以重读到的 remote 值重提（source_wins 等策略归上层，本方法不自动重提）。
        ``blobs`` 原样交给每条 ``deliver``：按 ``sha256:`` 引用给出服务端缺少的 blob 字节
        （``BlobSource`` 或内存字节），缺省时缺字节由核心如实报错（不自行解引用任何来源）。
        """
        contract = self._client.core().contract
        entries, expected = self._prepare(documents, expected_base, contract)
        if not entries:
            return []
        heads = self._client.batch_head([entry.uri for entry in entries])
        plans = self._plan_commit(entries, heads, expected, contract)
        self._persist({plan.uri: plan.new_hash for plan in plans if plan.action == "write"})
        outcomes = self._run_all(plans, partial(self._commit_one, blobs=blobs))
        results = self._finish_commit(plans, outcomes)
        self._save()
        return results

    def delete(
        self,
        uris: Sequence[str],
        *,
        expected_base: Mapping[str, str | None] | None = None,
    ) -> list[PushResult]:
        """删除一批文档（按 URI）：CAS 走 ``If-Match``，返回与输入同序的逐条回报。

        - 远端已不存在＝目标状态已达成，记 ``deleted``、不发写；
        - ``expected_base`` 覆盖同步状态中的 L（值为 ``None`` 即确认「不存在」）；
        - 冲突（他人改写）如实回报，绝不自动 force；``source_wins`` 等策略归上层（用重读到的
          值作 ``expected_base`` 重提）。
        """
        contract = self._client.core().contract
        normalized, expected = self._prepare_uris(uris, expected_base, contract)
        if not normalized:
            return []
        heads = self._client.batch_head(normalized)
        plans = self._plan_delete(normalized, heads, expected, contract)
        self._persist({plan.uri: None for plan in plans if plan.action == "write"})
        outcomes = self._run_all(plans, self._delete_one)
        results = self._finish_delete(plans, outcomes)
        self._save()
        return results

    def move(
        self,
        moves: Sequence[tuple[str, str]],
        *,
        expected_base: Mapping[str, str | None] | None = None,
    ) -> list[PushResult]:
        """按 URI 批量原子改名（``(from_uri, to_uri)`` 序列）：CAS 走源文档的 ``base_hash``。

        - 源已不存在而目标内容等于 base＝已达成，记 ``moved``、不发写；源已不存在而目标未达成
          为条目失败（``side="from"``）；
        - 成功与已达成都把同步状态条目从源迁到目标；
        - ``expected_base`` 以 ``from_uri`` 为键（值为 ``None`` 即确认源「不存在」）；
        - 同一 URI 在本批 from 与 to 中至多出现一次（链式、共用目标整批拒绝，保序拆批归调用方）；
        - 目标被占（``DPE_ALREADY_EXISTS``）为条目失败（``side="to"``），源被改写为冲突
          （``side="from"``），都如实回报、绝不自动退化。
        """
        contract = self._client.core().contract
        pairs, expected = self._prepare_moves(moves, expected_base, contract)
        if not pairs:
            return []
        from_heads, to_heads = self._read_move_sides(pairs)
        plans = self._plan_move(pairs, from_heads, to_heads, expected, contract)
        self._persist(self._move_intents(plans))
        outcomes = self._run_all(plans, self._move_one)
        results = self._finish_move(plans, outcomes)
        self._save()
        return results

    def _read_move_sides(
        self, pairs: Sequence[tuple[str, str]]
    ) -> tuple[list[str | None], list[str | None]]:
        """一次 ``batch_head`` 读全部 move 的两侧：先全部源、再全部目标（本批 URI 两两不同）。"""
        heads = self._client.batch_head([f for f, _ in pairs] + [t for _, t in pairs])
        return heads[: len(pairs)], heads[len(pairs) :]

    def _run_all(
        self, plans: Sequence[_Plan], worker: Callable[[_Plan], _Outcome]
    ) -> list[_Outcome | None]:
        """对全部 ``write`` 计划按 ``concurrency`` 有界并发地执行 ``worker``，结果按序回填。"""
        outcomes: list[_Outcome | None] = [None] * len(plans)
        indexes = [index for index, plan in enumerate(plans) if plan.action == "write"]
        if not indexes:
            return outcomes
        if len(indexes) == 1 or self._concurrency == 1:
            for index in indexes:
                outcomes[index] = worker(plans[index])
            return outcomes
        with ThreadPoolExecutor(max_workers=min(self._concurrency, len(indexes))) as pool:
            batch = pool.map(worker, [plans[index] for index in indexes])
            for index, outcome in zip(indexes, batch, strict=True):
                outcomes[index] = outcome
        return outcomes

    def _commit_one(self, plan: _Plan, *, blobs: BlobLookup | None) -> _Outcome:
        assert plan.precondition is not None and plan.document is not None  # write 计划必有
        try:
            result = self._client.deliver(plan.uri, plan.document, plan.precondition, blobs=blobs)
        except (PreconditionFailedError, AlreadyExistsError, NotFoundError) as exc:
            remote, re_error = self._re_read(plan.uri)
            return _Outcome(error=exc, kind="conflict", remote=remote, remote_error=re_error)
        except Exception as exc:
            # 一切非冲突失败都按核心的结果标记归类（含普通 DpeError 与 UnexpectedResponseError）：
            # 首发丢响应、重发得到 429 耗尽、重发全建连失败等序列里首次尝试都可能已生效；
            # 只有核心明确标记「确定未生效」才清除意图，无标记一律按结果未知（保留）。
            return _Outcome(error=exc, unknown=outcome_unknown(exc))
        return _Outcome(result=result)

    def _delete_one(self, plan: _Plan) -> _Outcome:
        assert isinstance(plan.precondition, BaseHash)  # write 计划必有 base
        try:
            self._client.delete(plan.uri, plan.precondition.value)
        except PreconditionFailedError as exc:
            remote, re_error = self._re_read(plan.uri)
            return _Outcome(error=exc, kind="conflict", remote=remote, remote_error=re_error)
        except NotFoundError as exc:
            # 文档不存在即目标状态已达成（§7.2 冲突处置表 delete 行），不是失败
            return _Outcome(error=exc, kind="achieved")
        except Exception as exc:
            return _Outcome(error=exc, unknown=outcome_unknown(exc))
        return _Outcome()

    def _move_one(self, plan: _Plan) -> _Outcome:
        assert plan.to_uri is not None and plan.base is not None  # write 计划必有
        try:
            self._client.move(plan.uri, plan.to_uri, plan.base)
        except PreconditionFailedError as exc:
            remote, re_error = self._re_read(plan.uri)  # 源被改写：重读源侧
            return _Outcome(
                error=exc, kind="conflict", side="from", remote=remote, remote_error=re_error
            )
        except AlreadyExistsError as exc:
            remote, re_error = self._re_read(plan.to_uri)  # 目标被占：重读目标侧
            return _Outcome(
                error=exc, kind="failed", side="to", remote=remote, remote_error=re_error
            )
        except NotFoundError as exc:
            # 源已不存在、目标未达成（§7.2 条目失败）
            return _Outcome(error=exc, kind="failed", side="from")
        except Exception as exc:
            return _Outcome(error=exc, unknown=outcome_unknown(exc))
        return _Outcome()

    def _re_read(self, uri: str) -> tuple[RemoteState | None, Exception | None]:
        try:
            return RemoteState(self._client.head(uri)), None
        except Exception as exc:
            return None, exc


class AsyncStatefulPusher(_PusherBase):
    """``StatefulPusher`` 的异步版本（基于 ``AsyncDpeClient``）。"""

    def __init__(
        self, client: AsyncDpeClient, *, state: StateStore | None = None, concurrency: int = 4
    ) -> None:
        super().__init__(client.remote, state=state, concurrency=concurrency)
        self._client = client

    async def push(
        self,
        documents: Mapping[str, Document],
        *,
        expected_base: Mapping[str, str | None] | None = None,
        blobs: BlobLookup | None = None,
    ) -> list[PushResult]:
        """``StatefulPusher.push`` 的异步版本：语义完全相同。"""
        contract = (await self._client.core()).contract
        entries, expected = self._prepare(documents, expected_base, contract)
        if not entries:
            return []
        heads = await self._client.batch_head([entry.uri for entry in entries])
        plans = self._plan_commit(entries, heads, expected, contract)
        self._persist({plan.uri: plan.new_hash for plan in plans if plan.action == "write"})
        outcomes = await self._run_all(plans, partial(self._commit_one, blobs=blobs))
        results = self._finish_commit(plans, outcomes)
        self._save()
        return results

    async def delete(
        self,
        uris: Sequence[str],
        *,
        expected_base: Mapping[str, str | None] | None = None,
    ) -> list[PushResult]:
        """``StatefulPusher.delete`` 的异步版本：语义完全相同。"""
        contract = (await self._client.core()).contract
        normalized, expected = self._prepare_uris(uris, expected_base, contract)
        if not normalized:
            return []
        heads = await self._client.batch_head(normalized)
        plans = self._plan_delete(normalized, heads, expected, contract)
        self._persist({plan.uri: None for plan in plans if plan.action == "write"})
        outcomes = await self._run_all(plans, self._delete_one)
        results = self._finish_delete(plans, outcomes)
        self._save()
        return results

    async def move(
        self,
        moves: Sequence[tuple[str, str]],
        *,
        expected_base: Mapping[str, str | None] | None = None,
    ) -> list[PushResult]:
        """``StatefulPusher.move`` 的异步版本：语义完全相同。"""
        contract = (await self._client.core()).contract
        pairs, expected = self._prepare_moves(moves, expected_base, contract)
        if not pairs:
            return []
        from_heads, to_heads = await self._read_move_sides(pairs)
        plans = self._plan_move(pairs, from_heads, to_heads, expected, contract)
        self._persist(self._move_intents(plans))
        outcomes = await self._run_all(plans, self._move_one)
        results = self._finish_move(plans, outcomes)
        self._save()
        return results

    async def _read_move_sides(
        self, pairs: Sequence[tuple[str, str]]
    ) -> tuple[list[str | None], list[str | None]]:
        """``StatefulPusher._read_move_sides`` 的异步版本。"""
        heads = await self._client.batch_head([f for f, _ in pairs] + [t for _, t in pairs])
        return heads[: len(pairs)], heads[len(pairs) :]

    async def _run_all(
        self, plans: Sequence[_Plan], worker: Callable[[_Plan], Awaitable[_Outcome]]
    ) -> list[_Outcome | None]:
        """``StatefulPusher._run_all`` 的异步版本（anyio 任务组 + 信号量限流）。"""
        outcomes: list[_Outcome | None] = [None] * len(plans)
        indexes = [index for index, plan in enumerate(plans) if plan.action == "write"]
        if not indexes:
            return outcomes
        limiter = anyio.Semaphore(self._concurrency)

        async def run(index: int) -> None:
            async with limiter:
                outcomes[index] = await worker(plans[index])

        async with anyio.create_task_group() as group:
            for index in indexes:
                group.start_soon(run, index)
        return outcomes

    async def _commit_one(self, plan: _Plan, *, blobs: BlobLookup | None) -> _Outcome:
        assert plan.precondition is not None and plan.document is not None  # write 计划必有
        try:
            result = await self._client.deliver(
                plan.uri, plan.document, plan.precondition, blobs=blobs
            )
        except (PreconditionFailedError, AlreadyExistsError, NotFoundError) as exc:
            remote, re_error = await self._re_read(plan.uri)
            return _Outcome(error=exc, kind="conflict", remote=remote, remote_error=re_error)
        except Exception as exc:
            # 一切非冲突失败都按核心的结果标记归类（含普通 DpeError 与 UnexpectedResponseError）：
            # 首发丢响应、重发得到 429 耗尽、重发全建连失败等序列里首次尝试都可能已生效；
            # 只有核心明确标记「确定未生效」才清除意图，无标记一律按结果未知（保留）。
            return _Outcome(error=exc, unknown=outcome_unknown(exc))
        return _Outcome(result=result)

    async def _delete_one(self, plan: _Plan) -> _Outcome:
        assert isinstance(plan.precondition, BaseHash)  # write 计划必有 base
        try:
            await self._client.delete(plan.uri, plan.precondition.value)
        except PreconditionFailedError as exc:
            remote, re_error = await self._re_read(plan.uri)
            return _Outcome(error=exc, kind="conflict", remote=remote, remote_error=re_error)
        except NotFoundError as exc:
            # 文档不存在即目标状态已达成（§7.2 冲突处置表 delete 行），不是失败
            return _Outcome(error=exc, kind="achieved")
        except Exception as exc:
            return _Outcome(error=exc, unknown=outcome_unknown(exc))
        return _Outcome()

    async def _move_one(self, plan: _Plan) -> _Outcome:
        assert plan.to_uri is not None and plan.base is not None  # write 计划必有
        try:
            await self._client.move(plan.uri, plan.to_uri, plan.base)
        except PreconditionFailedError as exc:
            remote, re_error = await self._re_read(plan.uri)  # 源被改写：重读源侧
            return _Outcome(
                error=exc, kind="conflict", side="from", remote=remote, remote_error=re_error
            )
        except AlreadyExistsError as exc:
            remote, re_error = await self._re_read(plan.to_uri)  # 目标被占：重读目标侧
            return _Outcome(
                error=exc, kind="failed", side="to", remote=remote, remote_error=re_error
            )
        except NotFoundError as exc:
            # 源已不存在、目标未达成（§7.2 条目失败）
            return _Outcome(error=exc, kind="failed", side="from")
        except Exception as exc:
            return _Outcome(error=exc, unknown=outcome_unknown(exc))
        return _Outcome()

    async def _re_read(self, uri: str) -> tuple[RemoteState | None, Exception | None]:
        try:
            return RemoteState(await self._client.head(uri)), None
        except Exception as exc:
            return None, exc
