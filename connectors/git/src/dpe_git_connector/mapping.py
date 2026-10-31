"""仓库的提交历史 → DPE 产出条目（connector 契约 §6.5）。

一篇 ``document`` = 一个仓库（``file_type`` ``git_repo``）；页与元素是提交历史的可读面
（README「映射规则」）：

- **默认分支按月一页**：月份 = 改动**进入主线的时间**——该提交由 first-parent 链上哪个提交
  引入（链上提交就是它自己，合并带入的提交是把它合并进来的那个提交），就取那个提交的
  committer 时间（UTC）的 ``YYYY-MM``；``page_metadata`` 为 ``{ref: <默认分支名>, period: …}``；
- **其他跟踪分支每条一页**：只含该分支独有（``<default>..<branch>``）的提交，
  ``page_metadata`` 为 ``{ref: <分支名>}``；
- **附注标签一页**（#220）：放在最后，``page_metadata`` 为 ``{kind: "tags"}``——换一个键而不是
  ``ref``，分支名可以叫 ``tags``，键集合不同即从结构上区分页的种类；没有附注标签不产该页；
- **元素 = 一次提交**，带 git note（``refs/notes/commits``）的提交其后紧跟一个 note 元素（#222）；
  标签页上是一个附注标签（见 ``extractors``）。

页序：月页按月份升序在前，分支页按分支名（字节序）在后，标签页最后；标签页内按
``order_tags``（标签时间，再按名字）；提交页内旧在前——**自有拓扑序**（从
起始提交出发按父提交记录顺序做后序 DFS，见 ``gitrepo.order_commits`` 与 README「确定性」；
只依赖提交图，不依赖 git 的输出次序，跨 git 版本稳定），合并带入的提交排在其合并提交之前。
不写易变字段、不写由位置算出的序号（core §2.4）；``file_uri`` 是身份（实例前缀 + 仓库标识），
身份不进 hash。

失败如实产出 ``error`` 条目（§6.5 的封闭枚举），不静默跳过——**读取类与内容类分开**：

- ``source_unavailable``（``retryable: true``——源可能恢复）：仓库不可读、默认分支无法确定、
  发现层读不了的子树；
- ``content_invalid``（不可重试）：``file_uri`` 不合法、提交说明 / 作者不是合法 UTF-8、
  时间戳不合规（``GitContentError``——同样扫描重跑不会成功）、notes ref 存在但指不到提交
  或本文档里的被注对象在 notes 树里有多条路径（#233，源状态确定性地损坏），或按固定预算
  切分后仍超过远端 ``max_payload_bytes``（部署配置问题，不静默按远端值重新切分）。

**增量**（#66）：映射是「仓库状态」的纯函数——默认分支名与 tip、``since`` 解析出的提交、
各跟踪分支的 tip、各附注标签的标签对象（#220）、默认 notes ref 上的 note（#222）（提交、标签
对象与 note blob 按 SHA 不可变，配置与映射规则版本由运行器的内容身份承担）。``resolve`` 一次
解析出这组值，``RepoState.fingerprint`` 是它的摘要：指纹与上一轮相同即文档相同，``scan``
直接跳过、不读提交日志；不同则用**同一次解析出的 tip** 映射整篇文档（避免两次解析之间仓库
被推送造成指纹与内容不符）。
仓库消失 / 改名由 ``server`` 在仓库集合层面表达 delete / move，改名的配对判据「根提交集合 +
默认分支名」由 ``move_key`` 给出（#96）。
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from dpe_hash import (
    ContractUnsupportedError,
    ElementObject,
    ValidationError,
    jcs,
    normalize_file_uri,
)

from dpe_git_connector.extractors import (
    commit_elements,
    element_bytes,
    note_elements,
    tag_elements,
)
from dpe_git_connector.gitrepo import (
    NOTES_REF,
    CommitRecord,
    GitContentError,
    GitError,
    GitRepo,
    RepoDir,
    TagRecord,
    first_parent_chain,
    order_commits,
    root_commits,
)

__all__ = [
    "DocumentMapper",
    "Item",
    "Limits",
    "RepoMark",
    "RepoSpec",
    "RepoState",
    "build_file_uri",
    "month_partition",
    "move_key",
    "order_tags",
]

#: RFC 3986 unreserved —— 这些字节照原样进 URI（其余一律百分号编码）
_UNRESERVED = frozenset(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
#: 路径分隔符是 URI 的结构，保持字面
_KEEP = _UNRESERVED | {ord("/")}


def build_file_uri(prefix: str, identifier: bytes) -> str:
    """实例前缀 + 仓库标识字节 → ``file_uri``（已按 core §1.1 规范化，作为身份）。

    ``identifier`` 是相对 ``root`` 的仓库路径字节（单仓库模式是目录名）。``prefix`` 是实例
    前缀，运行器已保证它是规范化不动点（契约 §6.3）且自带分隔符——码点前缀匹配不识别 path
    段边界，前缀末尾不带 ``/`` 时相邻路径会拼接成越界 URI，因此这里显式要求。
    """
    if not prefix:
        raise ValueError("实例 URI 前缀不得为空")
    encoded = "".join(chr(byte) if byte in _KEEP else f"%{byte:02X}" for byte in identifier)
    return normalize_file_uri(prefix + encoded)


@dataclass(frozen=True)
class Item:
    """一条产出条目（§6.5）：``kind`` 与其余字段。"""

    kind: str
    fields: dict[str, Any]

    def payload(self) -> dict[str, Any]:
        return {"kind": self.kind, **self.fields}


@dataclass(frozen=True)
class Limits:
    """远端限额中与切分决策相关的项（``initialize`` 的 ``remote_limits``，可选）。"""

    max_payload_bytes: int | None = None

    def too_large(self, size: int) -> bool:
        return self.max_payload_bytes is not None and size > self.max_payload_bytes


@dataclass(frozen=True)
class RepoSpec:
    """实例配置里作用于每个仓库的部分（``server`` 解析，``mapping`` 消费）。"""

    #: 显式配置的默认分支名；缺省用仓库自身的 refs/remotes/origin/HEAD
    default_branch: str | None = None
    #: 跟踪的分支名集合（默认分支本身不产页，由月页表达）
    branches: tuple[str, ...] = ()
    #: 历史起点提交（含起点；仅单仓库模式允许）
    since: str | None = None


def _digest(value: Any) -> str:
    """游标里的摘要：JCS 后取 sha256，截断为 128 位（只用于比较相等，不是内容 hash）。"""
    return hashlib.sha256(jcs(value).encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True)
class RepoState:
    """一次解析出的仓库状态：映射只依赖它（与配置），指纹相同即文档相同。

    ``default_name`` 为 ``None`` 表示空仓库（没有任何 ref，零页文档）。``branches`` 是按名字
    字节序排列的跟踪分支与其 tip（解析不到为 ``None``，不产该页）。
    """

    default_name: str | None
    default_tip: str | None = None
    #: ``since`` 解析出的提交：配置可以写会移动的 tag / 分支名，必须按解析结果比较
    since: str | None = None
    branches: tuple[tuple[str, str | None], ...] = ()
    #: 浅克隆的边界：tip 不变时它变化（``fetch --deepen``）同样改变可见的提交集合
    shallow: tuple[str, ...] = ()
    #: 附注标签（名字, 标签对象 SHA），按名字字节序。轻量标签不产内容、不在其中：指纹恰好
    #: 等于映射的输入，多了只会无谓重扫；标签对象 SHA 已覆盖说明、tagger、target 的变化
    tags: tuple[tuple[str, str], ...] = ()
    #: 默认 notes ref 上的（被注对象, note blob），按被注对象排序（#222）。不用 notes ref 的 tip：
    #: tip 会在内容不变时变化（notes 合并、重写），blob 对才对准内容；不在本文档里的被注对象
    #: 也在其中——指纹必须覆盖全部映射输入，多出的只导致一次无产出的重扫
    notes: tuple[tuple[str, str], ...] = ()

    def fingerprint(self) -> str:
        return _digest(
            {
                "default": self.default_name,
                "tip": self.default_tip,
                "since": self.since,
                "branches": [[name, tip] for name, tip in self.branches],
                "shallow": list(self.shallow),
                "tags": [[name, sha] for name, sha in self.tags],
                "notes": [[commit, blob] for commit, blob in self.notes],
            }
        )


def move_key(default_name: str | None, roots: Sequence[str]) -> str | None:
    """改名的配对键（#96）：默认分支名 + 根提交集合。没有根提交（空仓库）不参与配对。"""
    if default_name is None or not roots:
        return None
    return _digest({"default": default_name, "roots": sorted(roots)})


