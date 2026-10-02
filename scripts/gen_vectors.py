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
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

VECTORS_DIR = Path(__file__).resolve().parent.parent / "vectors"

# ---------------------------------------------------------------------------
# hash 契约 1（spec/hash-contract-1.md §3–§5）：三层同构的 tree，唯一原语 SHA-256(JCS(对象))
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

#: category → 允许的内容字段（契约 1 §4.1，按表中顺序），写入 manifest 供各实现互校。
CATEGORY_CONTENT_FIELDS: dict[str, tuple[str, ...]] = dict(
    sorted(
        {
            **{c: ("text",) for c in TEXT_ONLY_CATEGORIES},
            **{c: ("text", "text_as_html") for c in HTML_CATEGORIES},
            "Image": ("text", "image_blob", "image_mime_type"),
        }.items()
    )
)

#: file_type 封闭枚举（core.md §2.5），由源提供，进 doc_hash（契约 1 §5）。
FILE_TYPES = (
    "bmp", "csv", "doc", "docx", "eml", "epub", "heic", "html", "jpg", "json", "md", "msg", "ndjson",
    "odt", "org", "pdf", "png", "ppt", "pptx", "rst", "rtf", "tiff", "tsv", "txt", "wav", "xls", "xlsx",
    "xml", "zip", "java_repo", "python_repo", "javascript_repo", "typescript_repo", "unk", "empty",
    "tfchat", "jira_project", "jira_issue",
)  # fmt: skip

#: dpe2 是**仅用于契约升级演练的假想契约**（vectors/README.md）：
#: 算法与 dpe1 相同，但每次摘要额外前置一个内容为 b"dpe2" 的段。
TEST_CONTRACTS = ("dpe1", "dpe2")


