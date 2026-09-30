"""hash 契约 1（``dpe1:``）——spec/hash-contract-1.md 的 SDK 实现。

输入是文档的内容身份视图（普通 dict，形状同 vectors/README.md）。
本模块运行时零依赖，不 import 内核、生成器或 SDK 其它层。

``dpe2`` 仅用于契约升级演练（vectors/README.md）：算法与 dpe1 相同，
但每次摘要额外前置一个内容为 ``b"dpe2"`` 的段。
"""

from __future__ import annotations

import hashlib
from typing import Any

from dpe_hash.jcs import jcs

__all__ = [
    "CONTRACT",
    "TEXT_ONLY_CATEGORIES",
    "HTML_CATEGORIES",
    "RESERVED_METADATA_KEYS",
    "content_hash",
    "page_hash",
    "document_hashes",
]

#: 契约版本（hash 值前缀）
CONTRACT = "dpe1"

#: 契约常量（#3 S1：由 SDK 导出，内核直接 import，不自行维护副本）
TEXT_ONLY_CATEGORIES = frozenset(
    {
        "UncategorizedText",
        "CheckBox",
        "CompositeElement",
        "FigureCaption",
        "NarrativeText",
        "ListItem",
        "Title",
        "Address",
        "EmailAddress",
        "PageBreak",
        "TableChunk",
        "Header",
        "Footer",
        "CodeSnippet",
        "PageNumber",
        "FormKeysValues",
        "tfchat",
    }
)
HTML_CATEGORIES = frozenset({"Table", "Formula"})

#: metadata 保留键集合（契约 1 §2.1）：过滤后才进 hash
RESERVED_METADATA_KEYS = frozenset({"page_number", "page_name", "seq_in_page", "coordinates", "keywords"})


def _seg(b: bytes) -> bytes:
    return len(b).to_bytes(4, "big") + b


def _hval(parts: list[bytes], contract: str) -> str:
    if contract == "dpe2":
        parts = [b"dpe2", *parts]
    elif contract != "dpe1":
        raise ValueError(f"unsupported hash contract: {contract}")
    return f"{contract}:{hashlib.sha256(b''.join(_seg(p) for p in parts)).hexdigest()}"


def _text(s: str | None) -> bytes:
    if s is None:
        return b""
    if not isinstance(s, str):
        raise TypeError(f"text segment must be str or None, got {type(s)}")
    return s.encode("utf-8")


def _strip_nulls(value: Any) -> Any:
    """递归删除对象中值为 null 的键（缺省 ≡ null）；数组元素不受影响。"""
    if isinstance(value, dict):
        return {k: _strip_nulls(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_strip_nulls(v) for v in value]
    return value


def _meta(m: dict[str, Any] | None) -> bytes:
    """metadata 的 hash 输入：过滤保留键（顶层）→ 递归删 null 键 → JCS → UTF-8。"""
    filtered = {k: v for k, v in (m or {}).items() if k not in RESERVED_METADATA_KEYS}
    return jcs(_strip_nulls(filtered)).encode("utf-8")


def content_hash(element: dict[str, Any], contract: str = CONTRACT) -> str:
    """元素 ``content_hash``（契约 1 §4）：首段 category，末段 metadata。"""
    cat = element["category"]
    parts = [_text(cat)]
    if cat == "Image":
        blob = element.get("image_blob")
        # image_url 是访问方式，不进 hash（§4.2）
        blob_ref = f"blob:{blob}" if blob else None
        parts += [_text(element.get("text")), _text(blob_ref), _text(element.get("image_mime_type"))]
    elif cat in HTML_CATEGORIES:
        parts += [_text(element.get("text")), _text(element.get("text_as_html"))]
    elif cat in TEXT_ONLY_CATEGORIES:
        parts += [_text(element.get("text"))]
    else:
        raise ValueError(f"unknown category: {cat}")  # 封闭枚举，禁止退化为 text-only
    parts.append(_meta(element.get("metadata")))
    return _hval(parts, contract)


def page_hash(page: dict[str, Any], element_hashes: list[str], contract: str = CONTRACT) -> str:
    """``page_hash``（契约 1 §5）：页号 ASCII、title、metadata、元素 hash 序列。"""
    parts = [str(page["number"]).encode("ascii"), _text(page.get("title")), _meta(page.get("page_metadata"))]
    parts += [h.encode("utf-8") for h in element_hashes]
    return _hval(parts, contract)


def document_hashes(document: dict[str, Any], contract: str = CONTRACT) -> dict[str, Any]:
    """整篇文档的三层 hash：``{"doc_hash": …, "pages": [{"number", "page_hash", "elements"}…]}``。

    ``pages`` 按输入数组顺序返回；``doc_hash`` 内部按页号升序聚合（契约 1 §5）。
    """
    numbers = [p["number"] for p in document.get("pages", [])]
    if len(numbers) != len(set(numbers)):
        raise ValueError(f"page numbers must be unique, got {numbers}")
    pages_out: list[dict[str, Any]] = []
    ph_by_number: dict[int, str] = {}
    for page in document.get("pages", []):
        ehashes = [content_hash(el, contract) for el in page.get("elements", [])]
        ph = page_hash(page, ehashes, contract)
        ph_by_number[page["number"]] = ph
        pages_out.append({"number": page["number"], "page_hash": ph, "elements": ehashes})
    parts = [_meta(document.get("doc_metadata"))]
    parts += [ph_by_number[n].encode("utf-8") for n in sorted(ph_by_number)]
    return {"doc_hash": _hval(parts, contract), "pages": pages_out}
