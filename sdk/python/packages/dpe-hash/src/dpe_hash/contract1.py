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
带子 hash 列表的入口（``page_hash`` / ``doc_hash`` / ``object_hash`` / ``children``）另有可选的
关键字参数 ``contracts``：本次校验接受的「受支持契约」基准集合，缺省 ``SUPPORTED_CONTRACTS``。
声明多契约的服务端（core §3.1）传自己的声明集合，于是 dpe1 对象引用 dpe2 子 hash 由「不受支持」
改判契约混用。
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Collection, Mapping, Sequence
from typing import Any, TypeVar

from dpe_hash.constants import (
    CATEGORY_CONTENT_FIELDS,
    CONTRACT,
    DRILL_CONTRACT,
    KNOWN_CONTRACTS,
    SUPPORTED_CONTRACTS,
)
from dpe_hash.errors import (
    CategoryUnknownError,
    ContractUnsupportedError,
    FileTypeInvalidError,
    InvalidUnicodeError,
    UndefinedFieldError,
    ValidationError,
)
from dpe_hash.file_type import is_valid_file_type
from dpe_hash.jcs import canonical, has_invalid_unicode, pointer, utf16_key
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
_HEX64 = re.compile(r"[0-9a-f]{64}")
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
    unknown = accepted - KNOWN_CONTRACTS
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
    extra = sorted((str(k) for k in obj if k not in allowed), key=utf16_key)
    if extra:
        raise UndefinedFieldError(f"{what}不允许字段 {extra[0]!r}", at + pointer(extra[0]))


def _string_part(obj: Mapping[str, Any], field: str, at: str) -> str | None:
    """可选字符串字段的 JCS 片段；缺省或 null 返回 None（不进原像）。"""
    value = obj.get(field)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValidationError(
            f"{field} 必须是字符串或 null，实际为 {type(value).__name__}", at + pointer(field)
        )
    return canonical(value, at=at + pointer(field))


def _metadata_part(obj: Mapping[str, Any], field: str, at: str) -> str:
    """metadata 字段的 JCS 片段：缺省视同 ``{}``，递归删 null 键，并完整校验全部值（§3.2）。"""
    value = obj.get(field)
    if value is None:
        return "{}"
    _mapping(value, at + pointer(field), f"{field} ")
    return canonical(value, strip_nulls=True, at=at + pointer(field))


def _base_contracts(contracts: Collection[str]) -> frozenset[str]:
    """本次校验的「受支持契约」基准集合（``parse_hash`` 的参数校验同形）。

    实际接受的集合是它与本次对象契约的并集（``_hash_list``）：演练契约 dpe2 只在被选为本次契约时
    才算受支持；集合里出现本包不认识的契约直接拒绝。默认基准是 ``SUPPORTED_CONTRACTS``——
    向量与已发布行为因此不变；声明多契约的服务端（core §3.1）传入自己的声明集合，
    于是「受支持但与本对象不同」的契约按契约混用判 ``DPE_VALIDATION``。
    """
    if isinstance(contracts, str):
        raise TypeError('contracts 必须是契约的集合（如 ("dpe1",)），不能是单个字符串')
    accepted = frozenset(contracts)
    unknown = accepted - KNOWN_CONTRACTS
    if unknown:
        raise ContractUnsupportedError(f"不支持的 hash 契约：{sorted(unknown)!r}")
    return accepted


def _hash_list(value: object, contract: str, at: str, contracts: Collection[str]) -> list[str]:
    """子对象 hash 列表：每项必须是同一契约下的合法 hash 值（契约 1 §5）。

    接受集合 = ``contracts``（基准，缺省 ``SUPPORTED_CONTRACTS``）∪ {本次对象的契约}：前缀不在其中
    → ``ContractUnsupportedError``；在其中、但与本对象的契约不同（契约混用）→ ``ValidationError``。
    基准确认了「同意接受哪些契约」，并集里加入本次契约则让演练契约 dpe2 在被选中时才算受支持。
    """
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise ValidationError(f"必须是 hash 值数组，实际为 {type(value).__name__}", at)
    accepted = _base_contracts(contracts) | {contract}
    for i, item in enumerate(value):
        prefix, _ = _split_hash(item, accepted, at + pointer(i))
        if prefix != contract:
            raise ValidationError(
                f"子对象 hash 的契约 {prefix} 与本对象的契约 {contract} 不一致", at + pointer(i)
            )
    return list(value)