def strip_nulls(value: Any) -> Any:
    """递归删除对象中值为 null 的键（缺省 ≡ null，§3.2）；数组元素不受影响。"""
    if isinstance(value, dict):
        return {k: strip_nulls(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [strip_nulls(v) for v in value]
    return value


def obj_hash(obj: dict[str, Any], contract: str) -> tuple[str, str]:
    """唯一原语（§3.1）：hash = 前缀 + SHA-256(JCS(对象) 的 UTF-8 字节)。返回 (hash, JCS 原像)。

    dpe2 仅用于升级演练：在原像字节前加 ASCII "dpe2"（vectors/README.md）。
    """
    preimage = jcs(obj)
    data = preimage.encode("utf-8")
    if contract == "dpe2":
        data = b"dpe2" + data
    return f"{contract}:{hashlib.sha256(data).hexdigest()}", preimage


def metadata(m: dict[str, Any] | None) -> dict[str, Any]:
    """metadata 字段的规范化（§3.2）：缺省 / null 视同 {}，递归删 null 键；非对象值即违规。"""
    if m is None:
        return {}
    if not isinstance(m, dict):
        raise TypeError(f"metadata must be an object, got {type(m).__name__}")
    return strip_nulls(m)


def content_fields(cat: str) -> frozenset[str]:
    """元素对象允许的字段 = content_hash 的完整原像（core.md §2.3）；未知 category 拒绝。"""
    if cat not in CATEGORY_CONTENT_FIELDS:
        raise ValueError(f"unknown category: {cat}")
    return frozenset({"category", "metadata", *CATEGORY_CONTENT_FIELDS[cat]})


def validate_element(el: dict[str, Any]) -> None:
    """向量元素就是一个元素对象（content_hash 的完整原像）；多余字段即违规。"""
    cat = el["category"]
    extra = set(el) - content_fields(cat)
    if extra:
        raise ValueError(f"{cat}: fields outside content object: {sorted(extra)}")


def element_object(el: dict[str, Any]) -> dict[str, Any]:
    validate_element(el)
    for k, v in el.items():
        if k != "metadata" and v is not None and not isinstance(v, str):
            raise TypeError(f"content field {k} must be a string or null")
    obj = strip_nulls({k: v for k, v in el.items() if k != "metadata"})
    obj["metadata"] = metadata(el.get("metadata"))
    return obj


PAGE_FIELDS = frozenset({"title", "page_metadata", "elements"})
DOCUMENT_FIELDS = frozenset({"file_type", "title", "doc_metadata", "pages"})


def page_object(page: dict[str, Any], element_hashes: list[str]) -> dict[str, Any]:
    extra = set(page) - PAGE_FIELDS
    if extra:
        raise ValueError(f"page: undefined fields: {sorted(extra)}")
    title = page.get("title")
    if title is not None and not isinstance(title, str):
        raise TypeError("page title must be a string or null")
    obj: dict[str, Any] = {
        "page_metadata": metadata(page.get("page_metadata")),
        "elements": element_hashes,
    }
    if title is not None:
        obj["title"] = title
    return obj


def document_object(doc: dict[str, Any], page_hashes: list[str]) -> dict[str, Any]:
    extra = set(doc) - DOCUMENT_FIELDS
    if extra:
        raise ValueError(f"document: undefined fields: {sorted(extra)}")
    if doc["file_type"] not in FILE_TYPES:
        raise ValueError(f"unknown file_type: {doc['file_type']}")
    title = doc.get("title")
    if title is not None and not isinstance(title, str):
        raise TypeError("doc title must be a string or null")
    obj: dict[str, Any] = {
        "file_type": doc["file_type"],
        "doc_metadata": metadata(doc.get("doc_metadata")),
        "pages": page_hashes,
    }
    if title is not None:
        obj["title"] = title
    return obj


def doc_hashes(
    doc: dict[str, Any], contract: str, with_preimages: bool = False
) -> dict[str, Any]:
    """返回一篇文档在某契约下的全部期望值：三层同构的 tree（§4–§5）。"""
    pages_out = []
    page_hashes: list[str] = []
    pre_pages: list[str] = []
    pre_elements: list[list[str]] = []
    for page in doc["pages"]:
        hashed = [obj_hash(element_object(el), contract) for el in page["elements"]]
        ehs = [h for h, _ in hashed]
        ph, ppre = obj_hash(page_object(page, ehs), contract)
        page_hashes.append(ph)
        pages_out.append({"page_hash": ph, "elements": ehs})
        pre_pages.append(ppre)
        pre_elements.append([p for _, p in hashed])
    dh, dpre = obj_hash(document_object(doc, page_hashes), contract)
    out: dict[str, Any] = {"doc_hash": dh, "pages": pages_out}
    if with_preimages:
        out["preimages"] = {"document": dpre, "pages": pre_pages, "elements": pre_elements}
    return out


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


_JCS_ESCAPES = {
    '"': '\\"',
    "\\": "\\\\",
    "\b": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\f": "\\f",
    "\r": "\\r",
}


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
    title: str | None = None,
    doc_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {"file_type": file_type, "pages": list(pages)}
    if title is not None:
        out["title"] = title
    if doc_metadata is not None:
        out["doc_metadata"] = doc_metadata
    return out


def page(title: str | None, *elements: dict[str, Any], **kw: Any) -> dict[str, Any]:
    """页没有 number：阅读顺序就是它在 pages 数组中的位置（契约 1 §5）。"""
    return {"title": title, "elements": list(elements), **kw}


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
        "description": "最小文档：单页单 NarrativeText，三层 metadata 均缺省（规范化为 {}）。",
        "documents": {"doc": doc(page("p1", el("NarrativeText", "Hello, DPE.")))},
    },
    {
        "name": "preimage_basic",
        "description": "给出三层对象规范化后的 JCS 原像（expected.*.preimages），供实现者逐字节排查；hash 即原像 UTF-8 字节的 SHA-256。",
        "preimages": True,
        "documents": {
            "doc": doc(
                page(
                    "p1",
                    el("Title", "标题", metadata={"lang": "zh"}),
                    el("Table", "a", text_as_html="<table/>"),
                ),
                page(
                    None,
                    el("Image", None, image_blob=_BLOB, image_mime_type="image/png"),
                ),
                title="季度报告",
                doc_metadata={"author": "gmq"},
            )
        },
    },
    {
        "name": "all_text_categories",
        "description": "全部 text-only category 各一个元素，同页排列；category 进入 hash，故同文本不同 category 的值互不相同。",
        "documents": {
            "doc": doc(
                page(None, *[el(c, "same text") for c in sorted(TEXT_ONLY_CATEGORIES)])
            )
        },
        "relations": [
            ne(*[f"doc.pages.0.elements.{i}" for i in range(len(TEXT_ONLY_CATEGORIES))])
        ],
    },
    {
        "name": "table_formula_html",
        "description": "Table / Formula 的身份包含 text_as_html：text 相同而 html 不同时 hash 不同。",
        "documents": {
            "doc": doc(
                page(
                    "tables",
                    el(
                        "Table",
                        "a b",
                        text_as_html="<table><tr><td>a</td><td>b</td></tr></table>",
                    ),
                    el(
                        "Table",
                        "a b",
                        text_as_html="<table><tr><td>a</td></tr><tr><td>b</td></tr></table>",
                    ),
                    el(
                        "Formula",
                        "E=mc^2",
                        text_as_html="<math>E=mc<sup>2</sup></math>",
                    ),
                    el("TableChunk", "a b"),
                )
            )
        },
        "relations": [
            ne(
                "doc.pages.0.elements.0",
                "doc.pages.0.elements.1",
                "doc.pages.0.elements.3",
            )
        ],
    },
    {
        "name": "metadata_identity",
        "description": "源即内容（plan §0.1 P2）：doc / page / element 任一层 metadata 变化，对应层及以上的 hash 变化，下层不受影响。",
        "documents": {
            "base": doc(
                page(
                    "p",
                    el("NarrativeText", "x", metadata={"lang": "zh"}),
                    page_metadata={"src": "b1"},
                ),
                doc_metadata={"author": "gmq", "created_at": "2026-09-01T02:03:04Z"},
            ),
            "ele_meta_changed": doc(
                page(
                    "p",
                    el("NarrativeText", "x", metadata={"lang": "en"}),
                    page_metadata={"src": "b1"},
                ),
                doc_metadata={"author": "gmq", "created_at": "2026-09-01T02:03:04Z"},
            ),
            "page_meta_changed": doc(
                page(
                    "p",
                    el("NarrativeText", "x", metadata={"lang": "zh"}),
                    page_metadata={"src": "b2"},
                ),
                doc_metadata={"author": "gmq", "created_at": "2026-09-01T02:03:04Z"},
            ),
            "doc_meta_changed": doc(
                page(
                    "p",
                    el("NarrativeText", "x", metadata={"lang": "zh"}),
                    page_metadata={"src": "b1"},
                ),
                doc_metadata={"author": "gmq", "created_at": "2026-09-02T00:00:00Z"},
            ),
        },
        "relations": [
            ne(
                "base.doc_hash",
                "ele_meta_changed.doc_hash",
                "page_meta_changed.doc_hash",
                "doc_meta_changed.doc_hash",
            ),
            ne("base.pages.0.elements.0", "ele_meta_changed.pages.0.elements.0"),
            eq("base.pages.0.elements.0", "page_meta_changed.pages.0.elements.0"),
            eq("base.pages.0.page_hash", "doc_meta_changed.pages.0.page_hash"),
        ],
    },
    {
        "name": "metadata_null_equivalence",
        "description": "值为 null 的键与缺省等价（缺省 ≡ null，#3 S5）：两篇文档的全部 hash 相同。",
        "documents": {
            "plain": doc(page(None, el("NarrativeText", "x", metadata={"lang": "zh"}))),
            "with_nulls": doc(
                page(
                    None,
                    el(
                        "NarrativeText",
                        "x",
                        metadata={"lang": "zh", "new_optional": None},
                    ),
                )
            ),
        },
        "relations": [eq("plain.doc_hash", "with_nulls.doc_hash")],
    },
    {
        "name": "metadata_empty_equivalence",
        "description": "三层 metadata 缺省、为 null、为 {} 三者等价（规范化时补为 {}，契约 1 §3.2）；页 title 缺省与为 null 等价。",
        "documents": {
            "absent": doc(page("p", el("NarrativeText", "x"))),
            "null": doc(
                page("p", el("NarrativeText", "x", metadata=None), page_metadata=None),
                doc_metadata=None,
            ),
            "empty": doc(
                page("p", el("NarrativeText", "x", metadata={}), page_metadata={}),
                doc_metadata={},
            ),
            "title_null": doc(page(None, el("NarrativeText", "x"))),
            "title_absent": doc({"elements": [el("NarrativeText", "x")]}),
        },
        "relations": [
            eq("absent.doc_hash", "null.doc_hash", "empty.doc_hash"),
            eq("title_null.doc_hash", "title_absent.doc_hash"),
        ],
    },
    {
        "name": "metadata_all_content",
        "description": "源即内容（plan §0.1 P2）：metadata 的每个键都进 hash，没有保留键、没有过滤；坐标、页码标签等任一键变化，content_hash 都变化。",
        "documents": {
            "base": doc(
                page(
                    None,
                    el(
                        "NarrativeText",
                        "x",
                        metadata={"lang": "zh", "coordinates": _BOX_A},
                    ),
                )
            ),
            "coords_changed": doc(
                page(
                    None,
                    el(
                        "NarrativeText",
                        "x",
                        metadata={"lang": "zh", "coordinates": _BOX_B},
                    ),
                )
            ),
            "label_added": doc(
                page(
                    None,
                    el(
                        "NarrativeText",
                        "x",
                        metadata={
                            "lang": "zh",
                            "coordinates": _BOX_A,
                            "page_label": "iv",
                        },
                    ),
                )
            ),
        },
        "relations": [
            ne(
                "base.pages.0.elements.0",
                "coords_changed.pages.0.elements.0",
                "label_added.pages.0.elements.0",
            ),
            ne("base.doc_hash", "coords_changed.doc_hash", "label_added.doc_hash"),
        ],
    },
    {
        "name": "metadata_null_nested",
        "description": "null 键删除是递归的：嵌套对象中的 null 键同样删除；数组中的 null 保留。",
        "documents": {
            "with_nulls": doc(
                page(
                    None,
                    el(
                        "NarrativeText",
                        "x",
                        metadata={"a": {"b": None, "c": 1}, "arr": [None, 1]},
                    ),
                )
            ),
            "stripped": doc(
                page(
                    None,
                    el(
                        "NarrativeText", "x", metadata={"a": {"c": 1}, "arr": [None, 1]}
                    ),
                )
            ),
            "array_null_dropped": doc(
                page(
                    None, el("NarrativeText", "x", metadata={"a": {"c": 1}, "arr": [1]})
                )
            ),
        },
        "relations": [
            eq("with_nulls.doc_hash", "stripped.doc_hash"),
            ne("stripped.doc_hash", "array_null_dropped.doc_hash"),
        ],
    },
    {
        "name": "image_blob",
        "description": "Image 的字节以 image_blob（sha256 引用）进 hash；url 等源信息作为元素 metadata 另进 hash（见 image_url_is_content）。",
        "documents": {
            "doc": doc(
                page(
                    None,
                    el(
                        "Image", "架构图", image_blob=_BLOB, image_mime_type="image/png"
                    ),
                )
            )
        },
    },
    {
        "name": "image_url_is_content",
        "description": "图片 url 是源提供的元素 metadata，进 hash（plan §0.1 P2）：同一 blob 配不同 url 时 content_hash 不同；blob 变化同样是内容变化。",
        "documents": {
            "url_a": doc(
                page(
                    None,
                    el(
                        "Image",
                        "",
                        image_blob=_BLOB,
                        image_mime_type="image/png",
                        metadata={"image_url": "https://a.cdn/x.png"},
                    ),
                )
            ),
            "url_b": doc(
                page(
                    None,
                    el(
                        "Image",
                        "",
                        image_blob=_BLOB,
                        image_mime_type="image/png",
                        metadata={"image_url": "https://b.cdn/y.png"},
                    ),
                )
            ),
            "blob_changed": doc(
                page(
                    None,
                    el(
                        "Image",
                        "",
                        image_blob=_BLOB2,
                        image_mime_type="image/png",
                        metadata={"image_url": "https://a.cdn/x.png"},
                    ),
                )
            ),
        },
        "relations": [
            ne(
                "url_a.pages.0.elements.0",
                "url_b.pages.0.elements.0",
                "blob_changed.pages.0.elements.0",
            )
        ],
    },
    {
        "name": "image_placeholder_null",
        "description": "占位图片：blob / mime 为 null 与缺省等价（null 键删除）。",
        "documents": {
            "absent": doc(page(None, el("Image", "占位"))),
            "null_fields": doc(
                page(None, el("Image", "占位", image_blob=None, image_mime_type=None))
            ),
        },
        "relations": [eq("absent.doc_hash", "null_fields.doc_hash")],
    },
    {
        "name": "null_vs_empty_distinct",
        "description": 'null（等价于缺省）与空串 "" 是不同的值（hash 的是投递的内容）：元素 text、页 title 为 null 与为 "" 时 hash 都不同。',
        "documents": {
            "null": doc(page(None, el("NarrativeText", None))),
            "empty_text": doc(page(None, el("NarrativeText", ""))),
            "empty_title": doc(page("", el("NarrativeText", None))),
        },
        "relations": [
            ne("null.pages.0.elements.0", "empty_text.pages.0.elements.0"),
            ne("null.pages.0.page_hash", "empty_title.pages.0.page_hash"),
            ne("null.doc_hash", "empty_text.doc_hash", "empty_title.doc_hash"),
        ],
    },
    {
        "name": "doc_title",
        "description": '文档对象可选 title 与页 title 对称：缺省与 null 等价，"" 是独立的值；title 变化只改变 doc_hash，页与元素 hash 不变；title 与 doc_metadata 中的 filename 是不同的源属性。',
        "documents": {
            "absent": doc(
                page("p1", el("NarrativeText", "正文")),
                doc_metadata={"filename": "report_v3_final.docx"},
            ),
            "null": {
                **doc(
                    page("p1", el("NarrativeText", "正文")),
                    doc_metadata={"filename": "report_v3_final.docx"},
                ),
                "title": None,
            },
            "empty": doc(
                page("p1", el("NarrativeText", "正文")),
                title="",
                doc_metadata={"filename": "report_v3_final.docx"},
            ),
            "titled": doc(
                page("p1", el("NarrativeText", "正文")),
                title="季度报告",
                doc_metadata={"filename": "report_v3_final.docx"},
            ),
            "renamed": doc(
                page("p1", el("NarrativeText", "正文")),
                title="年度报告",
                doc_metadata={"filename": "report_v3_final.docx"},
            ),
            "filename_only": doc(
                page("p1", el("NarrativeText", "正文")),
                doc_metadata={"filename": "季度报告"},
            ),
            "title_only": doc(
                page("p1", el("NarrativeText", "正文")), title="季度报告"
            ),
        },
        "relations": [
            eq("absent.doc_hash", "null.doc_hash"),
            ne(
                "absent.doc_hash",
                "empty.doc_hash",
                "titled.doc_hash",
                "renamed.doc_hash",
            ),
            eq(
                "absent.pages.0.page_hash",
                "titled.pages.0.page_hash",
                "renamed.pages.0.page_hash",
            ),
            ne("filename_only.doc_hash", "title_only.doc_hash"),
        ],
    },
    {
        "name": "duplicate_elements",
        "description": "同页两个相同元素：content_hash 相同（元素对象共享），page 的 elements 列表中按位置重复出现。",
        "documents": {
            "doc": doc(
                page(
                    None,
                    el("ListItem", "重复项"),
                    el("NarrativeText", "中间"),
                    el("ListItem", "重复项"),
                )
            )
        },
        "relations": [eq("doc.pages.0.elements.0", "doc.pages.0.elements.2")],
    },
    {
        "name": "duplicate_pages",
        "description": "两页内容完全相同：page_hash 相同（页对象共享，如同 Git 中相同的 tree），文档对象的 pages 列表中按位置重复出现。",
        "documents": {
            "doc": doc(
                page("同页", el("NarrativeText", "x")),
                page("同页", el("NarrativeText", "x")),
            )
        },
        "relations": [eq("doc.pages.0.page_hash", "doc.pages.1.page_hash")],
    },
    {
        "name": "duplicate_different_coordinates",
        "description": "同一文本出现在两处、坐标不同：坐标是内容，两处是不同的元素对象（content_hash 不同）；坐标相同时才共享同一元素对象。",
        "documents": {
            "doc": doc(
                page(
                    None,
                    el("ListItem", "重复项", metadata={"coordinates": _BOX_A}),
                    el("ListItem", "重复项", metadata={"coordinates": _BOX_B}),
                    el("ListItem", "重复项", metadata={"coordinates": _BOX_A}),
                )
            ),
        },
        "relations": [
            ne("doc.pages.0.elements.0", "doc.pages.0.elements.1"),
            eq("doc.pages.0.elements.0", "doc.pages.0.elements.2"),
        ],
    },
    {
        "name": "file_type_identity",
        "description": "file_type 由源提供，属于文档对象，进 doc_hash：只改 file_type 时页与元素 hash 不变，doc_hash 变化。",
        "documents": {
            "md": doc(page(None, el("NarrativeText", "x")), file_type="md"),
            "txt": doc(page(None, el("NarrativeText", "x")), file_type="txt"),
        },
        "relations": [
            eq("md.pages.0.page_hash", "txt.pages.0.page_hash"),
            ne("md.doc_hash", "txt.doc_hash"),
        ],
    },
    {
        "name": "page_insert_reuse",
        "description": "在中间插入一页（plan §0.1 P3）：其余各页的 page_hash 全部不变、无需重传（页不含位置，如同 Git 的 tree 条目移动不改变子树），只有 doc_hash 变化。",
        "documents": {
            "before": doc(
                page("A", el("NarrativeText", "a")),
                page("B", el("NarrativeText", "b")),
                page("C", el("NarrativeText", "c")),
            ),
            "after": doc(
                page("A", el("NarrativeText", "a")),
                page("新页", el("NarrativeText", "new")),
                page("B", el("NarrativeText", "b")),
                page("C", el("NarrativeText", "c")),
            ),
        },
        "relations": [
            eq("before.pages.0.page_hash", "after.pages.0.page_hash"),
            eq("before.pages.1.page_hash", "after.pages.2.page_hash"),
            eq("before.pages.2.page_hash", "after.pages.3.page_hash"),
            ne("before.doc_hash", "after.doc_hash"),
        ],
    },
    {
        "name": "page_reorder",
        "description": "页序就是 pages 数组顺序，属于内容：对调两页，各页 page_hash 不变，doc_hash 变化。",
        "documents": {
            "before": doc(
                page("A", el("NarrativeText", "a")), page("B", el("NarrativeText", "b"))
            ),
            "after": doc(
                page("B", el("NarrativeText", "b")), page("A", el("NarrativeText", "a"))
            ),
        },
        "relations": [
            eq("before.pages.0.page_hash", "after.pages.1.page_hash"),
            eq("before.pages.1.page_hash", "after.pages.0.page_hash"),
            ne("before.doc_hash", "after.doc_hash"),
        ],
    },
    {
        "name": "cross_page_move",
        "description": "元素跨页移动：content_hash 不变，两页 page_hash 与 doc_hash 变化（delta 应为 added=0/removed=0，retained 全保留）。",
        "documents": {
            "before": doc(
                page("p1", el("NarrativeText", "留守"), el("NarrativeText", "迁徙")),
                page("p2"),
            ),
            "after": doc(
                page("p1", el("NarrativeText", "留守")),
                page("p2", el("NarrativeText", "迁徙")),
            ),
        },
        "relations": [
            eq("before.pages.0.elements.1", "after.pages.1.elements.0"),
            ne("before.pages.0.page_hash", "after.pages.0.page_hash"),
            ne("before.doc_hash", "after.doc_hash"),
        ],
    },
    {
        "name": "element_reorder",
        "description": "顺序属于内容（#3 S7）：同页元素对调，content_hash 多重集不变（delta 为 0），但 page_hash / doc_hash 变化。",
        "documents": {
            "before": doc(
                page(
                    None,
                    el("NarrativeText", "他们离婚了"),
                    el("NarrativeText", "A 与 C 再婚了"),
                )
            ),
            "after": doc(
                page(
                    None,
                    el("NarrativeText", "A 与 C 再婚了"),
                    el("NarrativeText", "他们离婚了"),
                )
            ),
        },
        "relations": [
            eq("before.pages.0.elements.0", "after.pages.0.elements.1"),
            eq("before.pages.0.elements.1", "after.pages.0.elements.0"),
            ne("before.doc_hash", "after.doc_hash"),
        ],
    },
    {
        "name": "unicode_text",
        "description": "Unicode 不做规范化（hash 的是投递的内容）：NFC 与 NFD、emoji（代理对）、组合字符都按原样参与。",
        "documents": {
            "nfc": doc(page(None, el("NarrativeText", "café 🚀 中文"))),
            "nfd": doc(
                page(None, el("NarrativeText", "café 🚀 中文"))
            ),  # 显式转义，防止编辑器归一化
        },
        "relations": [ne("nfc.doc_hash", "nfd.doc_hash")],
    },
    {
        "name": "empty_document",
        "description": "空文档（0 页）与空页（0 元素）均合法。",
        "documents": {
            "no_pages": doc(file_type="empty"),
            "empty_page": doc(page(None), file_type="empty"),
            "no_pages_with_meta": doc(
                file_type="empty", doc_metadata={"author": "gmq"}
            ),
        },
        "relations": [
            ne(
                "no_pages.doc_hash",
                "empty_page.doc_hash",
                "no_pages_with_meta.doc_hash",
            )
        ],
    },
    {
        "name": "upgrade_drill",
        "description": "契约升级演练（#3 S8）：同一份内容给出 dpe1 与假想 dpe2 的期望值；两契约的逐页、逐元素结果按位置一一对应，即升级时的原位对应关系。",
        "contracts": ["dpe1", "dpe2"],
        "documents": {
            "doc": doc(
                page(
                    "p1",
                    el("Title", "标题"),
                    el("Table", "a", text_as_html="<table/>"),
                    page_metadata={"src": "b1"},
                ),
                page(
                    None,
                    el(
                        "Image",
                        "图",
                        image_blob=_BLOB,
                        image_mime_type="image/webp",
                        metadata={"image_url": "https://a.cdn/x.webp"},
                    ),
                ),
                doc_metadata={"author": "gmq"},
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
            {"input": {"text": 'line1\nline2\ttab "quote" \\ \u0007bell \u001f'}},
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
    name: str,
    relations: list[dict[str, Any]],
    expected: dict[str, Any],
    contracts: list[str],
) -> None:
    """断言向量声明的等值 / 不等关系在每个契约下都成立：它们是本向量要证明的规范性质。"""
    for rel in relations:
        ((op, refs),) = rel.items()
        if op not in ("equal", "distinct"):
            raise ValueError(f"{name}: unknown relation op: {op}")
        for c in contracts:
            values = [resolve_ref(r, expected, c) for r in refs]
            ok = (
                len(set(values)) == 1
                if op == "equal"
                else len(set(values)) == len(values)
            )
            if not ok:
                raise AssertionError(f"{name}: relation {op} {refs} violated under {c}")


def build_files() -> dict[str, str]:
    """返回 {相对路径: 文件内容}，内容确定性。"""
    files: dict[str, str] = {}

    for vec in DOCUMENT_VECTORS:
        contracts = vec.get("contracts", ["dpe1"])
        expected = {
            key: {c: doc_hashes(d, c, vec.get("preimages", False)) for c in contracts}
            for key, d in vec["documents"].items()
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
            {
                "name": vec["name"],
                "kind": "jcs",
                "description": vec["description"],
                "cases": cases,
            }
        )

    manifest = {
        "contract": "dpe1",
        "spec": "spec/hash-contract-1.md",
        "status": "draft",
        "file_types": list(FILE_TYPES),
        "category_content_fields": {c: list(f) for c, f in CATEGORY_CONTENT_FIELDS.items()},
        "provenance": {
            "generator": "scripts/gen_vectors.py",
            "note": "向量由规范参考实现生成（plan §1：向量归本仓库）；dpe2 仅用于升级演练，定义见 vectors/README.md",
        },
        "files": [
            {
                "name": name,
                "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            }
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
