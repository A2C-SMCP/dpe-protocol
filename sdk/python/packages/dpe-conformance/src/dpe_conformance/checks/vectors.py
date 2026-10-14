"""章：一致性向量经 HTTP 消费（#56）。

- ``vectors/invalid_objects.json``（CORE §2.8 校验顺序）：每条拒绝类用例嵌进一次 commit，
  断言错误码与 HTTP §5 的状态码。**只断言 code**：违例位置（``path``）是 SDK 与诊断层面的
  要求，HTTP problem 体不携带它（CORE §2.8、HTTP §5），由两个 SDK 的向量测试覆盖；
- ``vectors/nesting_depth.json``（CORE §2.8 第 0 步第三项）：恰在上界的对象通过校验，超过一层
  即 ``DPE_VALIDATION``。

``expanded_document`` 类用例是 SDK 的展开视图，不是线上形态，不经 HTTP 消费（不收集、不计 skip）。
向量文件随包分发（``dpe_conformance/vectors/``），由 ``scripts/gen_vectors.py`` 逐字节写出并在
``--check`` 中校验，与仓库根 ``vectors/`` 同源。

嵌入方式：被测对象原样放在 commit 体中它所属的位置（文档对象 → ``document``；页对象 →
``pages``；元素对象 → ``objects``），其余层用该契约下格式合法的占位 hash 补齐——报文校验是
schema 层判定，先于任何 hash 重算与可得性判定（CORE §3.3 第 1 步先于第 6 步）。有 ``input_json``
的用例按原始文本拼接请求体，保留重复键、孤立代理、超范围数字等字节级特征。
"""

from __future__ import annotations

import json
import re
from importlib import resources
from typing import Any

import pytest

from dpe_conformance.config import Identity
from dpe_conformance.plugin import Target
from dpe_conformance.support import assert_problem

#: 线上形态的对象种类（``expanded_document`` 是 SDK 展开视图，不经 HTTP）
_WIRE_KINDS = ("document", "page", "element")


#: 输入中出现的契约前缀（只做提取，不判定契约名是否合乎契约 1 §1 的规则——文法外的前缀同样要
#: 参与「受支持与否」的比较）；``sha256:`` 是 blob 引用，不是契约
_PREFIX = re.compile(r"\b(dpe[0-9a-z]+):")


def _read(name: str) -> dict[str, Any]:
    text = (resources.files("dpe_conformance") / "vectors" / name).read_text(encoding="utf-8")
    data: dict[str, Any] = json.loads(text)
    return data


def _wire_cases(name: str) -> list[dict[str, Any]]:
    return [case for case in _read(name)["cases"] if case["object_kind"] in _WIRE_KINDS]


_INVALID = _wire_cases("invalid_objects.json")
_DEPTH = _wire_cases("nesting_depth.json")
#: 向量的主契约（manifest 的 ``contract``）：向量口径的「受支持契约」= 它 ∪ 用例的 ``contract``
_MANIFEST_CONTRACT: str = _read("manifest.json")["contract"]


def _object_text(case: dict[str, Any]) -> str:
    """用例对象的 JSON 文本：有 ``input_json`` 时取原文（字节级特征不能经解析往返保留）。"""
    raw = case.get("input_json")
    return raw if isinstance(raw, str) else json.dumps(case["input"], ensure_ascii=False)


def _commit_text(case: dict[str, Any]) -> str:
    """把用例对象嵌进 commit 体（文本拼接）；其余层用该契约下格式合法的占位 hash。"""
    placeholder = json.dumps(f"{case['contract']}:" + "0" * 64)
    target = _object_text(case)
    kind = case["object_kind"]
    if kind == "document":
        return f'{{"document": {target}}}'
    document = f'{{"file_type": "md", "pages": [{placeholder}]}}'
    if kind == "page":
        return f'{{"document": {document}, "pages": [{target}]}}'
    page = f'{{"elements": [{placeholder}]}}'
    return f'{{"document": {document}, "pages": [{page}], "objects": [{target}]}}'


@pytest.fixture(scope="module")
def declared_contracts(conformance_target: Target, full_identity: Identity) -> frozenset[str]:
    response = conformance_target.send("GET", "capabilities", contract=None, identity=full_identity)
    assert response.status_code == 200, f"capabilities 返回 {response.status_code}"
    return frozenset(response.json()["hash_contracts"])


def _commit_case(
    target: Target, identity: Identity, contracts: frozenset[str], case: dict[str, Any]
) -> Any:
    contract = case["contract"]
    if contract not in contracts:
        pytest.skip(f"服务端未声明契约 {contract}（hash_contracts={sorted(contracts)}）")
    # 向量按「manifest 契约 ∪ 用例契约」判定受支持与否（vectors/README.md），服务端按自己声明的
    # 集合判定（CORE §3.1）。只有输入涉及的每个契约前缀在两边的受支持与否一致时，期望码才适用于
    # 该服务端（例如服务端声明了 dpex 时，「dpex 前缀不受支持」的用例就不适用）
    supported = {_MANIFEST_CONTRACT, contract}
    differing = sorted(
        prefix
        for prefix in set(_PREFIX.findall(_object_text(case)))
        if (prefix in supported) != (prefix in contracts)
    )
    if differing:
        pytest.skip(
            f"用例按受支持契约 {sorted(supported)} 判定，服务端声明 {sorted(contracts)}，"
            f"{differing} 的受支持与否不同，期望码不适用"
        )
    return target.send(
        "PUT",
        "documents",
        identity=identity,
        contract=contract,
        params={"uri": target.uri(f"vectors/{case['name']}")},
        headers={"If-None-Match": "*", "Content-Type": "application/json"},
        content=_commit_text(case).encode("utf-8"),
    )


@pytest.mark.parametrize("case", _INVALID, ids=[case["name"] for case in _INVALID])
def test_invalid_object_vector(
    conformance_target: Target,
    full_identity: Identity,
    declared_contracts: frozenset[str],
    case: dict[str, Any],
) -> None:
    """CORE §2.8 + ``vectors/invalid_objects.json``：每条输入都被拒绝，错误码与向量一致
    （多处违例的用例借此固定校验顺序），状态码按 HTTP §5（这三个码均为 400）。"""
    response = _commit_case(conformance_target, full_identity, declared_contracts, case)
    assert_problem(response, status=400, code=case["code"])


@pytest.mark.parametrize("case", _DEPTH, ids=[case["name"] for case in _DEPTH])
def test_nesting_depth_vector(
    conformance_target: Target,
    full_identity: Identity,
    declared_contracts: frozenset[str],
    case: dict[str, Any],
) -> None:
    """CORE §2.8 第 0 步第三项 + ``vectors/nesting_depth.json``：恰在上界接受、加一层拒绝。

    接受例的判据是「通过了报文校验」：commit 成功，或在之后的可得性一步得到
    ``DPE_MISSING_CONTENT``（占位 hash 指向的对象不存在，CORE §3.3 第 6 步）。
    """
    response = _commit_case(conformance_target, full_identity, declared_contracts, case)
    if case.get("accept"):
        if response.is_success:
            return
        assert_problem(response, status=400, code="DPE_MISSING_CONTENT")
    else:
        assert_problem(response, status=400, code=case["code"])
