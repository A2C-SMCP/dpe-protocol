"""冒烟章：连通性、身份与钩子的最小可用性（框架自检）。

只证明「打得通、认得出、凭证与钩子配置可用」——协议语义的检查在各专章（#27 起的子集与
全量）。需要而未配置的身份/钩子使对应用例显式 skip 并计入计数。
"""

from __future__ import annotations

from collections.abc import Mapping

import dpe_hash

from dpe_conformance.config import Identity
from dpe_conformance.hooks import HookDriver, self_write_payload
from dpe_conformance.plugin import Target


def test_remote_reachable(conformance_target: Target) -> None:
    """GET capabilities：200、JSON 与协议标记（字段深检在各专章）。"""
    response = conformance_target.send("GET", "capabilities", contract=None)
    assert response.status_code == 200, f"capabilities 返回 {response.status_code}"
    content_type = response.headers.get("content-type", "")
    assert content_type.startswith("application/json"), (
        f"capabilities 的 Content-Type 是 {content_type!r}"
    )
    body = response.json()
    assert isinstance(body, dict), f"capabilities 的响应体不是 JSON 对象：{body!r}"
    assert body.get("protocol") == "dpe/1", f"protocol 字段异常：{body.get('protocol')!r}"


def test_identities_are_accepted(
    conformance_target: Target, identities: Mapping[str, Identity]
) -> None:
    """每个已配置身份都对不存在的文档得到 DPE 错误（而非 401/403）：凭证被服务端接受。"""
    uri = conformance_target.uri("smoke/absent")
    for role, identity in identities.items():
        response = conformance_target.send(
            "HEAD", "documents", identity=identity, params={"uri": uri}
        )
        assert response.status_code == 404, (
            f"{role} 身份：不存在的文档预期 404，实际 {response.status_code}"
        )
        assert response.headers.get("DPE-Error-Code") == "DPE_NOT_FOUND", (
            f"{role} 身份：DPE-Error-Code 是 {response.headers.get('DPE-Error-Code')!r}"
        )


def test_self_write_hook_is_usable(
    conformance_target: Target, full_identity: Identity, self_write_hook: HookDriver
) -> None:
    """self-write 钩子写得进一份最小文档，且 doc_hash 与按 dpe-hash 的期望值一致。"""
    uri = conformance_target.uri("smoke/self-write")
    result = self_write_hook.call(
        self_write_payload(
            file_uri=uri, document={"file_type": "empty", "pages": []}, if_absent=True
        )
    )
    assert result.ok, f"self-write 钩子失败：{result.detail}"
    response = conformance_target.send(
        "HEAD", "documents", identity=full_identity, params={"uri": uri}
    )
    assert response.status_code == 200, (
        f"self-write 后 HEAD 返回 {response.status_code}（钩子可能没有真正写入）"
    )
    expected = dpe_hash.doc_hash({"file_type": "empty"}, [])
    assert response.headers.get("DPE-Doc-Hash") == expected, (
        f"DPE-Doc-Hash 是 {response.headers.get('DPE-Doc-Hash')!r}，按 dpe-hash 期望 {expected!r}"
    )
