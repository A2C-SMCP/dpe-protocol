"""插件产出的解析与本地校验（connector 契约 §6.4–§6.6）：纯逻辑，不做 IO。

- ``parse_scan_result``：``scan`` 响应的形状（§6.4）。中间批不得带 ``cursor`` / ``incremental``，
  结束批必带两者；违例是协议级失败（插件违反线协议，运行器无法确定本轮边界）。
- ``parse_item``：四种产出项的封闭 schema（§6.5）、URI 文法与前缀强制（§3、§6.5）、元素线格式
  （§6.6 第 1–2 步）。插件产出是不可信输入（§6.1）：任何违例都只让该条目失败，不抛出。
- ``SegmentAssembler``：文档分段的拼接规则（§6.5）。

元素按 core §2.8 的顺序校验：线上 ``blob`` 是字节来源对象，先换成占位引用做页视图校验
（``models._page_hashes``，与 ``Page`` 模型同一实现；形状 → category → 封闭 schema → 逐字段，
含「不允许 blob 的 category 不得出现 blob」），通过后再校验字节来源对象本身（§6.6：``inline`` /
``handle`` 互斥、标准 base64、``size`` 界限）。线上 ``blob`` 若不是对象（如插件伪造的
``sha256:`` 引用）一律拒绝——引用只由运行器计算。
"""

from __future__ import annotations

import base64
import binascii
import contextlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import dpe_hash
from dpe_hash import DpeHashError, PageHashes

from dpe_sdk._plain import plain
from dpe_sdk.models import _page_hashes
from dpe_sdk.run.errors import ItemFailure, ProtocolFailure

__all__ = [
    "PLACEHOLDER_BLOB",
    "BlobSpec",
    "DeleteItem",
    "DocumentSegment",
    "MoveItem",
    "ReadyDocument",
    "ScanBatch",
    "SegmentAssembler",
    "parse_item",
    "parse_scan_result",
]

#: 校验期间顶替字节来源的占位引用；读到字节后由运行器换成真实引用（§6.6 第 3 步）
PLACEHOLDER_BLOB = "sha256:" + "0" * 64

_MAX_SIZE = 2**53 - 1
_ERROR_CODES = frozenset({"source_unavailable", "content_invalid", "internal"})
_DOCUMENT_KEYS = frozenset({"kind", "file_uri", "document", "pages", "continued"})
_DELETE_KEYS = frozenset({"kind", "file_uri"})
_MOVE_KEYS = frozenset({"kind", "from_uri", "to_uri"})
_ERROR_KEYS = frozenset({"kind", "file_uri", "code", "message", "retryable"})
_SCAN_KEYS = frozenset({"items", "next", "cursor", "incremental"})


# ---------------------------------------------------------------------- scan 响应


@dataclass(frozen=True)
class ScanBatch:
    """一次 ``scan`` 的结果：``final`` 为 True 时是结束批，``cursor`` / ``incremental`` 有效。"""

    items: list[Any]
    next: str | None
    cursor: str | None = None
    incremental: bool | None = None

    @property
    def final(self) -> bool:
        return self.next is None


def parse_scan_result(result: Any) -> ScanBatch:
    """按 §6.4 解析 ``scan`` 响应；违例抛 ``ProtocolFailure``。"""
    if not isinstance(result, dict):
        raise ProtocolFailure("scan 响应的 result 必须是对象")
    unknown = set(result) - _SCAN_KEYS
    if unknown:
        raise ProtocolFailure(f"scan 响应含未定义的字段：{sorted(unknown)}")
    items = result.get("items")
    if not isinstance(items, list):
        raise ProtocolFailure("scan 响应的 items 必须是数组")
    next_token = result.get("next")
    if next_token is not None:
        if not isinstance(next_token, str):
            raise ProtocolFailure("scan 响应的 next 必须是字符串或缺省")
        if "cursor" in result or "incremental" in result:
            raise ProtocolFailure("中间批不得带 cursor 或 incremental（§6.4）")
        return ScanBatch(items, next_token)
    if "cursor" not in result or "incremental" not in result:
        raise ProtocolFailure("结束批必须带 cursor 与 incremental（§6.4）")
    cursor, incremental = result["cursor"], result["incremental"]
    if cursor is not None and not isinstance(cursor, str):
        raise ProtocolFailure("结束批的 cursor 必须是字符串或 null")
    if not isinstance(incremental, bool):
        raise ProtocolFailure("结束批的 incremental 必须是布尔")
    return ScanBatch(items, None, cursor, incremental)


