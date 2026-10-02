"""一致性向量：dpe_hash 逐字节通过 vectors/ 全部向量，含 dpe2 升级演练、relations 与 preimages。"""

from __future__ import annotations

import decimal
import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from dpe_hash import (
    DRILL_CONTRACT,
    DpeHashError,
    children,
    content_hash,
    doc_hash,
    document_hashes,
    jcs,
    object_hash,
    page_hash,
)
from dpe_hash.contract1 import _document_preimage, _element_preimage, _page_preimage
from dpe_hash.jcs import pointer


def _load(vectors_dir: Path, kind: str) -> list[dict[str, Any]]:
    vectors = [
        json.loads(f.read_text(encoding="utf-8"))
        for f in sorted(vectors_dir.glob("*.json"))
        if f.name != "manifest.json"
    ]
    selected = [v for v in vectors if v["kind"] == kind]
    assert selected, f"vectors/ 中没有 kind={kind} 的向量"
    return selected


def _resolve(computed: dict[str, Any], ref: str) -> Any:
    """按 vectors/README.md 的引用语法取值：``<文档键>.<路径>``。"""
    key, *path = ref.split(".")
    node: Any = computed[key]
    for part in path:
        node = node[int(part)] if isinstance(node, list) else node[part]
    return node


def _digest(contract: str, preimage: str) -> str:
    salt = b"dpe2" if contract == DRILL_CONTRACT else b""
    return f"{contract}:{hashlib.sha256(salt + preimage.encode('utf-8')).hexdigest()}"


def test_document_vectors(vectors_dir: Path) -> None:
    for vec in _load(vectors_dir, "document"):
        contracts = {c for exp in vec["expected"].values() for c in exp}
        for contract in sorted(contracts):
            computed: dict[str, Any] = {}
            for key, document in vec["documents"].items():
                expected = dict(vec["expected"][key][contract])
                preimages = expected.pop("preimages", None)
                got = document_hashes(document, contract)
                assert got == expected, f"{vec['name']}/{key}/{contract}"
                assert _layered(document, contract) == expected, f"{vec['name']}/{key}/{contract}"
                assert _wire(document, contract) == expected, f"{vec['name']}/{key}/{contract}"
                if preimages is not None:
                    _check_preimages(document, got, preimages, contract)
                computed[key] = got
            # relations 是向量要证明的规范性质，消费方一并断言（vectors/README.md）
            for rel in vec.get("relations", []):
                ((op, refs),) = rel.items()
                values = [_resolve(computed, r) for r in refs]
                if op == "equal":
                    assert len(set(values)) == 1, f"{vec['name']}/{contract}: equal {refs}"
                else:
                    assert op == "distinct", op
                    assert len(set(values)) == len(values), f"{vec['name']}/{contract}: {refs}"


def _layered(document: dict[str, Any], contract: str) -> dict[str, Any]:
    """逐层上溯：content_hash → page_hash(页字段, 子 hash) → doc_hash(文档字段, 子 hash)。"""
    # 向量是未加类型的 JSON 数据，对应调用方从线上反序列化得到的 Any
    pages: list[dict[str, Any]] = []
    for page in document["pages"]:
        hashes = [content_hash(el, contract) for el in page["elements"]]
        page_fields: Any = {k: v for k, v in page.items() if k != "elements"}
        pages.append({"page_hash": page_hash(page_fields, hashes, contract), "elements": hashes})
    doc_fields: Any = {k: v for k, v in document.items() if k != "pages"}
    page_hashes = [p["page_hash"] for p in pages]
    return {"doc_hash": doc_hash(doc_fields, page_hashes, contract), "pages": pages}


def _wire(document: dict[str, Any], contract: str) -> dict[str, Any]:
    """线上原像：每层对象带子 hash 列表，经 object_hash 计算。"""
    pages = []
    for page in document["pages"]:
        hashes = [object_hash(el, "element", contract) for el in page["elements"]]
        wire_page = {**page, "elements": hashes}
        pages.append({"page_hash": object_hash(wire_page, "page", contract), "elements": hashes})
    wire_doc = {**document, "pages": [p["page_hash"] for p in pages]}
    return {"doc_hash": object_hash(wire_doc, "document", contract), "pages": pages}


