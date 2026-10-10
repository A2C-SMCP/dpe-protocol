"""core §2.8 第 0 步第三项（#94）：嵌套深度上界的文本入口与参考服务端。

消费 ``vectors/nesting_depth.json``：文本入口（严格解析 ``input_json`` 后校验，本文件）与
对象级入口（dpe-hash 的 ``tests/test_nesting_depth.py``）对同一条输入 MUST 同判；参考服务端把
用例放进 commit 信封后，信封的固定层数不计入对象深度。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import dpe_hash
import pytest
from dpe_sdk import errors
from dpe_sdk.models import Document, DocumentObject, ElementObject, PageObject
from dpe_sdk.testing import IfAbsent
from engine_helpers import inline, make_engine, text_doc
from pydantic import BaseModel

C = dpe_hash.CONTRACT

_MODELS: dict[str, type[BaseModel]] = {
    "element": ElementObject,
    "page": PageObject,
    "document": DocumentObject,
    "expanded_document": Document,
}


def _load(vectors_dir: Path) -> dict[str, Any]:
    text = (vectors_dir / "nesting_depth.json").read_text(encoding="utf-8")
    return cast(dict[str, Any], json.loads(text))


def test_text_entry_agrees_with_object_entry(vectors_dir: Path) -> None:
    """同一条用例经文本入口与对象级入口：接受例都不抛；拒绝例 code（与声明的 path）一致。"""
    vec = _load(vectors_dir)
    assert vec["limit"] == dpe_hash.MAX_NESTING_DEPTH
    for case in vec["cases"]:
        model = _MODELS[case["object_kind"]]
        context = {"contract": case["contract"]}
        if case.get("accept"):
            model.model_validate_json(case["input_json"], context=context)
            model.model_validate(case["input"], context=context)
            continue
        with pytest.raises(dpe_hash.DpeHashError) as info:
            model.model_validate_json(case["input_json"], context=context)
        assert info.value.code == case["code"], case["name"]
        if "path" in case:
            assert info.value.path == case["path"], case["name"]
        with pytest.raises(dpe_hash.DpeHashError) as info:
            model.model_validate(case["input"], context=context)
        assert info.value.code == case["code"], case["name"]


def _accept_body(kind: str, obj: dict[str, Any]) -> dict[str, Any]:
    """接受例的 commit 信封：引用关系由用例对象现算的 hash 织好，提交应成功。"""
    if kind == "document":
        return {"document": obj}
    if kind == "page":
        page_hash = dpe_hash.object_hash(obj, "page", C)
        return {"document": {"file_type": "md", "pages": [page_hash]}, "pages": [obj]}
    assert kind == "element", kind
    content_hash = dpe_hash.object_hash(obj, "element", C)
    page = {"elements": [content_hash]}
    page_hash = dpe_hash.object_hash(page, "page", C)
    return {
        "document": {"file_type": "md", "pages": [page_hash]},
        "pages": [page],
        "objects": [obj],
    }


def _reject_body(kind: str, obj: Any) -> dict[str, Any]:
    """拒绝例的 commit 信封：对象本身非法，不能本地算 hash，按层级原样放进请求体。"""
    if kind == "document":
        return {"document": obj}
    key = "pages" if kind == "page" else "objects"
    return {"document": inline(text_doc(["x"])).document, key: [obj]}


def test_reference_server_counts_per_object(vectors_dir: Path) -> None:
    """参考服务端：恰好 64 层经 commit 信封成功、加一层返回 DPE_VALIDATION（信封层数不计入）。"""
    engine = make_engine()
    vec = _load(vectors_dir)
    for case in vec["cases"]:
        kind = case["object_kind"]
        if kind == "expanded_document":
            continue  # 线上没有展开视图这种对象：它只经 SDK 的展开入口消费
        uri = f"test://depth/{case['name']}"
        if case.get("accept"):
            body = _accept_body(kind, case["input"])
        else:
            body = _reject_body(kind, case["input"])
        payload = json.dumps(body).encode("utf-8")
        if case.get("accept"):
            result = engine.commit("u", uri, payload, C, IfAbsent())
            assert result.doc_hash.startswith(f"{C}:")
        else:
            with pytest.raises(errors.DpeError) as info:
                engine.commit("u", uri, payload, C, IfAbsent())
            assert info.value.code == case["code"], case["name"]
