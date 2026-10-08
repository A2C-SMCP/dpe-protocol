"""参考服务端的内存存储：按内容寻址的对象表与各文档的当前状态。

- 对象按**主契约**（配置的第一个契约）的 hash 存储；每个受支持契约一张别名索引，把该契约下的
  hash 映射到主契约 hash。页对象与文档对象的 body 以主契约的子 hash 保存，其它契约的 hash 由
  子对象在该契约下的 hash 逐层算出（契约 1 §6 原位重算），按位置一一对应。
- 文档状态记录它引用的全部页、元素与 blob。去重范围只由文档状态决定（core §3.3、§8）：
  对象表里存着某个对象，不代表它对某个调用者可得。
- 对象按引用计数回收：一个对象被多少篇文档的当前状态引用，计数就是多少。

blob 在 #42 阶段只有引用、没有字节（上传随 #43 的暂存会话接入）。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

import dpe_hash

__all__ = ["DocState", "ObjectRecord", "Store", "TreeObjectKind"]

TreeObjectKind = Literal["page", "element"]


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

    def matches(self, base_hash: str) -> bool:
        """``base_hash`` 是否等于当前内容在任一受支持契约下的 doc_hash（core §5.1，按值比较）。"""
        return base_hash in self.doc_hashes.values()


@dataclass
class Store:
    contracts: tuple[str, ...]
    objects: dict[str, ObjectRecord] = field(default_factory=dict)
    alias: dict[str, dict[str, str]] = field(default_factory=dict)
    docs: dict[str, DocState] = field(default_factory=dict)

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
        self, kind: Literal["page", "document"], body: Mapping[str, Any], children: Sequence[str]
    ) -> dict[str, str]:
        """以主契约子 hash 给出的页 / 文档对象在各受支持契约下的 hash（子对象须已在表中）。"""
        field_name = "elements" if kind == "page" else "pages"
        out: dict[str, str] = {}
        for contract in self.contracts:
            translated = [self.objects[h].hashes[contract] for h in children]
            out[contract] = dpe_hash.object_hash({**body, field_name: translated}, kind, contract)
        return out

    def element_hashes(self, body: Mapping[str, Any]) -> dict[str, str]:
        return {c: dpe_hash.object_hash(body, "element", c) for c in self.contracts}

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
        for h in hashes:
            self.objects[h].refs += 1

    def release(self, hashes: Iterable[str]) -> None:
        for h in hashes:
            record = self.objects[h]
            record.refs -= 1
            if record.refs == 0:
                del self.objects[h]
                for contract, value in record.hashes.items():
                    self.alias[contract].pop(value, None)

    def replace(self, uri: str, state: DocState | None) -> DocState | None:
        """原子切换文档状态：先计入新状态的引用，再释放旧状态的引用。返回旧状态。"""
        old = self.docs.get(uri)
        if state is not None:
            self.retain(state.pages | state.contents)
            self.docs[uri] = state
        else:
            self.docs.pop(uri, None)
        if old is not None:
            self.release(old.pages | old.contents)
        return old
