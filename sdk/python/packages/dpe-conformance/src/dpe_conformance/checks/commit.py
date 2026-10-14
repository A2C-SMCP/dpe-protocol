"""章：commit 快路径与 CAS 基础（#27 子集 1）。

- HTTP §4.8：快路径请求体（内联 document/pages/objects）与成功响应（created/updated/unchanged、
  ``doc_hash``、``delta``；201/200 的状态码）；
- HTTP §3.2：前置条件的载体（``If-Match`` / ``If-None-Match: *`` 条件头；``force`` 在请求体）
  与语法违例；不匹配时的 412 与 code 选择（文档不存在 → ``DPE_NOT_FOUND``）；
- CORE §3.3：求值顺序——报文校验 → 授权 → 前置条件存在性（428「即使内容未变」）→
  unchanged（先于条件求值，HTTP §3.2 与 CORE §5.2 的幂等）→ 条件求值；``delta`` 是多集差的
  规范量（元素 content_hash 计），跑分器校验它与 get_skeleton 读回互相印证；
- CORE §5.1：condition 为真的字面值比较（不匹配 → ``DPE_PRECONDITION_FAILED``）。
"""

from __future__ import annotations

from collections import Counter

import dpe_hash
import pytest

from dpe_conformance.config import Identity
from dpe_conformance.plugin import Target
from dpe_conformance.support import (
    WRONG_HASH,
    WireDocument,
    assert_commit_success,
    assert_problem,
    build_document,
    commit,
    text_element,
)


def _document() -> WireDocument:
    return build_document(
        [[text_element("alpha"), text_element("beta", metadata={"revision": 1})]],
        title="commit 样例",
    )


def _readback_multiset(target: Target, identity: Identity, uri: str) -> Counter[str]:
    """经 get_skeleton 读回并展开为元素 content_hash 多重集（delta 的印证基准，CORE §3.3）。"""
    response = target.send("GET", "documents", identity=identity, params={"uri": uri})
    assert response.status_code == 200, f"读回失败：{response.status_code}"
    body = response.json()
    return Counter(h for page in body["pages"] for h in page["elements"])


def test_commit_create(conformance_target: Target, full_identity: Identity) -> None:
    """HTTP §4.8 + CORE §3.3：``If-None-Match: *`` 新建 → 201 + created，delta 全量计入 added。"""
    uri = conformance_target.uri("commit/create")
    document = _document()
    response = commit(
        conformance_target, uri, document, identity=full_identity, if_none_match_star=True
    )
    body = assert_commit_success(
        response, status=201, status_text="created", doc_hash=document.doc_hash
    )
    assert body["delta"] == {"added": 2, "removed": 0, "retained": 0}, (
        f"created 的 delta 应把全部元素计入 added：{body['delta']!r}"
    )


def test_commit_unchanged_is_idempotent(
    conformance_target: Target, full_identity: Identity
) -> None:
    """HTTP §3.2（有意偏离 RFC 9110）+ CORE §5.2：内容已是提交值时返回 unchanged 而非 412。"""
    uri = conformance_target.uri("commit/idempotent")
    document = _document()
    first = commit(
        conformance_target, uri, document, identity=full_identity, if_none_match_star=True
    )
    assert first.status_code == 201, f"首次提交失败：{first.status_code}"
    again = commit(
        conformance_target, uri, document, identity=full_identity, if_none_match_star=True
    )
    body = assert_commit_success(
        again, status=200, status_text="unchanged", doc_hash=document.doc_hash
    )
    assert body["delta"] == {"added": 0, "removed": 0, "retained": 2}, (
        f"unchanged 的 delta 必须 added=removed=0：{body['delta']!r}"
    )


def test_commit_unchanged_precedes_precondition(
    conformance_target: Target, full_identity: Identity
) -> None:
    """HTTP §3.2：unchanged 先于条件求值——条件头不满足但内容相同 → 200 unchanged，非 412。"""
    uri = conformance_target.uri("commit/unchanged-precedes")
    document = _document()
    first = commit(
        conformance_target, uri, document, identity=full_identity, if_none_match_star=True
    )
    assert first.status_code == 201, f"首次提交失败：{first.status_code}"
    response = commit(
        conformance_target, uri, document, identity=full_identity, if_match=WRONG_HASH
    )
    assert_commit_success(response, status=200, status_text="unchanged", doc_hash=document.doc_hash)


def test_commit_missing_precondition_even_when_unchanged(
    conformance_target: Target, full_identity: Identity
) -> None:
    """CORE §3.3：前置条件存在性（第 3 步）先于 unchanged（第 4 步）——即使内容未变也 428。"""
    uri = conformance_target.uri("commit/required-unchanged")
    document = _document()
    first = commit(
        conformance_target, uri, document, identity=full_identity, if_none_match_star=True
    )
    assert first.status_code == 201, f"首次提交失败：{first.status_code}"
    response = commit(conformance_target, uri, document, identity=full_identity)
    assert_problem(response, status=428, code="DPE_PRECONDITION_REQUIRED")


