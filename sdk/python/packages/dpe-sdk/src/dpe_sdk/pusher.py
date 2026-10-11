"""增量推送器（#14，Part 1）：把一批文档变成「只推变化的部分」，本地状态只作缓存。

**分层**：``StatefulPusher`` / ``AsyncStatefulPusher`` 建在 ``DpeClient`` / ``AsyncDpeClient``
与核心的 ``deliver`` / ``batch_head`` 之上，只做编排——CAS、412、响应丢失重发全部由核心与
传输层承担，这里不另写一套，也**绝不自动 force**。

**缓存语义**（connector 契约 §7.2，本模块实现其机制、策略归调用方）：

- **同步状态**：每篇文档最近一次确认的 remote doc_hash（类比 Git 的 remote-tracking ref），
  键为 core §1.1 规范化后的 file_uri；只作缓存，丢失时退化为以即时读到的 R 作 base、
  冲突保护只覆盖读写之间的竞态窗口，结果仍然正确。
- **意图日志**：写入请求发出前先持久化本次写入预期的 remote 状态（commit 记 ``uri → N``）——
  本模块**在 ``StateStore.save`` 返回之后才发出写请求**。结果一旦确定（成功、失败、含冲突）
  即清除对应意图；只有结果未知才保留——判据是核心的 ``outcome_unknown`` 标记：写请求本身
  任何一次尝试结果不确定（传输异常未带「确定未发出」底标、或无 DPE 错误码的任何响应——
  含校验失败的 2xx、未化解的裸 412、无码 4xx/5xx）即未知，**包括「首发丢响应、重发得到
  DPE 错误或建连失败」这类首发可能已生效的序列**。下一轮判定前先对账：R 等于待定意图值
  （含「不存在」）即视为本实例自己已生效的写入，按意图更新同步状态后再查判定表。
- **按 remote 隔离**：状态经 ``StateStore`` 以 remote 为命名空间存取，同一 store 服务多个
  remote 也不会串；缺省 ``MemoryStateStore`` 只在进程内、**不保证崩溃持久**（持久化实现属于
  运行器的状态目录，connector §4.4）。

**前置条件判定表**（L = 同步状态，R = ``batch_head`` 读到的 remote 值，N = 本地内容 doc_hash；
``expected_base`` 存在时覆盖 L，值为 ``None`` 即调用方确认「不存在」）：

- R == N → 已达成，跳过（``achieved``）——这一行先于 ``expected_base`` 的判定：内容已相同，
  即使调用方声明「不存在」也记 ``achieved``；
- 无 L（含契约不一致的条目按缺失）、R 不存在 → ``if_absent``；R 存在 → 以 R 作 base；
- 有 L、R == L → 以 L 作 base；
- 有 L、R ≠ L 或 R 不存在；以及 ``expected_base`` 声明「不存在」而 R 存在 → **冲突**，
  不发写、如实回报（不自动重提；上层可用 ``expected_base`` 覆盖后重提）；
- 比较 MUST 在同一 hash 契约下进行：同步状态中与当前契约不一致的条目按缺失处理。

判定表判出的冲突（未发写）不产生新意图、也**不清除**既有待定意图：那条待定写入的结果仍然
未知（R ≠ 意图值既兼容「未落地」也兼容「落地后被覆盖」），按 §7.2 只有结果确定才清除。

**快/暂存分界**：完全复用核心 ``deliver`` 的判据（inline 请求体 ≤
``capabilities.limits.max_payload_bytes`` 走快路径，否则 negotiate → upload → commit），
本模块不引入任何阈值；``concurrency`` 只限制并发度（并发调用 ``deliver``，各篇互不拖累）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Literal, Protocol

import anyio
import dpe_hash
from dpe_hash import DpeHashError

from dpe_sdk.errors import (
    AlreadyExistsError,
    NotFoundError,
    PreconditionFailedError,
)
from dpe_sdk.models import Document
from dpe_sdk.protocol import BaseHash, IfAbsent, outcome_unknown
from dpe_sdk.transport import AsyncDpeClient, DpeClient
from dpe_sdk.wire import CommitResult, Delta

__all__ = [
    "AsyncStatefulPusher",
    "MemoryStateStore",
    "PushResult",
    "PushStatus",
    "RemoteState",
    "StateSnapshot",
    "StateStore",
    "StatefulPusher",
]

#: 一条回报的状态（Part 2 追加 ``deleted`` / ``moved``）
PushStatus = Literal["created", "updated", "unchanged", "achieved", "conflict", "failed"]

#: 一条回报对应的操作（Part 2 追加 ``delete`` / ``move``）
PushOp = Literal["commit"]


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

    - ``precondition``：本次写入使用的前置条件（``BaseHash`` / ``IfAbsent``）；未发写时为 ``None``；
    - ``remote``：冲突时读/重读到的 remote 状态；``None`` 表示未读到（正常写入成功、判定表
      判出的冲突不重读，或重读本身失败）；
    - ``delta``：commit 成功时服务端给出的元素级差值；
    - ``error``：失败/冲突时的异常（``DpeError`` 或传输层异常，按其类型区分；冲突时为裁决它
      的 CAS 错误，其 ``code`` 可原样上报，§7.1）；
    - ``remote_error``：冲突后重读失败的异常（此时 ``remote`` 为 ``None``、``error`` 仍是
      CAS 错误）；
    - ``unknown``：结果未确定（响应丢失、请求可能已发出的传输失败等）——对应的待定意图被
      保留，下一轮按意图对账恢复；为 ``False`` 的失败都是已确定的（服务端已裁决、确定未发出、
      或本地校验未发出）。

    ``status`` 的来源：``achieved``＝判定表初判 R == N（推送器自己判定，跳过写入）；
    ``unchanged``＝如实转述服务端 commit 的结果（本模块不自行产出）；``created`` /
    ``updated`` 同；``conflict``＝判定表判定或服务端 CAS 裁决（后者带重读到的 ``remote``）；
    ``failed``＝不可重试的失败（``unknown`` 见上）。
    """

    op: PushOp
    file_uri: str
    status: PushStatus
    precondition: BaseHash | IfAbsent | None = None
    delta: Delta | None = None
    remote: RemoteState | None = None
    error: Exception | None = None
    remote_error: Exception | None = None
    unknown: bool = False


