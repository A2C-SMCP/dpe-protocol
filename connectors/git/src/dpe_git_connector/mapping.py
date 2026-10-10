"""仓库的提交历史 → DPE 产出条目（connector 契约 §6.5）。

一篇 ``document`` = 一个仓库（``file_type`` ``git_repo``）；页与元素是提交历史的可读面
（README「映射规则」）：

- **默认分支按月一页**：月份 = 改动**进入主线的时间**——该提交由 first-parent 链上哪个提交
  引入（链上提交就是它自己，合并带入的提交是把它合并进来的那个提交），就取那个提交的
  committer 时间（UTC）的 ``YYYY-MM``；``page_metadata`` 为 ``{ref: <默认分支名>, period: …}``；
- **其他跟踪分支每条一页**：只含该分支独有（``<default>..<branch>``）的提交，
  ``page_metadata`` 为 ``{ref: <分支名>}``；
- **元素 = 一次提交**（见 ``extractors``）。

页序：月页按月份升序在前，分支页按分支名（字节序）在后；页内旧在前——**自有拓扑序**（从
起始提交出发按父提交记录顺序做后序 DFS，见 ``gitrepo.order_commits`` 与 README「确定性」；
只依赖提交图，不依赖 git 的输出次序，跨 git 版本稳定），合并带入的提交排在其合并提交之前。
不写易变字段、不写由位置算出的序号（core §2.4）；``file_uri`` 是身份（实例前缀 + 仓库标识），
身份不进 hash。

失败如实产出 ``error`` 条目（§6.5 的封闭枚举），不静默跳过——**读取类与内容类分开**：

- ``source_unavailable``（``retryable: true``——源可能恢复）：仓库不可读、默认分支无法确定、
  发现层读不了的子树；
- ``content_invalid``（不可重试）：``file_uri`` 不合法、提交说明 / 作者不是合法 UTF-8、
  时间戳不合规（``GitContentError``——同样扫描重跑不会成功），或按固定预算切分后仍超过远端
  ``max_payload_bytes``（部署配置问题，不静默按远端值重新切分）。

本模块只做全量枚举：增量游标与删除 / 移动意图归后续拆分（#66）。仓库集合与单仓库映射在这里
已经分开（``RepoDir`` 进、条目出）：仓库消失 / 改名由发现层（``gitrepo.discover_repos`` 的结果
集合）在上层表达 delete / move，本模块的映射规则不需要为它改动。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from dpe_hash import ContractUnsupportedError, ElementObject, ValidationError, normalize_file_uri

from dpe_git_connector.extractors import commit_elements, element_bytes
from dpe_git_connector.gitrepo import (
    CommitRecord,
    GitContentError,
    GitError,
    GitRepo,
    RepoDir,
    first_parent_chain,
    order_commits,
)

__all__ = [
    "DocumentMapper",
    "Item",
    "Limits",
    "RepoSpec",
    "build_file_uri",
    "month_partition",
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


def _elements(records: Sequence[CommitRecord]) -> list[ElementObject]:
    """提交列表（旧在前）→ 元素列表（说明超预算的提交切成多个连续元素）。"""
    elements: list[ElementObject] = []
    for record in records:
        elements += commit_elements(record)
    return elements


class DocumentMapper:
    """把一个仓库的提交历史映射成一篇文档的产出条目。"""

    def __init__(self, prefix: str, limits: Limits | None = None) -> None:
        self.prefix = prefix
        self.limits = limits or Limits()

    def document(self, repo: GitRepo, entry: RepoDir, spec: RepoSpec) -> Item:
        """枚举一个仓库 → ``document`` 条目；失败给 ``error`` 条目。"""
        try:
            file_uri = build_file_uri(self.prefix, entry.identifier)
        except (ValueError, ValidationError, ContractUnsupportedError) as exc:
            return self.error_item(None, "content_invalid", f"file_uri 不合法：{exc}", False)
        if not file_uri.startswith(self.prefix):
            # 标识由前缀拼接而来，正常不会越界；防御性上报（契约 §6.5 要求不静默忽略）
            return self.error_item(file_uri, "internal", "file_uri 越出实例前缀", retryable=False)
        try:
            pages = self._pages(repo, spec)
        except GitContentError as exc:
            # 内容类失败（非 UTF-8 的说明 / 作者、非法时间戳）：映射不出合法 DPE 内容，
            # 同样扫描重跑也不会成功——content_invalid、不可重试（§6.5 定义）
            return self.error_item(file_uri, "content_invalid", str(exc), retryable=False)
        except GitError as exc:
            return self.error_item(file_uri, "source_unavailable", str(exc), retryable=True)
        except Exception as exc:
            # 条目级兜底：任何未预期的解析异常（超大字段、解析器内部错误）都不得带崩进程——
            # 崩了会让每轮都以 plugin_crashed 收场、实例永不收敛（§8.1-5）
            return self.error_item(
                file_uri, "internal", f"映射时发生未预期错误：{type(exc).__name__}: {exc}", False
            )
        oversize = self._oversize(pages)
        if oversize is not None:
            return self.error_item(
                file_uri,
                "content_invalid",
                "元素按固定预算切分后仍超过远端 max_payload_bytes"
                f"（{oversize} 字节 > {self.limits.max_payload_bytes}）",
                retryable=False,
            )
        return Item(
            kind="document",
            fields={
                "file_uri": file_uri,
                "document": {"file_type": "git_repo"},
                "pages": pages,
            },
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
        try:
            file_uri = build_file_uri(self.prefix, entry.identifier)
        except (ValueError, ValidationError, ContractUnsupportedError) as exc:
            return self.error_item(None, "content_invalid", f"file_uri 不合法：{exc}", False)
        return self.error_item(
            file_uri, "source_unavailable", f"无法读取（发现层）：{entry.path}", retryable=True
        )

    def _pages(self, repo: GitRepo, spec: RepoSpec) -> list[dict[str, Any]]:
        """一个仓库的全部页对象（月页在前、分支页在后）。"""
        repo.open()
        if not repo.has_refs():
            # 空仓库（没有任何 ref）：零页文档（契约 1 §5 允许空文档），不是错误
            return []
        default_name = repo.default_branch(spec.default_branch)
        default_tip = repo.resolve_branch(default_name)
        if default_tip is None:
            raise GitError(f"默认分支 {default_name!r} 无法解析为提交")
        records = repo.log(default_tip)  # 只取集合；次序由 order_commits 自己规定
        ordered = order_commits(records, default_tip)
        chain = first_parent_chain(records, default_tip)
        since_sha: str | None = None
        if spec.since is not None:
            since_sha = repo.resolve_ref(spec.since)
            if since_sha is None:
                raise GitError(f"since 无法解析为提交：{spec.since}")
            if not repo.is_ancestor(since_sha, default_tip):
                raise GitError(f"since 不是默认分支 {default_name!r} 的祖先：{spec.since}")

        pages: list[dict[str, Any]] = []
        for month, month_records in sorted(month_partition(chain, ordered, since_sha).items()):
            pages.append(
                {
                    "page_metadata": {"ref": default_name, "period": month},
                    "elements": _elements(month_records),
                }
            )
        tracked = sorted(
            {name for name in spec.branches if name != default_name},
            key=lambda name: name.encode("utf-8"),
        )
        for name in tracked:
            tip = repo.resolve_branch(name)
            if tip is None:
                # 解析不到（未建或已删）：不产该页，也不是错误——与「删除分支 = 删页」一致
                continue
            pages.append(
                {
                    "page_metadata": {"ref": name},
                    # 独有提交集合同样用自有排序（从分支 tip 出发做后序 DFS）
                    "elements": _elements(order_commits(repo.log(f"{default_tip}..{tip}"), tip)),
                }
            )
        return pages

    def _oversize(self, pages: list[dict[str, Any]]) -> int | None:
        """仍超远端 ``max_payload_bytes`` 的元素字节数（守卫；不据此重新切分）。"""
        for page in pages:
            for element in page.get("elements", []):
                size = element_bytes(element)
                if self.limits.too_large(size):
                    return size
        return None