def test_commit_update_delta_matches_readback(
    conformance_target: Target, full_identity: Identity
) -> None:
    """CORE §3.3 + HTTP §4.8：updated 的 ``delta`` 与 get_skeleton 读回互相印证（多集差）。"""
    uri = conformance_target.uri("commit/delta")
    before_document = build_document([[text_element("a"), text_element("b")]])
    first = commit(
        conformance_target, uri, before_document, identity=full_identity, if_none_match_star=True
    )
    assert first.status_code == 201, f"首次提交失败：{first.status_code}"
    before = _readback_multiset(conformance_target, full_identity, uri)

    after_document = build_document([[text_element("a"), text_element("c")]])
    response = commit(
        conformance_target,
        uri,
        after_document,
        identity=full_identity,
        if_match=before_document.doc_hash,
    )
    body = assert_commit_success(
        response, status=200, status_text="updated", doc_hash=after_document.doc_hash
    )
    after = _readback_multiset(conformance_target, full_identity, uri)
    expected = {
        "added": sum((after - before).values()),
        "removed": sum((before - after).values()),
        "retained": sum((after & before).values()),
    }
    assert body["delta"] == expected, (
        f"delta 与读回结果不符：服务端报 {body['delta']!r}，按读回多重集计算 {expected!r}"
    )


def test_commit_precondition_failed(conformance_target: Target, full_identity: Identity) -> None:
    """CORE §5.1 + HTTP §3.2：``If-Match`` 不匹配 → 412 + DPE_PRECONDITION_FAILED。"""
    uri = conformance_target.uri("commit/mismatch")
    document = _document()
    first = commit(
        conformance_target, uri, document, identity=full_identity, if_none_match_star=True
    )
    assert first.status_code == 201, f"首次提交失败：{first.status_code}"
    changed = build_document([[text_element("changed")]])
    response = commit(conformance_target, uri, changed, identity=full_identity, if_match=WRONG_HASH)
    assert_problem(response, status=412, code="DPE_PRECONDITION_FAILED")


def test_commit_if_match_on_absent_is_not_found(
    conformance_target: Target, full_identity: Identity
) -> None:
    """HTTP §3.2/§5：带 ``If-Match`` 写不存在的文档 → 412 + DPE_NOT_FOUND（非 VALIDATION）。"""
    uri = conformance_target.uri("commit/absent")
    response = commit(
        conformance_target,
        uri,
        _document(),
        identity=full_identity,
        if_match=WRONG_HASH,
    )
    assert_problem(response, status=412, code="DPE_NOT_FOUND")


def test_commit_if_none_match_star_on_existing(
    conformance_target: Target, full_identity: Identity
) -> None:
    """CORE §5.1：``If-None-Match: *`` 命中已存在且内容不同 → 412 + DPE_ALREADY_EXISTS。"""
    uri = conformance_target.uri("commit/exists")
    first = commit(
        conformance_target, uri, _document(), identity=full_identity, if_none_match_star=True
    )
    assert first.status_code == 201, f"首次提交失败：{first.status_code}"
    changed = build_document([[text_element("other")]])
    response = commit(
        conformance_target, uri, changed, identity=full_identity, if_none_match_star=True
    )
    assert_problem(response, status=412, code="DPE_ALREADY_EXISTS")


def test_commit_missing_precondition(conformance_target: Target, full_identity: Identity) -> None:
    """CORE §5.1：缺前置条件（无 ``base_hash``/``if_absent``/``force``）→ 428。"""
    uri = conformance_target.uri("commit/no-precondition")
    response = commit(conformance_target, uri, _document(), identity=full_identity)
    assert_problem(response, status=428, code="DPE_PRECONDITION_REQUIRED")


@pytest.mark.parametrize(
    "headers",
    [
        {"If-Match": "*"},
        {"If-Match": '"a", "b"'},
        {"If-Match": f'"{WRONG_HASH}"', "If-None-Match": "*"},
        {"If-None-Match": '"x"'},
    ],
)
def test_commit_condition_syntax_is_validation(
    conformance_target: Target, full_identity: Identity, headers: dict[str, str]
) -> None:
    """HTTP §3.2：条件头语法违例（``*`` 给 If-Match、多 tag、并存、非 ``*``
    的 If-None-Match）→ 400 + DPE_VALIDATION。"""
    uri = conformance_target.uri("commit/syntax")
    response = conformance_target.send(
        "PUT",
        "documents",
        identity=full_identity,
        params={"uri": uri},
        headers=headers,
        json=_document().commit_body(),
    )
    assert_problem(response, status=400, code="DPE_VALIDATION")


