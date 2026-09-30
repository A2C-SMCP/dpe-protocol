#!/usr/bin/env python3
"""一致性向量生成器 —— hash 契约 1（spec/hash-contract-1.md）的规范参考实现。

治理模型（docs/plan/v1-plan.md §1）：向量是规范的一部分，由本仓库产出；
SDK 与内核只消费向量。本脚本因此：

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
# hash 契约 1 原语（spec/hash-contract-1.md §3）
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


def content_hash(el: dict[str, Any], contract: str) -> str:
    cat = el["category"]
    parts = [text(cat)]
    if cat == "Image":
        blob, url = el.get("image_blob"), el.get("image_url")
        if blob and url:
            raise ValueError("Image element must not carry both blob and url")
        ref = f"blob:{blob}" if blob else url
        parts += [text(el.get("text")), text(ref), text(el.get("image_mime_type"))]
    elif cat in HTML_CATEGORIES:
        parts += [text(el.get("text")), text(el.get("text_as_html"))]
    elif cat in TEXT_ONLY_CATEGORIES:
        parts += [text(el.get("text"))]
    else:
        raise ValueError(f"unknown category: {cat}")
    return hval(parts, contract)


def page_hash(page: dict[str, Any], element_hashes: list[str], contract: str) -> str:
    parts = [str(page["number"]).encode("ascii"), text(page.get("title"))]
    parts += [h.encode("utf-8") for h in element_hashes]
    return hval(parts, contract)


def doc_hashes(doc: dict[str, Any], contract: str) -> dict[str, Any]:
    """返回一篇文档在某契约下的全部期望值。"""
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
    parts = [text(doc.get("title"))]
    parts += [ph_by_number[n].encode("utf-8") for n in sorted(ph_by_number)]
    return {"doc_hash": hval(parts, contract), "pages": pages_out}


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


def doc(title: str | None, *pages: dict[str, Any]) -> dict[str, Any]:
    return {"title": title, "pages": list(pages)}


def page(number: int, title: str | None, *elements: dict[str, Any]) -> dict[str, Any]:
    return {"number": number, "title": title, "elements": list(elements)}


_BLOB = "sha256:" + hashlib.sha256(b"dpe-vector-blob").hexdigest()

DOCUMENT_VECTORS: list[dict[str, Any]] = [
    {
        "name": "element_text_basic",
        "description": "最小文档：单页单 NarrativeText。",
        "documents": {"doc": doc("hello", page(1, "p1", el("NarrativeText", "Hello, DPE.")))},
    },
    {
        "name": "all_text_categories",
        "description": "全部 text-only category 各一个元素，同页排列；category 进入 hash，故同文本不同 category 的值互不相同。",
        "documents": {
            "doc": doc(
                "categories",
                page(1, None, *[el(c, "same text") for c in sorted(TEXT_ONLY_CATEGORIES)]),
            )
        },
    },
    {
        "name": "table_formula_html",
        "description": "Table / Formula 的身份包含 text_as_html：text 相同而 html 不同时 hash 不同。",
        "documents": {
            "doc": doc(
                None,
                page(
                    1,
                    "tables",
                    el("Table", "a b", text_as_html="<table><tr><td>a</td><td>b</td></tr></table>"),
                    el("Table", "a b", text_as_html="<table><tr><td>a</td></tr><tr><td>b</td></tr></table>"),
                    el("Formula", "E=mc^2", text_as_html="<math>E=mc<sup>2</sup></math>"),
                    el("TableChunk", "a b"),
                ),
            )
        },
    },
    {
        "name": "image_blob",
        "description": "Image 引用 blob：image_ref = \"blob:\" + sha256 引用。",
        "documents": {
            "doc": doc(None, page(1, None, el("Image", "架构图", image_blob=_BLOB, image_mime_type="image/png")))
        },
    },
    {
        "name": "image_url_identity",
        "description": "Image 引用外部 URL：URL 原文即身份，不做规范化，差一个字符就是不同内容。",
        "documents": {
            "a": doc(None, page(1, None, el("Image", "", image_url="https://example.com/a.png", image_mime_type="image/png"))),
            "b": doc(None, page(1, None, el("Image", "", image_url="https://example.com/A.png", image_mime_type="image/png"))),
        },
    },
    {
        "name": "image_placeholder_null",
        "description": "占位图片：blob / url / mime 全为 null，各段按空串编码（null 与空串同字节）。",
        "documents": {
            "null": doc(None, page(1, None, el("Image", "占位"))),
            "empty": doc(None, page(1, None, el("Image", "占位", image_url="", image_mime_type=""))),
        },
    },
    {
        "name": "duplicate_elements",
        "description": "同页两个相同元素：content_hash 相同（内容对象共享），page_hash 中按位置重复出现。",
        "documents": {
            "doc": doc(None, page(1, None, el("ListItem", "重复项"), el("NarrativeText", "中间"), el("ListItem", "重复项")))
        },
    },
    {
        "name": "cross_page_move",
        "description": "元素跨页移动：content_hash 不变，两页 page_hash 与 doc_hash 变化（delta 应为 added=0/removed=0/retained 全保留）。",
        "documents": {
            "before": doc(
                "move", page(1, "p1", el("NarrativeText", "留守"), el("NarrativeText", "迁徙")), page(2, "p2")
            ),
            "after": doc(
                "move", page(1, "p1", el("NarrativeText", "留守")), page(2, "p2", el("NarrativeText", "迁徙"))
            ),
        },
    },
    {
        "name": "unicode_text",
        "description": "Unicode 不做规范化：NFC 与 NFD、emoji（代理对）、组合字符都按 UTF-8 原字节参与。",
        "documents": {
            "nfc": doc(None, page(1, None, el("NarrativeText", "café 🚀 中文"))),
            "nfd": doc(None, page(1, None, el("NarrativeText", "café 🚀 中文"))),
        },
    },
    {
        "name": "null_vs_empty_title",
        "description": "null 与空串产出相同字节：两文档 doc_hash / page_hash 完全相同。",
        "documents": {
            "null": doc(None, page(1, None, el("NarrativeText", "x"))),
            "empty": doc("", page(1, "", el("NarrativeText", "x"))),
        },
    },
    {
        "name": "empty_document",
        "description": "空文档（0 页）与空页（0 元素）均合法。",
        "documents": {
            "no_pages": doc("empty"),
            "empty_page": doc("empty", page(1, None)),
        },
    },
    {
        "name": "page_number_identity",
        "description": "页号进入 page_hash：内容不动、只改页号，doc_hash 必须变化（否则 head 快路径漏推）。",
        "documents": {
            "before": doc(None, page(1, "p", el("NarrativeText", "x")), page(2, "q", el("NarrativeText", "y"))),
            "after": doc(None, page(10, "p", el("NarrativeText", "x")), page(20, "q", el("NarrativeText", "y"))),
        },
    },
    {
        "name": "page_array_order_irrelevant",
        "description": "doc_hash 按页号升序：pages 数组 [3,1] 与 [1,3] 的 doc_hash 相同（页内元素顺序才是内容身份）。",
        "documents": {
            "sorted": doc(None, page(1, None, el("NarrativeText", "a")), page(3, None, el("NarrativeText", "b"))),
            "unsorted": doc(None, page(3, None, el("NarrativeText", "b")), page(1, None, el("NarrativeText", "a"))),
        },
    },
    {
        "name": "negative_page_number",
        "description": "页号的十进制 ASCII 编码：负号保留、无前导零。",
        "documents": {"doc": doc(None, page(-3, None, el("NarrativeText", "x")), page(0, None))},
    },
    {
        "name": "upgrade_drill",
        "description": "契约升级演练：同一份内容同时给出 dpe1 与假想 dpe2（定义见 vectors/README.md）的期望值。",
        "contracts": ["dpe1", "dpe2"],
        "documents": {
            "doc": doc(
                "upgrade",
                page(1, "p1", el("Title", "标题"), el("Table", "a", text_as_html="<table/>")),
                page(2, None, el("Image", "图", image_blob=_BLOB, image_mime_type="image/webp")),
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


def build_files() -> dict[str, str]:
    """返回 {相对路径: 文件内容}，内容确定性。"""
    files: dict[str, str] = {}

    for vec in DOCUMENT_VECTORS:
        contracts = vec.get("contracts", ["dpe1"])
        expected = {
            key: {c: doc_hashes(d, c) for c in contracts} for key, d in vec["documents"].items()
        }
        files[f"{vec['name']}.json"] = dump_json(
            {
                "name": vec["name"],
                "kind": "document",
                "description": vec["description"],
                "documents": vec["documents"],
                "expected": expected,
            }
        )

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
    for name, content in files.items():
        (VECTORS_DIR / name).write_text(content, encoding="utf-8")
    print(f"wrote {len(files)} files to {VECTORS_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