# ---------------------------------------------------------------------- 产出项


@dataclass(frozen=True)
class BlobSpec:
    """元素的字节来源（§6.6）：``inline`` 与 ``handle`` 恰有其一。"""

    inline: bytes | None = None
    handle: str | None = None
    size: int | None = None


@dataclass(frozen=True)
class BlobSlot:
    """文档中一个待解析的 blob：所在页序号、页内元素序号与字节来源。"""

    page: int
    element: int
    spec: BlobSpec


@dataclass
class DocumentSegment:
    """``document`` 产出项的一段（§6.5）。``error`` 非空时信封可识别（URI、``continued`` 已知）
    但内容不合法——分段规则仍要据此判断这篇文档的边界。"""

    file_uri: str
    continued: bool
    document: dict[str, Any] | None
    pages: list[dict[str, Any]]
    blobs: list[BlobSlot]
    error: ItemFailure | None = None
    #: 与 ``pages`` 逐项对齐的预算页 hash（blob 仍是占位引用；prepare 阶段按需补算）
    hashes: list[PageHashes] = field(default_factory=list)


@dataclass(frozen=True)
class DeleteItem:
    file_uri: str


@dataclass(frozen=True)
class MoveItem:
    from_uri: str
    to_uri: str


Item = DocumentSegment | DeleteItem | MoveItem