def test_commit_force_with_condition_header(
    conformance_target: Target, full_identity: Identity
) -> None:
    """HTTP §3.2 + CORE §3.3：``force`` 与条件头并存 → 400 + DPE_VALIDATION。"""
    uri = conformance_target.uri("commit/force-conflict")
    response = conformance_target.send(
        "PUT",
        "documents",
        identity=full_identity,
        params={"uri": uri},
        headers={"If-Match": f'"{WRONG_HASH}"'},
        json={**_document().commit_body(), "force": True},
    )
    assert_problem(response, status=400, code="DPE_VALIDATION")


# ---------------------------------------------------------------------- CAS 全矩阵：force（#55）


def test_commit_force_creates_absent_document(
    conformance_target: Target, full_identity: Identity
) -> None:
    """CORE §5.1：``force: true`` 是不带条件头的强制覆盖——文档不存在时按新建立即写入。"""
    uri = conformance_target.uri("commit/force-create")
    document = _document()
    response = commit(conformance_target, uri, document, identity=full_identity, force=True)
    body = assert_commit_success(
        response, status=201, status_text="created", doc_hash=document.doc_hash
    )
    assert body["delta"] == {"added": 2, "removed": 0, "retained": 0}


def test_commit_force_overwrites_current_content(
    conformance_target: Target, full_identity: Identity
) -> None:
    """CORE §5.1 + §5.2：force 不看现状直接覆盖；读回内容是提交值（互证，非只看状态码）。"""
    uri = conformance_target.uri("commit/force-overwrite")
    before = _document()
    first = commit(conformance_target, uri, before, identity=full_identity, if_none_match_star=True)
    assert first.status_code == 201, f"铺垫失败：{first.status_code}"

    after = build_document([[text_element("forced")]], title="force 覆盖")
    response = commit(conformance_target, uri, after, identity=full_identity, force=True)
    assert_commit_success(response, status=200, status_text="updated", doc_hash=after.doc_hash)
    readback = conformance_target.send(
        "GET", "documents", identity=full_identity, params={"uri": uri}
    )
    assert readback.status_code == 200
    skeleton = readback.json()
    assert dpe_hash.object_hash(skeleton["document"], "document") == after.doc_hash, (
        "force 覆盖后读回的文档对象按契约 1 重算必须等于提交内容的 doc_hash"
    )


def test_commit_force_with_unchanged_content_is_unchanged(
    conformance_target: Target, full_identity: Identity
) -> None:
    """CORE §3.3 + §5.2：内容幂等先于一切——force 提交与当前内容相同时仍是 unchanged。"""
    uri = conformance_target.uri("commit/force-unchanged")
    document = _document()
    first = commit(
        conformance_target, uri, document, identity=full_identity, if_none_match_star=True
    )
    assert first.status_code == 201, f"铺垫失败：{first.status_code}"
    response = commit(conformance_target, uri, document, identity=full_identity, force=True)
    body = assert_commit_success(
        response, status=200, status_text="unchanged", doc_hash=document.doc_hash
    )
    assert body["delta"] == {"added": 0, "removed": 0, "retained": 2}


def test_commit_force_without_permission_is_forbidden(
    conformance_target: Target, no_force_identity: Identity
) -> None:
    """CORE §3.3 第 2 步 + §5.1：无 force 权限者提交 force → 403，即使内容未变。

    用 ``no-force`` 身份（该角色的文档语义就是「无 force 权限」）。要断言 403 出自 force
    权限，必须先证明同一身份对目标 URI 有写权限——以铺垫提交为证；铺垫被拒（如该身份只有
    前缀写权、测试命名空间不在其内）时无法构造对照，显式 skip 并计入计数（合规服务端不得
    被判失败）。求值顺序上 force 授权（第 2 步）先于 unchanged（第 4 步），「内容未变」不能
    把它降级成 200。
    """
    uri = conformance_target.uri("commit/force-forbidden")
    document = _document()
    seeded = commit(
        conformance_target, uri, document, identity=no_force_identity, if_none_match_star=True
    )
    if seeded.status_code == 403:
        pytest.skip("no-force 身份对测试命名空间无写权，无法构造「有写权、无 force 权」的对照")
    assert seeded.status_code == 201, f"铺垫失败：{seeded.status_code} {seeded.text[:200]}"

    response = commit(conformance_target, uri, document, identity=no_force_identity, force=True)
    assert_problem(response, status=403, code="DPE_FORBIDDEN")

    changed = build_document([[text_element("changed")]])
    overwrite = commit(conformance_target, uri, changed, identity=no_force_identity, force=True)
    assert_problem(overwrite, status=403, code="DPE_FORBIDDEN")