@dataclass(frozen=True)
class RepoMark:
    """游标里一个仓库的记录：状态指纹与移动键（``server`` 的轮间对比只用这两项）。"""

    fingerprint: str
    move_key: str | None


def _month(committed_at: str) -> str:
    """RFC 3339 UTC 时间戳（``YYYY-MM-DDThh:mm:ssZ``）→ ``YYYY-MM``。"""
    return committed_at[:7]


def _introductions(chain: Sequence[str], records: Sequence[CommitRecord]) -> dict[str, int]:
    """每个提交的「引入提交」在 first-parent 链上的下标（旧在前计）。

    逆拓扑序（子先于父）扫一遍：链上提交的引入点是自身；其余提交的引入点是**其子提交引入点
    的最小者**——最早包含它的链上提交，也就是把它带入主线的那个提交（同一提交被多次带入时
    取最早的一次，如被两个分支先后合并）。
    """
    chain_index = {sha: index for index, sha in enumerate(chain)}
    introductions: dict[str, int] = {}
    best: dict[str, int] = {}
    for record in reversed(records):
        own = chain_index.get(record.sha)
        introduction = own if own is not None else best.get(record.sha)
        if introduction is None:
            raise GitError(f"提交图不完整：{record.sha} 不在任何已处理子提交之后出现")
        introductions[record.sha] = introduction
        for parent in record.parents:
            current = best.get(parent)
            best[parent] = introduction if current is None else min(current, introduction)
    return introductions