def _hash_list_part(value: object, contract: str, at: str, contracts: Collection[str]) -> str:
    """子 hash 列表的 JCS 片段（已校验的 hash 值只含 ASCII 字母数字与冒号，无需转义）。"""
    return "[" + ",".join(f'"{h}"' for h in _hash_list(value, contract, at, contracts)) + "]"


def _assemble(parts: Mapping[str, str]) -> str:
    """把各字段的 JCS 片段拼成对象原像。键都是规范定义的 ASCII 字段名，码点序即 UTF-16 码元序。"""
    return "{" + ",".join(f'"{k}":{parts[k]}' for k in sorted(parts)) + "}"


# 以下按 core.md §2.8 的顺序校验：形状 → category（元素）→ 封闭 schema → 按表中字段顺序
# 逐字段完整校验。每个字段校验通过即产出 JCS 片段，最后拼装原像，不对对象做第二遍遍历。


def _element_preimage(element: object, at: str) -> str:
    el = _mapping(element, at, "元素对象")
    if el.get("category") is None:  # 必有字段为 null 视同缺省（§2.8 第 1 步）
        raise ValidationError("元素对象缺少 category", at)
    category = el["category"]
    if not isinstance(category, str):
        raise ValidationError("category 必须是字符串", at + pointer("category"))
    allowed = CATEGORY_CONTENT_FIELDS.get(category)
    if allowed is None:  # 封闭枚举，禁止退化为 text-only（§4）；先于封闭 schema（§2.8）
        raise CategoryUnknownError(f"未知 category：{category!r}", at + pointer("category"))
    _closed(el, frozenset({"category", "metadata", *allowed}), at, f"{category} 元素")
    parts = {"category": canonical(category, at=at + pointer("category"))}
    for field in allowed:
        part = _string_part(el, field, at)
        if part is None:
            continue
        if field == "blob":
            _blob_hex(el[field], at + pointer(field))
        parts[field] = part
    parts["metadata"] = _metadata_part(el, "metadata", at)
    return _assemble(parts)


def _page_parts(page: Mapping[str, Any], at: str, *, with_children: bool) -> dict[str, str]:
    """页对象除 ``elements`` 外的字段片段。``with_children``：``page`` 自带 ``elements`` 键
    （线上原像或展开视图），其值由调用方处理。"""
    if not with_children and "elements" in page:
        raise UndefinedFieldError(
            "page_hash 的页字段不得带 elements（子 hash 单独传入；线上原像请用 object_hash）",
            at + pointer("elements"),
        )
    _closed(page, _PAGE_FIELDS | {"elements"} if with_children else _PAGE_FIELDS, at, "页对象")
    parts: dict[str, str] = {}
    title = _string_part(page, "title", at)
    if title is not None:
        parts["title"] = title
    parts["page_metadata"] = _metadata_part(page, "page_metadata", at)
    return parts


def _page_preimage(
    page: Mapping[str, Any],
    element_hashes: object,
    contract: str,
    at: str,
    *,
    with_children: bool,
    contracts: Collection[str],
) -> str:
    parts = _page_parts(page, at, with_children=with_children)
    parts["elements"] = _hash_list_part(
        element_hashes, contract, at + pointer("elements"), contracts
    )
    return _assemble(parts)


def _document_parts(document: Mapping[str, Any], at: str, *, with_children: bool) -> dict[str, str]:
    """文档对象除 ``pages`` 外的字段片段（``with_children`` 同 ``_page_parts``）。"""
    if document.get("file_type") is None:  # 形状先于封闭 schema；null 视同缺省
        raise ValidationError("文档对象缺少 file_type", at)
    if not with_children and "pages" in document:
        raise UndefinedFieldError(
            "doc_hash 的文档字段不得带 pages（子 hash 单独传入；线上原像请用 object_hash）",
            at + pointer("pages"),
        )
    allowed = _DOCUMENT_FIELDS | {"pages"} if with_children else _DOCUMENT_FIELDS
    _closed(document, allowed, at, "文档对象")
    file_type = document["file_type"]
    if not isinstance(file_type, str):
        raise ValidationError("file_type 必须是字符串", at + pointer("file_type"))
    if not is_valid_file_type(file_type):
        raise FileTypeInvalidError(f"file_type 不合语法：{file_type!r}", at + pointer("file_type"))
    parts = {"file_type": canonical(file_type)}
    title = _string_part(document, "title", at)
    if title is not None:
        parts["title"] = title
    parts["doc_metadata"] = _metadata_part(document, "doc_metadata", at)
    return parts


