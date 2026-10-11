"""章：报文校验与校验顺序（#27 子集 1）。

- CORE §2.8：对象校验（封闭 schema：未定义字段、category 未允许的内容字段 → ``DPE_VALIDATION``；
  整数字面量超 2^53−1 → ``DPE_VALIDATION``，CORE §2.6）；
- HTTP §3.1：校验顺序（传输层四步 → I-JSON → 契约 → 信封 → 对象；授权之前的校验链）；
- HTTP §3.1：uri 的文法校验在契约声明之后（无体端点「契约 → uri（文法）」）。
"""

from __future__ import annotations

import json
from typing import Any

from dpe_conformance.config import Identity
from dpe_conformance.plugin import Target
from dpe_conformance.support import (
    assert_head_error,
    assert_problem,
    build_document,
    optional_identity,
    text_element,
)

_PLACEHOLDER_HASH = "dpe1:" + "0" * 64


def _commit_body() -> dict[str, Any]:
    return build_document([[text_element("hello")]]).commit_body()


def _invalid_element_body(element: dict[str, Any]) -> dict[str, Any]:
    """含一个非法元素对象的 commit 体：hash 用占位值——报文校验是 schema 层判定，
    先于任何 hash 重算；非法对象无法（也不该）经 dpe-hash 构造。"""
    return {
        "document": {"file_type": "md", "pages": [_PLACEHOLDER_HASH]},
        "pages": [{"elements": [_PLACEHOLDER_HASH]}],
        "objects": [element],
    }


def test_undefined_body_member(conformance_target: Target, full_identity: Identity) -> None:
    """HTTP §3.1（请求信封）+ CORE §3.3 第 1 步第 3 项：commit 请求体**顶层**出现未定义
    成员（信封封闭）→ 400 + DPE_VALIDATION。"""
    uri = conformance_target.uri("validation/extra-member")
    body = {**_commit_body(), "extra": 1}
    response = conformance_target.send(
        "PUT", "documents", identity=full_identity, params={"uri": uri}, json=body
    )
    assert_problem(response, status=400, code="DPE_VALIDATION")


def test_category_disallowed_field(conformance_target: Target, full_identity: Identity) -> None:
    """CORE §2.8 第 3 步 + CORE §2.3：元素带其 category 未允许的字段（NarrativeText 带
    ``text_as_html``）→ 400 + DPE_VALIDATION；且校验先于前置条件存在性（请求未带任何条件头）。"""
    uri = conformance_target.uri("validation/category-field")
    body = _invalid_element_body(
        {"category": "NarrativeText", "text": "x", "text_as_html": "<p>x</p>"}
    )
    response = conformance_target.send(
        "PUT", "documents", identity=full_identity, params={"uri": uri}, json=body
    )
    assert_problem(response, status=400, code="DPE_VALIDATION")


def test_integer_over_2_53(conformance_target: Target, full_identity: Identity) -> None:
    """CORE §2.6 + §2.8 第 4 步：整数字面量超过 2^53−1（如 metadata 中的超大 id）→ 400。"""
    uri = conformance_target.uri("validation/integer")
    body = _invalid_element_body(
        {"category": "NarrativeText", "text": "x", "metadata": {"id": 2**53}}
    )
    response = conformance_target.send(
        "PUT", "documents", identity=full_identity, params={"uri": uri}, json=body
    )
    assert_problem(response, status=400, code="DPE_VALIDATION")


def test_non_json_body_precedes_contract(
    conformance_target: Target, full_identity: Identity
) -> None:
    """HTTP §3.1：I-JSON 先于契约声明——非 JSON 体且缺契约头时得到 DPE_VALIDATION。"""
    uri = conformance_target.uri("validation/non-json")
    response = conformance_target.send(
        "PUT",
        "documents",
        identity=full_identity,
        contract=None,
        params={"uri": uri},
        content=b"{",
        headers={"Content-Type": "application/json"},
    )
    assert_problem(response, status=400, code="DPE_VALIDATION")


def test_transport_query_form_precedes_contract(conformance_target: Target) -> None:
    """HTTP §3.1：查询参数形态是传输层第 3 步——``HEAD`` 缺 ``uri`` 时即使契约头也缺失，
    先报的是 DPE_VALIDATION（缺 ``uri``），不是契约错误。"""
    response = conformance_target.send(
        "HEAD", "documents", identity=optional_identity(conformance_target), contract=None
    )
    assert_head_error(response, status=400, code="DPE_VALIDATION")


def test_contract_precedes_uri_grammar(conformance_target: Target) -> None:
    """HTTP §3.1：无体端点的「契约 → uri（文法）」——uri 值不含法时先报契约错误；
    带上契约后同一请求才轮到 DPE_VALIDATION。"""
    bad_uri = "zzz-no-scheme"
    without_contract = conformance_target.send(
        "HEAD",
        "documents",
        identity=optional_identity(conformance_target),
        contract=None,
        params={"uri": bad_uri},
    )
    assert_head_error(without_contract, status=400, code="DPE_CONTRACT_UNSUPPORTED")
    with_contract = conformance_target.send(
        "HEAD",
        "documents",
        identity=optional_identity(conformance_target),
        params={"uri": bad_uri},
    )
    assert_head_error(with_contract, status=400, code="DPE_VALIDATION")


def test_body_must_be_json_object(conformance_target: Target, full_identity: Identity) -> None:
    """HTTP §3.1：合法 JSON 但不是对象（如数组）→ 400 + DPE_VALIDATION（信封校验）。"""
    uri = conformance_target.uri("validation/array-body")
    response = conformance_target.send(
        "PUT", "documents", identity=full_identity, params={"uri": uri}, content=json.dumps([])
    )
    assert_problem(response, status=400, code="DPE_VALIDATION")