def _check_preimages(
    document: dict[str, Any], got: Any, preimages: dict[str, Any], contract: str
) -> None:
    """向量给出的原像：其摘要等于对应 hash，且本实现规范化出的原像与之逐字节相同。"""
    assert _digest(contract, preimages["document"]) == got["doc_hash"]
    page_hashes = [p["page_hash"] for p in got["pages"]]
    assert preimages["document"] == _document_preimage(
        document, page_hashes, contract, "", with_children=True
    )
    for i, (page, page_pre, element_pres) in enumerate(
        zip(document["pages"], preimages["pages"], preimages["elements"], strict=True)
    ):
        page_got = got["pages"][i]
        assert _digest(contract, page_pre) == page_got["page_hash"]
        assert page_pre == _page_preimage(
            page, page_got["elements"], contract, pointer("pages", i), with_children=True
        )
        assert [_digest(contract, p) for p in element_pres] == page_got["elements"]
        assert element_pres == [_element_preimage(el, "") for el in page["elements"]]


def test_vectors_cover_drill_contract(vectors_dir: Path) -> None:
    drills = [
        v
        for v in _load(vectors_dir, "document")
        if any(DRILL_CONTRACT in exp for exp in v["expected"].values())
    ]
    assert drills, "向量应包含 dpe2 升级演练（契约 1 §6）"


@pytest.mark.parametrize("name", ["jcs_basic", "jcs_numbers"])
def test_jcs_vectors(vectors_dir: Path, name: str) -> None:
    vec = json.loads((vectors_dir / f"{name}.json").read_text(encoding="utf-8"))
    for case in vec["cases"]:
        canonical = jcs(case["input"])
        assert canonical == case["canonical"], f"{name}: {case['input']!r}"
        assert hashlib.sha256(canonical.encode("utf-8")).hexdigest() == case["sha256"]


def test_all_jcs_vectors_listed(vectors_dir: Path) -> None:
    assert {v["name"] for v in _load(vectors_dir, "jcs")} == {"jcs_basic", "jcs_numbers"}


def test_jcs_numbers_ignore_decimal_context(vectors_dir: Path) -> None:
    """数字序列化不受调用方线程全局 decimal 上下文影响（否则浮点 metadata 的 hash 会静默改变）。"""
    vec = json.loads((vectors_dir / "jcs_numbers.json").read_text(encoding="utf-8"))
    with decimal.localcontext() as ctx:
        ctx.prec = 6
        ctx.rounding = decimal.ROUND_DOWN
        for case in vec["cases"]:
            assert jcs(case["input"]) == case["canonical"]
        assert jcs(0.1234567890123456) == "0.1234567890123456"


class _DuplicateKey(Exception):
    """严格解析器在解析阶段拒绝重复键（I-JSON，core §2.8 第 0 步）。"""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    if len({k for k, _ in pairs}) != len(pairs):
        raise _DuplicateKey
    return dict(pairs)


def _entries(kind: Any) -> list[Callable[[Any, str], object]]:
    """拒绝类用例要经过的入口：展开视图走 document_hashes，线上原像走 object_hash 与 children。"""
    if kind == "expanded_document":
        return [document_hashes]

    def via_object_hash(obj: Any, contract: str) -> object:
        return object_hash(obj, kind, contract)

    def via_children(obj: Any, contract: str) -> object:
        return children(obj, kind, contract)

    return [via_object_hash, via_children]


def test_invalid_vectors(vectors_dir: Path) -> None:
    """拒绝类向量（core.md §2.8）：每条都被拒绝且错误码一致；声明了 path 的用例，位置也一致。

    ``input_json`` 用例按 vectors/README.md 以拒绝重复键的严格解析器读取：解析阶段拒绝即视为
    ``DPE_VALIDATION``、位置 ``""``（重复键属于解析器职责，dpe-hash 接收的是解析后的对象）。
    """
    for vec in _load(vectors_dir, "invalid"):
        for case in vec["cases"]:
            label = f"{vec['name']}/{case['name']}"
            if "input_json" in case:
                try:
                    obj = json.loads(case["input_json"], object_pairs_hook=_reject_duplicate_keys)
                except _DuplicateKey:  # 只认重复键；坏 JSON 照常报错，不会被静默当作通过
                    assert case["code"] == "DPE_VALIDATION", label
                    assert case.get("path", "") == "", label
                    continue
            else:
                obj = case["input"]
            for call in _entries(case["object_kind"]):
                with pytest.raises(DpeHashError) as info:
                    call(obj, case["contract"])
                assert info.value.code == case["code"], label
                if "path" in case:
                    assert info.value.path == case["path"], label
