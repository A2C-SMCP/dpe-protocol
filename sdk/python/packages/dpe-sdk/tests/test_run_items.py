"""产出项的解析与本地校验（connector 契约 §6.4–§6.6）：纯逻辑。"""

from __future__ import annotations

import base64
from typing import Any

import pytest
from dpe_sdk.run import ItemFailure, ProtocolFailure
from dpe_sdk.run.items import (
    PLACEHOLDER_BLOB,
    BlobSpec,
    DeleteItem,
    DocumentSegment,
    MoveItem,
    ReadyDocument,
    SegmentAssembler,
    parse_item,
    parse_scan_result,
)

P = "dpe://src/"


def failure(value: object) -> ItemFailure:
    assert isinstance(value, ItemFailure), value
    return value


def segment(value: object) -> DocumentSegment:
    assert isinstance(value, DocumentSegment), value
    return value


def doc(name: str = "a.md", elements: list[Any] | None = None, **extra: Any) -> dict[str, Any]:
    return {
        "kind": "document",
        "file_uri": P + name,
        "document": {"file_type": "md"},
        "pages": [{"elements": elements or [{"category": "NarrativeText", "text": "t"}]}],
        **extra,
    }


def image(blob: Any, category: str = "Image") -> dict[str, Any]:
    return doc(elements=[{"category": category, "mime_type": "image/png", "blob": blob}])


# ---------------------------------------------------------------------- scan 响应


def test_scan_result_shapes() -> None:
    middle = parse_scan_result({"items": [], "next": "n1"})
    assert not middle.final and middle.next == "n1"
    end = parse_scan_result({"items": [], "cursor": None, "incremental": False})
    assert end.final and end.cursor is None and end.incremental is False
    malformed: list[Any] = [
        [],
        {"items": {}},
        {"items": [], "next": "n", "cursor": "c"},  # 中间批不得带 cursor
        {"items": [], "cursor": "c"},  # 结束批缺 incremental
        {"items": [], "cursor": 1, "incremental": True},
        {"items": [], "cursor": "c", "incremental": "yes"},
        {"items": [], "cursor": "c", "incremental": True, "extra": 1},
        {"items": [], "next": 3},
    ]
    for bad in malformed:
        with pytest.raises(ProtocolFailure):
            parse_scan_result(bad)


# ---------------------------------------------------------------------- 产出项


def test_delete_and_move_are_normalized_and_prefix_checked() -> None:
    assert parse_item({"kind": "delete", "file_uri": "DPE://SRC/a.md"}, P) == DeleteItem(P + "a.md")
    assert parse_item(
        {"kind": "move", "from_uri": P + "a.md", "to_uri": P + "b.md"}, P
    ) == MoveItem(P + "a.md", P + "b.md")
    out = failure(parse_item({"kind": "move", "from_uri": P + "a.md", "to_uri": "dpe://x/b"}, P))
    assert (out.code, out.source, out.from_uri, out.to_uri) == (
        "out_of_prefix",
        "prefix",
        P + "a.md",
        "dpe://x/b",
    )
    # 码点前缀匹配，不识别段边界：dpe://srcX/ 不在 dpe://src/ 内，但 dpe://src/x 在
    assert failure(parse_item({"kind": "delete", "file_uri": "dpe://srcx/a"}, P)).code == (
        "out_of_prefix"
    )
    assert failure(parse_item({"kind": "delete", "file_uri": "relative"}, P)).code == (
        "DPE_VALIDATION"
    )
    same = failure(parse_item({"kind": "move", "from_uri": P + "a", "to_uri": P + "a"}, P))
    assert same.code == "DPE_VALIDATION"


@pytest.mark.parametrize(
    "raw",
    [
        {"kind": "unknown", "file_uri": P + "a"},
        {"kind": "delete", "file_uri": P + "a", "extra": 1},
        {"kind": "delete"},
        {"kind": "move", "from_uri": P + "a"},
        {**doc(), "continued": "yes"},
        {**doc(), "unknown": 1},
        "not an object",
    ],
)
def test_closed_schema_violations_are_validation_failures(raw: Any) -> None:
    result = failure(parse_item(raw, P))
    assert (result.code, result.source, result.retryable) == ("DPE_VALIDATION", "validation", False)


def test_error_items() -> None:
    reported = failure(
        parse_item(
            {
                "kind": "error",
                "file_uri": "DPE://SRC/e.md",
                "code": "content_invalid",
                "message": "m",
                "retryable": False,
            },
            P,
        )
    )
    assert (reported.code, reported.source, reported.file_uri) == (
        "content_invalid",
        "plugin",
        P + "e.md",
    )
    no_uri = failure(
        parse_item({"kind": "error", "code": "internal", "message": "m", "retryable": True}, P)
    )
    assert no_uri.retryable is True and no_uri.file_uri is None
    bad_codes: list[Any] = ["oops", [], {}, 1, None]  # 不在闭集或不是字符串（含不可哈希的值）
    for code in bad_codes:
        bad = failure(
            parse_item({"kind": "error", "code": code, "message": "m", "retryable": True}, P)
        )
        assert (bad.code, bad.source) == ("DPE_VALIDATION", "validation")


