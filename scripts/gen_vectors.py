#!/usr/bin/env python3
"""一致性向量生成器 —— hash 契约 1（spec/hash-contract-1.md）的规范参考实现。

治理模型（docs/plan/v1-plan.md §1）：向量是规范的一部分，由本仓库产出；
SDK 与各服务端实现只消费向量。本脚本因此：

- 只用标准库，不 import 任何 SDK 或内核代码；
- 输出确定性字节（同一版本脚本重复运行产物逐字节一致），`--check` 用于 CI 校验
  已提交向量与脚本一致。

用法：
    python3 scripts/gen_vectors.py            # 重新生成 vectors/
    python3 scripts/gen_vectors.py --check    # 校验 vectors/ 与脚本一致（不落盘）
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

VECTORS_DIR = Path(__file__).resolve().parent.parent / "vectors"

# ---------------------------------------------------------------------------
# hash 契约 1 原语（spec/hash-contract-1.md §2–§3）
# ---------------------------------------------------------------------------

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

#: 保留键集合（契约 1 §2.1）分两组，每个键只属于一组。SDK 须以常量导出同一集合。
#: 随位投递：该内容在某个位置出现时附带、不进 hash 的字段，线上只能出现在骨架 entry 的 occurrence 里。
OCCURRENCE_KEYS = frozenset(
    {
        "coordinates",  # 寻址坐标
        "image_url",  # 访问方式（仅 Image）
        "parent_id",  # 分区关系
        "related_ids",  # 分区关系
    }
)
#: 不投递：由骨架位置派生、服务端衍生或推送侧本地的字段，线上出现在任何位置即拒绝。
UNDELIVERED_KEYS = frozenset(
    {
        "page_number",  # 骨架位置派生
        "page_name",  # 骨架位置派生
        "seq_in_page",  # 骨架位置派生
        "keywords",  # 服务端衍生
        "image_path",  # 推送侧本地
        "image_base64",  # 须转成 blob
        "file_directory",  # 推送侧本地
    }
)
#: hash 核心过滤的顶层键 = 两组的并集。
RESERVED_METADATA_KEYS = OCCURRENCE_KEYS | UNDELIVERED_KEYS
assert not OCCURRENCE_KEYS & UNDELIVERED_KEYS

#: file_type 封闭枚举（core.md §2.6），由源提供，进 doc_hash（契约 1 §5）。
FILE_TYPES = (
    "bmp", "csv", "doc", "docx", "eml", "epub", "heic", "html", "jpg", "json", "md", "msg", "ndjson",
    "odt", "org", "pdf", "png", "ppt", "pptx", "rst", "rtf", "tiff", "tsv", "txt", "wav", "xls", "xlsx",
    "xml", "zip", "java_repo", "python_repo", "javascript_repo", "typescript_repo", "unk", "empty",
    "tfchat", "jira_project", "jira_issue",
)  # fmt: skip

#: attributes 键形如 <ns>/<key>（core.md §2.5）。
ATTRIBUTE_KEY = re.compile(r"[a-z0-9_-]+/[a-z0-9_-]+")

#: dpe2 是**仅用于契约升级演练的假想契约**（vectors/README.md）：
#: 算法与 dpe1 相同，但每次摘要额外前置一个内容为 b"dpe2" 的段。
TEST_CONTRACTS = ("dpe1", "dpe2")


def seg(b: bytes) -> bytes:
    return len(b).to_bytes(4, "big") + b


def hval(parts: list[bytes], contract: str) -> str:
    if contract == "dpe2":
        parts = [b"dpe2", *parts]
    digest = hashlib.sha256(b"".join(seg(p) for p in parts)).hexdigest()
    return f"{contract}:{digest}"


def text(s: str | None) -> bytes:
    assert s is None or isinstance(s, str)
    return (s or "").encode("utf-8")


def strip_nulls(value: Any) -> Any:
    """递归删除对象中值为 null 的键（缺省 ≡ null，§2.1）；数组元素不受影响。"""
    if isinstance(value, dict):
        return {k: strip_nulls(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [strip_nulls(v) for v in value]
    return value


def meta(m: dict[str, Any] | None) -> bytes:
    """metadata 的 hash 输入：过滤保留键（顶层）→ 递归删 null 键 → JCS → UTF-8。"""
    filtered = {k: v for k, v in (m or {}).items() if k not in RESERVED_METADATA_KEYS}
    return jcs(strip_nulls(filtered)).encode("utf-8")


def content_fields(cat: str) -> frozenset[str]:
    """内容对象允许的字段 = content_hash 的完整原像（core.md §2.3）；未知 category 拒绝。"""
    base = frozenset({"category", "text", "metadata"})
    if cat == "Image":
        return base | {"image_blob", "image_mime_type"}
    if cat in HTML_CATEGORIES:
        return base | {"text_as_html"}
    if cat in TEXT_ONLY_CATEGORIES:
        return base
    raise ValueError(f"unknown category: {cat}")


def validate_element(el: dict[str, Any]) -> None:
    """向量元素 = 内容对象 + 可选 occurrence（骨架 entry 上的随位字段）；多余字段即违规。"""
    cat = el["category"]
    extra = set(el) - content_fields(cat) - {"occurrence"}
    if extra:
        raise ValueError(f"{cat}: fields outside content object: {sorted(extra)}")
    occ = el.get("occurrence") or {}
    if set(occ) - OCCURRENCE_KEYS:
        raise ValueError(f"occurrence keys not allowed: {sorted(set(occ) - OCCURRENCE_KEYS)}")
    if "image_url" in occ and cat != "Image":
        raise ValueError("image_url is only allowed on Image entries")


def content_hash(el: dict[str, Any], contract: str) -> str:
    validate_element(el)
    cat = el["category"]
    parts = [text(cat)]
    if cat == "Image":
        blob = el.get("image_blob")
        # image_url 是访问方式，在 occurrence 上，不进 hash（§4.2，#3 S4）
        blob_ref = f"blob:{blob}" if blob else None
        parts += [text(el.get("text")), text(blob_ref), text(el.get("image_mime_type"))]
    elif cat in HTML_CATEGORIES:
        parts += [text(el.get("text")), text(el.get("text_as_html"))]
    else:
        parts += [text(el.get("text"))]
    parts.append(meta(el.get("metadata")))
    return hval(parts, contract)


def page_hash(page: dict[str, Any], element_hashes: list[str], contract: str) -> str:
    parts = [str(page["number"]).encode("ascii"), text(page.get("title")), meta(page.get("page_metadata"))]
    parts += [h.encode("utf-8") for h in element_hashes]
    return hval(parts, contract)


def state_hash(doc: dict[str, Any], doc_hash_: str, contract: str) -> str:
    """投递状态摘要（§6）：doc_hash + attributes + 各位置的 occurrence（页按 number 升序）。"""
    attributes = doc.get("attributes") or {}
    bad = [k for k in attributes if not ATTRIBUTE_KEY.fullmatch(k)]
    if bad:
        raise ValueError(f"invalid attribute keys: {bad}")
    pages = sorted(doc.get("pages", []), key=lambda p: p["number"])
    surface = {
        "attributes": attributes,
        "occurrences": [[el.get("occurrence") or {} for el in p.get("elements", [])] for p in pages],
    }
    parts = [text("state"), doc_hash_.encode("utf-8"), jcs(strip_nulls(surface)).encode("utf-8")]
    return hval(parts, contract)


def doc_hashes(doc: dict[str, Any], contract: str) -> dict[str, Any]:
    """返回一篇文档在某契约下的全部期望值。"""
    file_type = doc["file_type"]
    if file_type not in FILE_TYPES:
        raise ValueError(f"unknown file_type: {file_type}")
    numbers = [p["number"] for p in doc.get("pages", [])]
    if len(numbers) != len(set(numbers)):
        raise ValueError(f"duplicate page numbers: {numbers}")
    pages_out = []
    ph_by_number: dict[int, str] = {}
    for page in doc.get("pages", []):
        ehashes = [content_hash(el, contract) for el in page.get("elements", [])]
        ph = page_hash(page, ehashes, contract)
        ph_by_number[page["number"]] = ph
        pages_out.append({"number": page["number"], "page_hash": ph, "elements": ehashes})
    parts = [text(file_type), meta(doc.get("doc_metadata"))]
    parts += [ph_by_number[n].encode("utf-8") for n in sorted(ph_by_number)]
    dh = hval(parts, contract)
    return {"doc_hash": dh, "state_hash": state_hash(doc, dh, contract), "pages": pages_out}


# ---------------------------------------------------------------------------
# RFC 8785 JCS（spec/hash-contract-1.md §3.3）
# ---------------------------------------------------------------------------


def es_number(x: int | float) -> str:
    """ECMAScript ``Number::toString``（base 10）。"""
    if isinstance(x, bool):  # bool 是 int 子类，须先拦截
        raise TypeError("bool is not a number")
    if isinstance(x, int):
        if abs(x) > 2**53 - 1:
            raise ValueError(f"integer out of IEEE-754 safe range: {x}")
        return str(x)
    if math.isnan(x) or math.isinf(x):
        raise ValueError("NaN / Infinity not allowed in JCS")
    if x == 0:
        return "0"  # 含 -0.0
    d = Decimal(repr(x)).normalize()  # repr 即最短往返十进制
    sign, digits, exp = d.as_tuple()
    s = "".join(map(str, digits))
    k = len(s)
    n = k + int(exp)  # value = 0.s × 10^n
    if k <= n <= 21:
        body = s + "0" * (n - k)
    elif 0 < n <= 21:
        body = s[:n] + "." + s[n:]
    elif -6 < n <= 0:
        body = "0." + "0" * (-n) + s
    else:
        mantissa = s[0] + ("." + s[1:] if k > 1 else "")
        e = n - 1
        body = f"{mantissa}e{'+' if e >= 0 else '-'}{abs(e)}"
    return ("-" if sign else "") + body


_JCS_ESCAPES = {'"': '\\"', "\\": "\\\\", "\b": "\\b", "\t": "\\t", "\n": "\\n", "\f": "\\f", "\r": "\\r"}


def jcs_string(s: str) -> str:
    out = ['"']
    for ch in s:
        if ch in _JCS_ESCAPES:
            out.append(_JCS_ESCAPES[ch])
        elif ord(ch) < 0x20:
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def jcs(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float)):
        return es_number(value)
    if isinstance(value, str):
        return jcs_string(value)
    if isinstance(value, list):
        return "[" + ",".join(jcs(v) for v in value) + "]"
    if isinstance(value, dict):
        keys = sorted(value, key=lambda k: k.encode("utf-16-be"))  # UTF-16 码元序
        return "{" + ",".join(f"{jcs_string(k)}:{jcs(value[k])}" for k in keys) + "}"
    raise TypeError(f"unsupported type: {type(value)}")


# ---------------------------------------------------------------------------
# 向量定义
# ---------------------------------------------------------------------------


def el(category: str, text_: str | None = None, **kw: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"category": category}
    if text_ is not None:
        out["text"] = text_
    out.update(kw)
    return out


def doc(
    *pages: dict[str, Any],
    file_type: str = "md",
    doc_metadata: dict[str, Any] | None = None,
    attributes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {"file_type": file_type, "pages": list(pages)}
    if doc_metadata is not None:
        out["doc_metadata"] = doc_metadata
    if attributes is not None:
        out["attributes"] = attributes
    return out


def page(number: int, title: str | None, *elements: dict[str, Any], **kw: Any) -> dict[str, Any]:
    return {"number": number, "title": title, "elements": list(elements), **kw}


def eq(*refs: str) -> dict[str, Any]:
    """relations：所列引用的值两两相等。引用形如 ``<文档键>.<路径>``（路径见 vectors/README.md）。"""
    return {"equal": list(refs)}


def ne(*refs: str) -> dict[str, Any]:
    """relations：所列引用的值两两不同。"""
    return {"distinct": list(refs)}


_BLOB = "sha256:" + hashlib.sha256(b"dpe-vector-blob").hexdigest()
_BLOB2 = "sha256:" + hashlib.sha256(b"dpe-vector-blob-2").hexdigest()
_BOX_A = {"points": [[0.1, 0.1], [0.4, 0.2]], "system": "PixelSpace"}
_BOX_B = {"points": [[0.1, 0.5], [0.4, 0.6]], "system": "PixelSpace"}

DOCUMENT_VECTORS: list[dict[str, Any]] = [
    {
        "name": "element_text_basic",
        "description": "最小文档：单页单 NarrativeText，无 metadata（meta 段为 \"{}\" 的字节），无 attributes 与 occurrence。",
        "documents": {"doc": doc(page(1, "p1", el("NarrativeText", "Hello, DPE.")))},
    },
    {
        "name": "all_text_categories",
        "description": "全部 text-only category 各一个元素，同页排列；category 进入 hash，故同文本不同 category 的值互不相同。",
        "documents": {
            "doc": doc(page(1, None, *[el(c, "same text") for c in sorted(TEXT_ONLY_CATEGORIES)]))
        },
        "relations": [ne(*[f"doc.pages.0.elements.{i}" for i in range(len(TEXT_ONLY_CATEGORIES))])],
    },
    {
        "name": "table_formula_html",
        "description": "Table / Formula 的身份包含 text_as_html：text 相同而 html 不同时 hash 不同。",
        "documents": {
            "doc": doc(
                page(
                    1,
                    "tables",
                    el("Table", "a b", text_as_html="<table><tr><td>a</td><td>b</td></tr></table>"),
                    el("Table", "a b", text_as_html="<table><tr><td>a</td></tr><tr><td>b</td></tr></table>"),
                    el("Formula", "E=mc^2", text_as_html="<math>E=mc<sup>2</sup></math>"),
                    el("TableChunk", "a b"),
                )
            )
        },
        "relations": [ne("doc.pages.0.elements.0", "doc.pages.0.elements.1", "doc.pages.0.elements.3")],
    },
    {
        "name": "metadata_identity",
        "description": "源提供的 metadata 是内容身份（#3 S2）：doc / page / element 任一层 metadata 变化，对应层及以上的 hash 变化，下层不受影响。",
        "documents": {
            "base": doc(
                page(1, "p", el("NarrativeText", "x", metadata={"lang": "zh"}), page_metadata={"src": "b1"}),
                doc_metadata={"author": "gmq", "created_at": "2026-09-01T02:03:04Z"},
            ),
            "ele_meta_changed": doc(
                page(1, "p", el("NarrativeText", "x", metadata={"lang": "en"}), page_metadata={"src": "b1"}),
                doc_metadata={"author": "gmq", "created_at": "2026-09-01T02:03:04Z"},
            ),
            "page_meta_changed": doc(
                page(1, "p", el("NarrativeText", "x", metadata={"lang": "zh"}), page_metadata={"src": "b2"}),
                doc_metadata={"author": "gmq", "created_at": "2026-09-01T02:03:04Z"},
            ),
            "doc_meta_changed": doc(
                page(1, "p", el("NarrativeText", "x", metadata={"lang": "zh"}), page_metadata={"src": "b1"}),
                doc_metadata={"author": "gmq", "created_at": "2026-09-02T00:00:00Z"},
            ),
        },
        "relations": [
            ne("base.doc_hash", "ele_meta_changed.doc_hash", "page_meta_changed.doc_hash", "doc_meta_changed.doc_hash"),
            ne("base.pages.0.elements.0", "ele_meta_changed.pages.0.elements.0"),
            eq("base.pages.0.elements.0", "page_meta_changed.pages.0.elements.0"),
            eq("base.pages.0.page_hash", "doc_meta_changed.pages.0.page_hash"),
        ],
    },
    {
        "name": "metadata_reserved_and_null",
        "description": "hash 核心按保留键全集（随位投递 + 不投递，契约 1 §2.1）过滤顶层键，并删除值为 null 的键（缺省 ≡ null，#3 S5）：三篇文档的全部 hash 相同。"
        "注意这是 hash 核心的输入视图：线上报文的 metadata 出现任一保留键都会被拒绝；计算 state_hash 前，随位键须先从 metadata 移入 occurrence（契约 1 §6），故本向量不对 state_hash 作断言。",
        "documents": {
            "plain": doc(page(1, None, el("NarrativeText", "x", metadata={"lang": "zh"}))),
            "with_reserved": doc(
                page(
                    1,
                    None,
                    el(
                        "NarrativeText",
                        "x",
                        metadata={
                            "lang": "zh",
                            "coordinates": _BOX_A,
                            "image_url": "https://a.cdn/x.png",
                            "parent_id": "e-1",
                            "related_ids": ["e-2"],
                            "page_number": 1,
                            "page_name": "Sheet1",
                            "seq_in_page": 3,
                            "keywords": ["k"],
                            "image_path": "/tmp/x.png",
                            "image_base64": "aGk=",
                            "file_directory": "/tmp",
                        },
                    ),
                    page_metadata={"page_number": 1, "keywords": ["k"]},
                ),
                doc_metadata={"keywords": ["k"]},
            ),
            "with_nulls": doc(page(1, None, el("NarrativeText", "x", metadata={"lang": "zh", "new_optional": None}))),
        },
        "relations": [
            eq("plain.doc_hash", "with_reserved.doc_hash", "with_nulls.doc_hash"),
            # 不断言 state_hash：state_hash 的输入是线上 occurrence 视图（契约 1 §6），
            # 随位键须先从 metadata 移入 occurrence，留在 metadata 里不代表它们不属于投递状态。
        ],
    },
    {
        "name": "metadata_null_nested",
        "description": "null 键删除是递归的：嵌套对象中的 null 键同样删除；数组中的 null 保留。",
        "documents": {
            "with_nulls": doc(
                page(1, None, el("NarrativeText", "x", metadata={"a": {"b": None, "c": 1}, "arr": [None, 1]}))
            ),
            "stripped": doc(page(1, None, el("NarrativeText", "x", metadata={"a": {"c": 1}, "arr": [None, 1]}))),
            "array_null_dropped": doc(page(1, None, el("NarrativeText", "x", metadata={"a": {"c": 1}, "arr": [1]}))),
        },
        "relations": [
            eq("with_nulls.doc_hash", "stripped.doc_hash"),
            ne("stripped.doc_hash", "array_null_dropped.doc_hash"),
        ],
    },
    {
        "name": "image_blob",
        "description": "Image 身份只认 blob：blob_ref = \"blob:\" + sha256 引用。",
        "documents": {
            "doc": doc(page(1, None, el("Image", "架构图", image_blob=_BLOB, image_mime_type="image/png")))
        },
    },
    {
        "name": "image_url_not_identity",
        "description": "url 是访问方式不是身份（#3 S4）：image_url 在骨架 entry 的 occurrence 上，同一 blob 配不同 url 时 doc_hash 相同、state_hash 不同；blob 变化才是内容变化。",
        "documents": {
            "url_a": doc(page(1, None, el("Image", "", image_blob=_BLOB, image_mime_type="image/png", occurrence={"image_url": "https://a.cdn/x.png"}))),
            "url_b": doc(page(1, None, el("Image", "", image_blob=_BLOB, image_mime_type="image/png", occurrence={"image_url": "https://b.cdn/y.png"}))),
            "blob_changed": doc(page(1, None, el("Image", "", image_blob=_BLOB2, image_mime_type="image/png", occurrence={"image_url": "https://a.cdn/x.png"}))),
        },
        "relations": [
            eq("url_a.pages.0.elements.0", "url_b.pages.0.elements.0"),
            eq("url_a.doc_hash", "url_b.doc_hash"),
            ne("url_a.state_hash", "url_b.state_hash"),
            ne("url_a.doc_hash", "blob_changed.doc_hash"),
        ],
    },
    {
        "name": "image_placeholder_null",
        "description": "占位图片：blob / mime 为 null 与缺省同字节（段级 null 按空串处理）。",
        "documents": {
            "absent": doc(page(1, None, el("Image", "占位"))),
            "null_fields": doc(page(1, None, el("Image", "占位", image_blob=None, image_mime_type=None))),
        },
        "relations": [eq("absent.doc_hash", "null_fields.doc_hash")],
    },
    {
        "name": "duplicate_elements",
        "description": "同页两个相同元素：content_hash 相同（内容对象共享），page_hash 中按位置重复出现。",
        "documents": {
            "doc": doc(page(1, None, el("ListItem", "重复项"), el("NarrativeText", "中间"), el("ListItem", "重复项")))
        },
        "relations": [eq("doc.pages.0.elements.0", "doc.pages.0.elements.2")],
    },
    {
        "name": "duplicate_occurrence",
        "description": "同一内容出现在两个位置、各带不同坐标（A1）：两处共用一个内容对象（content_hash 相同），坐标分别记在各自 entry 的 occurrence 上；对调两处坐标不改变 doc_hash，但改变 state_hash。",
        "documents": {
            "ab": doc(page(1, None, el("ListItem", "重复项", occurrence={"coordinates": _BOX_A}), el("ListItem", "重复项", occurrence={"coordinates": _BOX_B}))),
            "ba": doc(page(1, None, el("ListItem", "重复项", occurrence={"coordinates": _BOX_B}), el("ListItem", "重复项", occurrence={"coordinates": _BOX_A}))),
        },
        "relations": [
            eq("ab.pages.0.elements.0", "ab.pages.0.elements.1", "ba.pages.0.elements.0"),
            eq("ab.doc_hash", "ba.doc_hash"),
            ne("ab.state_hash", "ba.state_hash"),
        ],
    },
    {
        "name": "state_surface",
        "description": "state_hash 覆盖全部不进 hash 的投递字段（A2，契约 1 §6）：只改 occurrence 或 attributes 时 doc_hash 不变、state_hash 变化；"
        "null 与缺省等价，attributes 缺省视同 {}。",
        "documents": {
            "base": doc(
                page(1, None, el("NarrativeText", "x", occurrence={"coordinates": _BOX_A, "parent_id": "e-0"})),
                attributes={"example/owner": "u-1"},
            ),
            "coords_changed": doc(
                page(1, None, el("NarrativeText", "x", occurrence={"coordinates": _BOX_B, "parent_id": "e-0"})),
                attributes={"example/owner": "u-1"},
            ),
            "parent_changed": doc(
                page(1, None, el("NarrativeText", "x", occurrence={"coordinates": _BOX_A, "parent_id": "e-9"})),
                attributes={"example/owner": "u-1"},
            ),
            "attributes_changed": doc(
                page(1, None, el("NarrativeText", "x", occurrence={"coordinates": _BOX_A, "parent_id": "e-0"})),
                attributes={"example/owner": "u-2"},
            ),
            "with_nulls": doc(
                page(1, None, el("NarrativeText", "x", occurrence={"coordinates": _BOX_A, "parent_id": "e-0", "related_ids": None})),
                attributes={"example/owner": "u-1", "example/group": None},
            ),
            "bare": doc(page(1, None, el("NarrativeText", "x"))),
            "bare_empty": doc(page(1, None, el("NarrativeText", "x", occurrence={})), attributes={}),
        },
        "relations": [
            eq(
                "base.doc_hash",
                "coords_changed.doc_hash",
                "parent_changed.doc_hash",
                "attributes_changed.doc_hash",
                "with_nulls.doc_hash",
                "bare.doc_hash",
            ),
            ne("base.state_hash", "coords_changed.state_hash", "parent_changed.state_hash", "attributes_changed.state_hash", "bare.state_hash"),
            eq("base.state_hash", "with_nulls.state_hash"),
            eq("bare.state_hash", "bare_empty.state_hash"),
        ],
    },
    {
        "name": "file_type_identity",
        "description": "file_type 由源提供，进 doc_hash（#3，契约 1 §5）：只改 file_type 时元素与页 hash 不变，doc_hash 变化。",
        "documents": {
            "md": doc(page(1, None, el("NarrativeText", "x")), file_type="md"),
            "txt": doc(page(1, None, el("NarrativeText", "x")), file_type="txt"),
        },
        "relations": [
            eq("md.pages.0.page_hash", "txt.pages.0.page_hash"),
            ne("md.doc_hash", "txt.doc_hash"),
        ],
    },
    {
        "name": "cross_page_move",
        "description": "元素跨页移动：content_hash 不变，两页 page_hash 与 doc_hash 变化（delta 应为 added=0/removed=0，retained 全保留）。",
        "documents": {
            "before": doc(page(1, "p1", el("NarrativeText", "留守"), el("NarrativeText", "迁徙")), page(2, "p2")),
            "after": doc(page(1, "p1", el("NarrativeText", "留守")), page(2, "p2", el("NarrativeText", "迁徙"))),
        },
        "relations": [
            eq("before.pages.0.elements.1", "after.pages.1.elements.0"),
            ne("before.pages.0.page_hash", "after.pages.0.page_hash"),
            ne("before.doc_hash", "after.doc_hash"),
        ],
    },
    {
        "name": "element_reorder",
        "description": "顺序属于内容身份（#3 S7）：同页元素对调，content_hash 多重集不变（delta 为 0），但 page_hash / doc_hash 变化。",
        "documents": {
            "before": doc(page(1, None, el("NarrativeText", "他们离婚了"), el("NarrativeText", "A 与 C 再婚了"))),
            "after": doc(page(1, None, el("NarrativeText", "A 与 C 再婚了"), el("NarrativeText", "他们离婚了"))),
        },
        "relations": [
            eq("before.pages.0.elements.0", "after.pages.0.elements.1"),
            eq("before.pages.0.elements.1", "after.pages.0.elements.0"),
            ne("before.doc_hash", "after.doc_hash"),
        ],
    },
    {
        "name": "unicode_text",
        "description": "Unicode 不做规范化（hash 的是投递的字节）：NFC 与 NFD、emoji（代理对）、组合字符都按 UTF-8 原字节参与。",
        "documents": {
            "nfc": doc(page(1, None, el("NarrativeText", "caf\u00e9 🚀 中文"))),
            "nfd": doc(page(1, None, el("NarrativeText", "cafe\u0301 🚀 中文"))),  # 显式转义，防止编辑器归一化
        },
        "relations": [ne("nfc.doc_hash", "nfd.doc_hash")],
    },
    {
        "name": "null_vs_empty_page_title",
        "description": "段级 null 与空串产出相同字节：页 title 为 null 与 \"\" 的两篇文档 hash 完全相同。",
        "documents": {
            "null": doc(page(1, None, el("NarrativeText", "x"))),
            "empty": doc(page(1, "", el("NarrativeText", "x"))),
        },
        "relations": [eq("null.doc_hash", "empty.doc_hash")],
    },
    {
        "name": "empty_document",
        "description": "空文档（0 页）与空页（0 元素）均合法；契约 1 没有 doc title（#3 S6）。",
        "documents": {
            "no_pages": doc(file_type="empty"),
            "empty_page": doc(page(1, None), file_type="empty"),
            "no_pages_with_meta": doc(file_type="empty", doc_metadata={"author": "gmq"}),
        },
        "relations": [ne("no_pages.doc_hash", "empty_page.doc_hash", "no_pages_with_meta.doc_hash")],
    },
    {
        "name": "page_number_identity",
        "description": "页号进入 page_hash（契约 1 §5）：内容不动、只改页号，doc_hash 必须变化（否则 head 快路径漏推）；content_hash 不变，不构成刷新衍生物的理由（core §3.3）。",
        "documents": {
            "before": doc(page(1, "p", el("NarrativeText", "x")), page(2, "q", el("NarrativeText", "y"))),
            "after": doc(page(10, "p", el("NarrativeText", "x")), page(20, "q", el("NarrativeText", "y"))),
        },
        "relations": [
            eq("before.pages.0.elements.0", "after.pages.0.elements.0"),
            ne("before.doc_hash", "after.doc_hash"),
        ],
    },
    {
        "name": "page_array_order_irrelevant",
        "description": "页的阅读顺序是 number 升序（core §2.2）：hash 核心对输入数组顺序宽容，pages 数组 [3,1] 与 [1,3] 的 doc_hash 与 state_hash 均相同"
        "（线上报文仍 MUST 升序）。",
        "documents": {
            "sorted": doc(
                page(1, None, el("NarrativeText", "a", occurrence={"coordinates": _BOX_A})),
                page(3, None, el("NarrativeText", "b", occurrence={"coordinates": _BOX_B})),
            ),
            "unsorted": doc(
                page(3, None, el("NarrativeText", "b", occurrence={"coordinates": _BOX_B})),
                page(1, None, el("NarrativeText", "a", occurrence={"coordinates": _BOX_A})),
            ),
        },
        "relations": [eq("sorted.doc_hash", "unsorted.doc_hash"), eq("sorted.state_hash", "unsorted.state_hash")],
    },
    {
        "name": "negative_page_number",
        "description": "页号的十进制 ASCII 编码：负号保留、无前导零。",
        "documents": {"doc": doc(page(-3, None, el("NarrativeText", "x")), page(0, None))},
    },
    {
        "name": "upgrade_drill",
        "description": "契约升级演练（#3 S8）：同一份内容给出 dpe1 与假想 dpe2 的期望值；两契约的逐页、逐元素结果按位置一一对应，即升级时的原位对应关系。",
        "contracts": ["dpe1", "dpe2"],
        "documents": {
            "doc": doc(
                page(1, "p1", el("Title", "标题"), el("Table", "a", text_as_html="<table/>"), page_metadata={"src": "b1"}),
                page(2, None, el("Image", "图", image_blob=_BLOB, image_mime_type="image/webp", occurrence={"image_url": "https://a.cdn/x.webp"})),
                doc_metadata={"author": "gmq"},
                attributes={"example/owner": "u-1"},
            )
        },
    },
]

JCS_VECTORS: list[dict[str, Any]] = [
    {
        "name": "jcs_basic",
        "description": "JCS 字面量、字符串转义、键排序（UTF-16 码元序）。",
        "cases": [
            {"input": None},
            {"input": [True, False, None, "", []]},
            {"input": {"b": 1, "a": 2, "A": 3, "é": 4, "10": 5, "1": 6}},
            {"input": {"text": "line1\nline2\ttab \"quote\" \\ \u0007bell \u001f"}},
            # U+FF61 是单码元 0xFF61；U+10000 是代理对 0xD800 0xDC00。
            # UTF-16 码元序中代理对排在前，按码点排序的实现会在此出错。
            {"input": {"｡": "halfwidth", "\U00010000": "surrogate"}},
            {"input": {"nested": {"z": [1, {"y": None}], "a": "中文 🚀"}}},
        ],
    },
    {
        "name": "jcs_numbers",
        "description": "ECMAScript Number::toString 边界：整数展开阈值 1e21、指数下界 1e-7、无前导零指数、-0 归一为 0。",
        "cases": [
            {"input": 0},
            {"input": -0.0},
            {"input": 1},
            {"input": -1.5},
            {"input": 0.1},
            {"input": 1e-6},
            {"input": 1e-7},
            {"input": 1e20},
            {"input": 1e21},
            {"input": 1.5e22},
            {"input": 9007199254740991},
            {"input": 3.141592653589793},
            {"input": 333333333.33333331},
            {"input": [1e30, -1e-30]},
        ],
    },
]


# ---------------------------------------------------------------------------
# 生成与校验
# ---------------------------------------------------------------------------


def resolve_ref(ref: str, expected: dict[str, Any], contract: str) -> str:
    """把 ``<文档键>.<路径>`` 解析为该契约下的期望值（路径分量为键名或数组下标）。"""
    key, *path = ref.split(".")
    node: Any = expected[key][contract]
    for part in path:
        node = node[int(part)] if isinstance(node, list) else node[part]
    if not isinstance(node, str):
        raise ValueError(f"relation ref does not resolve to a hash value: {ref}")
    return node


def check_relations(
    name: str, relations: list[dict[str, Any]], expected: dict[str, Any], contracts: list[str]
) -> None:
    """断言向量声明的等值 / 不等关系在每个契约下都成立：它们是本向量要证明的规范性质。"""
    for rel in relations:
        (op, refs), = rel.items()
        if op not in ("equal", "distinct"):
            raise ValueError(f"{name}: unknown relation op: {op}")
        for c in contracts:
            values = [resolve_ref(r, expected, c) for r in refs]
            ok = len(set(values)) == 1 if op == "equal" else len(set(values)) == len(values)
            if not ok:
                raise AssertionError(f"{name}: relation {op} {refs} violated under {c}")


def build_files() -> dict[str, str]:
    """返回 {相对路径: 文件内容}，内容确定性。"""
    files: dict[str, str] = {}

    for vec in DOCUMENT_VECTORS:
        contracts = vec.get("contracts", ["dpe1"])
        expected = {
            key: {c: doc_hashes(d, c) for c in contracts} for key, d in vec["documents"].items()
        }
        relations = vec.get("relations", [])
        check_relations(vec["name"], relations, expected, contracts)
        out = {
            "name": vec["name"],
            "kind": "document",
            "description": vec["description"],
            "documents": vec["documents"],
            "expected": expected,
        }
        if relations:
            out["relations"] = relations
        files[f"{vec['name']}.json"] = dump_json(out)

    for vec in JCS_VECTORS:
        cases = []
        for case in vec["cases"]:
            canonical = jcs(case["input"])
            cases.append(
                {
                    "input": case["input"],
                    "canonical": canonical,
                    "sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
                }
            )
        files[f"{vec['name']}.json"] = dump_json(
            {"name": vec["name"], "kind": "jcs", "description": vec["description"], "cases": cases}
        )

    manifest = {
        "contract": "dpe1",
        "spec": "spec/hash-contract-1.md",
        "status": "draft",
        "reserved_metadata_keys": sorted(RESERVED_METADATA_KEYS),
        "occurrence_keys": sorted(OCCURRENCE_KEYS),
        "undelivered_keys": sorted(UNDELIVERED_KEYS),
        "file_types": list(FILE_TYPES),
        "provenance": {
            "generator": "scripts/gen_vectors.py",
            "note": "向量由规范参考实现生成（plan §1：向量归本仓库）；dpe2 仅用于升级演练，定义见 vectors/README.md",
        },
        "files": [
            {"name": name, "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest()}
            for name, content in sorted(files.items())
        ],
    }
    files["manifest.json"] = dump_json(manifest)
    return files


def dump_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def main() -> int:
    check = "--check" in sys.argv[1:]
    files = build_files()
    if check:
        stale = []
        for name, content in files.items():
            path = VECTORS_DIR / name
            if not path.exists() or path.read_text(encoding="utf-8") != content:
                stale.append(name)
        extra = {p.name for p in VECTORS_DIR.glob("*.json")} - set(files)
        if stale or extra:
            for name in stale:
                print(f"stale: vectors/{name}", file=sys.stderr)
            for name in sorted(extra):
                print(f"unexpected: vectors/{name}", file=sys.stderr)
            print("run: python3 scripts/gen_vectors.py", file=sys.stderr)
            return 1
        print(f"vectors ok ({len(files)} files)")
        return 0
    VECTORS_DIR.mkdir(parents=True, exist_ok=True)
    for name in {p.name for p in VECTORS_DIR.glob("*.json")} - set(files):
        (VECTORS_DIR / name).unlink()
    for name, content in files.items():
        (VECTORS_DIR / name).write_text(content, encoding="utf-8")
    print(f"wrote {len(files)} files to {VECTORS_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