def month_partition(
    chain: Sequence[str], records: Sequence[CommitRecord], since: str | None
) -> dict[str, list[CommitRecord]]:
    """默认分支的提交按月分组（``YYYY-MM`` → 该月提交，页内序 = 传入的拓扑序，旧在前）。

    月份取**改动进入主线的时间**：该提交由 first-parent 链上哪个提交引入，就取那个提交的
    committer 时间（UTC）的月份——被合并进来的提交随其合并提交归月，**不按自身提交时间**
    （分支上的提交时间可以早于或晚于合并时间，都与归月无关）。``since`` 是已解析的历史
    起点提交：保留引入点不早于它的提交（含起点）。
    """
    committed_at = {record.sha: record.committed_at for record in records}
    introduction_of = _introductions(chain, records)
    chain_month = [_month(committed_at[sha]) for sha in chain]

    cutoff: int | None = None
    if since is not None:
        cutoff = introduction_of.get(since)
        if cutoff is None:
            raise GitError(f"since 不在默认分支的历史中：{since}")

    months: dict[str, list[CommitRecord]] = {}
    for record in records:
        introduction = introduction_of[record.sha]
        if cutoff is not None and introduction < cutoff:
            continue
        months.setdefault(chain_month[introduction], []).append(record)
    return months


def order_tags(records: Sequence[TagRecord]) -> list[TagRecord]:
    """标签页内的自有顺序：按 ``tagged_at`` 升序，同时间按名字（UTF-8 字节序）；没有 tagger 的
    老标签排在最前（再按名字）。只依赖标签对象与 ref 名，不依赖 git 的输出次序。"""
    return sorted(
        records,
        key=lambda tag: (tag.tagged_at is not None, tag.tagged_at or "", tag.name.encode("utf-8")),
    )


def _elements(records: Sequence[CommitRecord], notes: dict[str, str]) -> list[ElementObject]:
    """提交列表（旧在前）→ 元素列表（说明超预算的提交切成多个连续元素）；带 note 的提交其后
    紧跟 note 元素（``notes``：提交 SHA → note 原文，#222）。"""
    elements: list[ElementObject] = []
    for record in records:
        elements += commit_elements(record)
        note = notes.get(record.sha)
        if note is not None:
            elements += note_elements(record.sha, note)
    return elements