def _raw_uri(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _invalid(
    message: str,
    *,
    file_uri: str | None = None,
    from_uri: str | None = None,
    to_uri: str | None = None,
) -> ItemFailure:
    return ItemFailure(
        "DPE_VALIDATION",
        message,
        source="validation",
        file_uri=file_uri,
        from_uri=from_uri,
        to_uri=to_uri,
    )


def _check_uri(value: Any, name: str, uri_prefix: str) -> str | ItemFailure:
    """规范化 URI（core §1.1）并做前缀强制（§6.5：码点前缀匹配，不识别 path 段边界）。"""
    if not isinstance(value, str):
        return _invalid(f"{name} 必须是字符串")
    try:
        normalized = dpe_hash.normalize_file_uri(value)
    except DpeHashError as exc:
        return ItemFailure(exc.code, f"{name} 不合法：{exc}", source="validation")
    if not normalized.startswith(uri_prefix):
        return ItemFailure(
            "out_of_prefix", f"{name} 不在实例前缀 {uri_prefix!r} 内", source="prefix"
        )
    return normalized


def _with_uri(failure: ItemFailure, **uris: str | None) -> ItemFailure:
    for key, value in uris.items():
        if value is not None and getattr(failure, key) is None:
            setattr(failure, key, value)
    return failure


def parse_item(
    raw: Any, uri_prefix: str, *, contract: str = dpe_hash.CONTRACT
) -> Item | ItemFailure:
    """解析并校验一个产出项；失败返回 ``ItemFailure``（带所涉 URI），不抛出。

    ``contract`` 是本次轮次声明的契约（运行器在能力协商后取 ``core.contract``）；
    缺省 ``dpe1``，供不涉及契约切换的调用方使用。``error`` 产出项（§6.5）直接转为
    ``source="plugin"`` 的条目失败。
    """
    if not isinstance(raw, dict):
        return _invalid("产出项必须是对象")
    kind = raw.get("kind")
    if kind == "document":
        return _parse_document(raw, uri_prefix, contract)
    if kind == "delete":
        return _parse_delete(raw, uri_prefix)
    if kind == "move":
        return _parse_move(raw, uri_prefix)
    if kind == "error":
        return _parse_error(raw)
    return _invalid(f"未知的产出项 kind：{kind!r}", file_uri=_raw_uri(raw.get("file_uri")))


def _closed(
    raw: Mapping[str, Any], allowed: frozenset[str], required: tuple[str, ...]
) -> str | None:
    unknown = sorted(set(raw) - allowed)
    if unknown:
        return f"产出项含未定义的字段：{unknown}"
    missing = [name for name in required if name not in raw]
    if missing:
        return f"产出项缺少必填字段：{missing}"
    return None


def _parse_delete(raw: dict[str, Any], uri_prefix: str) -> DeleteItem | ItemFailure:
    problem = _closed(raw, _DELETE_KEYS, ("file_uri",))
    if problem is not None:
        return _invalid(problem, file_uri=_raw_uri(raw.get("file_uri")))
    uri = _check_uri(raw["file_uri"], "file_uri", uri_prefix)
    if isinstance(uri, ItemFailure):
        return _with_uri(uri, file_uri=_raw_uri(raw["file_uri"]))
    return DeleteItem(uri)


def _parse_move(raw: dict[str, Any], uri_prefix: str) -> MoveItem | ItemFailure:
    from_raw, to_raw = _raw_uri(raw.get("from_uri")), _raw_uri(raw.get("to_uri"))
    problem = _closed(raw, _MOVE_KEYS, ("from_uri", "to_uri"))
    if problem is not None:
        return _invalid(problem, from_uri=from_raw, to_uri=to_raw)
    source = _check_uri(raw["from_uri"], "from_uri", uri_prefix)
    target = _check_uri(raw["to_uri"], "to_uri", uri_prefix)
    for checked in (source, target):
        if isinstance(checked, ItemFailure):
            return _with_uri(
                checked,
                from_uri=source if isinstance(source, str) else from_raw,
                to_uri=target if isinstance(target, str) else to_raw,
            )
    assert isinstance(source, str) and isinstance(target, str)
    if source == target:
        return _invalid("move 的源与目标是同一篇文档", from_uri=source, to_uri=target)
    return MoveItem(source, target)


def _parse_error(raw: dict[str, Any]) -> ItemFailure:
    uri_raw = raw.get("file_uri")
    problem = _closed(raw, _ERROR_KEYS, ("code", "message", "retryable"))
    if problem is None and (not isinstance(raw["code"], str) or raw["code"] not in _ERROR_CODES):
        problem = f"error 产出项的 code 不在闭集内：{raw['code']!r}"
    if problem is None and not isinstance(raw["message"], str):
        problem = "error 产出项的 message 必须是字符串"
    if problem is None and not isinstance(raw["retryable"], bool):
        problem = "error 产出项的 retryable 必须是布尔"
    if problem is None and uri_raw is not None and not isinstance(uri_raw, str):
        problem = "error 产出项的 file_uri 必须是字符串或 null"
    file_uri = _raw_uri(uri_raw)
    if file_uri is not None:
        with contextlib.suppress(DpeHashError):  # 错误条目本身不投递，非法 URI 原样上报
            file_uri = dpe_hash.normalize_file_uri(file_uri)
    if problem is not None:
        return _invalid(problem, file_uri=file_uri)
    return ItemFailure(
        raw["code"],
        raw["message"],
        source="plugin",
        retryable=raw["retryable"],
        file_uri=file_uri,
    )


def _parse_document(
    raw: dict[str, Any], uri_prefix: str, contract: str
) -> DocumentSegment | ItemFailure:
    uri_raw = _raw_uri(raw.get("file_uri"))
    problem = _closed(raw, _DOCUMENT_KEYS, ("file_uri", "pages"))
    continued = raw.get("continued", False)
    if problem is None and not isinstance(continued, bool):
        problem = "continued 必须是布尔"
    # 只要 file_uri 可识别（可规范化），信封违例与越界都作为带 error 的分段交给
    # SegmentAssembler：同一篇分段文档的后续段被吞掉，整篇只报一次
    identity: str | None = None
    if uri_raw is not None:
        with contextlib.suppress(DpeHashError):
            identity = dpe_hash.normalize_file_uri(uri_raw)
    # continued 非法时保守视为「可能还有续段」：同一文档随后的段被吞掉，整篇只报一次
    is_continued = continued if isinstance(continued, bool) else True
    if problem is not None:
        if identity is None:
            return _invalid(problem, file_uri=uri_raw)
        return DocumentSegment(
            identity, is_continued, None, [], [], _invalid(problem, file_uri=identity)
        )
    uri = _check_uri(raw["file_uri"], "file_uri", uri_prefix)
    if isinstance(uri, ItemFailure):
        failure = _with_uri(uri, file_uri=identity or uri_raw)
        if identity is None:
            return failure
        return DocumentSegment(identity, is_continued, None, [], [], failure)
    segment = DocumentSegment(uri, continued, None, [], [])
    document = raw.get("document")
    if document is not None:
        if not isinstance(document, dict):
            segment.error = _invalid("document 必须是对象", file_uri=uri)
            return segment
        if "pages" in document:
            segment.error = _invalid("document 不得含 pages（页由产出项给出）", file_uri=uri)
            return segment
        try:
            segment.document = plain(document)
        except DpeHashError as exc:
            segment.error = _invalid(exc.message, file_uri=uri)
            return segment
    elif "document" in raw:
        segment.error = _invalid("document 不得为 null", file_uri=uri)
        return segment
    pages = raw["pages"]
    if not isinstance(pages, list):
        segment.error = _invalid("pages 必须是数组", file_uri=uri)
        return segment
    for index, page in enumerate(pages):
        parsed = _parse_page(page, index, contract)
        if isinstance(parsed, ItemFailure):
            segment.error = _with_uri(parsed, file_uri=uri)
            return segment
        page_obj, page_hashes, slots = parsed
        segment.pages.append(page_obj)
        segment.hashes.append(page_hashes)
        segment.blobs.extend(slots)
    return segment


def _parse_page(
    page: Any, index: int, contract: str
) -> tuple[dict[str, Any], PageHashes, list[BlobSlot]] | ItemFailure:
    """校验一页（§6.6 第 2 步）：先以占位引用做页 hash 校验，再校验字节来源对象。"""
    at = f"/pages/{index}"
    if not isinstance(page, dict):
        return _invalid(f"页必须是对象（位置 {at}）")
    try:
        working = plain(page)
    except DpeHashError as exc:
        return ItemFailure(exc.code, f"{exc.message}（位置 {at}）", source="validation")
    if dpe_hash.nesting_depth(working, "elements") > dpe_hash.MAX_NESTING_DEPTH:
        return _invalid(
            f"页对象的嵌套深度超过上限 {dpe_hash.MAX_NESTING_DEPTH} 层"
            f"（core §2.8 第 0 步）（位置 {at}）"
        )
    sources: list[tuple[int, Any]] = []
    elements = working.get("elements")
    if isinstance(elements, list):
        for position, element in enumerate(elements):
            if isinstance(element, dict) and isinstance(element.get("blob"), dict):
                sources.append((position, element["blob"]))
                element["blob"] = PLACEHOLDER_BLOB
    try:
        page_hashes = _page_hashes(working, contract)
    except DpeHashError as exc:
        return ItemFailure(exc.code, f"{exc.message}（位置 {at}{exc.path}）", source="validation")
    slots: list[BlobSlot] = []
    by_position = dict(sources)
    for position, element in enumerate(working["elements"]):
        if position in by_position:
            spec = _parse_blob(by_position[position], f"{at}/elements/{position}/blob")
            if isinstance(spec, ItemFailure):
                return spec
            slots.append(BlobSlot(index, position, spec))
        elif element.get("blob") is not None:
            # 通过了页视图校验的非对象 blob（只可能是格式合法的 sha256: 引用）：线上不允许
            return _invalid(
                f"线上的 blob 必须是字节来源对象，不得是引用（位置 {at}/elements/{position}/blob）"
            )
    return working, page_hashes, slots


def _parse_blob(source: dict[str, Any], at: str) -> BlobSpec | ItemFailure:
    unknown = sorted(set(source) - {"inline", "handle", "size"})
    if unknown:
        return _invalid(f"字节来源对象含未定义的字段 {unknown}（位置 {at}）")
    has_inline, has_handle = "inline" in source, "handle" in source
    if has_inline == has_handle:
        return _invalid(f"字节来源对象必须恰有 inline 或 handle 之一（位置 {at}）")
    if has_inline:
        if "size" in source:
            return _invalid(f"内联字节来源不带 size（位置 {at}）")
        data = source["inline"]
        if not isinstance(data, str):
            return _invalid(f"inline 必须是 base64 字符串（位置 {at}）")
        try:
            decoded = base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError):
            return _invalid(f"inline 不是带填充的标准 base64（位置 {at}）")
        if len(data) % 4:
            return _invalid(f"inline 不是带填充的标准 base64（位置 {at}）")
        return BlobSpec(inline=decoded)
    handle = source["handle"]
    if not isinstance(handle, str) or not handle:
        return _invalid(f"handle 必须是非空字符串（位置 {at}）")
    size = source.get("size")
    if size is not None and (
        isinstance(size, bool) or not isinstance(size, int) or not 0 <= size <= _MAX_SIZE
    ):
        return _invalid(f"size 必须是 0..2^53-1 的整数（位置 {at}）")
    if "size" in source and size is None:
        return _invalid(f"size 不得为 null（位置 {at}）")
    return BlobSpec(handle=handle, size=size)


