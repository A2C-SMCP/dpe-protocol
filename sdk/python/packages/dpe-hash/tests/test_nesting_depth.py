"""core §2.8 第 0 步第三项：对象嵌套深度上界（#94）。

消费 ``vectors/nesting_depth.json``：对象级入口（本文件）与文本入口（dpe-sdk 的
``tests/test_nesting_depth_entry_points.py``）对同一条输入 MUST 得出同一结论。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest
from dpe_hash import (
    MAX_NESTING_DEPTH,
    DocumentFields,
    DpeHashError,
    ElementObject,
    ExpandedDocument,
    PageFields,
    children,
    content_hash,
    doc_hash,
    document_hashes,
    nesting_depth,
    object_hash,
    page_hash,
)

V = "DPE_VALIDATION"


def _load(vectors_dir: Path) -> dict[str, Any]:
    text = (vectors_dir / "nesting_depth.json").read_text(encoding="utf-8")
    return cast(dict[str, Any], json.loads(text))


def _entries(kind: Any) -> list[Callable[[Any, str], object]]:
    """拒绝/接受类用例要经过的对象级入口（按对象层级）。"""

    def via_object_hash(obj: Any, contract: str) -> object:
        return object_hash(obj, kind, contract)

    def via_children(obj: Any, contract: str) -> object:
        return children(obj, kind, contract)

    if kind == "element":
        return [via_object_hash, via_children, content_hash]
    if kind in ("page", "document"):
        return [via_object_hash, via_children]
    assert kind == "expanded_document", kind
    return [document_hashes]


def test_nesting_depth_vector_entries(vectors_dir: Path) -> None:
    """向量逐条经对象级入口：接受的不许抛；拒绝的 code（与声明的 path）一致。"""
    vec = _load(vectors_dir)
    assert vec["limit"] == MAX_NESTING_DEPTH
    for case in vec["cases"]:
        label = f"{vec['name']}/{case['name']}"
        for call in _entries(case["object_kind"]):
            if case.get("accept"):
                call(case["input"], case["contract"])
                continue
            with pytest.raises(DpeHashError) as info:
                call(case["input"], case["contract"])
            assert info.value.code == case["code"], label
            if "path" in case:
                assert info.value.path == case["path"], label


def test_counting_convention_examples() -> None:
    """计数口径（core §2.8，#94 裁决）：标量记 0；对象或数组记 1 + 子值最大深度；空容器记 1。"""
    assert nesting_depth(None) == 0
    assert nesting_depth("x") == 0
    assert nesting_depth(1) == 0
    assert nesting_depth({}) == 1
    assert nesting_depth([]) == 1
    assert nesting_depth({"a": 1}) == 1
    assert nesting_depth({"a": {"b": [1]}}) == 3
    assert nesting_depth({"a": {"b": []}}) == 3


def test_child_hash_lists_do_not_count_inward() -> None:
    """页的 elements、文档的 pages 按子 hash 列表计：列表自身记 1，向内不再计数。"""
    assert nesting_depth({"elements": ["dpe1:" + "a" * 64]}, "elements") == 2
    assert nesting_depth({"elements": []}, "elements") == 2
    # 展开视图里内联的元素对象同样不计入页/文档的深度
    nested = {"elements": [{"category": "Title", "metadata": _nested_empty(20)}]}
    assert nesting_depth(nested, "elements") == 2


def _nested_empty(levels: int, leaf: Any = None) -> Any:
    """恰好 levels 层的容器链，最深一层是空容器。"""
    value: Any = {} if leaf is None else leaf
    for _ in range(levels - 1):
        value = {"k": value}
    return value