class DocumentMapper:
    """把一个仓库的提交历史映射成一篇文档的产出条目。"""

    def __init__(self, prefix: str, limits: Limits | None = None) -> None:
        self.prefix = prefix
        self.limits = limits or Limits()

    def document(self, repo: GitRepo, entry: RepoDir, spec: RepoSpec) -> Item:
        """枚举一个仓库 → ``document`` 条目；失败给 ``error`` 条目（全量，不比较指纹）。"""
        item, _ = self.scan(repo, entry, spec)
        assert item is not None  # 未给上一轮指纹：不会被跳过
        return item

    def scan(
        self, repo: GitRepo, entry: RepoDir, spec: RepoSpec, unchanged_if: str | None = None
    ) -> tuple[Item | None, RepoMark | None]:
        """枚举一个仓库：返回（条目, 新的游标记录）。

        - 状态指纹等于 ``unchanged_if``（上一轮的指纹）：``(None, None)``——文档未变，不产出；
        - 映射成功：``document`` 条目与本次的 ``RepoMark``；
        - 失败：``error`` 条目与 ``None``（调用方保留旧记录，新游标不越过未产出的变更，§6.8）。
        """
        file_uri = self.file_uri(entry)
        if isinstance(file_uri, Item):
            return file_uri, None
        try:
            state = self.resolve(repo, spec)
            fingerprint = state.fingerprint()
            if fingerprint == unchanged_if:
                return None, None
            pages, roots = self._pages(repo, state)
        except Exception as exc:
            return self.repo_error(file_uri, exc), None
        oversize = self._oversize(pages)
        if oversize is not None:
            return self.error_item(
                file_uri,
                "content_invalid",
                "元素按固定预算切分后仍超过远端 max_payload_bytes"
                f"（{oversize} 字节 > {self.limits.max_payload_bytes}）",
                retryable=False,
            ), None
        item = Item(
            kind="document",
            fields={
                "file_uri": file_uri,
                "document": {"file_type": "git_repo"},
                "pages": pages,
            },
        )
        return item, RepoMark(fingerprint, move_key(state.default_name, roots))

    def move_key_of(self, repo: GitRepo, spec: RepoSpec) -> str | None:
        """本轮新出现的仓库的移动键（还没读它的提交日志：只解析状态与根提交）。"""
        state = self.resolve(repo, spec)
        if state.default_tip is None:
            return None
        return move_key(state.default_name, repo.root_commits(state.default_tip))

    def file_uri(self, entry: RepoDir) -> str | Item:
        """仓库标识 → ``file_uri``；不合法时给条目级 ``error``。"""
        try:
            file_uri = build_file_uri(self.prefix, entry.identifier)
        except (ValueError, ValidationError, ContractUnsupportedError) as exc:
            return self.error_item(None, "content_invalid", f"file_uri 不合法：{exc}", False)
        if not file_uri.startswith(self.prefix):
            # 标识由前缀拼接而来，正常不会越界；防御性上报（契约 §6.5 要求不静默忽略）
            return self.error_item(file_uri, "internal", "file_uri 越出实例前缀", retryable=False)
        return file_uri

    def repo_error(self, file_uri: str, exc: Exception) -> Item:
        """读取或映射一个仓库时的失败 → 条目级 ``error``（读取类与内容类分开，§6.5）。"""
        if isinstance(exc, GitContentError):
            # 内容类失败（非 UTF-8 的说明 / 作者、非法时间戳）：映射不出合法 DPE 内容，
            # 同样扫描重跑也不会成功——content_invalid、不可重试（§6.5 定义）
            return self.error_item(file_uri, "content_invalid", str(exc), retryable=False)
        if isinstance(exc, GitError):
            return self.error_item(file_uri, "source_unavailable", str(exc), retryable=True)
        # 条目级兜底：任何未预期的解析异常（超大字段、解析器内部错误）都不得带崩进程——
        # 崩了会让每轮都以 plugin_crashed 收场、实例永不收敛（§8.1-5）
        return self.error_item(
            file_uri, "internal", f"映射时发生未预期错误：{type(exc).__name__}: {exc}", False
        )

    def error_item(
        self, file_uri: str | None, code: str, message: str, retryable: bool = False
    ) -> Item:
        """构造一条 ``error`` 条目（§6.5；``server`` 的单条预算超限上报也用这里）。"""
        return Item(
            kind="error",
            fields={
                "file_uri": file_uri,
                "code": code,
                "message": message,
                "retryable": retryable,
            },
        )

    def unreadable_directory(self, entry: RepoDir) -> Item:
        """发现层读不了的子树 → 条目级 ``source_unavailable``（§6.5：不静默跳过）。

        子树里是否藏着仓库无从得知，因此不假装知道：如实上报该路径（身份 = 前缀 + 相对路径），
        其余仓库照常枚举。权限修好后重跑即可成功，故 ``retryable: true``。
        """
        file_uri = self.file_uri(entry)
        if isinstance(file_uri, Item):
            return file_uri
        return self.error_item(
            file_uri, "source_unavailable", f"无法读取（发现层）：{entry.path}", retryable=True
        )

    def resolve(self, repo: GitRepo, spec: RepoSpec) -> RepoState:
        """解析仓库状态（映射的全部输入）：不读提交日志，每轮对每个仓库都执行。"""
        repo.open()
        if not repo.has_refs():
            # 空仓库（没有任何 ref）：零页文档（契约 1 §5 允许空文档），不是错误
            return RepoState(default_name=None)
        default_name = repo.default_branch(spec.default_branch)
        default_tip = repo.resolve_branch(default_name)
        if default_tip is None:
            raise GitError(f"默认分支 {default_name!r} 无法解析为提交")
        since_sha: str | None = None
        if spec.since is not None:
            since_sha = repo.resolve_ref(spec.since)
            if since_sha is None:
                raise GitError(f"since 无法解析为提交：{spec.since}")
            if not repo.is_ancestor(since_sha, default_tip):
                raise GitError(f"since 不是默认分支 {default_name!r} 的祖先：{spec.since}")
        tracked = sorted(
            {name for name in spec.branches if name != default_name},
            key=lambda name: name.encode("utf-8"),
        )
        return RepoState(
            default_name=default_name,
            default_tip=default_tip,
            since=since_sha,
            # 解析不到（未建或已删）记 None：不产该页，也不是错误——与「删除分支 = 删页」一致
            branches=tuple((name, repo.resolve_branch(name)) for name in tracked),
            shallow=repo.shallow_boundary(),
            tags=repo.annotated_tags(),
            notes=repo.notes(),
        )

    def _pages(
        self, repo: GitRepo, state: RepoState
    ) -> tuple[list[dict[str, Any]], tuple[str, ...]]:
        """按已解析的状态给出全部页对象（月页、分支页、标签页依次排列）与默认分支的根提交。"""
        if state.default_name is None or state.default_tip is None:
            return [], ()
        default_name, default_tip = state.default_name, state.default_tip
        records = repo.log(default_tip)  # 只取集合；次序由 order_commits 自己规定
        ordered = order_commits(records, default_tip)
        chain = first_parent_chain(records, default_tip)

        commit_pages: list[tuple[dict[str, str], list[CommitRecord]]] = [
            ({"ref": default_name, "period": month}, month_records)
            for month, month_records in sorted(month_partition(chain, ordered, state.since).items())
        ]
        for name, tip in state.branches:
            if tip is None:
                continue
            # 独有提交集合同样用自有排序（从分支 tip 出发做后序 DFS）
            branch_records = order_commits(repo.log(f"{default_tip}..{tip}"), tip)
            commit_pages.append(({"ref": name}, branch_records))

        # 只读出现在本文档页里的提交的 note：其余 note（已丢弃的提交、树或 blob 上的 note）
        # 不属于本文档，内容也不必是合法 UTF-8
        shown = {record.sha for _, page_records in commit_pages for record in page_records}
        noted: dict[str, str] = {}
        for commit, blob in state.notes:
            if commit not in shown:
                continue
            if commit in noted:
                # notes 树损坏：同一被注对象既有 fanout 路径又有平铺路径（或别的重名写法）。
                # 不替用户合并（git notes list 会就地合并并写对象）——如实上报，不可重试；
                # 不在本文档里的被注对象不受影响（上面已跳过，#233）
                raise GitContentError(
                    f"提交 {commit} 在 notes 树（{NOTES_REF}）里有多条路径"
                    f"（{noted[commit]}、{blob}）：notes 树损坏，不替用户合并"
                )
            noted[commit] = blob
        texts = repo.note_texts(list(noted.values())) if noted else {}
        notes = {commit: texts[blob] for commit, blob in noted.items()}

        pages: list[dict[str, Any]] = [
            {"page_metadata": metadata, "elements": _elements(page_records, notes)}
            for metadata, page_records in commit_pages
        ]
        if state.tags:
            elements: list[ElementObject] = []
            for tag in order_tags(repo.tags(state.tags)):
                elements += tag_elements(tag)
            pages.append({"page_metadata": {"kind": "tags"}, "elements": elements})
        return pages, root_commits(records)

    def _oversize(self, pages: list[dict[str, Any]]) -> int | None:
        """仍超远端 ``max_payload_bytes`` 的元素字节数（守卫；不据此重新切分）。"""
        for page in pages:
            for element in page.get("elements", []):
                size = element_bytes(element)
                if self.limits.too_large(size):
                    return size
        return None