def _document_preimage(
    document: Mapping[str, Any],
    page_hashes: object,
    contract: str,
    at: str,
    *,
    with_children: bool,
    contracts: Collection[str],
) -> str:
    parts = _document_parts(document, at, with_children=with_children)
    parts["pages"] = _hash_list_part(page_hashes, contract, at + pointer("pages"), contracts)
    return _assemble(parts)


def _required(obj: Mapping[str, Any], field: str, at: str, what: str) -> Any:
    if obj.get(field) is None:  # null 视同缺省（§2.8 第 1 步）
        raise ValidationError(f"{what}缺少 {field}", at)
    return obj[field]


def _preimage(obj: object, kind: ObjectKind, contract: str, contracts: Collection[str]) -> str:
    """线上原像的 JCS 原像（``object_hash`` / ``children`` 共用）。"""
    if kind == "element":
        return _element_preimage(obj, "")
    if kind == "page":
        page = _mapping(obj, "", "页对象")
        elements = _required(page, "elements", "", "页对象")
        return _page_preimage(page, elements, contract, "", with_children=True, contracts=contracts)
    if kind == "document":
        document = _mapping(obj, "", "文档对象")
        pages = _required(document, "pages", "", "文档对象")
        return _document_preimage(
            document, pages, contract, "", with_children=True, contracts=contracts
        )
    raise ValueError(f"未知的对象层级：{kind!r}")


_T = TypeVar("_T")


def _ijson_first(top: object, compute: Callable[[], _T]) -> _T:
    """core §2.8 第 0 步：I-JSON 违例先于其他所有校验，位置为对象自身。

    孤立代理项在逐字段序列化时才会遇到。若在此之前已因 ``DPE_CATEGORY_UNKNOWN`` /
    ``DPE_CONTRACT_UNSUPPORTED`` 失败，再整体扫描一次输入；只在出错时扫描，合法输入不多花一遍。
    （同为 ``DPE_VALIDATION`` 的更早违例无需改判：多处违例时报告哪一处由实现决定。）
    """
    message = "字符串或对象键含孤立代理项，报文不是 I-JSON（core §2.8 第 0 步）"
    try:
        return compute()
    except InvalidUnicodeError:
        raise InvalidUnicodeError(message) from None
    except (CategoryUnknownError, ContractUnsupportedError):
        if has_invalid_unicode(top):
            raise InvalidUnicodeError(message) from None
        raise


# ---------------------------------------------------------------------------
# 公开入口
# ---------------------------------------------------------------------------


def content_hash(element: ElementObject, contract: str = CONTRACT) -> str:
    """元素对象的 ``content_hash``（契约 1 §4）。"""
    salt = _salt(contract)
    return _digest(_ijson_first(element, lambda: _element_preimage(element, "")), contract, salt)


def page_hash(
    page: PageFields,
    element_hashes: Sequence[str],
    contract: str = CONTRACT,
    *,
    contracts: Collection[str] = SUPPORTED_CONTRACTS,
) -> str:
    """``page_hash``（契约 1 §5）：页自身字段 + 已有的 content_hash 列表（按页内顺序）。

    ``page`` 带 ``elements`` 键即报错，避免两份元素列表的歧义；``element_hashes`` 中的错误
    路径记为 ``/elements/<i>``。``contracts`` 是本次校验接受的「受支持契约」基准集合
    （见 ``_base_contracts``）：声明多契约的服务端传自己的声明集合。
    """
    salt = _salt(contract)
    pre = _ijson_first(
        (page, element_hashes),
        lambda: _page_preimage(
            _mapping(page, "", "页对象"),
            element_hashes,
            contract,
            "",
            with_children=False,
            contracts=contracts,
        ),
    )
    return _digest(pre, contract, salt)


def doc_hash(
    document: DocumentFields,
    page_hashes: Sequence[str],
    contract: str = CONTRACT,
    *,
    contracts: Collection[str] = SUPPORTED_CONTRACTS,
) -> str:
    """``doc_hash``（契约 1 §5）：文档自身字段 + 已有的 page_hash 列表（按页序），不需加载元素。

    ``document`` 带 ``pages`` 键即报错，避免两份页序的歧义；``page_hashes`` 中的错误路径记为
    ``/pages/<i>``；``contracts`` 同 ``page_hash``。
    """
    salt = _salt(contract)
    pre = _ijson_first(
        (document, page_hashes),
        lambda: _document_preimage(
            _mapping(document, "", "文档对象"),
            page_hashes,
            contract,
            "",
            with_children=False,
            contracts=contracts,
        ),
    )
    return _digest(pre, contract, salt)


