"""每类违例的异常类型、规范错误码与 JSON Pointer 路径。"""

from __future__ import annotations

import math
from typing import Any

import pytest
from dpe_hash import (
    CategoryUnknownError,
    ContractUnsupportedError,
    DpeHashError,
    FileTypeUnknownError,
    IntegerOutOfRangeError,
    InvalidUnicodeError,
    UndefinedFieldError,
    ValidationError,
    children,
    content_hash,
    doc_hash,
    document_hashes,
    jcs,
    object_hash,
    page_hash,
    parse_blob_ref,
    parse_hash,
)

H = "dpe1:" + "a" * 64


def _raises(error: type[DpeHashError], code: str, path: str, fn: Any, *args: Any) -> None:
    with pytest.raises(error) as info:
        fn(*args)
    assert info.value.code == code
    assert info.value.path == path
    assert isinstance(info.value, ValueError)


@pytest.mark.parametrize(
    ("error", "code", "path", "obj", "kind"),
    [
        (
            CategoryUnknownError,
            "DPE_CATEGORY_UNKNOWN",
            "/category",
            {"category": "Video"},
            "element",
        ),
        (ValidationError, "DPE_VALIDATION", "", {"text": "x"}, "element"),
        (
            UndefinedFieldError,
            "DPE_VALIDATION",
            "/text_as_html",
            {"category": "NarrativeText", "text_as_html": "<p/>"},
            "element",
        ),
        (
            ValidationError,
            "DPE_VALIDATION",
            "/text",
            {"category": "NarrativeText", "text": 1},
            "element",
        ),
        (
            ValidationError,
            "DPE_VALIDATION",
            "/blob",
            {"category": "Image", "blob": "https://a/x.png"},
            "element",
        ),
        (
            ValidationError,
            "DPE_VALIDATION",
            "/metadata",
            {"category": "Title", "metadata": [1]},
            "element",
        ),
        (
            IntegerOutOfRangeError,
            "DPE_VALIDATION",
            "/metadata/a~1b/0",
            {"category": "Title", "metadata": {"a/b": [2**53]}},
            "element",
        ),
        (
            ValidationError,
            "DPE_VALIDATION",
            "/metadata/x",
            {"category": "Title", "metadata": {"x": math.nan}},
            "element",
        ),
        (
            ValidationError,
            "DPE_VALIDATION",
            "/metadata/s",
            {"category": "Title", "metadata": {"s": {1, 2}}},
            "element",
        ),
        (
            InvalidUnicodeError,
            "DPE_VALIDATION",
            "",
            {"category": "Title", "text": "bad \ud800"},
            "element",
        ),
        (UndefinedFieldError, "DPE_VALIDATION", "/number", {"number": 1, "elements": []}, "page"),
        (ValidationError, "DPE_VALIDATION", "", {"title": "p"}, "page"),
        (
            ContractUnsupportedError,
            "DPE_CONTRACT_UNSUPPORTED",
            "/elements/0",
            {"elements": ["a" * 64]},
            "page",
        ),
        (
            ContractUnsupportedError,
            "DPE_CONTRACT_UNSUPPORTED",
            "/elements/1",
            {"elements": [H, "dpe2:" + "a" * 64]},
            "page",
        ),
        (ValidationError, "DPE_VALIDATION", "/elements/0", {"elements": ["dpe1:ABC"]}, "page"),
        (ValidationError, "DPE_VALIDATION", "/elements", {"elements": H}, "page"),
        (
            FileTypeUnknownError,
            "DPE_VALIDATION",
            "/file_type",
            {"file_type": "markdown", "pages": []},
            "document",
        ),
        (ValidationError, "DPE_VALIDATION", "", {"file_type": "md"}, "document"),
        (
            UndefinedFieldError,
            "DPE_VALIDATION",
            "/attributes",
            {"file_type": "md", "pages": [], "attributes": {}},
            "document",
        ),
    ],
)
def test_object_hash_errors(
    error: type[DpeHashError], code: str, path: str, obj: dict[str, Any], kind: Any
) -> None:
    _raises(error, code, path, object_hash, obj, kind)
    _raises(error, code, path, children, obj, kind)  # children 先完整校验


def test_page_hash_rejects_elements_key() -> None:
    _raises(UndefinedFieldError, "DPE_VALIDATION", "/elements", page_hash, {"elements": []}, [])


def test_doc_hash_rejects_pages_key() -> None:
    _raises(
        UndefinedFieldError,
        "DPE_VALIDATION",
        "/pages",
        doc_hash,
        {"file_type": "md", "pages": []},
        [],
    )


def test_child_hash_paths_in_layered_api() -> None:
    _raises(ValidationError, "DPE_VALIDATION", "/elements/1", page_hash, {}, [H, "dpe1:zz"])
    _raises(ValidationError, "DPE_VALIDATION", "/pages/0", doc_hash, {"file_type": "md"}, [1])


