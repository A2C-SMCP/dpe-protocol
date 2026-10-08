"""参考服务端引擎测试的辅助：快路径请求体构造与测试用授权器（经 pytest pythonpath 导入）。"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import dpe_hash
from dpe_sdk.testing import AllowAll, Engine, EngineConfig


@dataclass(frozen=True)
class Inline:
    """一篇展开文档对应的快路径 commit 请求体与各层 hash。"""

    document: dict[str, Any]
    pages: list[dict[str, Any]]
    objects: list[dict[str, Any]]
    doc_hash: str
    page_hashes: list[str]
    content_hashes: list[list[str]]

    def body(self, **extra: Any) -> bytes:
        payload: dict[str, Any] = {
            "document": self.document,
            "pages": self.pages,
            "objects": self.objects,
        }
        payload.update(extra)
        return json.dumps(payload).encode("utf-8")


def inline(document: Mapping[str, Any], contract: str = dpe_hash.CONTRACT) -> Inline:
    """把展开视图（向量输入形状）拆成线上的文档对象、页对象与元素对象。"""
    pages: list[dict[str, Any]] = []
    objects: list[dict[str, Any]] = []
    page_hashes: list[str] = []
    content_hashes: list[list[str]] = []
    for page in document["pages"]:
        hashes = [dpe_hash.object_hash(e, "element", contract) for e in page["elements"]]
        objects.extend(page["elements"])
        wire_page = {**{k: v for k, v in page.items() if k != "elements"}, "elements": hashes}
        pages.append(wire_page)
        page_hashes.append(dpe_hash.object_hash(wire_page, "page", contract))
        content_hashes.append(hashes)
    wire_doc = {**{k: v for k, v in document.items() if k != "pages"}, "pages": page_hashes}
    return Inline(
        document=wire_doc,
        pages=pages,
        objects=objects,
        doc_hash=dpe_hash.object_hash(wire_doc, "document", contract),
        page_hashes=page_hashes,
        content_hashes=content_hashes,
    )


def text_doc(*pages: Sequence[str], file_type: str = "md", **fields: Any) -> dict[str, Any]:
    """由逐页的文本列表构造展开文档（每段文本一个 NarrativeText 元素）。"""
    return {
        "file_type": file_type,
        **fields,
        "pages": [
            {"elements": [{"category": "NarrativeText", "text": t} for t in page]} for page in pages
        ],
    }


@dataclass
class PrefixAuthorizer:
    """测试用授权器：调用者 → 可写前缀；``force`` 中的调用者有 force 权限。"""

    prefixes: dict[str, tuple[str, ...]]
    force: frozenset[str] = field(default_factory=frozenset)

    def can_write(self, caller: str, uri: str) -> bool:
        return any(uri.startswith(p) for p in self.prefixes.get(caller, ()))

    def can_force(self, caller: str, uri: str) -> bool:
        return caller in self.force


def make_engine(**overrides: Any) -> Engine:
    """按 ``EngineConfig`` 的字段覆盖默认值构造引擎（默认授权器放行一切）。"""
    overrides.setdefault("authorizer", AllowAll())
    return Engine(EngineConfig(**overrides))