# ---------------------------------------------------------------------- 分段拼接


@dataclass
class ReadyDocument:
    """拼接完成的一篇文档（尚未解析 blob、尚未做文档层校验）。"""

    file_uri: str
    document: dict[str, Any]
    pages: list[dict[str, Any]]
    blobs: list[BlobSlot] = field(default_factory=list)
    hashes: list[PageHashes] = field(default_factory=list)

    def __post_init__(self) -> None:
        if len(self.hashes) != len(self.pages):
            raise ValueError("ReadyDocument.pages 与 hashes 必须逐项对齐")


@dataclass
class _Open:
    file_uri: str
    document: dict[str, Any] | None
    pages: list[dict[str, Any]]
    hashes: list[PageHashes]
    blobs: list[BlobSlot]
    #: 已判整篇失败：吞掉同一文档的后续续段，直到末段（不重复上报）
    failed: bool = False


SegmentEvent = ReadyDocument | ItemFailure


class SegmentAssembler:
    """按 §6.5 拼接分段文档：只在页边界切分、同一 ``file_uri`` 的各段连续、首段必带
    ``document`` 而续段不得带；一轮内同一时刻至多一篇未闭合的文档。"""

    def __init__(self) -> None:
        self._open: _Open | None = None

    @property
    def open_uri(self) -> str | None:
        return self._open.file_uri if self._open is not None else None

    def interrupt(self) -> list[SegmentEvent]:
        """有其他产出项插入时调用：未闭合的文档整篇失败（各段 MUST 连续出现）。"""
        current, self._open = self._open, None
        if current is None or current.failed:
            return []
        return [
            _invalid("分段文档的各段未连续出现（中间插入了其他条目）", file_uri=current.file_uri)
        ]

    def feed(self, segment: DocumentSegment) -> list[SegmentEvent]:
        events: list[SegmentEvent] = []
        current = self._open
        if current is not None and current.file_uri != segment.file_uri:
            events.extend(self.interrupt())
            current = None
        elif current is not None and current.failed and segment.document is not None:
            # 已判失败的文档仍打开（continued 为真或非法），而同一 URI 又以首段标记（带 document）
            # 出现：按同 URI 的新文档处理（§6.4 后写覆盖），不把它当续段吞掉
            current = None
        if current is None:
            current = _Open(segment.file_uri, document=None, pages=[], hashes=[], blobs=[])
            if segment.error is None and segment.document is None:
                segment.error = _invalid("分段文档的首段必须带 document", file_uri=segment.file_uri)
            current.document = segment.document
        elif segment.error is None and segment.document is not None:
            segment.error = _invalid("分段文档的续段不得带 document", file_uri=segment.file_uri)
        if not current.failed:
            if segment.error is not None:
                current.failed = True
                events.append(segment.error)
            else:
                offset = len(current.pages)
                current.pages.extend(segment.pages)
                current.hashes.extend(segment.hashes)
                current.blobs.extend(
                    BlobSlot(slot.page + offset, slot.element, slot.spec) for slot in segment.blobs
                )
        if segment.continued:
            self._open = current
            return events
        self._open = None
        if not current.failed:
            assert current.document is not None
            events.append(
                ReadyDocument(
                    current.file_uri,
                    current.document,
                    current.pages,
                    blobs=current.blobs,
                    hashes=current.hashes,
                )
            )
        return events

    def finish(self) -> list[SegmentEvent]:
        """轮结束（收到结束批）：仍未闭合的文档整篇失败（``segment_unclosed``，§6.5）。"""
        current, self._open = self._open, None
        if current is None or current.failed:
            return []
        return [
            ItemFailure(
                "segment_unclosed",
                "分段文档在轮结束时仍未闭合",
                source="validation",
                file_uri=current.file_uri,
            )
        ]