def test_mixed_contracts_rejected() -> None:
    """dpe2 页对象引用 dpe1 元素：契约混用。"""
    _raises(ValidationError, "DPE_VALIDATION", "/elements/0", page_hash, {}, [H], "dpe2")


def test_unsupported_contract_argument() -> None:
    _raises(
        ContractUnsupportedError,
        "DPE_CONTRACT_UNSUPPORTED",
        "",
        content_hash,
        {"category": "Title"},
        "dpe9",
    )


def test_document_hashes_error_path() -> None:
    doc = {
        "file_type": "md",
        "pages": [
            {"elements": []},
            {
                "elements": [
                    {"category": "Title"},
                    {"category": "Title", "metadata": {"n": -(2**60)}},
                ]
            },
        ],
    }
    _raises(
        IntegerOutOfRangeError,
        "DPE_VALIDATION",
        "/pages/1/elements/1/metadata/n",
        document_hashes,
        doc,
    )


@pytest.mark.parametrize(
    ("value", "error"),
    [
        ("a" * 64, ContractUnsupportedError),
        ("sha256:" + "a" * 64, ContractUnsupportedError),
        ("dpe1:" + "A" * 64, ValidationError),
        ("dpe1:" + "a" * 63, ValidationError),
        (None, ValidationError),
    ],
)
def test_parse_hash_errors(value: Any, error: type[DpeHashError]) -> None:
    with pytest.raises(error):
        parse_hash(value)


@pytest.mark.parametrize(
    "value", ["sha256:" + "a" * 63, "dpe1:" + "a" * 64, "SHA256:" + "a" * 64, 7]
)
def test_parse_blob_ref_errors(value: Any) -> None:
    _raises(ValidationError, "DPE_VALIDATION", "", parse_blob_ref, value)


def test_jcs_rejects_non_json() -> None:
    _raises(ValidationError, "DPE_VALIDATION", "", jcs, math.inf)
    _raises(IntegerOutOfRangeError, "DPE_VALIDATION", "/0", jcs, [-(2**53)])
    _raises(ValidationError, "DPE_VALIDATION", "", jcs, {1: "x"})
    _raises(InvalidUnicodeError, "DPE_VALIDATION", "/\ud800", jcs, {"\ud800": 1})
    assert jcs(2**53 - 1) == "9007199254740991"


def test_parse_hash_contracts_must_be_a_collection() -> None:
    """单个字符串不是契约集合：否则 ``in`` 退化为子串匹配，放行 ``":"+hex`` 这类非法前缀。"""
    with pytest.raises(TypeError):
        parse_hash(":" + "a" * 64, "dpe1")
    with pytest.raises(ContractUnsupportedError):
        parse_hash(":" + "a" * 64, ("dpe1",))
    with pytest.raises(ContractUnsupportedError):
        parse_hash("dpe9:" + "a" * 64, ("dpe1", "dpe9"))  # 本包不认识的契约


def test_expanded_view_checks_document_fields_before_pages() -> None:
    """展开视图同样按 core §2.8：文档自身字段先于各页，页自身字段先于元素。"""
    bad_element = {"category": "Video"}
    _raises(
        FileTypeUnknownError,
        "DPE_VALIDATION",
        "/file_type",
        document_hashes,
        {"file_type": "markdown", "pages": [{"elements": [bad_element]}]},
    )
    _raises(
        IntegerOutOfRangeError,
        "DPE_VALIDATION",
        "/pages/0/page_metadata/n",
        document_hashes,
        {"file_type": "md", "pages": [{"page_metadata": {"n": 2**53}, "elements": [bad_element]}]},
    )


def test_ijson_precedes_category_and_contract_checks() -> None:
    """core §2.8 第 0 步：孤立代理项先于 category / 子 hash 前缀判定，位置为对象自身。"""
    _raises(
        InvalidUnicodeError,
        "DPE_VALIDATION",
        "",
        object_hash,
        {"category": "Video", "text": "\ud800"},
        "element",
    )
    _raises(InvalidUnicodeError, "DPE_VALIDATION", "", page_hash, {"title": "\ud800"}, ["a" * 64])
    _raises(
        InvalidUnicodeError,
        "DPE_VALIDATION",
        "",
        document_hashes,
        {"file_type": "md", "pages": [{"elements": [{"category": "Video", "text": "\ud800"}]}]},
    )


def test_required_field_null_is_missing() -> None:
    _raises(ValidationError, "DPE_VALIDATION", "", object_hash, {"elements": None}, "page")
    _raises(ValidationError, "DPE_VALIDATION", "", object_hash, {"category": None}, "element")
    _raises(
        ValidationError,
        "DPE_VALIDATION",
        "",
        object_hash,
        {"file_type": None, "pages": []},
        "document",
    )
