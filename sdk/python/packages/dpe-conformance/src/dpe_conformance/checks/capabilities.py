"""章：capabilities 与契约头（#27 子集 1）。

- HTTP §4.1：capabilities 的字段与约束（``staging_ttl_seconds`` ≥ 3600 见 HTTP §4.1 与 CORE §3.4）；
- HTTP §3.1：``DPE-Hash-Contract`` 是除 ``GET capabilities`` 外每个请求的必带头，
  缺失或不受支持 → ``DPE_CONTRACT_UNSUPPORTED``；无体端点的校验顺序为「契约 → uri」（HTTP §3.1）。
"""

from __future__ import annotations

from dpe_conformance.plugin import Target
from dpe_conformance.support import (
    assert_head_error,
    assert_problem,
    has_dpe_vary,
    optional_identity,
)

_LIMIT_FIELDS = (
    "max_payload_bytes",
    "page_max_bytes",
    "staging_ttl_seconds",
    "blob_max_bytes",
    "blob_chunk_bytes",
    "batch_head_max",
    "list_page_max",
)


def test_capabilities_fields(conformance_target: Target) -> None:
    """HTTP §4.1：capabilities 的响应体字段与约束；该端点不带
    ``Vary: DPE-Hash-Contract``（HTTP §3.1）。"""
    response = conformance_target.send(
        "GET", "capabilities", contract=None, identity=optional_identity(conformance_target)
    )
    assert response.status_code == 200, f"capabilities 返回 {response.status_code}"
    body = response.json()
    assert body.get("protocol") == "dpe/1", f"protocol 是 {body.get('protocol')!r}"
    contracts = body.get("hash_contracts")
    assert (
        isinstance(contracts, list) and contracts and all(isinstance(c, str) for c in contracts)
    ), f"hash_contracts 必须是非空字符串数组：{contracts!r}"
    assert "dpe1" in contracts, "跑分器以 dpe1 校验，服务端必须声明它"
    limits = body.get("limits")
    assert isinstance(limits, dict), f"limits 必须是对象：{limits!r}"
    for field in _LIMIT_FIELDS:
        value = limits.get(field)
        assert isinstance(value, int) and not isinstance(value, bool), (
            f"limits.{field} 必须是整数：{value!r}"
        )
    assert limits["staging_ttl_seconds"] >= 3600, (
        f"staging_ttl_seconds 必须 ≥ 3600，实际 {limits['staging_ttl_seconds']}"
    )
    for field in ("content_encodings", "features"):
        value = body.get(field)
        assert isinstance(value, list) and all(isinstance(item, str) for item in value), (
            f"{field} 必须是字符串数组：{value!r}"
        )
    assert not has_dpe_vary(response), "capabilities 不带 Vary: DPE-Hash-Contract（HTTP §3.1）"


def test_contract_header_missing_is_contract_unsupported(conformance_target: Target) -> None:
    """HTTP §3.1：无契约头的 head → 400 + DPE_CONTRACT_UNSUPPORTED（HEAD 无体、只有错误码头）。"""
    response = conformance_target.send(
        "HEAD",
        "documents",
        contract=None,
        identity=optional_identity(conformance_target),
        params={"uri": conformance_target.uri("caps/absent")},
    )
    assert_head_error(response, status=400, code="DPE_CONTRACT_UNSUPPORTED")


def test_contract_header_unsupported_is_contract_unsupported(conformance_target: Target) -> None:
    """HTTP §3.1：声明不受支持的契约 → 400 + DPE_CONTRACT_UNSUPPORTED。"""
    response = conformance_target.send(
        "HEAD",
        "documents",
        contract="dpe9",
        identity=optional_identity(conformance_target),
        params={"uri": conformance_target.uri("caps/absent")},
    )
    assert_head_error(response, status=400, code="DPE_CONTRACT_UNSUPPORTED")


def test_contract_header_missing_on_json_body_endpoint(conformance_target: Target) -> None:
    """HTTP §4.2 + §3.1：带 JSON 体的端点缺契约头 → 400 + DPE_CONTRACT_UNSUPPORTED。"""
    response = conformance_target.send(
        "POST",
        "heads",
        contract=None,
        identity=optional_identity(conformance_target),
        json={"uris": [conformance_target.uri("caps/absent")]},
    )
    assert_problem(response, status=400, code="DPE_CONTRACT_UNSUPPORTED", documents_path=False)