def object_hash(
    obj: Mapping[str, Any],
    kind: ObjectKind,
    contract: str = CONTRACT,
    *,
    contracts: Collection[str] = SUPPORTED_CONTRACTS,
) -> str:
    """线上原像的 hash：一步完成封闭 schema 校验、规范化、JCS 与摘要。

    页对象须带 ``elements``、文档对象须带 ``pages``（子 hash 列表，契约须与 ``contract`` 一致）。
    对同一文档，结果与 ``content_hash`` / ``page_hash`` / ``doc_hash`` 逐层相等；``contracts``
    同 ``page_hash``。
    """
    salt = _salt(contract)
    return _digest(
        _ijson_first(obj, lambda: _preimage(obj, kind, contract, contracts)), contract, salt
    )


def children(
    obj: Mapping[str, Any],
    kind: ObjectKind,
    contract: str = CONTRACT,
    *,
    contracts: Collection[str] = SUPPORTED_CONTRACTS,
) -> list[str]:
    """线上原像引用的下一层（先按 ``object_hash`` 的规则完整校验）。

    页 → ``elements`` 的 content_hash；文档 → ``pages`` 的 page_hash；元素 → ``blob``
    引用（没有则为空）。按出现顺序返回，重复保留，去重由调用方决定；``contracts`` 同
    ``page_hash``。
    """
    _salt(contract)
    _ijson_first(obj, lambda: _preimage(obj, kind, contract, contracts))
    if kind == "element":
        blob = obj.get("blob")
        return [blob] if isinstance(blob, str) else []
    return list(obj["elements" if kind == "page" else "pages"])


def document_hashes(document: ExpandedDocument, contract: str = CONTRACT) -> DocumentHashes:
    """展开视图（vectors/README.md）的三层 hash。

    结果形如 ``{"doc_hash", "pages": [{"page_hash", "elements"}…]}``，同向量的 ``expected``。

    页序即 ``pages`` 数组顺序，页内元素序即 ``elements`` 数组顺序。用于整篇计算与契约 1 §6
    的原位重算（按任一受支持契约重算已存的骨架与内容，结果按位置一一对应）。展开视图的子 hash
    都由本函数算出、必与本次契约相同，因此没有 ``contracts`` 参数。
    """
    salt = _salt(contract)
    return _ijson_first(document, lambda: _document_hashes(document, contract, salt))


def _document_hashes(document: object, contract: str, salt: bytes) -> DocumentHashes:
    """与线上请求同序（core §2.8）：文档自身字段 → 各页自身字段 → 各页的元素。"""
    doc = _mapping(document, "", "文档")
    pages = _required(doc, "pages", "", "文档")
    doc_parts = _document_parts(doc, "", with_children=True)
    if not isinstance(pages, (list, tuple)):
        raise ValidationError("pages 必须是数组", pointer("pages"))
    page_inputs: list[tuple[str, dict[str, str], Sequence[object]]] = []
    for i, raw_page in enumerate(pages):
        at = pointer("pages", i)
        page = _mapping(raw_page, at, "页")
        elements = _required(page, "elements", at, "页")
        page_parts = _page_parts(page, at, with_children=True)
        if not isinstance(elements, (list, tuple)):
            raise ValidationError("elements 必须是数组", at + pointer("elements"))
        page_inputs.append((at, page_parts, elements))
    out_pages: list[PageHashes] = []
    page_hashes: list[str] = []
    for at, page_parts, elements in page_inputs:
        element_hashes = [
            _digest(_element_preimage(el, at + pointer("elements", j)), contract, salt)
            for j, el in enumerate(elements)
        ]
        # 子 hash 都是本函数按本次契约算出的，基准集合只在形式上参与（契约必在并集内）
        page_parts["elements"] = _hash_list_part(
            element_hashes, contract, at + pointer("elements"), SUPPORTED_CONTRACTS
        )
        ph = _digest(_assemble(page_parts), contract, salt)
        page_hashes.append(ph)
        out_pages.append({"page_hash": ph, "elements": element_hashes})
    doc_parts["pages"] = _hash_list_part(
        page_hashes, contract, pointer("pages"), SUPPORTED_CONTRACTS
    )
    dh = _digest(_assemble(doc_parts), contract, salt)
    return {"doc_hash": dh, "pages": out_pages}
