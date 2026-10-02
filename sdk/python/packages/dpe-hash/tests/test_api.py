"""公开入口的等价性与辅助函数：逐层、线上原像、展开视图三种入口对同一文档结果相等。"""

from __future__ import annotations

import hashlib
from typing import Any

import pytest
from dpe_hash import (
    DRILL_CONTRACT,
    SUPPORTED_CONTRACTS,
    ContractUnsupportedError,
    DocumentFields,
    ElementObject,
    ExpandedDocument,
    PageFields,
    blob_ref,
    children,
    content_hash,
    doc_hash,
    document_hashes,
    has_invalid_unicode,
    object_hash,
    page_hash,
    parse_blob_ref,
    parse_hash,
)

BLOB = blob_ref(b"png bytes")

ELEMENTS: list[ElementObject] = [
    {"category": "Title", "text": "标题", "metadata": {"lang": "zh", "drop": None}},
    {"category": "Table", "text": "a", "text_as_html": "<table/>"},
    {"category": "Image", "image_blob": BLOB, "image_mime_type": "image/png"},
]
PAGE: PageFields = {"title": "p1", "page_metadata": {"page_label": "iv"}}
DOCUMENT: DocumentFields = {"file_type": "pdf", "title": "季度报告", "doc_metadata": {"a": 1}}


def _expanded() -> ExpandedDocument:
    return {
        **DOCUMENT,
        "pages": [
            {**PAGE, "elements": ELEMENTS[:2]},
            {"elements": [ELEMENTS[2], ELEMENTS[0]]},
            {"elements": []},
        ],
    }


@pytest.mark.parametrize("contract", ["dpe1", DRILL_CONTRACT])
def test_three_entry_points_agree(contract: str) -> None:
    expanded = _expanded()
    whole = document_hashes(expanded, contract)

    page_hashes = []
    for page, page_out in zip(expanded["pages"], whole["pages"], strict=True):
        hashes = [content_hash(el, contract) for el in page["elements"]]
        assert hashes == page_out["elements"]
        assert [object_hash(el, "element", contract) for el in page["elements"]] == hashes
        fields: PageFields = {}
        if "title" in page:
            fields["title"] = page["title"]
        if "page_metadata" in page:
            fields["page_metadata"] = page["page_metadata"]
        ph = page_hash(fields, hashes, contract)
        assert ph == object_hash({**fields, "elements": hashes}, "page", contract)
        assert ph == page_out["page_hash"]
        page_hashes.append(ph)

    dh = doc_hash(DOCUMENT, page_hashes, contract)
    assert dh == object_hash({**DOCUMENT, "pages": page_hashes}, "document", contract)
    assert dh == whole["doc_hash"]
    assert dh.startswith(f"{contract}:")


def test_children() -> None:
    hashes = [content_hash(el) for el in ELEMENTS] + [content_hash(ELEMENTS[0])]
    assert children({"elements": hashes}, "page") == hashes  # 保留顺序与重复
    assert children({"file_type": "md", "pages": ["dpe1:" + "0" * 64]}, "document") == [
        "dpe1:" + "0" * 64
    ]
    assert children(dict(ELEMENTS[2]), "element") == [BLOB]
    assert children(dict(ELEMENTS[0]), "element") == []


def test_blob_ref_and_parse() -> None:
    data = b"\x89PNG"
    ref = blob_ref(data)
    assert ref == "sha256:" + hashlib.sha256(data).hexdigest()
    assert blob_ref(bytearray(data)) == blob_ref(memoryview(data)) == ref
    assert parse_blob_ref(ref) == hashlib.sha256(data).hexdigest()


def test_parse_hash() -> None:
    h = content_hash(ELEMENTS[0])
    assert parse_hash(h) == ("dpe1", h.split(":", 1)[1])
    drill = content_hash(ELEMENTS[0], DRILL_CONTRACT)
    with pytest.raises(ContractUnsupportedError):
        parse_hash(drill)  # 默认只接受真实契约
    assert parse_hash(drill, (*SUPPORTED_CONTRACTS, DRILL_CONTRACT))[0] == DRILL_CONTRACT


def test_supported_contracts_exclude_drill() -> None:
    assert SUPPORTED_CONTRACTS == ("dpe1",)
    assert DRILL_CONTRACT not in SUPPORTED_CONTRACTS


def test_equivalences() -> None:
    """core.md §2.7：null ≡ 缺省、metadata 缺省 ≡ {}、1.0 ≡ 1；"" 与 null 不等价。"""
    base: dict[str, Any] = {"category": "NarrativeText", "text": "x"}
    same = [
        base,
        {**base, "metadata": None},
        {**base, "metadata": {}},
        {**base, "metadata": {"k": None}},
    ]
    assert len({object_hash(e, "element") for e in same}) == 1
    assert object_hash({**base, "metadata": {"n": 1.0}}, "element") == object_hash(
        {**base, "metadata": {"n": 1}}, "element"
    )
    assert object_hash({**base, "text": ""}, "element") != object_hash(
        {"category": "NarrativeText"}, "element"
    )


def test_tuple_arrays_and_mapping_inputs() -> None:
    """数组可以是 tuple、对象可以是任意 Mapping：都是无歧义的 JSON 编码。"""
    from types import MappingProxyType

    a = object_hash({"category": "Title", "metadata": {"box": [[0, 1], [2, 3]]}}, "element")
    b = object_hash(
        MappingProxyType({"category": "Title", "metadata": {"box": ((0, 1), (2, 3))}}), "element"
    )
    assert a == b


def test_has_invalid_unicode_for_whole_request_bodies() -> None:
    """服务端在判契约头之前，用它检查整个请求体（含信封字段）是否为 I-JSON。"""
    assert has_invalid_unicode({"staging_session": "st-\ud800", "document": {}})
    assert has_invalid_unicode([{"\udc00": 1}])
    assert not has_invalid_unicode({"document": {"title": "季度报告 🚀"}, "pages": [1.5, None]})