def test_document_segment_shapes() -> None:
    ok = segment(parse_item(doc(continued=True), P))
    assert ok.error is None and ok.continued and ok.document == {"file_type": "md"}
    with_pages = segment(parse_item({**doc(), "document": {"file_type": "md", "pages": []}}, P))
    assert with_pages.error is not None and with_pages.error.code == "DPE_VALIDATION"
    unknown_category = segment(parse_item(doc(elements=[{"category": "Nope"}]), P))
    assert unknown_category.error is not None
    assert unknown_category.error.code == "DPE_CATEGORY_UNKNOWN"
    assert unknown_category.error.file_uri == P + "a.md"


# ---------------------------------------------------------------------- 元素线格式


def test_blob_sources_become_placeholders_and_slots() -> None:
    data = b"\x89PNG bytes"
    inline = segment(parse_item(image({"inline": base64.b64encode(data).decode()}), P))
    assert inline.error is None
    assert inline.pages[0]["elements"][0]["blob"] == PLACEHOLDER_BLOB
    assert inline.blobs[0].spec == BlobSpec(inline=data)
    handle = segment(parse_item(image({"handle": "h", "size": 5}), P))
    assert handle.blobs[0].spec == BlobSpec(handle="h", size=5)


@pytest.mark.parametrize(
    "blob",
    [
        {"inline": "@@@@"},  # 非标准字母表
        {"inline": "YWJj"[:3]},  # 缺填充
        {"inline": "YQ==", "handle": "h"},  # 互斥
        {},
        {"handle": ""},
        {"handle": "h", "size": True},
        {"handle": "h", "size": -1},
        {"handle": "h", "size": 2**53},
        {"handle": "h", "size": None},
        {"inline": "YQ==", "size": 1},
        {"handle": "h", "extra": 1},
        "sha256:" + "a" * 64,  # 插件不得给出引用
    ],
)
def test_invalid_blob_sources(blob: Any) -> None:
    result = segment(parse_item(image(blob), P))
    assert result.error is not None and result.error.code == "DPE_VALIDATION"


def test_element_rules_are_checked_before_the_blob_source() -> None:
    """core §2.8 顺序：category 先于字段——未知 category 即使字节来源也坏，报 category。"""
    result = segment(parse_item(image({"inline": "@@"}, category="Nope"), P))
    assert result.error is not None and result.error.code == "DPE_CATEGORY_UNKNOWN"
    # 不允许 blob 的 category 出现 blob（字节来源对象）：按封闭 schema 拒绝
    text = segment(
        parse_item(doc(elements=[{"category": "NarrativeText", "blob": {"inline": "YQ=="}}]), P)
    )
    assert text.error is not None and text.error.code == "DPE_VALIDATION"


# ---------------------------------------------------------------------- 分段


def seg(name: str, *, first: bool, continued: bool, blob: bool = False) -> DocumentSegment:
    raw = image({"handle": "h"}) if blob else doc()
    raw = {**raw, "file_uri": P + name, "continued": continued}
    if not first:
        del raw["document"]
    return segment(parse_item(raw, P))


def test_segments_concatenate_pages_and_offset_blob_slots() -> None:
    assembler = SegmentAssembler()
    assert assembler.feed(seg("a", first=True, continued=True)) == []
    events = assembler.feed(seg("a", first=False, continued=False, blob=True))
    assert len(events) == 1 and isinstance(events[0], ReadyDocument)
    ready = events[0]
    assert len(ready.pages) == 2
    assert [(s.page, s.element) for s in ready.blobs] == [(1, 0)]


def test_segment_rule_violations_fail_the_whole_document_once() -> None:
    assembler = SegmentAssembler()
    # 续段带 document：整篇失败，同一文档的后续段被吞掉、不重复上报
    assembler.feed(seg("a", first=True, continued=True))
    events = assembler.feed(seg("a", first=True, continued=True))
    assert [failure(e).code for e in events] == ["DPE_VALIDATION"]
    assert assembler.feed(seg("a", first=False, continued=False)) == []
    # 首段缺 document
    events = assembler.feed(seg("b", first=False, continued=False))
    assert [failure(e).code for e in events] == ["DPE_VALIDATION"]
    # 插入其他文档：未闭合的整篇失败，新文档照常处理
    assembler.feed(seg("c", first=True, continued=True))
    events = assembler.feed(seg("d", first=True, continued=False))
    assert [type(e).__name__ for e in events] == ["ItemFailure", "ReadyDocument"]
    # 轮结束仍未闭合
    assembler.feed(seg("e", first=True, continued=True))
    assert [failure(e).code for e in assembler.finish()] == ["segment_unclosed"]
    assert assembler.finish() == []
