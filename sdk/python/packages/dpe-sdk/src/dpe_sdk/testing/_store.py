"""参考服务端的内存存储：按内容寻址的对象表与各文档的当前状态。

- 对象按**主契约**（配置的第一个契约）的 hash 存储；每个受支持契约一张别名索引，把该契约下的
  hash 映射到主契约 hash。页对象与文档对象的 body 以主契约的子 hash 保存，其它契约的 hash 由
  子对象在该契约下的 hash 逐层算出（契约 1 §6 原位重算），按位置一一对应。
- 文档状态记录它引用的全部页、元素与 blob。去重范围只由文档状态决定（core §3.3、§8）：
  对象表里存着某个对象，不代表它对某个调用者可得。
- 对象按引用计数回收：一个对象被多少篇文档的当前状态引用，计数就是多少。
- 反向索引 ``referrers`` 记录每个页、元素与 blob 被哪些文档的当前状态引用：去重范围的判定只看
  本次提交实际引用的对象，不必展开范围内全部文档。
- ``uris`` 是按码点序维护的文档 URI 列表，list 按 cursor 二分切片。

blob 在 #42 阶段只有引用、没有字节（上传随 #43 的暂存会话接入）。
"""

from __future__ import annotations

import bisect
from collections.abc import Iterable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from typing import Any, Literal

import dpe_hash

__all__ = ["DocState", "ObjectRecord", "Store", "TreeObjectKind"]

TreeObjectKind = Literal["page", "element"]

_EMPTY: frozenset[str] = frozenset()


@dataclass
class ObjectRecord:
    """页对象或元素对象，登记后不可变。

    ``body`` 是首次登记时的表示（页的 ``elements`` 为主契约 hash），只用于逐契约算 hash 与引用的
    下一层；各文档读回的原样表示由 ``DocState.page_bodies`` 保存。
    """

    kind: TreeObjectKind
    body: dict[str, Any]
    hashes: dict[str, str]
    refs: int = 0


@dataclass(frozen=True)
class DocState:
    """一篇文档的当前状态。``document`` 的 ``pages`` 为主契约 hash。"""

    document: dict[str, Any]
    #: 与 ``document["pages"]`` 逐位对应的页对象，即本文档最近一次写入的原样表示（core §2.7）
    page_bodies: tuple[dict[str, Any], ...]
    doc_hashes: Mapping[str, str]
    #: 按阅读顺序展开的全部元素（主契约 content_hash，重复保留），用于 delta
    element_seq: tuple[str, ...]
    pages: frozenset[str]
    contents: frozenset[str]
    blobs: frozenset[str]

    @property
    def referenced(self) -> frozenset[str]:
        """本状态引用的全部页、元素（主契约 hash）与 blob。"""
        return self.pages | self.contents | self.blobs

    def matches(self, base_hash: str) -> bool:
        """``base_hash`` 是否等于当前内容在任一受支持契约下的 doc_hash（core §5.1，按值比较）。"""
        return base_hash in self.doc_hashes.values()


