"""hash 契约 1（``dpe1:``）——spec/hash-contract-1.md 的 SDK 实现。

契约 1 是三层同构的 tree hash（PR #9）：元素、页、根三层对象统一为

    H(obj) = "dpe1:" + hex(sha256(utf8(JCS(norm(obj)))))

``norm``：递归删除值为 null 的键（缺省 ≡ null）；三层 metadata 字段缺省视同 ``{}``。
页没有页号字段，页序即 ``pages`` 数组顺序。

输入是文档的 hash 输入视图（普通 dict，形状同 vectors/README.md）。
本模块运行时零依赖，不 import 内核、生成器或 SDK 其它层。

``dpe2`` 仅用于契约升级演练（vectors/README.md）：摘要输入为 ASCII ``dpe2``
后接 JCS 原像字节。
"""

from __future__ import annotations

import hashlib
from typing import Any

from dpe_hash.jcs import jcs

__all__ = [
    "CONTRACT",
    "TEXT_ONLY_CATEGORIES",
    "HTML_CATEGORIES",
    "CATEGORY_CONTENT_FIELDS",
    "FILE_TYPES",
    "content_hash",
    "page_hash",
    "document_hashes",
]

#: 契约版本（hash 值前缀）
CONTRACT = "dpe1"

#: 契约常量（#3 S1：由 SDK 导出，消费方直接 import，不自行维护副本）
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

#: category → 允许的内容字段序列（契约 1 §4.1；#7 要求的统一映射表）
CATEGORY_CONTENT_FIELDS: dict[str, tuple[str, ...]] = {
    **{c: ("text",) for c in TEXT_ONLY_CATEGORIES},
    **{c: ("text", "text_as_html") for c in HTML_CATEGORIES},
    "Image": ("text", "image_blob", "image_mime_type"),
}

#: file_type 封闭枚举（core.md §2.5，进 doc_hash；与 vectors/manifest.json 的 file_types 一致）
FILE_TYPES = frozenset(
    {
        "bmp", "csv", "doc", "docx", "eml", "epub", "heic", "html", "jpg", "json",
        "md", "msg", "ndjson", "odt", "org", "pdf", "png", "ppt", "pptx", "rst",
        "rtf", "tiff", "tsv", "txt", "wav", "xls", "xlsx", "xml", "zip",
        "java_repo", "python_repo", "javascript_repo", "typescript_repo",
        "unk", "empty", "tfchat", "jira_project", "jira_issue",
    }
)


def _hval(obj: dict[str, Any], contract: str) -> str:
    preimage = jcs(obj).encode("utf-8")
    if contract == "dpe2":
        preimage = b"dpe2" + preimage
    elif contract != "dpe1":
        raise ValueError(f"unsupported hash contract: {contract}")
    return f"{contract}:{hashlib.sha256(preimage).hexdigest()}"


def _strip_nulls(value: Any) -> Any:
    """递归删除对象中值为 null 的键（缺省 ≡ null，契约 1 §3.2）；数组元素不受影响。"""
    if isinstance(value, dict):
        return {k: _strip_nulls(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_strip_nulls(v) for v in value]
    return value


def _meta(m: dict[str, Any] | None) -> dict[str, Any]:
    """metadata 规范化：缺省视同 {}，递归删 null 键。在原像中总是出现（§3.2）。"""
    if m is None:
        return {}
    if not isinstance(m, dict):
        raise TypeError(f"metadata must be dict or None, got {type(m)}")
    return _strip_nulls(m)


def _str_or_none(obj: dict[str, Any], field: str) -> str | None:
    value = obj.get(field)
    if value is not None and not isinstance(value, str):
        raise TypeError(f"{field} must be str or None, got {type(value)}")
    return value


def content_hash(element: dict[str, Any], contract: str = CONTRACT) -> str:
    """元素 ``content_hash``（契约 1 §4）：``H({category, 内容字段…, metadata})``。"""
    cat = element["category"]
    allowed = CATEGORY_CONTENT_FIELDS.get(cat)
    if allowed is None:
        raise ValueError(f"unknown category: {cat}")  # 封闭枚举，禁止退化为 text-only
    extra = set(element) - {"category", "metadata", *allowed}
    if extra:
        raise ValueError(f"fields not allowed for category {cat}: {sorted(extra)}")  # 封闭 schema（§3.4）
    obj: dict[str, Any] = {"category": cat}
    for field in allowed:
        value = _str_or_none(element, field)
        if value is not None:
            obj[field] = value
    obj["metadata"] = _meta(element.get("metadata"))
    return _hval(obj, contract)


def page_hash(page: dict[str, Any], element_hashes: list[str], contract: str = CONTRACT) -> str:
    """``page_hash``（契约 1 §5）：``H({title?, page_metadata, elements})``。页没有页号字段。"""
    extra = set(page) - {"title", "page_metadata", "elements"}
    if extra:
        raise ValueError(f"fields not allowed on page: {sorted(extra)}")
    obj: dict[str, Any] = {}
    title = _str_or_none(page, "title")
    if title is not None:
        obj["title"] = title
    obj["page_metadata"] = _meta(page.get("page_metadata"))
    obj["elements"] = list(element_hashes)
    return _hval(obj, contract)


def document_hashes(document: dict[str, Any], contract: str = CONTRACT) -> dict[str, Any]:
    """整篇文档的三层 hash：``{"doc_hash": …, "pages": [{"page_hash", "elements"}…]}``。

    页序即 ``pages`` 数组顺序（契约 1 §5）。
    """
    extra = set(document) - {"file_type", "title", "doc_metadata", "pages"}
    if extra:
        raise ValueError(f"fields not allowed on document: {sorted(extra)}")
    file_type = document["file_type"]
    if file_type not in FILE_TYPES:
        raise ValueError(f"unknown file_type: {file_type}")
    pages_out: list[dict[str, Any]] = []
    page_hashes: list[str] = []
    for page in document.get("pages", []):
        ehashes = [content_hash(el, contract) for el in page.get("elements", [])]
        ph = page_hash(page, ehashes, contract)
        page_hashes.append(ph)
        pages_out.append({"page_hash": ph, "elements": ehashes})
    root: dict[str, Any] = {"file_type": file_type}
    title = _str_or_none(document, "title")  # 根对象可选 title，与页对称（规范 0.1.3，撤回 #3 S6）
    if title is not None:
        root["title"] = title
    root["doc_metadata"] = _meta(document.get("doc_metadata"))
    root["pages"] = page_hashes
    return {"doc_hash": _hval(root, contract), "pages": pages_out}