@dataclass(frozen=True)
class _Entry:
    uri: str
    document: Document
    new_hash: str


@dataclass(frozen=True)
class _Plan:
    entry: _Entry
    action: Literal["achieved", "write", "conflict"]
    precondition: BaseHash | IfAbsent | None
    #: 本次读到的 remote 状态（判定表用的 R）
    remote: RemoteState


@dataclass(frozen=True)
class _Outcome:
    """一篇文档写入的执行结果（由两个驱动的 ``_commit_one`` 产出，``_finish`` 消费）。"""

    result: CommitResult | None = None
    error: Exception | None = None
    conflict: bool = False
    unknown: bool = False
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
                raise ValueError(
                    f"归一化后重复的 file_uri：{seen[normalized]!r} 与 {uri!r} 归一化后"
                    f"都是 {normalized!r}"
                )
            seen[normalized] = uri
            entries.append(_Entry(normalized, document, document.doc_hash(contract)))
        expected: dict[str, str | None] = {}
        for uri, value in (expected_base or {}).items():
            normalized = dpe_hash.normalize_file_uri(uri)
            if normalized not in seen:
                raise ValueError(
                    f"expected_base 的 {uri!r} 不在本次推送的文档里（归一化后 {normalized!r}）"
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
        return entries, expected

    def _plan(
        self,
        entries: Sequence[_Entry],
        heads: Sequence[str | None],
        expected: Mapping[str, str | None],
        contract: str,
    ) -> list[_Plan]:
        """意图对账 + 判定表（§7.2）：只更新内存状态，持久化归 ``_persist_intents`` / ``_save``。"""
        plans: list[_Plan] = []
        for entry, head in zip(entries, heads, strict=True):
            uri = entry.uri
            # 意图对账：R 等于待定意图值（含「不存在」）＝ 本实例自己已生效的写入
            if uri in self._intents and self._intents[uri] == head:
                confirmed = self._intents.pop(uri)
                if confirmed is None:
                    self._synced.pop(uri, None)
                else:
                    self._synced[uri] = confirmed
            if uri in expected:
                base = expected[uri]
                known_absent = base is None
            else:
                base = self._synced.get(uri)
                if base is not None and not _matches_contract(base, contract):
                    base = None  # 旧契约条目按缺失
                known_absent = False
            remote = RemoteState(head)
            if head == entry.new_hash:
                plans.append(_Plan(entry, "achieved", None, remote))
            elif known_absent or base is None:
                # 无 L（或调用方确认不存在）：R 不存在即 if_absent，否则以 R 作 base；调用方确认
                # 不存在而 R 存在＝与确认矛盾，判冲突（不静默覆盖）
                if head is None:
                    plans.append(_Plan(entry, "write", IfAbsent(), remote))
                elif known_absent:
                    plans.append(_Plan(entry, "conflict", None, remote))
                else:
                    plans.append(_Plan(entry, "write", BaseHash(head), remote))
            elif head == base:
                plans.append(_Plan(entry, "write", BaseHash(base), remote))
            else:
                plans.append(_Plan(entry, "conflict", None, remote))
        return plans

    def _persist_intents(self, plans: Sequence[_Plan]) -> None:
        """写前先把这一批意图持久化；``save`` 返回之后调用方才允许发出写请求（§7.2）。"""
        writes = [plan for plan in plans if plan.action == "write"]
        if not writes:
            return
        for plan in writes:
            self._intents[plan.entry.uri] = plan.entry.new_hash
        self._save()

    def _save(self) -> None:
        self._store.save(
            self.remote,
            StateSnapshot(synced=dict(self._synced), intents=dict(self._intents)),
        )

    def _finish(
        self, plans: Sequence[_Plan], outcomes: Sequence[_Outcome | None]
    ) -> list[PushResult]:
        """把执行结果并入状态并构造回报（意图：结果确定即清除，未确定（``unknown``）才保留）。"""
        results: list[PushResult] = []
        for plan, outcome in zip(plans, outcomes, strict=True):
            uri = plan.entry.uri
            if plan.action == "achieved":
                self._synced[uri] = plan.entry.new_hash
                self._intents.pop(uri, None)
                results.append(PushResult("commit", uri, "achieved", remote=plan.remote))
                continue
            if plan.action == "conflict":
                # 未发写：不产生新意图，也不清除既有待定意图（那条写入的结果仍未知，见模块说明）
                results.append(PushResult("commit", uri, "conflict", remote=plan.remote))
                continue
            assert outcome is not None  # write 计划必有执行结果
            if outcome.error is not None:
                if outcome.conflict:
                    self._intents.pop(uri, None)  # 服务端已裁决，结果确定
                    results.append(
                        PushResult(
                            "commit",
                            uri,
                            "conflict",
                            plan.precondition,
                            remote=outcome.remote,
                            error=outcome.error,
                            remote_error=outcome.remote_error,
                        )
                    )
                elif outcome.unknown:
                    results.append(
                        PushResult(
                            "commit",
                            uri,
                            "failed",
                            plan.precondition,
                            error=outcome.error,
                            unknown=True,
                        )
                    )
                else:
                    self._intents.pop(uri, None)
                    results.append(
                        PushResult("commit", uri, "failed", plan.precondition, error=outcome.error)
                    )
                continue
            result = outcome.result
            assert result is not None  # 无 error 必有 result
            self._synced[uri] = result.doc_hash
            self._intents.pop(uri, None)
            results.append(
                PushResult("commit", uri, result.status, plan.precondition, delta=result.delta)
            )
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
    ) -> list[PushResult]:
        """推送一批文档（file_uri → 展开文档）：只推变化的部分，返回与输入同序的逐条回报。

        ``expected_base`` 覆盖同步状态中的 L（值为 ``None`` 即调用方确认「不存在」）；上层可在
        冲突后以重读到的 remote 值重提（source_wins 等策略归上层，本方法不自动重提）。
        """
        contract = self._client.core().contract
        entries, expected = self._prepare(documents, expected_base, contract)
        if not entries:
            return []
        heads = self._client.batch_head([entry.uri for entry in entries])
        plans = self._plan(entries, heads, expected, contract)
        self._persist_intents(plans)
        outcomes = self._commit_all(plans)
        results = self._finish(plans, outcomes)
        self._save()
        return results

    def _commit_all(self, plans: Sequence[_Plan]) -> list[_Outcome | None]:
        outcomes: list[_Outcome | None] = [None] * len(plans)
        indexes = [index for index, plan in enumerate(plans) if plan.action == "write"]
        if not indexes:
            return outcomes
        if len(indexes) == 1 or self._concurrency == 1:
            for index in indexes:
                outcomes[index] = self._commit_one(plans[index])
            return outcomes
        with ThreadPoolExecutor(max_workers=min(self._concurrency, len(indexes))) as pool:
            batch = pool.map(self._commit_one, [plans[index] for index in indexes])
            for index, outcome in zip(indexes, batch, strict=True):
                outcomes[index] = outcome
        return outcomes

    def _commit_one(self, plan: _Plan) -> _Outcome:
        assert plan.precondition is not None  # write 计划必有前置条件
        try:
            result = self._client.deliver(plan.entry.uri, plan.entry.document, plan.precondition)
        except (PreconditionFailedError, AlreadyExistsError, NotFoundError) as exc:
            remote, re_error = self._re_read(plan.entry.uri)
            return _Outcome(error=exc, conflict=True, remote=remote, remote_error=re_error)
        except Exception as exc:
            # 一切非冲突失败都按核心的结果标记归类（含普通 DpeError 与 UnexpectedResponseError）：
            # 首发丢响应、重发得到 429 耗尽、重发全建连失败等序列里首次尝试都可能已生效；
            # 只有核心明确标记「确定未生效」才清除意图，无标记一律按结果未知（保留）。
            return _Outcome(error=exc, unknown=outcome_unknown(exc))
        return _Outcome(result=result)

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
    ) -> list[PushResult]:
        """``StatefulPusher.push`` 的异步版本：语义完全相同。"""
        contract = (await self._client.core()).contract
        entries, expected = self._prepare(documents, expected_base, contract)
        if not entries:
            return []
        heads = await self._client.batch_head([entry.uri for entry in entries])
        plans = self._plan(entries, heads, expected, contract)
        self._persist_intents(plans)
        outcomes = await self._commit_all(plans)
        results = self._finish(plans, outcomes)
        self._save()
        return results

    async def _commit_all(self, plans: Sequence[_Plan]) -> list[_Outcome | None]:
        outcomes: list[_Outcome | None] = [None] * len(plans)
        indexes = [index for index, plan in enumerate(plans) if plan.action == "write"]
        if not indexes:
            return outcomes
        limiter = anyio.Semaphore(self._concurrency)

        async def run(index: int) -> None:
            async with limiter:
                outcomes[index] = await self._commit_one(plans[index])

        async with anyio.create_task_group() as group:
            for index in indexes:
                group.start_soon(run, index)
        return outcomes

    async def _commit_one(self, plan: _Plan) -> _Outcome:
        assert plan.precondition is not None  # write 计划必有前置条件
        try:
            result = await self._client.deliver(
                plan.entry.uri, plan.entry.document, plan.precondition
            )
        except (PreconditionFailedError, AlreadyExistsError, NotFoundError) as exc:
            remote, re_error = await self._re_read(plan.entry.uri)
            return _Outcome(error=exc, conflict=True, remote=remote, remote_error=re_error)
        except Exception as exc:
            # 一切非冲突失败都按核心的结果标记归类（含普通 DpeError 与 UnexpectedResponseError）：
            # 首发丢响应、重发得到 429 耗尽、重发全建连失败等序列里首次尝试都可能已生效；
            # 只有核心明确标记「确定未生效」才清除意图，无标记一律按结果未知（保留）。
            return _Outcome(error=exc, unknown=outcome_unknown(exc))
        return _Outcome(result=result)

    async def _re_read(self, uri: str) -> tuple[RemoteState | None, Exception | None]:
        try:
            return RemoteState(await self._client.head(uri)), None
        except Exception as exc:
            return None, exc