def test_boundary_is_accepted_and_next_level_rejected() -> None:
    """恰好 MAX_NESTING_DEPTH 层接受、加一层拒绝（core §2.8）；违例位置为对象自身。"""
    ok: ElementObject = {
        "category": "Title",
        "text": "x",
        "metadata": _nested_empty(MAX_NESTING_DEPTH - 1),
    }
    assert content_hash(ok, "dpe1").startswith("dpe1:")
    over: ElementObject = {
        "category": "Title",
        "text": "x",
        "metadata": _nested_empty(MAX_NESTING_DEPTH),
    }
    with pytest.raises(DpeHashError) as info:
        content_hash(over, "dpe1")
    assert (info.value.code, info.value.path) == (V, "")


def test_layered_entries_check_depth() -> None:
    """page_hash / doc_hash 等逐层入口同样在第 0 步判定深度（core §2.8：每个公开入口）。"""
    page_ok: PageFields = {"page_metadata": _nested_empty(MAX_NESTING_DEPTH - 1)}
    assert page_hash(page_ok, [], "dpe1").startswith("dpe1:")
    over_page: PageFields = {"page_metadata": _nested_empty(MAX_NESTING_DEPTH)}
    with pytest.raises(DpeHashError) as info:
        page_hash(over_page, [], "dpe1")
    assert (info.value.code, info.value.path) == (V, "")

    doc_ok: DocumentFields = {
        "file_type": "md",
        "doc_metadata": _nested_empty(MAX_NESTING_DEPTH - 1),
    }
    assert doc_hash(doc_ok, [], "dpe1").startswith("dpe1:")
    over_doc: DocumentFields = {
        "file_type": "md",
        "doc_metadata": _nested_empty(MAX_NESTING_DEPTH),
    }
    with pytest.raises(DpeHashError) as info:
        doc_hash(over_doc, [], "dpe1")
    assert (info.value.code, info.value.path) == (V, "")


def test_unknown_object_kind_still_raises_value_error() -> None:
    """未知 kind 与既有行为一致：抛 ``ValueError``，不是 ``KeyError``。"""
    for call in (
        lambda: object_hash({"category": "Title"}, cast(Any, "pagex"), "dpe1"),
        lambda: children({"category": "Title"}, cast(Any, "pagex"), "dpe1"),
    ):
        with pytest.raises(ValueError, match="未知的对象层级"):
            call()


def test_depth_checked_before_category() -> None:
    """第 0 步先于第 2 步：超深且 category 未知时报 DPE_VALIDATION，不是 DPE_CATEGORY_UNKNOWN。"""
    over: ElementObject = {"category": "Video", "metadata": _nested_empty(MAX_NESTING_DEPTH)}
    with pytest.raises(DpeHashError) as info:
        content_hash(over, "dpe1")
    assert info.value.code == V


def test_deep_object_entry_rejects_iteratively() -> None:
    """远超上界的输入由对象级入口按 N 判定，不落进解释器递归限额（core §2.8）。"""
    deep: dict[str, Any] = {"category": "Title", "text": "x", "metadata": {"k": {}}}
    node: Any = deep["metadata"]
    for _ in range(5000):
        node["k"] = {"k": node["k"]}
    with pytest.raises(DpeHashError) as info:
        content_hash(cast(ElementObject, deep), "dpe1")
    assert info.value.code == V


def test_expanded_view_depth_is_per_object() -> None:
    """展开视图的深度按每个对象自身计：内联元素恰好 64 层时文档与页不被它的层数放大。"""
    element: ElementObject = {
        "category": "Title",
        "text": "x",
        "metadata": _nested_empty(MAX_NESTING_DEPTH - 1),
    }
    document: ExpandedDocument = {"file_type": "md", "pages": [{"elements": [element]}]}
    assert document_hashes(document, "dpe1")["doc_hash"].startswith("dpe1:")
    over_element: ElementObject = {
        "category": "Title",
        "text": "x",
        "metadata": _nested_empty(MAX_NESTING_DEPTH),
    }
    over: ExpandedDocument = {"file_type": "md", "pages": [{"elements": [over_element]}]}
    with pytest.raises(DpeHashError) as info:
        document_hashes(over, "dpe1")
    assert info.value.code == V
