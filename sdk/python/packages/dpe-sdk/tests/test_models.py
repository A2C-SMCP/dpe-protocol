"""数据模型（#11）：向量一致、拒绝类向量的错误码与位置、内容等价与构造语义。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from dpe_hash import (
    CategoryUnknownError,
    DpeHashError,
    FileTypeUnknownError,
    IntegerOutOfRangeError,
    UndefinedFieldError,
    content_hash,
)
from dpe_hash import ValidationError as DpeValidationError
from dpe_sdk import Document, DocumentObject, ElementObject, Page, PageObject
from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError


def _load(vectors_dir: Path, kind: str) -> list[dict[str, Any]]:
    vectors = [
        json.loads(f.read_text(encoding="utf-8"))
        for f in sorted(vectors_dir.glob("*.json"))
        if f.name != "manifest.json"
    ]
    selected = [v for v in vectors if v["kind"] == kind]
    assert selected, f"vectors/ 中没有 kind={kind} 的向量"
    return selected


def _document_cases(vectors_dir: Path) -> list[tuple[str, dict[str, Any], str, dict[str, Any]]]:
    cases = []
    for vec in _load(vectors_dir, "document"):
        for key, document in vec["documents"].items():
            for contract, expected in vec["expected"][key].items():
                expected = {k: v for k, v in expected.items() if k != "preimages"}
                cases.append((f"{vec['name']}/{key}/{contract}", document, contract, expected))
    return cases


# ---------------------------------------------------------------------------
# document 向量
# ---------------------------------------------------------------------------


def test_document_vectors_expanded(vectors_dir: Path) -> None:
    for label, document, contract, expected in _document_cases(vectors_dir):
        model = Document.model_validate(document)
        assert model.hashes(contract) == expected, label
        assert model.doc_hash(contract) == expected["doc_hash"], label
        # 无损往返：序列化结果就是向量输入
        assert model.model_dump(mode="json") == document, label
        from_json = Document.model_validate_json(json.dumps(document))
        assert from_json == model, label


def test_document_vectors_wire_objects(vectors_dir: Path) -> None:
    """线上对象逐层上溯：ElementObject → PageObject → DocumentObject。"""
    for label, document, contract, expected in _document_cases(vectors_dir):
        context = {"contract": contract}
        page_hashes = []
        for page, page_expected in zip(document["pages"], expected["pages"], strict=True):
            elements = [ElementObject.model_validate(el) for el in page["elements"]]
            hashes = [el.content_hash(contract) for el in elements]
            assert hashes == page_expected["elements"], label
            assert [el.model_dump(mode="json") for el in elements] == page["elements"], label
            wire_page = PageObject.model_validate({**page, "elements": hashes}, context=context)
            page_hashes.append(wire_page.page_hash(contract))
        assert page_hashes == [p["page_hash"] for p in expected["pages"]], label
        wire_doc = DocumentObject.model_validate(
            {**document, "pages": page_hashes}, context=context
        )
        assert wire_doc.doc_hash(contract) == expected["doc_hash"], label


def test_page_hash_via_standalone_page(vectors_dir: Path) -> None:
    """单独构造的 Page 与经 Document 构造的页相同。"""
    for label, document, _, _ in _document_cases(vectors_dir):
        via_document = Document.model_validate(document).pages
        assert [Page.model_validate(p) for p in document["pages"]] == via_document, label


# ---------------------------------------------------------------------------
# 拒绝类向量（core §2.8）
# ---------------------------------------------------------------------------

_MODELS: dict[str, type[ElementObject | PageObject | DocumentObject | Document]] = {
    "element": ElementObject,
    "page": PageObject,
    "document": DocumentObject,
    "expanded_document": Document,
}


def test_invalid_vectors(vectors_dir: Path) -> None:
    """每条都抛 DpeHashError 且错误码一致；声明了 path 的用例，位置也一致。

    ``input_json`` 用例走 ``model_validate_json``：重复键在解析阶段就以 ``DPE_VALIDATION`` 拒绝。
    """
    seen = 0
    for vec in _load(vectors_dir, "invalid"):
        for case in vec["cases"]:
            label = f"{vec['name']}/{case['name']}"
            model = _MODELS[case["object_kind"]]
            context = {"contract": case["contract"]}
            with pytest.raises(DpeHashError) as info:
                if "input_json" in case:
                    model.model_validate_json(case["input_json"], context=context)
                else:
                    model.model_validate(case["input"], context=context)
            assert info.value.code == case["code"], label
            if "path" in case:
                assert info.value.path == case["path"], label
            seen += 1
    assert seen >= 52


def test_invalid_vectors_page_standalone(vectors_dir: Path) -> None:
    """展开视图的页错误：单独构造 Page 时位置相对于页本身。"""
    for vec in _load(vectors_dir, "invalid"):
        for case in vec["cases"]:
            if case["object_kind"] != "expanded_document" or "path" not in case:
                continue
            page = case["input"]["pages"][0]
            with pytest.raises(DpeHashError) as info:
                Page.model_validate(page)
            assert info.value.code == case["code"]
            assert info.value.path == case["path"].removeprefix("/pages/0")


# ---------------------------------------------------------------------------
# Issue 要求的非法输入（关键字构造入口）
# ---------------------------------------------------------------------------


def test_undefined_field_rejected() -> None:
    with pytest.raises(UndefinedFieldError) as info:
        PageObject.model_validate({"elements": [], "number": 1})
    assert info.value.path == "/number"


@pytest.mark.parametrize("value", ["<p/>", None])
def test_category_disallowed_field_rejected_even_if_null(value: str | None) -> None:
    with pytest.raises(UndefinedFieldError) as info:
        ElementObject(category="NarrativeText", text_as_html=value)
    assert info.value.code == "DPE_VALIDATION"
    assert info.value.path == "/text_as_html"


def test_unknown_category_rejected() -> None:
    with pytest.raises(CategoryUnknownError) as info:
        ElementObject(category="Video")
    assert info.value.path == "/category"


def test_unknown_file_type_rejected() -> None:
    with pytest.raises(FileTypeUnknownError) as info:
        Document(file_type="markdown", pages=[])
    assert info.value.code == "DPE_VALIDATION"
    assert info.value.path == "/file_type"


@pytest.mark.parametrize("n", [2**53, -(2**53)])
def test_integer_out_of_range_rejected(n: int) -> None:
    with pytest.raises(IntegerOutOfRangeError) as info:
        ElementObject(category="Title", text="t", metadata={"id": [n]})
    assert info.value.path == "/metadata/id/0"


def test_integer_at_bound_accepted() -> None:
    bound = 2**53 - 1
    el = ElementObject(category="Title", metadata={"a": bound, "b": -bound})
    assert el.metadata == {"a": bound, "b": -bound}


@pytest.mark.parametrize(
    "raw",
    [
        b'{"category": "Title", "category": "Title"}',  # 重复键
        b'{"category": "Title", "metadata": {"x": NaN}}',  # 非 JSON 数值
        b'{"category": "Title"',  # 坏 JSON
        b'{"category": "Title", "text": "\xff"}',  # 非 UTF-8
    ],
)
def test_json_not_ijson_rejected(raw: bytes) -> None:
    with pytest.raises(DpeValidationError) as info:
        ElementObject.model_validate_json(raw)
    assert info.value.code == "DPE_VALIDATION"
    assert info.value.path == ""


# ---------------------------------------------------------------------------
# 内容等价（core §2.7）
# ---------------------------------------------------------------------------


def test_null_equivalent_to_missing() -> None:
    a = ElementObject(category="Title", text=None, metadata=None)
    b = ElementObject(category="Title")
    c = ElementObject(category="Title", metadata={})
    assert a.content_hash() == b.content_hash() == c.content_hash()
    # 原样表示保留：显式的 null 照样输出，未给出的字段不输出
    assert a.model_dump() == {"category": "Title", "text": None, "metadata": None}
    assert b.model_dump() == {"category": "Title"}


def test_empty_string_distinct_from_null() -> None:
    assert (
        ElementObject(category="Title", text="").content_hash()
        != ElementObject(category="Title").content_hash()
    )


def test_document_equivalence() -> None:
    a = Document(file_type="md", title=None, pages=[Page(elements=[], page_metadata=None)])
    b = Document.model_validate(
        {"file_type": "md", "doc_metadata": {}, "pages": [{"elements": []}]}
    )
    assert a.doc_hash() == b.doc_hash()
    assert a != b  # == 只比较结构，内容等价由 hash 判定


# ---------------------------------------------------------------------------
# 构造语义
# ---------------------------------------------------------------------------


def test_mixed_instances_and_dicts() -> None:
    el = ElementObject(category="NarrativeText", text="a")
    doc = Document.model_validate(
        {
            "file_type": "txt",
            "pages": [Page(elements=[el]), {"elements": [el, {"category": "Title", "text": "b"}]}],
        }
    )
    assert all(isinstance(p, Page) for p in doc.pages)
    assert all(isinstance(e, ElementObject) for p in doc.pages for e in p.elements)
    assert doc.hashes()["pages"][0]["elements"] == [
        content_hash({"category": "NarrativeText", "text": "a"})
    ]


def test_mixed_instances_validated_in_spec_order() -> None:
    """已构造的实例也随整树重新校验，错误位置相对于文档。"""
    with pytest.raises(CategoryUnknownError) as info:
        Document.model_validate(
            {
                "file_type": "txt",
                "pages": [Page(elements=[]), {"elements": [{"category": "Video"}]}],
            }
        )
    assert info.value.path == "/pages/1/elements/0/category"


def test_input_is_copied() -> None:
    metadata: dict[str, Any] = {"tags": ["a"]}
    raw: dict[str, Any] = {"category": "Title", "metadata": metadata}
    el = ElementObject.model_validate(raw)
    before = el.content_hash()
    metadata["tags"].append("b")
    raw["text"] = "x"
    assert el.metadata == {"tags": ["a"]}
    assert el.content_hash() == before


def test_frozen() -> None:
    el = ElementObject(category="Title")
    with pytest.raises(PydanticValidationError):
        setattr(el, "text", "x")  # noqa: B010 —— 验证运行时 frozen


def test_hash_recomputed_after_in_place_metadata_change() -> None:
    """metadata 原地修改不受保护，但 hash 不缓存，始终按当前内容计算。"""
    el = ElementObject(category="Title", metadata={"a": 1})
    before = el.content_hash()
    assert el.metadata is not None
    el.metadata["a"] = 2
    assert el.content_hash() != before
    assert el.content_hash() == content_hash({"category": "Title", "metadata": {"a": 2}})


def test_embedded_in_other_model_wraps_error() -> None:
    class Envelope(BaseModel):
        element: ElementObject

    with pytest.raises(PydanticValidationError) as info:
        Envelope.model_validate({"element": {"category": "Video"}})
    (error,) = info.value.errors()
    assert error["loc"] == ("element",)
    assert isinstance(error["ctx"]["error"], CategoryUnknownError)


def test_wire_contract_from_context() -> None:
    child = "dpe2:" + "0" * 64
    with pytest.raises(DpeHashError) as info:
        PageObject.model_validate({"elements": [child]})
    assert info.value.code == "DPE_CONTRACT_UNSUPPORTED"
    page = PageObject.model_validate({"elements": [child]}, context={"contract": "dpe2"})
    assert page.page_hash("dpe2").startswith("dpe2:")


def test_model_copy_update_revalidated() -> None:
    el = ElementObject(category="NarrativeText", text="x")
    with pytest.raises(UndefinedFieldError) as info:
        el.model_copy(update={"text_as_html": "<p/>"})
    assert info.value.path == "/text_as_html"
    with pytest.raises(CategoryUnknownError):
        el.model_copy(update={"category": "Video"})
    copied = el.model_copy(update={"text": "y"})
    assert copied.model_dump() == {"category": "NarrativeText", "text": "y"}
    assert el.model_copy() == el


@pytest.mark.parametrize("sign", ["", "-"])
def test_integer_beyond_python_digit_limit_reported_at_value(sign: str) -> None:
    """超过 Python int 位数上限的整数也不在解析阶段拒绝，越界在出错的值上报告（core §2.8）。"""
    raw = '{"category": "Title", "metadata": {"a": ' + sign + "9" * 5000 + "}}"
    with pytest.raises(IntegerOutOfRangeError) as info:
        ElementObject.model_validate_json(raw)
    assert info.value.path == "/metadata/a"
