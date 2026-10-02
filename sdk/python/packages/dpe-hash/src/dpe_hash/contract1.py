"""hash 契约 1（``dpe1:``）——spec/hash-contract-1.md 的实现。

契约 1 是三层同构的 tree：元素、页、文档三层对象统一为

    H(obj) = "dpe1:" + hex(sha256(utf8(JCS(norm(obj)))))

``norm``：递归删除值为 null 的键（缺省 ≡ null）；三层 metadata 字段缺省视同 ``{}``（§3.2）。
三层对象都是封闭 schema，未定义字段一律拒绝（§3.4）。页没有页号，顺序只由上层数组表达。

本模块是唯一的规范化与校验实现，全部公开函数都只是它的不同入口：

- ``content_hash`` / ``page_hash`` / ``doc_hash``：逐层，用已有的子 hash 上溯（内核按页增量维护）；
- ``object_hash``：直接接受线上原像（页带 ``elements``、文档带 ``pages``，服务端校验用）；
- ``document_hashes``：展开视图一次算出三层；
- ``children``：对象引用的下一层（缺失清单）。

``contract`` 参数用于契约 1 §6 的原位重算；``dpe2`` 只在显式传入时接受（见 ``DRILL_CONTRACT``）。
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Collection, Mapping, Sequence
from typing import Any

from dpe_hash.constants import (
    CATEGORY_CONTENT_FIELDS,
    CONTRACT,
    DRILL_CONTRACT,
    FILE_TYPES,
    SUPPORTED_CONTRACTS,
)
from dpe_hash.errors import (
    CategoryUnknownError,
    ContractUnsupportedError,
    FileTypeUnknownError,
    UndefinedFieldError,
    ValidationError,
)
from dpe_hash.jcs import canonical, pointer
from dpe_hash.models import (
    DocumentFields,
    DocumentHashes,
    ElementObject,
    ExpandedDocument,
    ObjectKind,
    PageFields,
    PageHashes,
)

__all__ = [
    "blob_ref",
    "children",
    "content_hash",
    "doc_hash",
    "document_hashes",
    "object_hash",
    "page_hash",
    "parse_blob_ref",
    "parse_hash",
]

#: 各契约的摘要前缀字节：dpe2 = ASCII "dpe2" ‖ 原像（vectors/README.md）
_SALT = {"dpe1": b"", DRILL_CONTRACT: b"dpe2"}
_FILE_TYPES = frozenset(FILE_TYPES)
_HEX64 = re.compile(r"[0-9a-f]{64}")
#: 本包认识的全部契约（真实契约 + 演练契约）；hash 值的前缀不在其中即 DPE_CONTRACT_UNSUPPORTED
_KNOWN_CONTRACTS = frozenset({*SUPPORTED_CONTRACTS, DRILL_CONTRACT})
_BLOB_PREFIX = "sha256:"

_PAGE_FIELDS = frozenset({"title", "page_metadata"})
_DOCUMENT_FIELDS = frozenset({"file_type", "title", "doc_metadata"})


# ---------------------------------------------------------------------------
# 值格式（§1）
# ---------------------------------------------------------------------------


def _salt(contract: str) -> bytes:
    if isinstance(contract, str) and (
        contract in SUPPORTED_CONTRACTS or contract == DRILL_CONTRACT
    ):
        return _SALT[contract]
    raise ContractUnsupportedError(f"不支持的 hash 契约：{contract!r}")


def _digest(preimage: str, contract: str, salt: bytes) -> str:
    return f"{contract}:{hashlib.sha256(salt + preimage.encode('utf-8')).hexdigest()}"


def _split_hash(value: object, accepted: frozenset[str], at: str) -> tuple[str, str]:
    if not isinstance(value, str):
        raise ValidationError(f"hash 值必须是字符串，实际为 {type(value).__name__}", at)
    prefix, sep, hexpart = value.partition(":")
    if not sep:
        raise ContractUnsupportedError("hash 值缺少契约前缀", at)
    if prefix not in accepted:
        raise ContractUnsupportedError(f"未知或不支持的契约前缀：{prefix!r}", at)
    if not _HEX64.fullmatch(hexpart):
        raise ValidationError("hash 值的摘要部分必须是 64 位小写 hex", at)
    return prefix, hexpart


def _blob_hex(value: object, at: str) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"blob 引用必须是字符串，实际为 {type(value).__name__}", at)
    if not value.startswith(_BLOB_PREFIX) or not _HEX64.fullmatch(value[len(_BLOB_PREFIX) :]):
        raise ValidationError("blob 引用必须是 sha256:<64 位小写 hex>", at)
    return value[len(_BLOB_PREFIX) :]


def parse_hash(value: str, contracts: Collection[str] = SUPPORTED_CONTRACTS) -> tuple[str, str]:
    """校验并拆分 hash 值，返回 ``(契约, 64 位 hex)``。

    无前缀或前缀不在 ``contracts`` 内抛 ``ContractUnsupportedError``（契约 1 §1）；摘要格式不对抛
    ``ValidationError``。默认只接受 ``SUPPORTED_CONTRACTS``，升级演练需显式传入
    ``(*SUPPORTED_CONTRACTS, DRILL_CONTRACT)``。``contracts`` 是契约的集合，不能是单个字符串；
    其中出现本包不认识的契约抛 ``ContractUnsupportedError``。
    """
    if isinstance(contracts, str):
        raise TypeError('contracts 必须是契约的集合（如 ("dpe1",)），不能是单个字符串')
    accepted = frozenset(contracts)
    unknown = accepted - _KNOWN_CONTRACTS
    if unknown:
        raise ContractUnsupportedError(f"不支持的 hash 契约：{sorted(unknown)!r}")
    return _split_hash(value, accepted, "")


def parse_blob_ref(value: str) -> str:
    """校验 blob 引用 ``sha256:<64 位小写 hex>``，返回 hex 部分；不合法抛 ``ValidationError``。"""
    return _blob_hex(value, "")


def blob_ref(data: bytes | bytearray | memoryview) -> str:
    """blob 原始字节的引用 ``"sha256:<hex>"``（契约 1 §1）。"""
    return _BLOB_PREFIX + hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# 三层对象的校验与规范化（§3–§5），返回 JCS 原像
# ---------------------------------------------------------------------------


def _mapping(value: object, at: str, what: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValidationError(f"{what}必须是 JSON 对象，实际为 {type(value).__name__}", at)
    return value


def _closed(obj: Mapping[str, Any], allowed: frozenset[str], at: str, what: str) -> None:
    extra = sorted(str(k) for k in obj if k not in allowed)
    if extra:
        raise UndefinedFieldError(f"{what}不允许字段 {extra[0]!r}", at + pointer(extra[0]))


def _opt_str(obj: Mapping[str, Any], field: str, at: str) -> str | None:
    value = obj.get(field)
    if value is not None and not isinstance(value, str):
        raise ValidationError(
            f"{field} 必须是字符串或 null，实际为 {type(value).__name__}", at + pointer(field)
        )
    return value


def _metadata(obj: Mapping[str, Any], field: str, at: str) -> Mapping[str, Any]:
    value = obj.get(field)
    if value is None:
        return {}  # 缺省视同 {}（§3.2）
    return _mapping(value, at + pointer(field), f"{field} ")


def _hash_list(value: object, contract: str, at: str) -> list[str]:
    """子对象 hash 列表：每项必须是同一契约下的合法 hash 值（契约 1 §5）。

    前缀不是受支持的契约 → ``ContractUnsupportedError``；是受支持的契约、但与本对象的契约不同
    （契约混用）→ ``ValidationError``。「受支持」= ``SUPPORTED_CONTRACTS`` 加上本次调用显式选择的
    契约：演练契约 dpe2 只在被选中时才算受支持，因此 dpe1 对象引用 dpe2 子 hash 与生产服务端一样
    得到 ``DPE_CONTRACT_UNSUPPORTED``。
    """
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise ValidationError(f"必须是 hash 值数组，实际为 {type(value).__name__}", at)
    accepted = frozenset({*SUPPORTED_CONTRACTS, contract})
    for i, item in enumerate(value):
        prefix, _ = _split_hash(item, accepted, at + pointer(i))
        if prefix != contract:
            raise ValidationError(
                f"子对象 hash 的契约 {prefix} 与本对象的契约 {contract} 不一致", at + pointer(i)
            )
    return list(value)


def _element_preimage(element: object, at: str) -> str:
    el = _mapping(element, at, "元素对象")
    if "category" not in el:
        raise ValidationError("元素对象缺少 category", at)
    category = el["category"]
    if not isinstance(category, str):
        raise ValidationError("category 必须是字符串", at + pointer("category"))
    allowed = CATEGORY_CONTENT_FIELDS.get(category)
    if allowed is None:  # 封闭枚举，禁止退化为 text-only（§4）
        raise CategoryUnknownError(f"未知 category：{category!r}", at + pointer("category"))
    _closed(el, frozenset({"category", "metadata", *allowed}), at, f"{category} 元素")
    obj: dict[str, Any] = {"category": category}
    for field in allowed:
        value = _opt_str(el, field, at)
        if value is not None:
            obj[field] = value
    if "image_blob" in obj:
        _blob_hex(obj["image_blob"], at + pointer("image_blob"))
    obj["metadata"] = _metadata(el, "metadata", at)
    return canonical(obj, strip_nulls=True, at=at)


def _page_preimage(
    page: Mapping[str, Any], element_hashes: object, contract: str, at: str, *, with_children: bool
) -> str:
    """``with_children``：``page`` 自带 ``elements`` 键（线上原像或展开视图），其值由调用方处理。"""
    allowed = _PAGE_FIELDS | {"elements"} if with_children else _PAGE_FIELDS
    if not with_children and "elements" in page:
        raise UndefinedFieldError(
            "page_hash 的页字段不得带 elements（子 hash 单独传入；线上原像请用 object_hash）",
            at + pointer("elements"),
        )
    _closed(page, allowed, at, "页对象")
    obj: dict[str, Any] = {}
    title = _opt_str(page, "title", at)
    if title is not None:
        obj["title"] = title
    obj["page_metadata"] = _metadata(page, "page_metadata", at)
    obj["elements"] = _hash_list(element_hashes, contract, at + pointer("elements"))
    return canonical(obj, strip_nulls=True, at=at)


def _document_preimage(
    document: Mapping[str, Any], page_hashes: object, contract: str, at: str, *, with_children: bool
) -> str:
    allowed = _DOCUMENT_FIELDS | {"pages"} if with_children else _DOCUMENT_FIELDS
    if not with_children and "pages" in document:
        raise UndefinedFieldError(
            "doc_hash 的文档字段不得带 pages（子 hash 单独传入；线上原像请用 object_hash）",
            at + pointer("pages"),
        )
    _closed(document, allowed, at, "文档对象")
    if "file_type" not in document:
        raise ValidationError("文档对象缺少 file_type", at)
    file_type = document["file_type"]
    if not isinstance(file_type, str):
        raise ValidationError("file_type 必须是字符串", at + pointer("file_type"))
    if file_type not in _FILE_TYPES:
        raise FileTypeUnknownError(f"未知 file_type：{file_type!r}", at + pointer("file_type"))
    obj: dict[str, Any] = {"file_type": file_type}
    title = _opt_str(document, "title", at)
    if title is not None:
        obj["title"] = title
    obj["doc_metadata"] = _metadata(document, "doc_metadata", at)
    obj["pages"] = _hash_list(page_hashes, contract, at + pointer("pages"))
    return canonical(obj, strip_nulls=True, at=at)


def _required(obj: Mapping[str, Any], field: str, at: str, what: str) -> Any:
    if field not in obj:
        raise ValidationError(f"{what}缺少 {field}", at)
    return obj[field]


def _preimage(obj: object, kind: ObjectKind, contract: str) -> str:
    """线上原像的 JCS 原像（``object_hash`` / ``children`` 共用）。"""
    if kind == "element":
        return _element_preimage(obj, "")
    if kind == "page":
        page = _mapping(obj, "", "页对象")
        elements = _required(page, "elements", "", "页对象")
        return _page_preimage(page, elements, contract, "", with_children=True)
    if kind == "document":
        document = _mapping(obj, "", "文档对象")
        pages = _required(document, "pages", "", "文档对象")
        return _document_preimage(document, pages, contract, "", with_children=True)
    raise ValueError(f"未知的对象层级：{kind!r}")


# ---------------------------------------------------------------------------
# 公开入口
# ---------------------------------------------------------------------------


def content_hash(element: ElementObject, contract: str = CONTRACT) -> str:
    """元素对象的 ``content_hash``（契约 1 §4）。"""
    salt = _salt(contract)
    return _digest(_element_preimage(element, ""), contract, salt)


def page_hash(page: PageFields, element_hashes: Sequence[str], contract: str = CONTRACT) -> str:
    """``page_hash``（契约 1 §5）：页自身字段 + 已有的 content_hash 列表（按页内顺序）。

    ``page`` 带 ``elements`` 键即报错，避免两份元素列表的歧义；``element_hashes`` 中的错误
    路径记为 ``/elements/<i>``。
    """
    salt = _salt(contract)
    pre = _page_preimage(
        _mapping(page, "", "页对象"), element_hashes, contract, "", with_children=False
    )
    return _digest(pre, contract, salt)


def doc_hash(document: DocumentFields, page_hashes: Sequence[str], contract: str = CONTRACT) -> str:
    """``doc_hash``（契约 1 §5）：文档自身字段 + 已有的 page_hash 列表（按页序），不需加载元素。

    ``document`` 带 ``pages`` 键即报错，避免两份页序的歧义；``page_hashes`` 中的错误路径记为
    ``/pages/<i>``。
    """
    salt = _salt(contract)
    pre = _document_preimage(
        _mapping(document, "", "文档对象"), page_hashes, contract, "", with_children=False
    )
    return _digest(pre, contract, salt)


def object_hash(obj: Mapping[str, Any], kind: ObjectKind, contract: str = CONTRACT) -> str:
    """线上原像的 hash：一步完成封闭 schema 校验、规范化、JCS 与摘要。

    页对象须带 ``elements``、文档对象须带 ``pages``（子 hash 列表，契约须与 ``contract`` 一致）。
    对同一文档，结果与 ``content_hash`` / ``page_hash`` / ``doc_hash`` 逐层相等。
    """
    salt = _salt(contract)
    return _digest(_preimage(obj, kind, contract), contract, salt)


def children(obj: Mapping[str, Any], kind: ObjectKind, contract: str = CONTRACT) -> list[str]:
    """线上原像引用的下一层（先按 ``object_hash`` 的规则完整校验）。

    页 → ``elements`` 的 content_hash；文档 → ``pages`` 的 page_hash；元素 → ``image_blob``
    引用（没有则为空）。按出现顺序返回，重复保留，去重由调用方决定。
    """
    _salt(contract)
    _preimage(obj, kind, contract)
    if kind == "element":
        blob = obj.get("image_blob")
        return [blob] if isinstance(blob, str) else []
    return list(obj["elements" if kind == "page" else "pages"])


def document_hashes(document: ExpandedDocument, contract: str = CONTRACT) -> DocumentHashes:
    """展开视图（vectors/README.md）的三层 hash。

    结果形如 ``{"doc_hash", "pages": [{"page_hash", "elements"}…]}``，同向量的 ``expected``。

    页序即 ``pages`` 数组顺序，页内元素序即 ``elements`` 数组顺序。用于整篇计算与契约 1 §6
    的原位重算（按任一受支持契约重算已存的骨架与内容，结果按位置一一对应）。
    """
    salt = _salt(contract)
    doc = _mapping(document, "", "文档")
    pages = _required(doc, "pages", "", "文档")
    if not isinstance(pages, (list, tuple)):
        raise ValidationError("pages 必须是数组", pointer("pages"))
    out_pages: list[PageHashes] = []
    page_hashes: list[str] = []
    for i, raw_page in enumerate(pages):
        at = pointer("pages", i)
        page = _mapping(raw_page, at, "页")
        elements = _required(page, "elements", at, "页")
        if not isinstance(elements, (list, tuple)):
            raise ValidationError("elements 必须是数组", at + pointer("elements"))
        element_hashes = [
            _digest(_element_preimage(el, at + pointer("elements", j)), contract, salt)
            for j, el in enumerate(elements)
        ]
        ph = _digest(
            _page_preimage(page, element_hashes, contract, at, with_children=True), contract, salt
        )
        page_hashes.append(ph)
        out_pages.append({"page_hash": ph, "elements": element_hashes})
    dh = _digest(
        _document_preimage(doc, page_hashes, contract, "", with_children=True), contract, salt
    )
    return {"doc_hash": dh, "pages": out_pages}
