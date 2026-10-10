#!/usr/bin/env python3
"""一致性向量生成器 —— hash 契约 1（spec/hash-contract-1.md）与 file_uri 语法规范化（spec/core.md §1.1）的规范参考实现。

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
from urllib.parse import unquote_to_bytes
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pattern_subset import (  # noqa: E402
    PatternOutsideSubset,
    check_pattern,
    search,
    translate_pattern,
)

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
            "Image": ("text", "blob", "mime_type"),
        }.items()
    )
)

#: file_type 推荐登记表（core.md §2.5，按规范表中顺序），由源提供，进 doc_hash（契约 1 §5）。
#: 取值只是推荐：语法合法的未登记取值同样被接受（见 FILE_TYPE_RE），接收方 MUST NOT 拒收。
RECOMMENDED_FILE_TYPES = (
    "bmp", "csv", "doc", "docx", "eml", "epub", "heic", "html", "jpg", "json", "md", "msg", "ndjson",
    "odt", "org", "pdf", "png", "ppt", "pptx", "rst", "rtf", "tiff", "tsv", "txt", "wav", "xls", "xlsx",
    "xml", "zip", "git_repo", "java_repo", "python_repo", "javascript_repo", "typescript_repo", "unk",
    "empty", "jira_project", "jira_issue",
)  # fmt: skip

#: file_type 的语法（core.md §2.5）：ASCII 小写字母、数字与下划线，首字符是字母或数字，长度 ≤ 32。
FILE_TYPE_RE = re.compile(r"[a-z0-9][a-z0-9_]{0,31}")

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
    """metadata 字段的规范化（§3.2）：缺省 / null 视同 {}，递归删 null 键（值已由校验器检查）。"""
    return {} if m is None else strip_nulls(m)


# ---------------------------------------------------------------------------
# 参考校验器（core.md §2.8）：按规范顺序校验线上原像，遇第一处违例即 Reject(code, path)。
# hash 计算与拒绝类向量共用这一套规则。
# ---------------------------------------------------------------------------

VALIDATION = "DPE_VALIDATION"
CATEGORY_UNKNOWN = "DPE_CATEGORY_UNKNOWN"
CONTRACT_UNSUPPORTED = "DPE_CONTRACT_UNSUPPORTED"

#: 真实契约；dpe2 只在被选为本次契约时才算受支持（契约 1 §5）
SUPPORTED_CONTRACTS = ("dpe1",)
_HEX64 = re.compile(r"[0-9a-f]{64}")


class Reject(Exception):  # noqa: N818
    def __init__(self, code: str, path: str) -> None:
        super().__init__(f"{code} at {path!r}")
        self.code = code
        self.path = path


def ptr(path: str, seg: str | int) -> str:
    """在 RFC 6901 JSON Pointer 后追加一段。"""
    if isinstance(seg, int):
        return f"{path}/{seg}"
    return path + "/" + seg.replace("~", "~0").replace("/", "~1")


def utf16_key(key: str) -> bytes:
    return key.encode("utf-16-be")


_SURROGATE = re.compile("[\ud800-\udfff]")


def has_invalid_unicode(value: Any) -> bool:
    """I-JSON（core §2.8 第 0 步）：字符串或对象键中含孤立代理项。"""
    if isinstance(value, str):
        return bool(_SURROGATE.search(value))
    if isinstance(value, list):
        return any(has_invalid_unicode(v) for v in value)
    if isinstance(value, dict):
        return any(
            (isinstance(k, str) and _SURROGATE.search(k)) or has_invalid_unicode(v)
            for k, v in value.items()
        )
    return False


def check_ijson(value: Any) -> None:
    """第 0 步，对整个被校验对象做检查，违例位置为对象自身。重复键由 strict_loads 在解析时拒绝。"""
    if has_invalid_unicode(value):
        raise Reject(VALIDATION, "")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        raise Reject(VALIDATION, "")
    return dict(pairs)


def strict_loads(text: str) -> Any:
    """I-JSON 解析：重复键即 Reject（core §2.8 第 0 步）。"""
    return json.loads(text, object_pairs_hook=_reject_duplicate_keys)


def check_json(value: Any, path: str) -> None:
    """JSON 值（§2.6、契约 1 §3.3）：深度优先，对象键按 UTF-16 码元序、数组按下标。"""
    if value is None or isinstance(value, (bool, str)):
        return
    if isinstance(value, int):
        if abs(value) > 2**53 - 1:
            raise Reject(VALIDATION, path)
        return
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise Reject(VALIDATION, path)
        return
    if isinstance(value, list):
        for i, v in enumerate(value):
            check_json(v, ptr(path, i))
        return
    if isinstance(value, dict):
        if not all(isinstance(k, str) for k in value):
            raise Reject(VALIDATION, path)
        for k in sorted(value, key=utf16_key):
            check_json(value[k], ptr(path, k))
        return
    raise Reject(VALIDATION, path)


def check_string(obj: dict[str, Any], field: str, path: str) -> None:
    value = obj.get(field)
    if value is not None and not isinstance(value, str):
        raise Reject(VALIDATION, ptr(path, field))


def check_metadata(obj: dict[str, Any], field: str, path: str) -> None:
    value = obj.get(field)
    if value is None:
        return
    if not isinstance(value, dict):
        raise Reject(VALIDATION, ptr(path, field))
    check_json(value, ptr(path, field))


def check_closed(obj: dict[str, Any], allowed: tuple[str, ...], path: str) -> None:
    extra = sorted((k for k in obj if k not in allowed), key=utf16_key)
    if extra:
        raise Reject(VALIDATION, ptr(path, extra[0]))


def check_hash_list(value: Any, contract: str, path: str) -> None:
    """子对象 hash 列表（契约 1 §1、§5），逐项按数组顺序。"""
    if not isinstance(value, list):
        raise Reject(VALIDATION, path)
    supported = {*SUPPORTED_CONTRACTS, contract}
    for i, h in enumerate(value):
        at = ptr(path, i)
        if not isinstance(h, str):
            raise Reject(VALIDATION, at)
        prefix, sep, hexpart = h.partition(":")
        if not sep or prefix not in supported:
            raise Reject(CONTRACT_UNSUPPORTED, at)
        if not _HEX64.fullmatch(hexpart):
            raise Reject(VALIDATION, at)
        if prefix != contract:
            raise Reject(VALIDATION, at)  # 契约混用


def check_element(el: Any, path: str = "") -> None:
    """元素对象（core §2.3）：形状 → category → 封闭 schema → 逐字段。"""
    if (
        not isinstance(el, dict) or el.get("category") is None
    ):  # 必有字段为 null 视同缺省
        raise Reject(VALIDATION, path)
    cat = el["category"]
    if not isinstance(cat, str):
        raise Reject(VALIDATION, ptr(path, "category"))
    if cat not in CATEGORY_CONTENT_FIELDS:
        raise Reject(CATEGORY_UNKNOWN, ptr(path, "category"))
    fields = CATEGORY_CONTENT_FIELDS[cat]
    check_closed(el, ("category", *fields, "metadata"), path)
    for field in fields:
        check_string(el, field, path)
        blob = el.get(field) if field == "blob" else None
        if blob is not None and not (
            blob.startswith("sha256:") and _HEX64.fullmatch(blob[7:])
        ):
            raise Reject(VALIDATION, ptr(path, field))
    check_metadata(el, "metadata", path)


def check_page(page: Any, contract: str, path: str = "") -> None:
    """页对象（core §2.2）线上原像：形状 → 封闭 schema → title、page_metadata、elements。"""
    if not isinstance(page, dict) or page.get("elements") is None:
        raise Reject(VALIDATION, path)
    check_closed(page, ("title", "page_metadata", "elements"), path)
    check_string(page, "title", path)
    check_metadata(page, "page_metadata", path)
    check_hash_list(page["elements"], contract, ptr(path, "elements"))


def check_document(doc: Any, contract: str, path: str = "") -> None:
    """文档对象（core §2.1）线上原像：形状 → 封闭 schema → file_type、title、doc_metadata、pages。"""
    check_document_fields(doc, path)
    check_hash_list(doc["pages"], contract, ptr(path, "pages"))


def check_document_fields(doc: Any, path: str) -> None:
    """文档对象除 pages 内容外的部分：形状 → 封闭 schema → file_type、title、doc_metadata。"""
    if (
        not isinstance(doc, dict)
        or doc.get("file_type") is None
        or doc.get("pages") is None
    ):
        raise Reject(VALIDATION, path)
    check_closed(doc, ("file_type", "title", "doc_metadata", "pages"), path)
    if not isinstance(doc["file_type"], str) or not FILE_TYPE_RE.fullmatch(doc["file_type"]):
        raise Reject(VALIDATION, ptr(path, "file_type"))
    check_string(doc, "title", path)
    check_metadata(doc, "doc_metadata", path)


def check_expanded(doc: Any, contract: str) -> None:
    """展开视图（vectors/README.md）：与线上请求同序——文档自身字段 → 各页自身字段（数组序）→
    各页的元素（逐页、数组序）。"""
    check_document_fields(doc, "")
    if not isinstance(doc["pages"], list):
        raise Reject(VALIDATION, "/pages")
    for i, page in enumerate(doc["pages"]):
        at = ptr("/pages", i)
        if not isinstance(page, dict) or page.get("elements") is None:
            raise Reject(VALIDATION, at)
        check_closed(page, ("title", "page_metadata", "elements"), at)
        check_string(page, "title", at)
        check_metadata(page, "page_metadata", at)
        if not isinstance(page["elements"], list):
            raise Reject(VALIDATION, ptr(at, "elements"))
    for i, page in enumerate(doc["pages"]):
        for j, el in enumerate(page["elements"]):
            check_element(el, ptr(ptr(ptr("/pages", i), "elements"), j))


def _ijson_first(check: Any) -> Any:
    def checker(obj: Any, contract: str) -> None:
        check_ijson(obj)
        check(obj, contract)

    return checker


CHECKERS = {
    "element": _ijson_first(lambda obj, contract: check_element(obj)),
    "page": _ijson_first(check_page),
    "document": _ijson_first(check_document),
    "expanded_document": _ijson_first(check_expanded),
}


# ---------------------------------------------------------------------------
# file_uri 的语法规范化（core.md §1.1）：合法性判定（封闭清单）+ 三步变换
# ---------------------------------------------------------------------------

#: unreserved（RFC 3986 §2.3）与 reserved（§2.2）字符集；其余字符（含非 ASCII）一律非法。
_UNRESERVED = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
)
_RESERVED = frozenset(":/?#[]@!$&'()*+,;=")
_URI_CHARS = _UNRESERVED | _RESERVED
_HEXDIG = frozenset("0123456789abcdefABCDEF")
#: scheme（RFC 3986 §3.1）：ALPHA *( ALPHA / DIGIT / "+" / "-" / "." ) 后接 ":"。
_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*:")


def normalize_file_uri(uri: str) -> str:
    """file_uri 的语法规范化（core.md §1.1），与 ``dpe_hash.normalize_file_uri`` 行为一致。

    合法性判定（封闭清单）：scheme 文法、全串字符集（pct-encoded / unreserved / reserved）、
    ``%`` 后两位 HEXDIG；不校验更深的成分文法。三步变换：解码表示 unreserved 的百分号三元组
    → scheme 与 host 中三元组之外的 ASCII 字母小写 → 其余三元组的 hex 大写。幂等。
    非法输入抛 ``Reject(DPE_VALIDATION, "")``。
    """
    match = _SCHEME.match(uri)
    if match is None:
        raise Reject(VALIDATION, "")
    scheme_end = match.end()

    # host 成分的范围：':' 后紧跟 "//" 才有 authority；authority 到第一个 "/"、"?"、"#" 为止，
    # host 是最后一个 "@"（userinfo 之后）到 port 之前的成分。三元组解码不改变这些边界
    # （unreserved 不含定界符），所以索引可以直接取自原串。
    host_start = host_end = scheme_end
    if uri.startswith("//", scheme_end):
        start = scheme_end + 2
        authority_end = len(uri)
        for ch in "/?#":
            at = uri.find(ch, start)
            if at != -1:
                authority_end = min(authority_end, at)
        at = uri.rfind("@", start, authority_end)
        host_start = at + 1 if at != -1 else start
        if host_start < authority_end and uri[host_start] == "[":
            close = uri.find("]", host_start, authority_end)
            host_end = authority_end if close == -1 else close + 1
        else:
            colon = uri.find(":", host_start, authority_end)
            host_end = authority_end if colon == -1 else colon

    out: list[str] = []
    i = 0
    n = len(uri)
    while i < n:
        ch = uri[i]
        if ch == "%":
            if i + 2 >= n or uri[i + 1] not in _HEXDIG or uri[i + 2] not in _HEXDIG:
                raise Reject(VALIDATION, "")
            decoded = chr(int(uri[i + 1 : i + 3], 16))
            if decoded in _UNRESERVED:
                if host_start <= i < host_end and "A" <= decoded <= "Z":
                    decoded = chr(ord(decoded) + 32)
                out.append(decoded)
            else:
                out.append("%" + uri[i + 1 : i + 3].upper())
            i += 3
            continue
        if ch not in _URI_CHARS:
            raise Reject(VALIDATION, "")
        if (i < scheme_end or host_start <= i < host_end) and "A" <= ch <= "Z":
            ch = chr(ord(ch) + 32)
        out.append(ch)
        i += 1
    return "".join(out)


# ---------------------------------------------------------------------------
# 三层对象（线上原像）的规范化
# ---------------------------------------------------------------------------


def element_object(el: dict[str, Any]) -> dict[str, Any]:
    check_element(el)
    obj = strip_nulls({k: v for k, v in el.items() if k != "metadata"})
    obj["metadata"] = metadata(el.get("metadata"))
    return obj


def page_object(
    page: dict[str, Any], element_hashes: list[str], contract: str
) -> dict[str, Any]:
    """展开视图中的页 + 已算出的子 hash → 页对象（先按线上原像校验）。"""
    wire = {
        **{k: v for k, v in page.items() if k != "elements"},
        "elements": element_hashes,
    }
    check_page(wire, contract)
    obj: dict[str, Any] = {
        "page_metadata": metadata(page.get("page_metadata")),
        "elements": element_hashes,
    }
    if page.get("title") is not None:
        obj["title"] = page["title"]
    return obj


def document_object(
    doc: dict[str, Any], page_hashes: list[str], contract: str
) -> dict[str, Any]:
    wire = {**{k: v for k, v in doc.items() if k != "pages"}, "pages": page_hashes}
    check_document(wire, contract)
    obj: dict[str, Any] = {
        "file_type": doc["file_type"],
        "doc_metadata": metadata(doc.get("doc_metadata")),
        "pages": page_hashes,
    }
    if doc.get("title") is not None:
        obj["title"] = doc["title"]
    return obj


def doc_hashes(
    doc: dict[str, Any], contract: str, with_preimages: bool = False
) -> dict[str, Any]:
    """返回一篇文档在某契约下的全部期望值：三层同构的 tree（§4–§5）。"""
    CHECKERS["expanded_document"](doc, contract)
    pages_out = []
    page_hashes: list[str] = []
    pre_pages: list[str] = []
    pre_elements: list[list[str]] = []
    for page in doc["pages"]:
        hashed = [obj_hash(element_object(el), contract) for el in page["elements"]]
        ehs = [h for h, _ in hashed]
        ph, ppre = obj_hash(page_object(page, ehs, contract), contract)
        page_hashes.append(ph)
        pages_out.append({"page_hash": ph, "elements": ehs})
        pre_pages.append(ppre)
        pre_elements.append([p for _, p in hashed])
    dh, dpre = obj_hash(document_object(doc, page_hashes, contract), contract)
    out: dict[str, Any] = {"doc_hash": dh, "pages": pages_out}
    if with_preimages:
        out["preimages"] = {
            "document": dpre,
            "pages": pre_pages,
            "elements": pre_elements,
        }
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
                    el("Image", None, blob=_BLOB, mime_type="image/png"),
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
        "name": "blob_ref",
        "description": "二进制字节以通用的 blob 字段（sha256 引用）与 mime_type 进 hash；哪些 category 可携带 blob 由契约 1 §4.1 表决定（目前为 Image）。url 等源信息作为元素 metadata 另进 hash（见 image_url_is_content）。",
        "documents": {
            "doc": doc(
                page(
                    None,
                    el("Image", "架构图", blob=_BLOB, mime_type="image/png"),
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
                        blob=_BLOB,
                        mime_type="image/png",
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
                        blob=_BLOB,
                        mime_type="image/png",
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
                        blob=_BLOB2,
                        mime_type="image/png",
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
                page(None, el("Image", "占位", blob=None, mime_type=None))
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
        "name": "file_type_open_values",
        "description": "file_type 是开放取值（core.md §2.5）：推荐登记表之外的语法合法取值照常被接受并进 doc_hash——含旧枚举下会被拒的 `markdown`（与 `md` 是两个取值，无别名等价），以及数字开头、长度上界 32 的取值；33 字符与 `md\\n`（尾随换行，全串匹配）见拒绝类用例。",
        "documents": {
            "registered": doc(page(None, el("NarrativeText", "x")), file_type="md"),
            "unregistered": doc(page(None, el("NarrativeText", "x")), file_type="custom_format_v2"),
            "old_alias": doc(page(None, el("NarrativeText", "x")), file_type="markdown"),
            "digit_leading": doc(page(None, el("NarrativeText", "x")), file_type="3mf"),
            "max_length": doc(
                page(None, el("NarrativeText", "x")),
                file_type="max_length_value_0123456789abcde",  # 恰 32 字符，语法合法
            ),
        },
        "relations": [
            eq(
                "registered.pages.0.page_hash",
                "unregistered.pages.0.page_hash",
                "old_alias.pages.0.page_hash",
                "digit_leading.pages.0.page_hash",
                "max_length.pages.0.page_hash",
            ),
            ne(
                "registered.doc_hash",
                "unregistered.doc_hash",
                "old_alias.doc_hash",
                "digit_leading.doc_hash",
                "max_length.doc_hash",
            ),
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
                        blob=_BLOB,
                        mime_type="image/webp",
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
        "description": "ECMAScript Number::toString 边界：整数展开阈值 1e21、指数下界 1e-7、无前导零指数、-0 归一为 0、最短候选等距时取偶。",
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
            # 最短候选等距时取偶（契约 1 §3.3）：真值恰为 …54.25 / …38.125 / …98.625，
            # 末位须为 2 而非 3（部分标准库的最短格式化在此取奇）
            {"input": [1059438285926254.25, 154616442297138.125, 87762274880598.625]},
        ],
    },
]


#: file_uri 语法规范化向量（core.md §1.1）。cases 的 ``expect`` 是手写期望，生成器逐例断言
#: （同时断言幂等）；invalid_cases 逐例断言被拒且错误码为 DPE_VALIDATION。
URI_VECTORS: list[dict[str, Any]] = [
    {
        "name": "file_uri_normalization",
        "description": "file_uri 的语法规范化（core.md §1.1）：cases 为合法输入与其规范化输出（幂等不动点），invalid_cases 为必须被拒绝（DPE_VALIDATION）的输入。",
        "cases": [
            # 大小写：scheme 与 host 小写，其余成分原样
            {"input": "HTTP://EXAMPLE.com/Path", "expect": "http://example.com/Path"},
            {"input": "feishu://DOC/Path", "expect": "feishu://doc/Path"},
            {"input": "s3://Bucket.Name:9000/K", "expect": "s3://bucket.name:9000/K"},
            {
                "input": "https://User@Example.com/A",
                "expect": "https://User@example.com/A",
            },
            {"input": "https://[2001:DB8::A]/x", "expect": "https://[2001:db8::a]/x"},
            {"input": "https://[FE80::1]:8080/x", "expect": "https://[fe80::1]:8080/x"},
            {"input": "weird+scheme.1-2://A/b", "expect": "weird+scheme.1-2://a/b"},
            {"input": "s3://b/K?Q=ABC#Frag", "expect": "s3://b/K?Q=ABC#Frag"},
            # 百分号编码：解码 unreserved；reserved 不解码、hex 大写；host 中保留的三元组不参与小写
            {"input": "feishu://doc/%7Euser", "expect": "feishu://doc/~user"},
            {"input": "feishu://doc/%2f", "expect": "feishu://doc/%2F"},
            {"input": "s3://b/k?a=%2b", "expect": "s3://b/k?a=%2B"},
            {"input": "s3://b/%7a", "expect": "s3://b/z"},
            {"input": "s3://b/%41", "expect": "s3://b/A"},
            {"input": "s3://b/100%25a", "expect": "s3://b/100%25a"},
            {"input": "http://ex%c3%a4mple.com/", "expect": "http://ex%C3%A4mple.com/"},
            {"input": "http://%45XAMPLE.com/", "expect": "http://example.com/"},
            {"input": "s3://us%7eer@Bucket/k", "expect": "s3://us~er@bucket/k"},
            # userinfo 里解码出的大写字母不参与 host 小写（%41 在 userinfo 中，保持大写）
            {"input": "s3://%41@B/k", "expect": "s3://A@b/k"},
            {"input": "s3://bucket/k?x=%7e", "expect": "s3://bucket/k?x=~"},
            # 不做 §6.2.3 / §6.2.4：保留默认端口、前导零端口、尾斜杠，不补空 path 的 "/"
            {
                "input": "https://example.com:443/a",
                "expect": "https://example.com:443/a",
            },
            {"input": "http://example.com:080/x", "expect": "http://example.com:080/x"},
            {"input": "s3://bucket/key/", "expect": "s3://bucket/key/"},
            {"input": "https://example.com", "expect": "https://example.com"},
            # 不做 §6.2.2.3：点段是字面内容（%2E 解码为 "." 后同样保留）
            {"input": "s3://b/a/../c", "expect": "s3://b/a/../c"},
            {"input": "feishu://doc/%2e%2E", "expect": "feishu://doc/.."},
            # 无 authority：只做 scheme 小写与百分号编码，其余是不透明内容
            {"input": "FEISHU:xxx", "expect": "feishu:xxx"},
            {"input": "FEISHU:Doc/A%2fB", "expect": "feishu:Doc/A%2FB"},
            {"input": "urn:ISBN:0-395-36341-1", "expect": "urn:ISBN:0-395-36341-1"},
            {"input": "urn:example:%7e", "expect": "urn:example:~"},
            # fragment 与空 host
            {"input": "feishu://doc/a#gid=0", "expect": "feishu://doc/a#gid=0"},
            {"input": "file:///a/b", "expect": "file:///a/b"},
            {"input": "file://User@/a", "expect": "file://User@/a"},
            # 混合长例：三步的顺序与作用范围（先解码再小写；host 中三元组不被小写）
            {
                "input": "HTTP://EX%43AMPLE.com:8080/A%2Fb?q=%7e#%2F",
                "expect": "http://excample.com:8080/A%2Fb?q=~#%2F",
            },
        ],
        "invalid_cases": [
            # 不是 URI（scheme 缺失或文法不符）
            "",
            "a/b",
            "/abs/path",
            "1http://x",
            "http//example.com",
            "%41:b",
            # 不在 URI 字符集（非 ASCII、空格、控制字符、其他）
            "feishu://doc/季度报告",
            "https://例子.example/",
            "feishu://doc/a b",
            "http://ex ample.com/",
            "feishu://doc/a\\b",
            "feishu://doc/\u0000",
            'feishu://doc/a"b',
            "feishu://doc/a{b}",
            # 坏的百分号三元组
            "feishu://doc/%",
            "feishu://doc/%2",
            "feishu://doc/%zz",
            "feishu://doc/%2g",
        ],
    },
]


_H1 = "dpe1:" + "a" * 64
_H2 = "dpe2:" + "a" * 64
_HUGE = 2**53


def bad(
    name: str,
    kind: str,
    input_: Any,
    code: str,
    path: str | None = None,
    contract: str = "dpe1",
) -> dict[str, Any]:
    """拒绝类用例：只有一处违例时给出 path；多处违例只断言 code（core §2.8）。"""
    case: dict[str, Any] = {
        "name": name,
        "object_kind": kind,
        "contract": contract,
        "input": input_,
        "code": code,
    }
    if path is not None:
        case["path"] = path
    return case


def raw(
    name: str,
    kind: str,
    text: str,
    code: str,
    path: str | None = None,
    contract: str = "dpe1",
) -> dict[str, Any]:
    """以原始 JSON 文本给出输入的拒绝类用例（I-JSON 违例无法以解析后的 JSON 值表达）。"""
    case = bad(name, kind, None, code, path, contract)
    del case["input"]
    case["input_json"] = text
    return case


V, CU, CTU = "DPE_VALIDATION", "DPE_CATEGORY_UNKNOWN", "DPE_CONTRACT_UNSUPPORTED"

INVALID_VECTORS: list[dict[str, Any]] = [
    {
        "name": "invalid_objects",
        "description": "拒绝类一致性用例（core.md §2.8 校验顺序）：每条输入都 MUST 被拒绝且错误码一致；只有一处违例的用例另断言违例位置 path（RFC 6901）。多处违例的用例只断言错误码，用来固定校验顺序。",
        "cases": [
            # 元素对象
            bad("element_not_object", "element", ["NarrativeText"], V, ""),
            bad("element_missing_category", "element", {"text": "x"}, V, ""),
            bad(
                "element_category_not_string",
                "element",
                {"category": 1},
                V,
                "/category",
            ),
            bad(
                "element_category_unknown",
                "element",
                {"category": "Video"},
                CU,
                "/category",
            ),
            bad(
                "element_undefined_field",
                "element",
                {"category": "NarrativeText", "text_as_html": "<p/>"},
                V,
                "/text_as_html",
            ),
            bad(
                "element_blob_category_not_allowed",
                "element",
                {"category": "NarrativeText", "blob": _BLOB},
                V,
                "/blob",
            ),
            bad(
                "element_mime_type_category_not_allowed",
                "element",
                {"category": "Title", "mime_type": "image/png"},
                V,
                "/mime_type",
            ),
            bad(
                "element_text_not_string",
                "element",
                {"category": "Title", "text": 1},
                V,
                "/text",
            ),
            bad(
                "element_blob_not_ref",
                "element",
                {"category": "Image", "blob": "https://a.cdn/x.png"},
                V,
                "/blob",
            ),
            bad(
                "element_metadata_not_object",
                "element",
                {"category": "Title", "metadata": [1]},
                V,
                "/metadata",
            ),
            bad(
                "element_metadata_integer_out_of_range",
                "element",
                {"category": "Title", "metadata": {"a/b": [_HUGE]}},
                V,
                "/metadata/a~1b/0",
            ),
            bad(
                "element_category_before_closed_schema",
                "element",
                {"category": "Video", "foo": 1},
                CU,
            ),
            bad(
                "element_category_null_is_missing",
                "element",
                {"category": None, "text": "x"},
                V,
                "",
            ),
            # 页对象
            bad("page_not_object", "page", [], V, ""),
            bad("page_missing_elements", "page", {"title": "p"}, V, ""),
            bad(
                "page_undefined_field",
                "page",
                {"number": 1, "elements": []},
                V,
                "/number",
            ),
            bad(
                "page_title_not_string",
                "page",
                {"title": 1, "elements": []},
                V,
                "/title",
            ),
            bad(
                "page_metadata_integer_out_of_range",
                "page",
                {"page_metadata": {"n": -_HUGE}, "elements": []},
                V,
                "/page_metadata/n",
            ),
            bad("page_elements_not_array", "page", {"elements": _H1}, V, "/elements"),
            bad(
                "page_child_hash_not_string",
                "page",
                {"elements": [1]},
                V,
                "/elements/0",
            ),
            bad(
                "page_child_hash_no_prefix",
                "page",
                {"elements": ["a" * 64]},
                CTU,
                "/elements/0",
            ),
            bad(
                "page_child_hash_unknown_contract",
                "page",
                {"elements": [_H1, "dpe9:" + "a" * 64]},
                CTU,
                "/elements/1",
            ),
            bad(
                "page_child_hash_drill_contract_unsupported",
                "page",
                {"elements": [_H2]},
                CTU,
                "/elements/0",
            ),
            bad(
                "page_child_hash_uppercase_hex",
                "page",
                {"elements": ["dpe1:" + "A" * 64]},
                V,
                "/elements/0",
            ),
            bad(
                "page_child_hash_mixed_contract",
                "page",
                {"elements": [_H1]},
                V,
                "/elements/0",
                contract="dpe2",
            ),
            bad("page_elements_null_is_missing", "page", {"elements": None}, V, ""),
            bad(
                "page_closed_schema_before_children",
                "page",
                {"number": 1, "elements": ["a" * 64]},
                V,
            ),
            bad(
                "page_title_before_children",
                "page",
                {"title": 1, "elements": ["a" * 64]},
                V,
            ),
            bad(
                "page_children_in_array_order_validation_first",
                "page",
                {"elements": ["dpe1:" + "A" * 64, "a" * 64]},
                V,
            ),
            bad(
                "page_children_in_array_order_contract_first",
                "page",
                {"elements": ["a" * 64, "dpe1:" + "A" * 64]},
                CTU,
            ),
            bad(
                "page_child_hash_prefix_before_hex",
                "page",
                {"elements": ["dpe9:" + "A" * 64]},
                CTU,
            ),
            bad(
                "page_metadata_before_children",
                "page",
                {"page_metadata": {"n": _HUGE}, "elements": ["a" * 64]},
                V,
            ),
            # 文档对象
            bad("document_missing_file_type", "document", {"pages": []}, V, ""),
            bad("document_missing_pages", "document", {"file_type": "md"}, V, ""),
            bad(
                "document_file_type_uppercase",
                "document",
                {"file_type": "Markdown", "pages": []},
                V,
                "/file_type",
            ),
            bad(
                "document_file_type_hyphen",
                "document",
                {"file_type": "git-repo", "pages": []},
                V,
                "/file_type",
            ),
            bad(
                "document_file_type_empty",
                "document",
                {"file_type": "", "pages": []},
                V,
                "/file_type",
            ),
            bad(
                "document_file_type_leading_underscore",
                "document",
                {"file_type": "_internal", "pages": []},
                V,
                "/file_type",
            ),
            bad(
                "document_file_type_too_long",
                "document",
                {"file_type": "max_length_value_0123456789abcdef", "pages": []},  # 33 字符
                V,
                "/file_type",
            ),
            bad(
                "document_file_type_trailing_newline",
                "document",
                {"file_type": "md\n", "pages": []},  # 全串匹配：Python 的 `$` 会匹配末尾换行之前，必须 fullmatch
                V,
                "/file_type",
            ),
            bad(
                "document_undefined_field",
                "document",
                {"file_type": "md", "pages": [], "attributes": {}},
                V,
                "/attributes",
            ),
            bad(
                "document_metadata_integer_out_of_range",
                "document",
                {"file_type": "md", "doc_metadata": {"n": _HUGE}, "pages": []},
                V,
                "/doc_metadata/n",
            ),
            bad(
                "document_child_hash_blob_prefix",
                "document",
                {"file_type": "md", "pages": ["sha256:" + "a" * 64]},
                CTU,
                "/pages/0",
            ),
            bad("document_shape_before_children", "document", {"pages": ["a" * 64]}, V),
            bad(
                "document_file_type_before_children",
                "document",
                {"file_type": "Markdown", "pages": ["a" * 64]},
                V,
            ),
            bad(
                "document_metadata_before_children",
                "document",
                {"file_type": "md", "doc_metadata": {"n": _HUGE}, "pages": ["a" * 64]},
                V,
            ),
            # I-JSON（第 0 步）：输入以原始 JSON 文本给出（input_json），消费方用严格的 I-JSON 解析器读取
            raw(
                "ijson_lone_surrogate",
                "element",
                '{"category": "Title", "text": "\\ud800"}',
                V,
                "",
            ),
            raw(
                "ijson_lone_surrogate_in_key",
                "element",
                '{"category": "Title", "metadata": {"\\udc00": 1}}',
                V,
                "",
            ),
            raw(
                "ijson_before_category",
                "element",
                '{"category": "Video", "text": "\\ud800"}',
                V,
            ),
            raw(
                "ijson_before_children",
                "page",
                '{"title": "\\ud800", "elements": ["a"]}',
                V,
            ),
            raw(
                "ijson_duplicate_key",
                "element",
                '{"category": "Title", "category": "Video"}',
                V,
                "",
            ),
            raw(
                "ijson_after_contract_unsupported",
                "page",
                '{"elements": ["aaaa", "\\ud800"]}',
                V,
                "",
            ),
            # 数值越界属于第 4 步（§2.6），不是解析阶段：位置指向出错的值，且排在 category 之后
            raw(
                "number_overflow_double",
                "element",
                '{"category": "Title", "metadata": {"a": 1e400}}',
                V,
                "/metadata/a",
            ),
            raw(
                "number_overflow_after_category",
                "element",
                '{"category": "Video", "metadata": {"a": 1e400}}',
                CU,
            ),
            # 展开视图（vectors/README.md）：文档字段 → 各页字段 → 各页元素
            bad(
                "expanded_element_category_unknown",
                "expanded_document",
                {"file_type": "md", "pages": [{"elements": [{"category": "Video"}]}]},
                CU,
                "/pages/0/elements/0/category",
            ),
            bad(
                "expanded_page_fields_before_elements",
                "expanded_document",
                {
                    "file_type": "md",
                    "pages": [
                        {"elements": [{"category": "Video"}]},
                        {"title": 1, "elements": []},
                    ],
                },
                V,
            ),
            bad(
                "expanded_document_fields_before_pages",
                "expanded_document",
                {
                    "file_type": "Markdown",
                    "pages": [{"elements": [{"category": "Video"}]}],
                },
                V,
            ),
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


# ---------------------------------------------------------------------------
# connector 契约 §4.1.1：config_schema 的 pattern / patternProperties 可移植子集
# （规范依据 spec/connector-contract.md §4.1.1；与 hash 契约无关）
# ---------------------------------------------------------------------------

#: 合法 pattern：MUST 被接受且可转译（生成器逐条断言）。note 供人阅读。
PATTERN_VALID: list[dict[str, str]] = [
    {"pattern": "^[a-z][a-z0-9_]*$", "note": "典型标识符"},
    {"pattern": "^(?:ab|cd)+$", "note": "(?…) 分组、非捕获同义"},
    {"pattern": "^[\\d\\w\\s-]+$", "note": "类内简写与类尾字面 -"},
    {"pattern": "[-a]", "note": "类首字面 -"},
    {"pattern": "[a-]", "note": "类尾字面 -"},
    {"pattern": "[\\-]", "note": "转义 -"},
    {"pattern": "[\\x00-\\x1f]", "note": "\\xHH 作区间端点"},
    {"pattern": "\\xE9", "note": "\\xHH 十六进制大小写均可"},
    {"pattern": "[^\\]a]", "note": "类内转义 ]"},
    {"pattern": "(a|)", "note": "空分支"},
    {"pattern": "()", "note": "空组至少计 1（分组至少计 1）"},
    {"pattern": "\\S+", "note": "补集简写"},
    {"pattern": "[\\D]", "note": "类内补集简写"},
    {"pattern": "[^\\D]", "note": "取反类内的补集简写"},
    {"pattern": "a{0}.*", "note": "0 次量词"},
    {"pattern": "\\W{255}", "note": "结构上界内的常见形状"},
    {"pattern": "[aaaa]{1024}", "note": "计数不去重：恰 4096（边界，合法）"},
    {"pattern": "(?:a|bc){1365}", "note": "选择与嵌套：恰 4095"},
    {"pattern": "a{4096}", "note": "恰 4096"},
    {"pattern": "a{4096,}", "note": "{m,} 因子记 m：恰 4096"},
    {"pattern": "a{1,4096}", "note": "恰 4096"},
    {"pattern": "\\d{4}-\\d{2}-\\d{2}", "note": "日期"},
    {"pattern": "[.]", "note": "类内元字符照字面（RFC 9485 / XSD 的类内语义）"},
    {"pattern": "[a*b]", "note": "类内元字符照字面"},
    {"pattern": "[^a$]", "note": "取反类内的元字符照字面"},
    {"pattern": "[&]", "note": "单个 & 是类内字面（只要不构成未转义序列）"},
    {
        "pattern": "[a\\x26\\x26b]",
        "note": "以 \\xHH 转义写出相邻的 &，不构成未转义序列",
    },
    {"pattern": "[a\\x7E\\x7Eb]", "note": "以 \\xHH 转义写出相邻的 ~"},
    {"pattern": "[\\x2D\\x2D]", "note": "以 \\xHH 转义写出相邻的 -"},
    {"pattern": "(?:){4096}", "note": "零尺寸原子的大计数：分组至少计 1，恰 4096"},
    {"pattern": "", "note": "空 pattern：匹配任意串"},
    {"pattern": "." * 1024, "note": "长度顶格（1024 个 .，展开规模 1024）"},
    {
        "pattern": "[" + "".join(chr(0x100 + 0x110 * k) for k in range(16)) + "]{255}",
        "note": "最坏情况：16 个跨字节宽度的互不相邻码点的手写类 ×255（展开规模 4080；与探针 pattern_budget 同款，各实现 MUST 能编译并匹配）",
    },
    {
        "pattern": "(" * 64 + "a" + ")" * 64,
        "note": "嵌套深度顶格（64 层，展开规模 1）",
    },
    {
        "pattern": "(" * 64 + "a{4096}" + ")" * 64,
        "note": "嵌套深度与展开规模同时顶格",
    },
    {
        "pattern": "(?:" * 64 + "[a]*" + ")*" * 64,
        "note": "最坏嵌套形状：64 层分组且每层带量词（引擎的解析嵌套还计入量词与字符类，各实现 MUST 能编译并匹配）",
    },
]

#: 越界 pattern：MUST 被拒绝（manifest_invalid）；reason 与参考实现一致，供人阅读。
PATTERN_INVALID: list[dict[str, str]] = [
    {"pattern": "(?=a)b", "reason": "lookaround", "note": "正向先行断言"},
    {"pattern": "(?!a)b", "reason": "lookaround", "note": "负向先行断言"},
    {"pattern": "(?<=a)b", "reason": "lookaround", "note": "正向后行断言"},
    {"pattern": "(?<!a)b", "reason": "lookaround", "note": "负向后行断言"},
    {
        "pattern": "\\p{L}",
        "reason": "unicode_property_escape",
        "note": "Unicode 属性转义",
    },
    {
        "pattern": "\\P{L}",
        "reason": "unicode_property_escape",
        "note": "Unicode 属性转义（补）",
    },
    {"pattern": "(a)\\1", "reason": "backreference", "note": "反向引用"},
    {"pattern": "\\1", "reason": "backreference", "note": "反向引用（无组）"},
    {"pattern": "(?i)a", "reason": "inline_flag", "note": "内联标志"},
    {"pattern": "(?<n>a)", "reason": "named_group", "note": "命名组"},
    {"pattern": "(?P<n>a)", "reason": "named_group", "note": "命名组（Python 语法）"},
    {"pattern": "(?#c)", "reason": "comment_group", "note": "注释组"},
    {"pattern": "a*?", "reason": "lazy_quantifier", "note": "惰性量词"},
    {"pattern": "a??", "reason": "lazy_quantifier", "note": "惰性量词"},
    {"pattern": "a{2}?", "reason": "lazy_quantifier", "note": "惰性量词（括号形）"},
    {"pattern": "a*+", "reason": "possessive_quantifier", "note": "占有量词"},
    {
        "pattern": "a{2}+",
        "reason": "possessive_quantifier",
        "note": "占有量词（括号形）",
    },
    {"pattern": "a{2}{3}", "reason": "stacked_quantifier", "note": "叠加量词"},
    {"pattern": "a**", "reason": "stacked_quantifier", "note": "叠加量词"},
    {"pattern": "{,3}", "reason": "invalid_repetition", "note": "{,n} 形式"},
    {"pattern": "a{", "reason": "invalid_repetition", "note": "未转义的字面 {"},
    {"pattern": "a}", "reason": "invalid_repetition", "note": "未转义的字面 }"},
    {"pattern": "a{2,1}", "reason": "invalid_repetition", "note": "下界大于上界"},
    {"pattern": "[z-a]", "reason": "class_range_order", "note": "反向区间"},
    {"pattern": "[\\d-z]", "reason": "class_range_endpoint", "note": "简写作区间端点"},
    {"pattern": "[]", "reason": "empty_class", "note": "空类"},
    {"pattern": "[^]", "reason": "empty_class", "note": "空类（取反）"},
    {"pattern": "[a&&b]", "reason": "class_set_operation", "note": "类集合运算"},
    {"pattern": "[a~~b]", "reason": "class_set_operation", "note": "类集合运算"},
    {"pattern": "[--]", "reason": "class_set_operation", "note": "字面 -- 序列"},
    {
        "pattern": "[a\\--b]",
        "reason": "class_set_operation",
        "note": "相邻 -- 无论前一个是否转义都违例",
    },
    {"pattern": "a^b", "reason": "anchor_position", "note": "锚点不在分支首尾"},
    {
        "pattern": "(a$)",
        "reason": "anchor_position",
        "note": "锚点只允许在顶层分支首尾",
    },
    {
        "pattern": "(?:^a)",
        "reason": "anchor_position",
        "note": "锚点只允许在顶层分支首尾",
    },
    {"pattern": "(^a)", "reason": "anchor_position", "note": "锚点不在顶层分支"},
    {"pattern": "a$b", "reason": "anchor_position", "note": "$ 不在分支尾"},
    {"pattern": "\\q", "reason": "unknown_escape", "note": "未知字母转义"},
    {"pattern": "\\/", "reason": "unknown_escape", "note": "/ 不在转义白名单"},
    {
        "pattern": "\\u0041",
        "reason": "unknown_escape",
        "note": "\\uHHHH 不在白名单（用字面字符）",
    },
    {"pattern": "\\b", "reason": "unknown_escape", "note": "词边界不支持"},
    {"pattern": "[a-b-c]", "reason": "class_dash_position", "note": "类中间的 -"},
    {"pattern": "*a", "reason": "syntax", "note": "量词没有作用对象"},
    {"pattern": "[aaaa]{1025}", "reason": "expansion", "note": "展开规模 4100（越界）"},
    {
        "pattern": "(?:a|bcd){1365}",
        "reason": "expansion",
        "note": "展开规模 5460（越界）",
    },
    {"pattern": "a{4097}", "reason": "expansion", "note": "展开规模 4097"},
    {"pattern": "a{4097,}", "reason": "expansion", "note": "因子记 m：4097"},
    {"pattern": "a{1,4097}", "reason": "expansion", "note": "展开规模 4097"},
    {"pattern": "a" * 1025, "reason": "length", "note": "长度 1025（越界）"},
    {
        "pattern": "(?:){4097}",
        "reason": "expansion",
        "note": "零尺寸原子按至少计 1：4097",
    },
    {"pattern": "(){4294967296}", "reason": "expansion", "note": "空组 × 极大计数"},
    {
        "pattern": "(?:(?:){4096}){4096}",
        "reason": "expansion",
        "note": "零尺寸嵌套相乘后越界",
    },
    {
        "pattern": "(" * 65 + "a" + ")" * 65,
        "reason": "nesting_depth",
        "note": "嵌套深度 65（越界）",
    },
    {
        "pattern_json": '"\\ud800"',
        "reason": "not_scalar_value",
        "note": "孤立代理项不是 Unicode 标量值；manifest 经 I-JSON 已禁，此处同样判越界",
    },
]

#: 匹配用例：**手写**的规范语义期望表（生成器只用参考匹配器交叉核对，不一致即生成失败）。
PATTERN_MATCH_CASES: list[dict[str, Any]] = [
    {"pattern": "^\\w+$", "value": "héllo", "match": False, "note": "\\w 为 ASCII"},
    {"pattern": "^\\w+$", "value": "abc_9", "match": True},
    {"pattern": "^\\d$", "value": "٣", "match": False, "note": "\\d 为 ASCII"},
    {"pattern": "^\\d$", "value": "5", "match": True},
    {"pattern": "^\\s$", "value": "\u000b", "match": True, "note": "\\s 含 \\v"},
    {"pattern": "^\\s$", "value": " ", "match": False, "note": "\\s 不含 Unicode 空白"},
    {"pattern": "a$", "value": "a\n", "match": False, "note": "$ 不匹配末尾换行之前"},
    {"pattern": "a$", "value": "a", "match": True},
    {"pattern": "^a", "value": "ba", "match": False},
    {"pattern": "a", "value": "ba", "match": True, "note": "未锚定（search）"},
    {"pattern": "^.$", "value": "\r", "match": False, "note": ". 排除 \\n 与 \\r"},
    {"pattern": "^.$", "value": "\n", "match": False},
    {"pattern": "^.$", "value": "a", "match": True},
    {"pattern": "^.$", "value": "😀", "match": True, "note": "按码点，非 BMP 单字符"},
    {"pattern": "^\\xE9$", "value": "é", "match": True},
    {"pattern": "^[^\\D]$", "value": "5", "match": True},
    {"pattern": "^[^\\D]$", "value": "x", "match": False},
    {
        "pattern": "^[\\W\\d]$",
        "value": "x",
        "match": False,
        "note": "\\W 并 \\d 不含字母与下划线",
    },
    {"pattern": "^[\\W\\d]$", "value": "é", "match": True},
    {"pattern": "^[\\W\\d]$", "value": "5", "match": True},
    {"pattern": "^[^a]$", "value": "\n", "match": True, "note": "取反类含换行"},
    {"pattern": "^\\S+$", "value": " ", "match": False},
    {"pattern": "^(a{2}){3}$", "value": "aaaaaa", "match": True},
    {
        "pattern": "^[a-cb]$",
        "value": "b",
        "match": True,
        "note": "计数不去重不影响语义",
    },
    {"pattern": "^\\d{4}-\\d{2}-\\d{2}$", "value": "2026-10-09", "match": True},
    {"pattern": "^[a-]$", "value": "-", "match": True},
    {"pattern": "^-?\\d+$", "value": "-12", "match": True},
    {"pattern": "^\\|$", "value": "|", "match": True, "note": "转义元字符"},
    {"pattern": "^\\x41$", "value": "A", "match": True},
    {"pattern": "^\\n$", "value": "\n", "match": True},
    {"pattern": "^é$", "value": "é", "match": True, "note": "字面非 ASCII 按码点"},
    {"pattern": "^a{0}$", "value": "", "match": True},
    {
        "pattern": "",
        "value": "任意文本",
        "match": True,
        "note": "空 pattern 匹配任意串",
    },
    {
        "pattern": "^.$",
        "value_json": '"\\ud800"',
        "match": False,
        "note": "匹配宇宙是 Unicode 标量值：孤立代理项不参与匹配（value 用原始 JSON 文本表达）",
    },
    {"pattern": "^[.]$", "value": ".", "match": True, "note": "类内元字符照字面"},
    {"pattern": "^[a*b]+$", "value": "*ab", "match": True},
    {
        "pattern": "^[^a$]$",
        "value": "$",
        "match": False,
        "note": "取反类里的 $ 是字面，被排除",
    },
    {"pattern": "^[^a$]$", "value": "b", "match": True},
]


def _nested_schema(levels: int) -> dict[str, Any]:
    node: dict[str, Any] = {"type": "object"}
    for _ in range(levels):
        node = {"type": "object", "properties": {"a": node}}
    return node


def build_defs(n: int) -> dict[str, Any]:
    """构造 n 条首尾相接的 $defs（引用链 n 跳，末端可匹配 "^a$"）。"""
    defs: dict[str, Any] = {}
    for i in range(n - 1):
        defs[f"d{i}"] = {"$ref": f"#/$defs/d{i + 1}"}
    defs[f"d{n - 1}"] = {"type": "string", "pattern": "^a$"}
    return defs


def build_layered_defs(n: int, layers: int) -> dict[str, Any]:
    """n 条首尾相接的 $defs，每跳在 $ref 外面包 layers 层原地 allOf。"""
    defs: dict[str, Any] = {}
    for i in range(n):
        node: Any = (
            {"$ref": f"#/$defs/d{i + 1}"}
            if i + 1 < n
            else {"type": "string", "pattern": "^a$"}
        )
        for _ in range(layers):
            node = {"allOf": [node]}
        defs[f"d{i}"] = node
    return defs


def _nested_array(levels: int) -> Any:
    node: Any = "x"
    for _ in range(levels):
        node = [node]
    return node


#: schema 遍历用例：封闭关键字子集、`$defs`/`$ref` 规则与求值结论。
#: ``manifest_valid`` 由生成器用 §4.1.1 封闭检查器交叉核对；``config`` 与 ``config_valid``
#: 是手写期望（生成器不做 JSON Schema 求值），由各实现逐条断言。
PATTERN_SCHEMA_CASES: list[dict[str, Any]] = [
    {
        "name": "const 内的 pattern 键是数据",
        "schema": {
            "type": "object",
            "properties": {"x": {"const": {"pattern": "(?=x)"}}},
        },
        "config": {"x": {"pattern": "(?=x)"}},
        "manifest_valid": True,
        "config_valid": True,
    },
    {
        "name": "default 内的 pattern 键是数据",
        "schema": {
            "type": "object",
            "properties": {"x": {"type": "object", "default": {"pattern": "("}}},
        },
        "config": {},
        "manifest_valid": True,
        "config_valid": True,
    },
    {
        "name": "examples 内的 pattern 键是数据",
        "schema": {
            "type": "object",
            "examples": [{"pattern": "[z-a]"}],
            "properties": {},
        },
        "config": {},
        "manifest_valid": True,
        "config_valid": True,
    },
    {
        "name": "名为 pattern 的属性（值通过）",
        "schema": {
            "type": "object",
            "properties": {"pattern": {"type": "string", "pattern": "^\\d+$"}},
        },
        "config": {"pattern": "123"},
        "manifest_valid": True,
        "config_valid": True,
    },
    {
        "name": "名为 pattern 的属性（值不通过）",
        "schema": {
            "type": "object",
            "properties": {"pattern": {"type": "string", "pattern": "^\\d+$"}},
        },
        "config": {"pattern": "abc"},
        "manifest_valid": True,
        "config_valid": False,
    },
    {
        "name": "patternProperties 键匹配属性名（通过）",
        "schema": {
            "type": "object",
            "patternProperties": {"^x_[a-z]+$": {"type": "integer"}},
        },
        "config": {"x_ab": 1},
        "manifest_valid": True,
        "config_valid": True,
    },
    {
        "name": "patternProperties 键匹配属性名（不通过）",
        "schema": {
            "type": "object",
            "patternProperties": {"^x_[a-z]+$": {"type": "integer"}},
        },
        "config": {"x_ab": "s"},
        "manifest_valid": True,
        "config_valid": False,
    },
    {
        "name": "additionalProperties 经 patternProperties 键判定（ASCII 边界）",
        "schema": {
            "type": "object",
            "patternProperties": {"^\\w+$": {}},
            "additionalProperties": False,
        },
        "config": {"é": 1},
        "manifest_valid": True,
        "config_valid": False,
        "note": "\\w 为 ASCII：é 不被模式覆盖，additionalProperties 生效；实现不得只覆写 pattern 关键字而漏掉内部拼接路径",
    },
    {
        "name": "additionalProperties 经 patternProperties 键判定（$ 语义）",
        "schema": {
            "type": "object",
            "patternProperties": {"^\\w+$": {}},
            "additionalProperties": False,
        },
        "config": {"a\n": 1},
        "manifest_valid": True,
        "config_valid": False,
    },
    {
        "name": "patternProperties 空键匹配任意属性名",
        "schema": {
            "type": "object",
            "patternProperties": {"": {"type": "integer"}},
            "additionalProperties": False,
        },
        "config": {"x": 1},
        "manifest_valid": True,
        "config_valid": True,
        "note": "空 pattern 语义为匹配任意串；实现侧转译不得产出会被「|」拼接吞掉的空串",
    },
    {
        "name": "patternProperties 键转译碰撞：子 schema 并存（不通过）",
        "schema": {
            "type": "object",
            "patternProperties": {"\\d": {"type": "string"}, "[0-9]": {"maxLength": 1}},
        },
        "config": {"1": "ab"},
        "manifest_valid": True,
        "config_valid": False,
    },
    {
        "name": "patternProperties 键转译碰撞：子 schema 并存（通过）",
        "schema": {
            "type": "object",
            "patternProperties": {"\\d": {"type": "string"}, "[0-9]": {"maxLength": 1}},
        },
        "config": {"1": "a"},
        "manifest_valid": True,
        "config_valid": True,
    },
    {
        "name": "关键字 pattern 越界即清单不合法",
        "schema": {
            "type": "object",
            "properties": {"repo": {"type": "string", "pattern": "(?=x)"}},
        },
        "manifest_valid": False,
    },
    {
        "name": "patternProperties 键越界即清单不合法",
        "schema": {"type": "object", "patternProperties": {"^(?=a)": {}}},
        "manifest_valid": False,
    },
    {
        "name": "$ref 指向 $defs（通过）",
        "schema": {
            "type": "object",
            "$defs": {"name": {"type": "string", "pattern": "^\\w+$"}},
            "properties": {"x": {"$ref": "#/$defs/name"}},
        },
        "config": {"x": "abc_9"},
        "manifest_valid": True,
        "config_valid": True,
    },
    {
        "name": "$ref 目标处的 pattern 仍按子集语义匹配",
        "schema": {
            "type": "object",
            "$defs": {"name": {"type": "string", "pattern": "^\\w+$"}},
            "properties": {"x": {"$ref": "#/$defs/name"}},
        },
        "config": {"x": "héllo"},
        "manifest_valid": True,
        "config_valid": False,
    },
    {
        "name": "$ref 名字按 RFC 6901 §6 解码（键含空格）",
        "schema": {
            "type": "object",
            "$defs": {"a b": {"type": "string", "pattern": "^a$"}},
            "properties": {"x": {"$ref": "#/$defs/a%20b"}},
        },
        "config": {"x": "a"},
        "manifest_valid": True,
        "config_valid": True,
    },
    {
        "name": "$ref 名字按 RFC 6901 §6 解码（键含非 ASCII）",
        "schema": {
            "type": "object",
            "$defs": {"é": {"type": "string", "pattern": "^a$"}},
            "properties": {"x": {"$ref": "#/$defs/%C3%A9"}},
        },
        "config": {"x": "a"},
        "manifest_valid": True,
        "config_valid": True,
    },
    {
        "name": "$ref 名字按 RFC 6901 §6 解码（键含斜杠）",
        "schema": {
            "type": "object",
            "$defs": {"a/b": {"type": "string", "pattern": "^a$"}},
            "properties": {"x": {"$ref": "#/$defs/a%7E1b"}},
        },
        "config": {"x": "a"},
        "manifest_valid": True,
        "config_valid": True,
        "note": "先百分号解码（%7E1 → ~1）再转义（~1 → /），顺序不可换",
    },
    {
        "name": "$ref 指向不存在的 $defs 条目",
        "schema": {
            "type": "object",
            "$defs": {"a": {}},
            "properties": {"x": {"$ref": "#/$defs/nope"}},
        },
        "manifest_valid": False,
    },
    {
        "name": "$ref 形式：根引用不合法",
        "schema": {"type": "object", "properties": {"x": {"$ref": "#"}}},
        "manifest_valid": False,
    },
    {
        "name": "$ref 形式：锚点不合法",
        "schema": {"type": "object", "properties": {"x": {"$ref": "#name"}}},
        "manifest_valid": False,
    },
    {
        "name": "$ref 形式：深于一层不合法",
        "schema": {
            "type": "object",
            "$defs": {"a": {"properties": {"y": {}}}},
            "properties": {"x": {"$ref": "#/$defs/a/properties/y"}},
        },
        "manifest_valid": False,
    },
    {
        "name": "$ref 形式：其他位置不合法",
        "schema": {"type": "object", "properties": {"x": {"$ref": "#/properties/x"}}},
        "manifest_valid": False,
    },
    {
        "name": "$ref 形式：空名字不合法",
        "schema": {
            "type": "object",
            "$defs": {"": {}},
            "properties": {"x": {"$ref": "#/$defs/"}},
        },
        "manifest_valid": False,
    },
    {
        "name": "$ref 形式：%2F 解码成额外分隔符不合法",
        "schema": {
            "type": "object",
            "$defs": {"a": True},
            "properties": {"x": {"$ref": "#/$defs/a%2Fb"}},
        },
        "manifest_valid": False,
        "note": "先百分号解码再按 / 拆分：%2F 成为分隔符，指针深于一层",
    },
    {
        "name": "$ref 形式：外部 URI 不合法",
        "schema": {
            "type": "object",
            "properties": {"x": {"$ref": "other.json#/$defs/x"}},
        },
        "manifest_valid": False,
    },
    {
        "name": "$defs 自引用成环",
        "schema": {
            "type": "object",
            "$defs": {"a": {"$ref": "#/$defs/a"}},
            "properties": {"x": {"$ref": "#/$defs/a"}},
        },
        "manifest_valid": False,
    },
    {
        "name": "$defs 互引成环",
        "schema": {
            "type": "object",
            "$defs": {"a": {"$ref": "#/$defs/b"}, "b": {"$ref": "#/$defs/a"}},
        },
        "manifest_valid": False,
    },
    {
        "name": "$defs 只允许在根",
        "schema": {"type": "object", "properties": {"x": {"$defs": {"d": {}}}}},
        "manifest_valid": False,
    },
    {
        "name": "引用布尔 schema 有效",
        "schema": {
            "type": "object",
            "$defs": {"f": True},
            "properties": {"x": {"$ref": "#/$defs/f"}},
        },
        "config": {"x": 1},
        "manifest_valid": True,
        "config_valid": True,
    },
    {
        "name": "布尔 schema 作为 additionalProperties",
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"x": {}},
        },
        "config": {"x": 1},
        "manifest_valid": True,
        "config_valid": True,
    },
    {
        "name": "if/then/else 在允许集内",
        "schema": {
            "type": "object",
            "if": {"required": ["a"]},
            "then": {"required": ["b"]},
        },
        "config": {"a": 1},
        "manifest_valid": True,
        "config_valid": False,
    },
    {
        "name": "propertyNames 在允许集内（其中 pattern 受子集约束）",
        "schema": {"type": "object", "propertyNames": {"pattern": "^[a-z]+$"}},
        "config": {"ab": 1},
        "manifest_valid": True,
        "config_valid": True,
    },
    {
        "name": "propertyNames 的 pattern 越界判不合法",
        "schema": {"type": "object", "propertyNames": {"pattern": "(?=a)"}},
        "manifest_valid": False,
    },
    {
        "name": "items 单 schema 有效",
        "schema": {
            "type": "object",
            "properties": {
                "x": {"type": "array", "items": {"type": "string", "pattern": "^a$"}}
            },
        },
        "config": {"x": ["a"]},
        "manifest_valid": True,
        "config_valid": True,
    },
    {
        "name": "items 数组形式不合法",
        "schema": {
            "type": "object",
            "properties": {"x": {"type": "array", "items": [{"type": "string"}]}},
        },
        "manifest_valid": False,
    },
    {
        "name": "$comment 在允许集内",
        "schema": {"type": "object", "$comment": "说明", "properties": {}},
        "config": {},
        "manifest_valid": True,
        "config_valid": True,
    },
    {
        "name": "contentEncoding / contentMediaType 只作注解",
        "schema": {
            "type": "object",
            "properties": {
                "x": {
                    "type": "string",
                    "contentEncoding": "base64",
                    "contentMediaType": "image/png",
                }
            },
        },
        "config": {"x": "这不是 base64"},
        "manifest_valid": True,
        "config_valid": True,
    },
    {
        "name": "contentSchema 不在允许集内",
        "schema": {
            "type": "object",
            "properties": {"x": {"contentSchema": {"type": "string"}}},
        },
        "manifest_valid": False,
    },
    {
        "name": "multipleOf 不在允许集内",
        "schema": {"type": "object", "properties": {"x": {"multipleOf": 2}}},
        "manifest_valid": False,
    },
    {
        "name": "$schema 仅根且必须 2020-12（带 # 等价）",
        "schema": {
            "type": "object",
            "$schema": "https://json-schema.org/draft/2020-12/schema#",
            "properties": {"x": {"type": "string", "pattern": "^a$"}},
        },
        "config": {"x": "a"},
        "manifest_valid": True,
        "config_valid": True,
    },
    {
        "name": "嵌套 $schema 不合法",
        "schema": {
            "type": "object",
            "$defs": {"d": {"$schema": "https://json-schema.org/draft/2020-12/schema"}},
        },
        "manifest_valid": False,
    },
    {
        "name": "$schema 非 2020-12 不合法",
        "schema": {
            "type": "object",
            "$schema": "http://json-schema.org/draft-07/schema#",
        },
        "manifest_valid": False,
    },
    {
        "name": "禁止的关键字：$id",
        "schema": {"type": "object", "properties": {"x": {"$id": "https://self/x"}}},
        "manifest_valid": False,
    },
    {
        "name": "禁止的关键字：$anchor / $dynamicAnchor / $dynamicRef",
        "schema": {
            "type": "object",
            "$defs": {"a": {"$anchor": "n", "$dynamicAnchor": "m"}},
            "properties": {"x": {"$dynamicRef": "#m"}},
        },
        "manifest_valid": False,
    },
    {
        "name": "禁止的关键字：definitions / dependencies / unevaluatedProperties",
        "schema": {
            "type": "object",
            "definitions": {"d": {}},
            "dependencies": {"a": ["b"]},
            "unevaluatedProperties": False,
        },
        "manifest_valid": False,
    },
    {
        "name": "禁止的关键字：prefixItems / contains",
        "schema": {
            "type": "object",
            "properties": {
                "x": {
                    "prefixItems": [{"type": "string"}],
                    "contains": {"type": "string"},
                }
            },
        },
        "manifest_valid": False,
    },
    {
        "name": "未定义的关键字",
        "schema": {"type": "object", "properties": {"x": {"customKeyword": 1}}},
        "manifest_valid": False,
    },
    {
        "name": "enum：1 与 1.0 相等",
        "schema": {"type": "object", "properties": {"x": {"enum": [1]}}},
        "config": {"x": 1.0},
        "manifest_valid": True,
        "config_valid": True,
    },
    {
        "name": "const：1 与 true 不等",
        "schema": {"type": "object", "properties": {"x": {"const": 1}}},
        "config": {"x": True},
        "manifest_valid": True,
        "config_valid": False,
    },
    {
        "name": "enum：0 与 false 不等",
        "schema": {"type": "object", "properties": {"x": {"enum": [0]}}},
        "config": {"x": False},
        "manifest_valid": True,
        "config_valid": False,
    },
    {
        "name": "uniqueItems：1 与 1.0 视为相同",
        "schema": {
            "type": "object",
            "properties": {"x": {"type": "array", "uniqueItems": True}},
        },
        "config": {"x": [1, 1.0]},
        "manifest_valid": True,
        "config_valid": False,
    },
    {
        "name": "uniqueItems：0 与 false 视为不同",
        "schema": {
            "type": "object",
            "properties": {"x": {"type": "array", "uniqueItems": True}},
        },
        "config": {"x": [0, False]},
        "manifest_valid": True,
        "config_valid": True,
    },
    {
        "name": "清单 JSON 嵌套深度 63（properties 链 30 层，未越界）",
        "schema": _nested_schema(30),
        "config": {},
        "manifest_valid": True,
        "config_valid": True,
    },
    {
        "name": "清单 JSON 嵌套深度越界（65：properties 链 31 层）",
        "schema": _nested_schema(31),
        "manifest_valid": False,
    },
    {
        "name": "清单 JSON 嵌套深度顶格（64：default 值嵌 61 层数组）",
        "schema": {"type": "object", "default": _nested_array(61)},
        "manifest_valid": True,
        "config": {},
        "config_valid": True,
        "note": "深度写在数据位置（default 的值）上同样受限",
    },
    {
        "name": "清单 JSON 嵌套深度越界（65：default 值嵌 62 层数组）",
        "schema": {"type": "object", "default": _nested_array(62)},
        "manifest_valid": False,
    },
    {
        "name": "展开深度顶格（64：纯引用链 62 跳，可求值）",
        "schema": {
            "type": "object",
            "$defs": build_defs(62),
            "properties": {"x": {"$ref": "#/$defs/d0"}},
        },
        "config": {"x": "a"},
        "manifest_valid": True,
        "config_valid": True,
    },
    {
        "name": "展开深度越界（65：纯引用链 63 跳）",
        "schema": {
            "type": "object",
            "$defs": build_defs(63),
            "properties": {"x": {"$ref": "#/$defs/d0"}},
        },
        "manifest_valid": False,
    },
    {
        "name": "链 × 原地嵌套（31 跳 × 1 层 allOf，未越界）",
        "schema": {
            "type": "object",
            "$defs": build_layered_defs(31, 1),
            "properties": {"x": {"$ref": "#/$defs/d0"}},
        },
        "config": {"x": "a"},
        "manifest_valid": True,
        "config_valid": True,
    },
    {
        "name": "未被引用的深 $defs 链不计入展开深度",
        "schema": {
            "type": "object",
            "$defs": build_defs(200),
            "properties": {"x": {"type": "string"}},
        },
        "config": {"x": "s"},
        "manifest_valid": True,
        "config_valid": True,
        "note": "深度从根计起；从未被求值的条目不受上界约束",
    },
    {
        "name": "deprecated 必须是布尔值",
        "schema": {"type": "object", "deprecated": 1},
        "manifest_valid": False,
    },
    {
        "name": "链 × 原地嵌套（32 跳 × 1 层 allOf，越界）",
        "schema": {
            "type": "object",
            "$defs": build_layered_defs(32, 1),
            "properties": {"x": {"$ref": "#/$defs/d0"}},
        },
        "manifest_valid": False,
    },
    {
        "name": "required 含非字符串成员",
        "schema": {"type": "object", "required": [1]},
        "manifest_valid": False,
    },
    {
        "name": "required 成员重复",
        "schema": {"type": "object", "required": ["a", "a"]},
        "manifest_valid": False,
    },
    {
        "name": "dependentRequired 的值不是字符串数组",
        "schema": {"type": "object", "dependentRequired": {"a": "b"}},
        "manifest_valid": False,
    },
    {
        "name": "maxLength 为负",
        "schema": {"type": "object", "properties": {"x": {"maxLength": -1}}},
        "manifest_valid": False,
    },
    {
        "name": "pattern 不是字符串",
        "schema": {"type": "object", "properties": {"x": {"pattern": 5}}},
        "manifest_valid": False,
    },
    {
        "name": "anyOf 为空数组",
        "schema": {"type": "object", "anyOf": []},
        "manifest_valid": False,
    },
    {
        "name": "type 数组成员重复",
        "schema": {"type": "object", "properties": {"x": {"type": ["string", "string"]}}},
        "manifest_valid": False,
    },
    {
        "name": "整数值的浮点边界合法（maxProperties 1.0）",
        "schema": {"type": "object", "maxProperties": 1.0},
        "config": {"a": 1},
        "manifest_valid": True,
        "config_valid": True,
        "note": "2020-12 元模式接受整数值的浮点",
    },
    {
        "name": "properties 不是对象",
        "schema": {"type": "object", "properties": []},
        "manifest_valid": False,
    },
    {
        "name": "type 取值不认识",
        "schema": {"type": "object", "properties": {"x": {"type": "strin"}}},
        "manifest_valid": False,
    },
    {
        "name": "allOf 不是数组",
        "schema": {"type": "object", "allOf": {}},
        "manifest_valid": False,
    },
    {
        "name": "enum 为空（合法但任何值都不通过）",
        "schema": {"type": "object", "properties": {"x": {"enum": []}}},
        "config": {"x": 1},
        "manifest_valid": True,
        "config_valid": False,
        "note": "2020-12 元模式不要求 enum 非空",
    },
    {
        "name": "$ref 前缀不得编码",
        "schema": {
            "type": "object",
            "$defs": {"name": True},
            "properties": {"x": {"$ref": "#%2F$defs%2Fname"}},
        },
        "manifest_valid": False,
        "note": "前缀 MUST 是字面 #/",
    },
    {
        "name": "$ref 名字含非法 ~ 转义",
        "schema": {
            "type": "object",
            "$defs": {"a~2b": True},
            "properties": {"x": {"$ref": "#/$defs/a~2b"}},
        },
        "manifest_valid": False,
    },
    {
        "name": "$ref 名字含非法百分号序列",
        "schema": {
            "type": "object",
            "$defs": {"a%ZZb": True},
            "properties": {"x": {"$ref": "#/$defs/a%ZZb"}},
        },
        "manifest_valid": False,
    },
    {
        "name": "$ref 名字的百分号序列不是合法 UTF-8",
        "schema": {
            "type": "object",
            "$defs": {"a": True},
            "properties": {"x": {"$ref": "#/$defs/a%FFb"}},
        },
        "manifest_valid": False,
        "note": "%FF 解码后不是合法 UTF-8，拒绝（unquote 的宽松 U+FFFD 替换不可用）",
    },
    {
        "name": "dependentRequired（不满足）",
        "schema": {"type": "object", "dependentRequired": {"a": ["b"]}},
        "config": {"a": 1},
        "manifest_valid": True,
        "config_valid": False,
    },
    {
        "name": "dependentRequired（满足）",
        "schema": {"type": "object", "dependentRequired": {"a": ["b"]}},
        "config": {"a": 1, "b": 2},
        "manifest_valid": True,
        "config_valid": True,
    },
    {
        "name": "allOf 求值",
        "schema": {
            "type": "object",
            "properties": {"x": {"allOf": [{"minimum": 1}, {"maximum": 5}]}},
        },
        "config": {"x": 3},
        "manifest_valid": True,
        "config_valid": True,
    },
    {
        "name": "oneOf 恰一分支",
        "schema": {
            "type": "object",
            "properties": {"x": {"oneOf": [{"type": "integer"}, {"maximum": 2}]}},
        },
        "config": {"x": 1},
        "manifest_valid": True,
        "config_valid": False,
        "note": "两个分支同时命中，oneOf 失败",
    },
    {
        "name": "minItems / maxItems",
        "schema": {
            "type": "object",
            "properties": {"x": {"type": "array", "minItems": 1, "maxItems": 2}},
        },
        "config": {"x": []},
        "manifest_valid": True,
        "config_valid": False,
    },
    {
        "name": "minProperties / maxProperties",
        "schema": {"type": "object", "minProperties": 1, "maxProperties": 2},
        "config": {},
        "manifest_valid": True,
        "config_valid": False,
    },
    {
        "name": "exclusiveMinimum / exclusiveMaximum",
        "schema": {
            "type": "object",
            "properties": {"x": {"exclusiveMinimum": 0, "exclusiveMaximum": 10}},
        },
        "config": {"x": 0},
        "manifest_valid": True,
        "config_valid": False,
    },
    {
        "name": "type 不匹配",
        "schema": {"type": "object", "properties": {"x": {"type": "string"}}},
        "config": {"x": 1},
        "manifest_valid": True,
        "config_valid": False,
    },
]


#: §4.1.1 封闭关键字子集（与 SDK 同源）：允许的关键字与位置约束
_ALLOWED_KEYWORDS = frozenset(
    {
        "type",
        "properties",
        "patternProperties",
        "additionalProperties",
        "required",
        "propertyNames",
        "items",
        "allOf",
        "anyOf",
        "oneOf",
        "not",
        "if",
        "then",
        "else",
        "$defs",
        "$ref",
        "$schema",
        "enum",
        "const",
        "pattern",
        "minLength",
        "maxLength",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "minItems",
        "maxItems",
        "uniqueItems",
        "minProperties",
        "maxProperties",
        "dependentRequired",
        "title",
        "description",
        "default",
        "examples",
        "deprecated",
        "readOnly",
        "writeOnly",
        "format",
        "contentEncoding",
        "contentMediaType",
        "$comment",
    }
)
_SUBSCHEMA_VALUE = (
    "additionalProperties",
    "propertyNames",
    "items",
    "not",
    "if",
    "then",
    "else",
)
_SUBSCHEMA_LIST = ("allOf", "anyOf", "oneOf")
_SUBSCHEMA_MAP = ("properties", "patternProperties")
_DRAFT = "https://json-schema.org/draft/2020-12/schema"


def _parse_defs_ref(ref: str) -> str | None:
    """解析 §4.1.1 的 `$ref` 唯一形式；不合形式返回 None。

    前缀必须是字面 `#/`；其后先百分号解码、再按 `/` 拆分、最后处理 `~1`/`~0`（RFC 6901 §6）；
    非法百分号序列与 `~` 后非 0/1 一律不合法。
    """
    if not ref.startswith("#/"):
        return None
    fragment = ref[2:]
    if re.search(r"%(?![0-9A-Fa-f]{2})", fragment):
        return None
    try:
        decoded = unquote_to_bytes(fragment).decode("utf-8")
    except UnicodeDecodeError:
        return None
    segments = decoded.split("/")
    if len(segments) != 2 or segments[0] != "$defs" or not segments[1]:
        return None
    name = segments[1]
    if re.search(r"~(?![01])", name):
        return None
    return name.replace("~1", "/").replace("~0", "~")


def _json_depth(value: Any) -> int:
    """JSON 值的嵌套深度（对象与数组各计一层，标量计 1）。"""
    depth = 0
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        item, level = stack.pop()
        depth = max(depth, level)
        if isinstance(item, dict):
            stack.extend((child, level + 1) for child in item.values())
        elif isinstance(item, list):
            stack.extend((child, level + 1) for child in item)
    return depth


def _closed_schema_problems(schema: Any) -> list[str]:
    """§4.1.1：封闭关键字子集、`$defs`/`$ref` 规则与 pattern 子集的全部违例（供生成器交叉核对）。

    另按 §4.1 建模清单文件的 JSON 嵌套深度：向量的 schema 会被包进清单对象（+1 层），
    超过 64 即判清单不合法。
    """
    problems: list[str] = []
    if 1 + _json_depth(schema) > 64:
        problems.append("清单的 JSON 嵌套深度超过 64（§4.1）")
    defs = schema.get("$defs") if isinstance(schema, dict) else None
    names = set(defs) if isinstance(defs, dict) else set()
    edges: dict[str, set[str]] = {}

    def check_node(node: Any, is_root: bool, position: str, owner: str | None) -> None:
        if isinstance(node, bool):
            return
        if not isinstance(node, dict):
            problems.append(f"{position}: 非对象/布尔 schema")
            return
        for keyword in node:
            if keyword not in _ALLOWED_KEYWORDS:
                problems.append(f"{position}: 不允许的关键字 {keyword!r}")
            if keyword in ("$defs", "$schema") and not is_root:
                problems.append(f"{position}: {keyword} 只允许出现在根上")
        dialect = node.get("$schema")
        if dialect is not None and dialect not in (_DRAFT, _DRAFT + "#"):
            problems.append(f"{position}: $schema {dialect!r} 不是 draft 2020-12")
        ref = node.get("$ref")
        if ref is not None:
            name = _parse_defs_ref(ref) if isinstance(ref, str) else None
            if name is None:
                problems.append(f"{position}: $ref {ref!r} 形式不合法")
            elif name not in names:
                problems.append(f"{position}: $ref {ref!r} 的目标不存在")
            elif owner is not None:
                edges.setdefault(owner, set()).add(name)
        if isinstance(node.get("items"), list):
            problems.append(f"{position}: items 只允许单个 schema")
        pattern = node.get("pattern")
        if isinstance(pattern, str):
            try:
                check_pattern(pattern)
            except PatternOutsideSubset as exc:
                problems.append(f"{position}: pattern {pattern!r} 越界（{exc.reason}）")
        props = node.get("patternProperties")
        if isinstance(props, dict):
            for key in props:
                if not isinstance(key, str):
                    continue  # 键类型在下方映射分支统一报错
                try:
                    check_pattern(key)
                except PatternOutsideSubset as exc:
                    problems.append(
                        f"{position}: patternProperties 键 {key!r} 越界（{exc.reason}）"
                    )
        types = node.get("type")
        if "type" in node:
            known = {
                "object",
                "array",
                "string",
                "number",
                "integer",
                "boolean",
                "null",
            }
            if isinstance(types, str):
                if types not in known:
                    problems.append(
                        f"{position}: type 取值 {types!r} 不是 2020-12 的已知类型"
                    )
            elif isinstance(types, list):
                if not types or any(t not in known for t in types):
                    problems.append(f"{position}: type 数组含未知类型")
                elif len(set(types)) != len(types):
                    problems.append(f"{position}: type 数组成员必须唯一")
            else:
                problems.append(f"{position}: type 必须是字符串或字符串数组")
        if "enum" in node and not isinstance(node["enum"], list):
            problems.append(f"{position}: enum 必须是数组")
        for keyword in (
            "title",
            "description",
            "$comment",
            "format",
            "contentEncoding",
            "contentMediaType",
        ):
            if keyword in node and not isinstance(node[keyword], str):
                problems.append(f"{position}: {keyword} 必须是字符串")
        if "examples" in node and not isinstance(node["examples"], list):
            problems.append(f"{position}: examples 必须是数组")
        if "uniqueItems" in node and not isinstance(node["uniqueItems"], bool):
            problems.append(f"{position}: uniqueItems 必须是布尔值")
        for keyword in ("deprecated", "readOnly", "writeOnly"):
            if keyword in node and not isinstance(node[keyword], bool):
                problems.append(f"{position}: {keyword} 必须是布尔值")
        if "pattern" in node and not isinstance(node["pattern"], str):
            problems.append(f"{position}: pattern 必须是字符串")
        for keyword in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"):
            value = node.get(keyword)
            if keyword in node and (isinstance(value, bool) or not isinstance(value, (int, float))):
                problems.append(f"{position}: {keyword} 必须是数值（布尔不是数值）")
        for keyword in (
            "minLength",
            "maxLength",
            "minItems",
            "maxItems",
            "minProperties",
            "maxProperties",
        ):
            value = node.get(keyword)
            if keyword not in node:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                problems.append(f"{position}: {keyword} 必须是非负整数")
            elif isinstance(value, float) and not value.is_integer():
                problems.append(f"{position}: {keyword} 必须是非负整数（整数值的浮点可接受）")
            elif value < 0:
                problems.append(f"{position}: {keyword} 必须是非负整数")
        if "required" in node:
            required_names = node["required"]
            if not isinstance(required_names, list) or any(
                not isinstance(n, str) for n in required_names
            ):
                problems.append(f"{position}: required 必须是字符串数组")
            elif len(set(required_names)) != len(required_names):
                problems.append(f"{position}: required 的成员必须唯一")
        if "dependentRequired" in node:
            deps = node["dependentRequired"]
            if not isinstance(deps, dict):
                problems.append(
                    f"{position}: dependentRequired 必须是「名字 → 字符串数组」映射"
                )
            else:
                for key, items in deps.items():
                    if not isinstance(items, list) or any(
                        not isinstance(n, str) for n in items
                    ):
                        problems.append(
                            f"{position}: dependentRequired[{key!r}] 必须是字符串数组"
                        )
                    elif len(set(items)) != len(items):
                        problems.append(
                            f"{position}: dependentRequired[{key!r}] 的成员必须唯一"
                        )
        for keyword in _SUBSCHEMA_VALUE:
            if keyword in node:
                check_node(node[keyword], False, f"{position}/{keyword}", owner)
        for keyword in _SUBSCHEMA_LIST:
            if keyword not in node:
                continue
            seq = node[keyword]
            if not isinstance(seq, list):
                problems.append(f"{position}/{keyword}: 必须是 schema 数组")
                continue
            if not seq:
                problems.append(f"{position}/{keyword}: 必须是 schema 数组（至少一项）")
            for index, item in enumerate(seq):
                check_node(item, False, f"{position}/{keyword}/{index}", owner)
        for keyword in _SUBSCHEMA_MAP:
            if keyword not in node:
                continue
            mapping = node[keyword]
            if not isinstance(mapping, dict):
                problems.append(
                    f"{position}/{keyword}: 必须是「名字 → schema」映射（对象）"
                )
                continue
            if any(not isinstance(key, str) for key in mapping):
                problems.append(f"{position}/{keyword}: 键必须是字符串")
            for key, sub in mapping.items():
                check_node(sub, False, f"{position}/{keyword}/{key}", owner)
        if is_root and "$defs" in node:
            root_defs = node["$defs"]
            if not isinstance(root_defs, dict):
                problems.append("/$defs: 必须是「名字 → schema」映射（对象）")
            else:
                for key, sub in root_defs.items():
                    check_node(sub, False, f"/$defs/{key}", key)

    check_node(schema, True, "", None)
    # $defs 引用成环
    state: dict[str, int] = {}

    def visit(start: str) -> None:
        state[start] = 1
        stack: list[tuple[str, list[str]]] = [(start, sorted(edges.get(start, ())))]
        while stack:
            current, targets = stack[-1]
            pending = False
            for target in targets:
                flag = state.get(target, 0)
                if flag == 1:
                    problems.append(f"$defs 引用成环（经 {target!r}）")
                    state[current] = 2
                    stack.pop()
                    pending = True
                    break
                if flag == 0:
                    state[target] = 1
                    stack.append((target, sorted(edges.get(target, ()))))
                    pending = True
                    break
            if not pending:
                state[current] = 2
                stack.pop()

    for name in sorted(names):
        if state.get(name, 0) == 0:
            visit(name)
    # 展开深度（§4.1.1）：迭代后序 + 记忆化；仅在图无环时计算（环上会不终止，且已判不合法）
    had_cycle = any("成环" in problem for problem in problems)
    def _children(node: dict[str, Any]) -> list[Any]:
        out: list[Any] = []
        for keyword in _SUBSCHEMA_VALUE:
            if keyword in node:
                out.append(node[keyword])
        for keyword in _SUBSCHEMA_LIST:
            seq = node.get(keyword)
            if isinstance(seq, list):
                out.extend(seq)
        for keyword in _SUBSCHEMA_MAP:
            mapping = node.get(keyword)
            if isinstance(mapping, dict):
                out.extend(mapping.values())
        ref = node.get("$ref")
        if isinstance(ref, str) and isinstance(defs, dict):
            name = _parse_defs_ref(ref)
            if name is not None and name in defs:
                out.append(defs[name])
        return out

    if not had_cycle:
        memo: dict[int, int] = {}
        stack: list[tuple[Any, bool]] = [(schema, False)]
        while stack:
            node, visited = stack.pop()
            if not isinstance(node, dict):
                memo[id(node)] = 1
                continue
            if visited:
                best = 0
                for child in _children(node):
                    best = max(best, memo.get(id(child), 1))
                memo[id(node)] = best + 1
                continue
            if id(node) in memo:
                continue
            stack.append((node, True))
            for child in _children(node):
                if id(child) not in memo:
                    stack.append((child, False))
        if memo.get(id(schema), 1) > 64:
            problems.append("schema 的展开深度超过 64（§4.1.1）")
    return problems


def pattern_vector() -> dict[str, Any]:
    """构建 config_schema_patterns 向量；生成器逐条交叉核对，不一致即失败。"""
    valid: list[dict[str, str]] = []
    for entry in PATTERN_VALID:
        try:
            check_pattern(entry["pattern"])
            translate_pattern(entry["pattern"])  # 合法 pattern 必须可转译
        except PatternOutsideSubset as exc:
            raise AssertionError(
                f"valid_patterns/{entry['pattern']!r}: {exc}"
            ) from None
        valid.append(entry)
    invalid: list[dict[str, str]] = []
    for entry in PATTERN_INVALID:
        pattern = entry.get("pattern")
        if pattern is None:
            # pattern_json：无法以普通字符串表达的输入（孤立代理项等），按原始 JSON 文本解析
            pattern = strict_loads(entry["pattern_json"])
        try:
            check_pattern(pattern)
        except PatternOutsideSubset as exc:
            if exc.reason != entry["reason"]:
                raise AssertionError(
                    f"invalid_patterns/{entry!r}: 期望 {entry['reason']}，实得 {exc.reason}"
                ) from None
        else:
            raise AssertionError(f"invalid_patterns/{entry!r}: 未被拒绝")
        invalid.append(entry)
    cases: list[dict[str, Any]] = []
    for case in PATTERN_MATCH_CASES:
        value = case.get("value")
        if "value_json" in case:
            value = strict_loads(case["value_json"])
        got = search(case["pattern"], value)
        if got != case["match"]:
            raise AssertionError(
                f"match_cases/{case['pattern']!r}: 期望 {case['match']}，实得 {got}"
            )
        cases.append(case)
    schema_cases: list[dict[str, Any]] = []
    for case in PATTERN_SCHEMA_CASES:
        problems = _closed_schema_problems(case["schema"])
        if case["manifest_valid"] and problems:
            raise AssertionError(
                f"schema_cases/{case['name']}: 清单应为有效却发现问题 {problems!r}"
            )
        if not case["manifest_valid"] and not problems:
            raise AssertionError(
                f"schema_cases/{case['name']}: 清单应为不合法，但未发现越界 pattern 或引用位置违例"
            )
        if ("config" in case) != ("config_valid" in case):
            raise AssertionError(
                f"schema_cases/{case['name']}: config 与 config_valid 必须成对给出"
            )
        schema_cases.append(case)
    return {
        "name": "config_schema_patterns",
        "kind": "pattern",
        "spec": "spec/connector-contract.md",
        "section": "§4.1.1",
        "description": "connector 契约 §4.1.1：config_schema 的 pattern / patternProperties 可移植子集——合法/越界 pattern、手写的语义匹配期望、schema 遍历用例",
        "valid_patterns": valid,
        "invalid_patterns": invalid,
        "match_cases": cases,
        "schema_cases": schema_cases,
    }


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

    for vec in URI_VECTORS:
        cases = []
        for case in vec["cases"]:
            got = normalize_file_uri(case["input"])
            if got != case["expect"]:
                raise AssertionError(
                    f"{vec['name']}: {case['input']!r} → {got!r}，期望 {case['expect']!r}"
                )
            if normalize_file_uri(got) != got:
                raise AssertionError(
                    f"{vec['name']}: {case['input']!r} 的规范化结果不是不动点"
                )
            cases.append({"input": case["input"], "normalized": got})
        invalid_cases = []
        for input_ in vec["invalid_cases"]:
            try:
                normalize_file_uri(input_)
            except Reject as rej:
                if rej.code != VALIDATION:
                    raise AssertionError(
                        f"{vec['name']}: {input_!r} 得到 {rej.code}，期望 {VALIDATION}"
                    ) from None
            else:
                raise AssertionError(f"{vec['name']}: {input_!r} 未被拒绝")
            invalid_cases.append({"input": input_, "code": VALIDATION})
        files[f"{vec['name']}.json"] = dump_json(
            {
                "name": vec["name"],
                "kind": "uri",
                "description": vec["description"],
                "cases": cases,
                "invalid_cases": invalid_cases,
            }
        )

    for vec in INVALID_VECTORS:
        for case in vec["cases"]:
            try:
                obj = (
                    strict_loads(case["input_json"])
                    if "input_json" in case
                    else case["input"]
                )
                CHECKERS[case["object_kind"]](obj, case["contract"])
            except Reject as rej:
                if rej.code != case["code"] or (
                    "path" in case and rej.path != case["path"]
                ):
                    raise AssertionError(
                        f"{vec['name']}/{case['name']}: got {rej.code} at {rej.path!r}"
                    ) from None
            else:
                raise AssertionError(f"{vec['name']}/{case['name']}: not rejected")
        files[f"{vec['name']}.json"] = dump_json(
            {
                "name": vec["name"],
                "kind": "invalid",
                "description": vec["description"],
                "cases": vec["cases"],
            }
        )

    files["config_schema_patterns.json"] = dump_json(pattern_vector())

    manifest = {
        "contract": "dpe1",
        "spec": "spec/hash-contract-1.md",
        "status": "draft",
        "file_types": list(RECOMMENDED_FILE_TYPES),
        "category_content_fields": {
            c: list(f) for c, f in CATEGORY_CONTENT_FIELDS.items()
        },
        "provenance": {
            "generator": "scripts/gen_vectors.py",
            "note": "向量由规范参考实现生成（plan §1：向量归本仓库）；dpe2 仅用于升级演练，定义见 vectors/README.md；file_uri_normalization 的规范依据是 core.md §1.1，config_schema_patterns 的规范依据是 connector 契约 §4.1.1",
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