@dataclass
class Store:
    contracts: tuple[str, ...]
    objects: dict[str, ObjectRecord] = field(default_factory=dict)
    alias: dict[str, dict[str, str]] = field(default_factory=dict)
    docs: dict[str, DocState] = field(default_factory=dict)
    #: 页 / 元素的主契约 hash 与 blob 引用 → 当前状态引用它的文档 URI。两类键不会冲突：
    #: blob 引用固定为 ``sha256:``，页 / 元素 hash 带契约前缀（契约 1 §1）
    referrers: dict[str, set[str]] = field(default_factory=dict)
    #: 全部文档 URI，按码点序（Python 的 str 比较）
    uris: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.alias = {c: {} for c in self.contracts}

    @property
    def primary(self) -> str:
        return self.contracts[0]

    # ------------------------------------------------------------------
    # 契约换算
    # ------------------------------------------------------------------

    def to_primary(self, contract: str, value: str) -> str | None:
        """某契约下的 hash → 主契约 hash；对象不在表中返回 ``None``。"""
        return self.alias[contract].get(value)

    def tree_hashes(
        self,
        kind: Literal["page", "document"],
        body: Mapping[str, Any],
        children: Sequence[str],
        known: Mapping[str, str] | None = None,
    ) -> dict[str, str]:
        """以主契约子 hash 给出的页 / 文档对象在各受支持契约下的 hash（子对象须已在表中）。

        ``known`` 是已算出的 {契约: hash}（如请求声明的契约），不再重算。
        """
        field_name = "elements" if kind == "page" else "pages"
        out = dict(known or {})
        for contract in self.contracts:
            if contract in out:
                continue
            translated = [self.objects[h].hashes[contract] for h in children]
            out[contract] = dpe_hash.object_hash({**body, field_name: translated}, kind, contract)
        return out

    def element_hashes(
        self, body: Mapping[str, Any], known: Mapping[str, str] | None = None
    ) -> dict[str, str]:
        out = dict(known or {})
        for contract in self.contracts:
            if contract not in out:
                out[contract] = dpe_hash.object_hash(body, "element", contract)
        return out

    def referrers_of(self, value: str) -> AbstractSet[str]:
        """当前状态引用该对象（主契约 hash 或 blob 引用）的文档 URI。返回内部集合的只读视图。"""
        return self.referrers.get(value, _EMPTY)

    def in_contract(
        self, body: Mapping[str, Any], kind: Literal["page", "document"], contract: str
    ) -> dict[str, Any]:
        """把以主契约子 hash 保存的 body 换成某契约下的表示（读接口按请求声明的契约返回）。"""
        field_name = "elements" if kind == "page" else "pages"
        if contract == self.primary:
            return dict(body)
        return {**body, field_name: [self.objects[h].hashes[contract] for h in body[field_name]]}

    # ------------------------------------------------------------------
    # 写入与回收
    # ------------------------------------------------------------------

    def put(self, primary_hash: str, record: ObjectRecord) -> None:
        """登记对象（不改引用计数）；已存在时保持原记录不变（同 hash 即同内容）。"""
        if primary_hash in self.objects:
            return
        self.objects[primary_hash] = record
        for contract, value in record.hashes.items():
            self.alias[contract][value] = primary_hash

    def retain(self, hashes: Iterable[str]) -> None:
        """为每个对象加一次引用；先确认全部在表中，缺任何一个都不改计数。"""
        records = [self.objects.get(h) for h in hashes]
        if any(r is None for r in records):
            raise KeyError("引用了对象表中不存在的对象")
        for record in records:
            assert record is not None
            record.refs += 1

    def release(self, hashes: Iterable[str]) -> None:
        for h in hashes:
            record = self.objects[h]
            record.refs -= 1
            if record.refs == 0:
                del self.objects[h]
                for contract, value in record.hashes.items():
                    self.alias[contract].pop(value, None)

    def _unlink(self, uri: str, state: DocState) -> None:
        for h in state.referenced:
            holders = self.referrers[h]
            holders.discard(uri)
            if not holders:
                del self.referrers[h]

    def _link(self, uri: str, state: DocState) -> None:
        for h in state.referenced:
            self.referrers.setdefault(h, set()).add(uri)

    def _add_uri(self, uri: str) -> None:
        bisect.insort(self.uris, uri)

    def _remove_uri(self, uri: str) -> None:
        del self.uris[bisect.bisect_left(self.uris, uri)]

    def replace(self, uri: str, state: DocState | None) -> DocState | None:
        """原子切换文档状态：先计入新状态的引用，再释放旧状态的引用。返回旧状态。"""
        old = self.docs.get(uri)
        # 先计入新状态的引用（唯一可能失败的一步：对象缺失时整体不改计数），再改任何索引
        if state is not None:
            self.retain(state.pages | state.contents)
        if old is not None:
            self._unlink(uri, old)
        if state is not None:
            self.docs[uri] = state
            self._link(uri, state)
            if old is None:
                self._add_uri(uri)
        elif old is not None:
            del self.docs[uri]
            self._remove_uri(uri)
        if old is not None:
            self.release(old.pages | old.contents)
        return old

    def rename(self, from_uri: str, to_uri: str) -> DocState:
        """整篇文档改名（move）：状态与引用计数原样保留，只更新 URI 相关的索引。"""
        state = self.docs.pop(from_uri)
        self._unlink(from_uri, state)
        self._remove_uri(from_uri)
        self.docs[to_uri] = state
        self._link(to_uri, state)
        self._add_uri(to_uri)
        return state

    def page_after(self, prefix: str, after: str | None, limit: int) -> list[str]:
        """码点序中以 ``prefix`` 开头、严格大于 ``after`` 的前 ``limit`` 个 URI。"""
        start = bisect.bisect_left(self.uris, prefix)
        if after is not None:
            start = max(start, bisect.bisect_right(self.uris, after))
        # 同一前缀的 URI 在码点序中连续；逐个取，不切片整个列表
        out: list[str] = []
        for i in range(start, len(self.uris)):
            uri = self.uris[i]
            if len(out) == limit or not uri.startswith(prefix):
                break
            out.append(uri)
        return out
