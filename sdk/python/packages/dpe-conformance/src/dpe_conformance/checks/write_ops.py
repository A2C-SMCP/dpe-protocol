"""章：写操作——delete / move 的语义、CAS 与幂等重试、删除复活防护、同级写入（#55 全量 2/6）。

- CORE §4/§5.1/§5.2：delete 与 move 的求值顺序、按值比较的 ``base_hash``、内容幂等
  （move 重试的「目标状态已达成」、delete 重试的 ``DPE_NOT_FOUND``、删除后以旧 ``base_hash``
  的写入不得复活文档、ABA 在重建后照常成立）；
- HTTP §3.2/§4.9：delete 的 CAS 走 ``If-Match``、move 的前置条件在请求体中（条件不满足 →
  ``409`` 而非 ``412``）、delete 的 ``204``；
- CORE §2.4 + §3.3 第 2 步 / §5.1（同级写入）：服务端经自身入口的写入走同一 commit 与 CAS
  语义——写后读到的 doc_hash 变化，基于新 doc_hash 的 commit 成功覆盖；陈旧 ``base_hash``
  的同级写入不改变内容。经服务端钩子（测试侧信道，见包 README）驱动，未配置时显式 skip。

暂存路径的幂等与会话语义归「大文档与逐层协商」章（#54），本文件不重复。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

import dpe_hash
import pytest

from dpe_conformance.config import Identity
from dpe_conformance.hooks import HookDriver, self_write_payload
from dpe_conformance.plugin import Target
from dpe_conformance.support import (
    WRONG_HASH,
    WireDocument,
    assert_commit_success,
    assert_problem,
    build_document,
    commit,
    delete,
    move,
    seed,
    text_element,
)


def _document() -> WireDocument:
    return build_document([[text_element("alpha"), text_element("beta")]], title="写操作样例")


def _payload(uri: str, document: WireDocument, **extra: Any) -> dict[str, Any]:
    """self-write 载荷；``pages`` / ``objects`` 的容器类型差异（``list[dict]`` 对
    ``list[Mapping]`` 不可协变）在这里一次性消化。"""
    return self_write_payload(
        file_uri=uri,
        document=document.document,
        pages=cast("list[Mapping[str, Any]]", document.pages),
        objects=cast("list[Mapping[str, Any]]", document.objects),
        **extra,
    )


def _head(target: Target, identity: Identity, uri: str) -> str | None:
    """HEAD 的 doc_hash；不存在为 ``None``（HTTP §3.3）。

    只有 404 映射为 ``None``：200 缺 ``DPE-Doc-Hash`` 是违 HTTP §3.3 的失败，不能与
    「不存在」混淆——否则删除类断言会在「删除未生效」时假绿。
    """
    response = target.send("HEAD", "documents", identity=identity, params={"uri": uri})
    if response.status_code == 404:
        return None
    assert response.status_code == 200, f"HEAD 返回 {response.status_code}"
    doc_hash = response.headers.get("DPE-Doc-Hash")
    assert doc_hash is not None, "HEAD 的 200 必须带 DPE-Doc-Hash（HTTP §3.3）"
    return str(doc_hash)


# --------------------------------------------------------------------- delete（CORE §4、§5.1/§5.2）


def test_delete_removes_document(conformance_target: Target, full_identity: Identity) -> None:
    """HTTP §4.9 + §3.3：带 ``If-Match`` 的 delete 成功 ``204``；此后 ``head`` 不再存在。"""
    uri = conformance_target.uri("write/delete")
    document = seed(conformance_target, uri, _document(), identity=full_identity)
    response = delete(conformance_target, uri, identity=full_identity, if_match=document.doc_hash)
    assert response.status_code == 204, f"delete 预期 204，实际 {response.status_code}"
    assert _head(conformance_target, full_identity, uri) is None, "删除后 head 不应存在"


def test_delete_retry_is_not_found(conformance_target: Target, full_identity: Identity) -> None:
    """CORE §5.2 + HTTP §5：删除后原样重试 → ``412`` + ``DPE_NOT_FOUND``（重试即成功）。"""
    uri = conformance_target.uri("write/delete-retry")
    document = seed(conformance_target, uri, _document(), identity=full_identity)
    first = delete(conformance_target, uri, identity=full_identity, if_match=document.doc_hash)
    assert first.status_code == 204, f"首次删除失败：{first.status_code}"
    retry = delete(conformance_target, uri, identity=full_identity, if_match=document.doc_hash)
    assert_problem(retry, status=412, code="DPE_NOT_FOUND")


def test_delete_wrong_base_hash_is_precondition_failed(
    conformance_target: Target, full_identity: Identity
) -> None:
    """CORE §5.1 + HTTP §5：``base_hash`` 与当前不符 → ``412`` + ``DPE_PRECONDITION_FAILED``；
    内容未被删除（读回互证）。"""
    uri = conformance_target.uri("write/delete-mismatch")
    first = _document()
    seed(conformance_target, uri, first, identity=full_identity)
    changed = build_document([[text_element("changed")]])
    updated = commit(
        conformance_target, uri, changed, identity=full_identity, if_match=first.doc_hash
    )
    assert updated.status_code == 200, f"铺垫失败：{updated.status_code}"

    response = delete(conformance_target, uri, identity=full_identity, if_match=first.doc_hash)
    assert_problem(response, status=412, code="DPE_PRECONDITION_FAILED")
    assert _head(conformance_target, full_identity, uri) == changed.doc_hash, (
        "前置条件失败不得删除文档"
    )


def test_delete_missing_precondition_is_required(
    conformance_target: Target, full_identity: Identity
) -> None:
    """CORE §5.1：delete 不带任何前置条件 → ``428`` + ``DPE_PRECONDITION_REQUIRED``。"""
    uri = conformance_target.uri("write/delete-no-precondition")
    seed(conformance_target, uri, _document(), identity=full_identity)
    response = delete(conformance_target, uri, identity=full_identity)
    assert_problem(response, status=428, code="DPE_PRECONDITION_REQUIRED")


def test_delete_if_none_match_is_validation(
    conformance_target: Target, full_identity: Identity
) -> None:
    """HTTP §3.2：delete 不接受 ``If-None-Match``（哪怕取值 ``*``）→ 400 + ``DPE_VALIDATION``。"""
    uri = conformance_target.uri("write/delete-if-none-match")
    seed(conformance_target, uri, _document(), identity=full_identity)
    response = delete(
        conformance_target, uri, identity=full_identity, headers={"If-None-Match": "*"}
    )
    assert_problem(response, status=400, code="DPE_VALIDATION")


def test_delete_absent_is_not_found(conformance_target: Target, full_identity: Identity) -> None:
    """HTTP §5：对从未存在的文档做带 ``If-Match`` 的 delete → ``412`` + ``DPE_NOT_FOUND``。"""
    uri = conformance_target.uri("write/delete-absent")
    response = delete(conformance_target, uri, identity=full_identity, if_match=WRONG_HASH)
    assert_problem(response, status=412, code="DPE_NOT_FOUND")


def test_deleted_document_stale_base_hash_does_not_resurrect(
    conformance_target: Target, full_identity: Identity
) -> None:
    """CORE §5.1（「文档已删 → DPE_NOT_FOUND」）：删除后以旧 ``base_hash`` 的提交不得复活文档，
    即使内容与删除前完全相同——unchanged 只对存在的文档成立。"""
    uri = conformance_target.uri("write/resurrection")
    document = seed(conformance_target, uri, _document(), identity=full_identity)
    removed = delete(conformance_target, uri, identity=full_identity, if_match=document.doc_hash)
    assert removed.status_code == 204, f"铺垫失败：{removed.status_code}"

    resurrection = commit(
        conformance_target, uri, document, identity=full_identity, if_match=document.doc_hash
    )
    assert_problem(resurrection, status=412, code="DPE_NOT_FOUND")
    assert _head(conformance_target, full_identity, uri) is None, "文档不得被旧 base_hash 复活"


def test_recreated_document_accepts_old_base_hash(
    conformance_target: Target, full_identity: Identity
) -> None:
    """CORE §5.1（ABA 无害）：删除后以相同内容重建，基于旧 doc_hash 的写入作用于新文档——
    防护的是「凭旧 base_hash 复活」，不是「重建后的正常 CAS 写入」。"""
    uri = conformance_target.uri("write/aba")
    document = seed(conformance_target, uri, _document(), identity=full_identity)
    removed = delete(conformance_target, uri, identity=full_identity, if_match=document.doc_hash)
    assert removed.status_code == 204, f"铺垫失败：{removed.status_code}"
    recreated = commit(
        conformance_target, uri, document, identity=full_identity, if_none_match_star=True
    )
    assert recreated.status_code == 201, f"重建失败：{recreated.status_code}"

    changed = build_document([[text_element("aba")]])
    response = commit(
        conformance_target, uri, changed, identity=full_identity, if_match=document.doc_hash
    )
    assert_commit_success(response, status=200, status_text="updated", doc_hash=changed.doc_hash)
    assert _head(conformance_target, full_identity, uri) == changed.doc_hash


# --------------------------------------------------------------------- move（CORE §5.2、HTTP §4.9）


def test_move_moves_content(conformance_target: Target, full_identity: Identity) -> None:
    """CORE §5.2 + HTTP §4.9：move 原子改名——源不再存在，目标读回的 doc_hash 与 ``base_hash``
    一致（file_uri 不进 hash，内容原样搬运）。"""
    source = conformance_target.uri("write/move-from")
    target_uri = conformance_target.uri("write/move-to")
    document = seed(conformance_target, source, _document(), identity=full_identity)
    response = move(
        conformance_target, source, target_uri, identity=full_identity, base_hash=document.doc_hash
    )
    assert response.status_code == 200, f"move 返回 {response.status_code}（{response.text[:200]}）"
    assert response.json()["doc_hash"] == document.doc_hash
    assert _head(conformance_target, full_identity, source) is None, "源应不复存在"
    readback = conformance_target.send(
        "GET", "documents", identity=full_identity, params={"uri": target_uri}
    )
    assert readback.status_code == 200
    skeleton = readback.json()
    assert dpe_hash.object_hash(skeleton["document"], "document") == document.doc_hash, (
        "目标读回的文档对象按契约 1 重算必须等于源内容"
    )


def test_move_retry_is_idempotent(conformance_target: Target, full_identity: Identity) -> None:
    """CORE §5.2 第 4 步（「目标状态已达成即成功」）：首次成功后原样重试仍返回成功。"""
    source = conformance_target.uri("write/move-retry-from")
    target_uri = conformance_target.uri("write/move-retry-to")
    document = seed(conformance_target, source, _document(), identity=full_identity)
    first = move(
        conformance_target, source, target_uri, identity=full_identity, base_hash=document.doc_hash
    )
    assert first.status_code == 200, f"首次 move 失败：{first.status_code}"
    retry = move(
        conformance_target, source, target_uri, identity=full_identity, base_hash=document.doc_hash
    )
    assert retry.status_code == 200, f"重试应命中「目标状态已达成」，实际 {retry.status_code}"
    assert retry.json()["doc_hash"] == document.doc_hash


def test_move_retry_after_target_rewritten_is_not_found(
    conformance_target: Target, full_identity: Identity
) -> None:
    """CORE §5.2：首次成功后目标被他人改写，重试 → ``404`` + ``DPE_NOT_FOUND``（如实上报）。"""
    source = conformance_target.uri("write/move-rewritten-from")
    target_uri = conformance_target.uri("write/move-rewritten-to")
    document = seed(conformance_target, source, _document(), identity=full_identity)
    first = move(
        conformance_target, source, target_uri, identity=full_identity, base_hash=document.doc_hash
    )
    assert first.status_code == 200, f"首次 move 失败：{first.status_code}"
    rewritten = build_document([[text_element("rewritten")]])
    overwrite = commit(
        conformance_target,
        target_uri,
        rewritten,
        identity=full_identity,
        if_match=document.doc_hash,
    )
    assert overwrite.status_code == 200, f"改写目标失败：{overwrite.status_code}"

    retry = move(
        conformance_target, source, target_uri, identity=full_identity, base_hash=document.doc_hash
    )
    assert_problem(retry, status=404, code="DPE_NOT_FOUND", documents_path=False)


def test_move_wrong_base_hash_is_precondition_failed(
    conformance_target: Target, full_identity: Identity
) -> None:
    """CORE §5.2 第 5 步 + HTTP §3.2：源存在但 ``base_hash`` 不符 → ``409`` +
    ``DPE_PRECONDITION_FAILED``（请求体前置条件取 409，非 412）；源未被移动。"""
    source = conformance_target.uri("write/move-mismatch-from")
    target_uri = conformance_target.uri("write/move-mismatch-to")
    document = seed(conformance_target, source, _document(), identity=full_identity)
    response = move(
        conformance_target, source, target_uri, identity=full_identity, base_hash=WRONG_HASH
    )
    assert_problem(response, status=409, code="DPE_PRECONDITION_FAILED", documents_path=False)
    assert _head(conformance_target, full_identity, source) == document.doc_hash, "源不应被移动"


def test_move_existing_target_is_already_exists(
    conformance_target: Target, full_identity: Identity
) -> None:
    """CORE §5.2 第 5 步：目标已存在 → ``409`` + ``DPE_ALREADY_EXISTS``；两侧内容不变。"""
    source = conformance_target.uri("write/move-exists-from")
    target_uri = conformance_target.uri("write/move-exists-to")
    source_document = seed(conformance_target, source, _document(), identity=full_identity)
    target_document = seed(
        conformance_target,
        target_uri,
        build_document([[text_element("occupant")]]),
        identity=full_identity,
    )
    response = move(
        conformance_target,
        source,
        target_uri,
        identity=full_identity,
        base_hash=source_document.doc_hash,
    )
    assert_problem(response, status=409, code="DPE_ALREADY_EXISTS", documents_path=False)
    assert _head(conformance_target, full_identity, source) == source_document.doc_hash
    assert _head(conformance_target, full_identity, target_uri) == target_document.doc_hash


def test_move_missing_base_hash_is_required(
    conformance_target: Target, full_identity: Identity
) -> None:
    """CORE §5.2 第 3 步：move 缺少 ``base_hash`` → ``428`` + ``DPE_PRECONDITION_REQUIRED``。"""
    source = conformance_target.uri("write/move-no-precondition-from")
    target_uri = conformance_target.uri("write/move-no-precondition-to")
    seed(conformance_target, source, _document(), identity=full_identity)
    response = move(conformance_target, source, target_uri, identity=full_identity)
    assert_problem(response, status=428, code="DPE_PRECONDITION_REQUIRED", documents_path=False)


def test_move_absent_source_is_not_found(
    conformance_target: Target, full_identity: Identity
) -> None:
    """CORE §5.2 第 4 步：源不存在且目标不满足「已达成」→ ``404`` + ``DPE_NOT_FOUND``。"""
    source = conformance_target.uri("write/move-absent-from")
    target_uri = conformance_target.uri("write/move-absent-to")
    response = move(
        conformance_target, source, target_uri, identity=full_identity, base_hash=WRONG_HASH
    )
    assert_problem(response, status=404, code="DPE_NOT_FOUND", documents_path=False)


def test_move_to_itself(conformance_target: Target, full_identity: Identity) -> None:
    """CORE §5.2：``from_uri`` 与 ``to_uri`` 规范化后相等时——源存在即「目标已存在」，
    ``base_hash`` 相符取 ``DPE_ALREADY_EXISTS``、不符时 ``DPE_PRECONDITION_FAILED`` 优先；
    MUST NOT 当作「目标状态已达成」返回成功。"""
    uri = conformance_target.uri("write/move-itself")
    document = seed(conformance_target, uri, _document(), identity=full_identity)
    same = move(conformance_target, uri, uri, identity=full_identity, base_hash=document.doc_hash)
    assert_problem(same, status=409, code="DPE_ALREADY_EXISTS", documents_path=False)
    mismatch = move(conformance_target, uri, uri, identity=full_identity, base_hash=WRONG_HASH)
    assert_problem(mismatch, status=409, code="DPE_PRECONDITION_FAILED", documents_path=False)


@pytest.mark.parametrize(
    ("case", "extra"),
    [("force", {"force": True}), ("extra-member", {"extra": 1})],
)
def test_move_envelope_is_closed(
    conformance_target: Target,
    full_identity: Identity,
    case: str,
    extra: Mapping[str, object],
) -> None:
    """CORE §5.2 第 1 步：move 请求信封封闭——``force``（move 不支持）与未定义成员都 →
    400 + ``DPE_VALIDATION``。"""
    source = conformance_target.uri(f"write/move-envelope/{case}/from")
    target_uri = conformance_target.uri(f"write/move-envelope/{case}/to")
    document = seed(conformance_target, source, _document(), identity=full_identity)
    response = move(
        conformance_target,
        source,
        target_uri,
        identity=full_identity,
        base_hash=document.doc_hash,
        extra_body=extra,
    )
    assert_problem(response, status=400, code="DPE_VALIDATION", documents_path=False)


# ---------------------------------------------------------------------- 同级写入（CORE §2.4）


def test_self_write_is_visible_and_commit_covers_it(
    conformance_target: Target, full_identity: Identity, self_write_hook: HookDriver
) -> None:
    """CORE §2.4 + §5.2：服务端经自身入口写入后 ``head`` 的 doc_hash 变化；来源（客户端）基于
    新 doc_hash 的 commit 成功覆盖（同级写入走同一 commit 与 CAS 语义）。"""
    uri = conformance_target.uri("write/self-write")
    first = _document()
    created = self_write_hook.call(_payload(uri, first, if_absent=True))
    assert created.ok, f"self-write 钩子失败：{created.detail}"
    assert _head(conformance_target, full_identity, uri) == first.doc_hash, (
        "同级写入后 head 的 doc_hash 应等于写入内容的 doc_hash"
    )

    second = build_document([[text_element("after-self-write")]])
    response = commit(
        conformance_target, uri, second, identity=full_identity, if_match=first.doc_hash
    )
    assert_commit_success(response, status=200, status_text="updated", doc_hash=second.doc_hash)
    assert _head(conformance_target, full_identity, uri) == second.doc_hash


def test_self_write_respects_cas(
    conformance_target: Target, full_identity: Identity, self_write_hook: HookDriver
) -> None:
    """CORE §2.4 + §5.1：同级写入同样受 CAS 约束——陈旧 ``base_hash`` 的写入不改变内容
    （钩子按契约如实判失败）；``base_hash`` 正确时照常写入（正反对照，避免负例空过）。"""
    uri = conformance_target.uri("write/self-write-cas")
    seeded = _document()
    result = self_write_hook.call(_payload(uri, seeded, if_absent=True))
    assert result.ok, f"self-write 钩子失败：{result.detail}"

    stale = build_document([[text_element("stale")]])
    stale_result = self_write_hook.call(_payload(uri, stale, base_hash=WRONG_HASH))
    assert not stale_result.ok, f"陈旧 base_hash 的同级写入必须判失败：{stale_result.detail}"
    assert _head(conformance_target, full_identity, uri) == seeded.doc_hash, (
        "陈旧 base_hash 的同级写入不得改变内容"
    )

    fresh = build_document([[text_element("fresh")]])
    updated = self_write_hook.call(_payload(uri, fresh, base_hash=seeded.doc_hash))
    assert updated.ok, f"base_hash 正确的同级写入应成功：{updated.detail}"
    assert _head(conformance_target, full_identity, uri) == fresh.doc_hash
