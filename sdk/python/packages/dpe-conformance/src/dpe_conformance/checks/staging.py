"""章：暂存会话（#54 全量 1/6）。

- HTTP §4.5：negotiate 的授权（对 ``file_uri`` 的写授权先于开会话）、会话与缺失清单一起给出；
- HTTP §4.6：重复上传 200（已存在）而非 201，判定只看本会话；
- HTTP §4.7：断点查询只读、不续期；成功操作（含 202 中间块）续期；
- CORE §3.4：会话绑定 ``(file_uri, 调用者)``；过期 / 已消费 / 不存在（伪造）/ 他人 /
  另一 file_uri 一律 ``410`` + ``DPE_SESSION_EXPIRED``（不区分原因）；失败的 commit 不消费、
  以同一会话换新前置条件可直接重试；``unchanged`` 不消费。
- conformance/README：暂存路径幂等——引用会话的 commit 成功后原样重试返回 ``unchanged``，
  不得 410。

过期类检查经 ``clock-advance`` 钩子推进服务端时钟完成（TTL ≥ 3600，不能干等；不靠睡眠）；
未配置钩子时显式 skip 并计入计数。「他人」情形需要第二个身份，未配置时同样 skip。
"""

from __future__ import annotations

import uuid

import dpe_hash
import httpx

from dpe_conformance.config import Identity
from dpe_conformance.hooks import HookDriver
from dpe_conformance.plugin import Target
from dpe_conformance.support import (
    assert_commit_success,
    assert_problem,
    assert_staging_head_error,
    breakpoint_query,
    build_document,
    expires_at_header,
    limits_of,
    negotiate,
    outside_scoped,
    session_of,
    stage_all,
    text_element,
    upload_blob,
    upload_offset,
    upload_page,
)


def test_negotiate_returns_session_and_missing_pages(
    conformance_target: Target, full_identity: Identity
) -> None:
    """HTTP §4.5：只交文档对象即完成协商——缺全部页、元素清单为空、会话 id 非空、
    ``expires_at`` 可解析（RFC 3339 UTC）。"""
    uri = conformance_target.uri("staging/negotiate")
    document = build_document([[text_element("n")]])
    response = negotiate(
        conformance_target, uri, document, identity=full_identity, attach_pages=False
    )
    assert response.status_code == 200, f"negotiate 返回 {response.status_code}"
    body = response.json()
    assert body["missing_pages"] == document.page_hashes, "只交文档对象时应缺全部页对象"
    assert body["missing_content_hashes"] == [], "未附带页对象时元素清单为空"
    session_id, expires_at = session_of(response)
    assert session_id, "会话 id 必须非空"
    assert expires_at.tzinfo is not None, "expires_at 必须是带时区的 RFC 3339 时间戳"


def test_negotiate_forbidden_without_write_access(
    conformance_target: Target, scoped_identity: Identity
) -> None:
    """HTTP §4.5 + CORE §3.4：对 ``file_uri`` 无写授权 → 403 + ``DPE_FORBIDDEN``，
    且 MUST NOT 获得会话与缺失清单。"""
    outside = outside_scoped(conformance_target, scoped_identity, "staging/overreach")
    document = build_document([[text_element("f")]])
    response = negotiate(conformance_target, outside, document, identity=scoped_identity)
    body = assert_problem(response, status=403, code="DPE_FORBIDDEN", documents_path=False)
    assert "staging_session" not in body and "missing_pages" not in body, (
        "未通过授权者不得获得会话或缺失清单（CORE §3.4）"
    )


def test_duplicate_upload_is_200_not_201(
    conformance_target: Target, full_identity: Identity
) -> None:
    """HTTP §4.6：本会话内重复上传同一 hash → 200（已存在）而非 201，响应仍给缺失清单。"""
    uri = conformance_target.uri("staging/duplicate")
    document = build_document([[text_element("d")]])
    # 不附带页对象：附带即存入会话，上传会直接命中「已存在」（HTTP §4.5）
    negotiated = negotiate(
        conformance_target, uri, document, identity=full_identity, attach_pages=False
    )
    assert negotiated.status_code == 200
    session_id, _ = session_of(negotiated)

    first = upload_page(
        conformance_target,
        session_id,
        document.pages[0],
        document.page_hashes[0],
        identity=full_identity,
    )
    assert first.status_code == 201, f"首次上传应为 201，实际 {first.status_code}"
    second = upload_page(
        conformance_target,
        session_id,
        document.pages[0],
        document.page_hashes[0],
        identity=full_identity,
    )
    assert second.status_code == 200, f"重复上传应为 200（已存在），实际 {second.status_code}"
    assert second.json()["missing_content_hashes"] == first.json()["missing_content_hashes"], (
        "重复上传的缺失清单仍按本会话给出"
    )


def test_commit_with_session_then_unchanged_replay(
    conformance_target: Target, full_identity: Identity
) -> None:
    """conformance/README + CORE §3.3：引用会话的 commit 成功后原样重试返回 ``unchanged``，
    不得 410（``unchanged`` 先于会话检查，且不消费会话）。"""
    uri = conformance_target.uri("staging/replay")
    document = build_document([[text_element("r1"), text_element("r2")]])
    negotiated = negotiate(conformance_target, uri, document, identity=full_identity)
    assert negotiated.status_code == 200
    session_id, _ = session_of(negotiated)
    stage_all(conformance_target, session_id, document, identity=full_identity)

    def commit_request() -> httpx.Response:
        return conformance_target.send(
            "PUT",
            "documents",
            identity=full_identity,
            params={"uri": uri},
            headers={"If-None-Match": "*"},
            json={"document": document.document, "staging_session": session_id},
        )

    created = commit_request()
    assert_commit_success(created, status=201, status_text="created", doc_hash=document.doc_hash)
    replay = commit_request()
    body = assert_commit_success(
        replay, status=200, status_text="unchanged", doc_hash=document.doc_hash
    )
    assert body["delta"] == {"added": 0, "removed": 0, "retained": 2}, (
        f"unchanged 的 delta 必须为零：{body['delta']!r}"
    )


def test_session_survives_precondition_failure(
    conformance_target: Target, full_identity: Identity
) -> None:
    """CORE §3.4：失败的 commit 不消费会话——412 后以同一会话、新前置条件直接重试成功，
    已上传内容无需重传。"""
    uri = conformance_target.uri("staging/survives")
    version1 = build_document([[text_element("s")]])
    first = conformance_target.send(
        "PUT",
        "documents",
        identity=full_identity,
        params={"uri": uri},
        headers={"If-None-Match": "*"},
        json=version1.commit_body(),
    )
    assert first.status_code == 201, f"首次提交失败：{first.status_code}"

    version2 = build_document([[text_element("s")], [text_element("new-page")]])
    negotiated = negotiate(
        conformance_target, uri, version2, identity=full_identity, attach_pages=False
    )
    assert negotiated.status_code == 200
    assert negotiated.json()["missing_pages"] == [version2.page_hashes[1]], "只有新页缺失"
    session_id, _ = session_of(negotiated)
    stage_all(conformance_target, session_id, version2, identity=full_identity)

    wrong = "dpe1:" + "0" * 64
    failed = conformance_target.send(
        "PUT",
        "documents",
        identity=full_identity,
        params={"uri": uri},
        headers={"If-Match": f'"{wrong}"'},
        json={"document": version2.document, "staging_session": session_id},
    )
    assert_problem(failed, status=412, code="DPE_PRECONDITION_FAILED")
    retried = conformance_target.send(
        "PUT",
        "documents",
        identity=full_identity,
        params={"uri": uri},
        headers={"If-Match": f'"{version1.doc_hash}"'},
        json={"document": version2.document, "staging_session": session_id},
    )
    assert_commit_success(retried, status=200, status_text="updated", doc_hash=version2.doc_hash)


def test_expired_session_is_410(
    conformance_target: Target, full_identity: Identity, clock_advance_hook: HookDriver
) -> None:
    """CORE §3.4 + HTTP §4.6：闲置超过 TTL（经时钟推进，不干等）→
    上传返回 410 + DPE_SESSION_EXPIRED。"""
    limits = limits_of(conformance_target, identity=full_identity)
    ttl = limits["staging_ttl_seconds"]
    assert isinstance(ttl, int) and ttl > 0, "staging_ttl_seconds 必须是正整数"
    # 注：≥ 3600 的 MUST 由 capabilities 章断言；此处只确保推进量有定义
    uri = conformance_target.uri("staging/expired")
    document = build_document([[text_element("e")]])
    negotiated = negotiate(conformance_target, uri, document, identity=full_identity)
    assert negotiated.status_code == 200
    session_id, _ = session_of(negotiated)

    advanced = clock_advance_hook.call({"advance_seconds": ttl + 60})
    assert advanced.ok, f"时钟推进钩子失败：{advanced.detail}"

    response = upload_page(
        conformance_target,
        session_id,
        document.pages[0],
        document.page_hashes[0],
        identity=full_identity,
    )
    assert_problem(response, status=410, code="DPE_SESSION_EXPIRED", documents_path=False)


def test_consumed_session_is_410(conformance_target: Target, full_identity: Identity) -> None:
    """CORE §3.4：commit 成功后会话被消费——其后再用同一会话 → 410（与「unchanged 不消费」区分）。"""
    uri = conformance_target.uri("staging/consumed")
    document = build_document([[text_element("c")]])
    negotiated = negotiate(conformance_target, uri, document, identity=full_identity)
    assert negotiated.status_code == 200
    session_id, _ = session_of(negotiated)
    stage_all(conformance_target, session_id, document, identity=full_identity)
    created = conformance_target.send(
        "PUT",
        "documents",
        identity=full_identity,
        params={"uri": uri},
        headers={"If-None-Match": "*"},
        json={"document": document.document, "staging_session": session_id},
    )
    assert_commit_success(created, status=201, status_text="created", doc_hash=document.doc_hash)

    response = upload_blob(conformance_target, session_id, b"after-consume", identity=full_identity)
    assert_problem(response, status=410, code="DPE_SESSION_EXPIRED", documents_path=False)


def test_forged_session_is_410_not_404(conformance_target: Target, full_identity: Identity) -> None:
    """CORE §3.4 + HTTP §4.7：不存在的（伪造）会话一律 410，不得 404；断点查询只有错误码头。"""
    forged = "st-" + uuid.uuid4().hex
    uri = conformance_target.uri("staging/forged")
    document = build_document([[text_element("g")]])
    negotiated = negotiate(conformance_target, uri, document, identity=full_identity)
    assert negotiated.status_code == 200
    ref = dpe_hash.blob_ref(b"x")

    head = breakpoint_query(
        conformance_target, forged, kind="blobs", ref=ref, identity=full_identity
    )
    assert_staging_head_error(head, status=410, code="DPE_SESSION_EXPIRED")

    uploaded = upload_page(
        conformance_target,
        forged,
        document.pages[0],
        document.page_hashes[0],
        identity=full_identity,
    )
    assert_problem(uploaded, status=410, code="DPE_SESSION_EXPIRED", documents_path=False)


def test_session_is_bound_to_caller(
    conformance_target: Target, full_identity: Identity, scoped_identity: Identity
) -> None:
    """CORE §3.4：会话绑定调用者——他人（另一身份）引用同一会话 → 410（不区分原因，不泄露）。"""
    assert scoped_identity.prefix is not None
    uri = f"{scoped_identity.prefix}{uuid.uuid4().hex}/other-caller"
    document = build_document([[text_element("oc")]])
    negotiated = negotiate(conformance_target, uri, document, identity=scoped_identity)
    assert negotiated.status_code == 200, (
        f"scoped 身份在自己前缀内应能开会话：{negotiated.status_code}"
    )
    session_id, _ = session_of(negotiated)

    response = conformance_target.send(
        "PUT",
        "documents",
        identity=full_identity,
        params={"uri": uri},
        headers={"If-None-Match": "*"},
        json={"document": document.document, "staging_session": session_id},
    )
    assert_problem(response, status=410, code="DPE_SESSION_EXPIRED")


def test_session_is_bound_to_file_uri(conformance_target: Target, full_identity: Identity) -> None:
    """CORE §3.4：会话绑定 ``file_uri``——同一调用者把会话引用到另一文档 → 410。"""
    uri_a = conformance_target.uri("staging/uri-a")
    uri_b = conformance_target.uri("staging/uri-b")
    document = build_document([[text_element("u")]])
    negotiated = negotiate(conformance_target, uri_a, document, identity=full_identity)
    assert negotiated.status_code == 200
    session_id, _ = session_of(negotiated)

    response = conformance_target.send(
        "PUT",
        "documents",
        identity=full_identity,
        params={"uri": uri_b},
        headers={"If-None-Match": "*"},
        json={"document": document.document, "staging_session": session_id},
    )
    assert_problem(response, status=410, code="DPE_SESSION_EXPIRED")


def test_renewal_and_breakpoint_no_renewal(
    conformance_target: Target,
    full_identity: Identity,
    clock_advance_hook: HookDriver,
) -> None:
    """HTTP §4.5/§4.7 + CORE §3.4：成功操作（含 202 中间块）把过期时间推后；
    断点查询不续期——过期时间头精确不变。对照全用响应里的时间字段，不依赖睡眠。"""
    limits = limits_of(conformance_target, identity=full_identity)
    uri = conformance_target.uri("staging/renewal")
    document = build_document([[text_element("t")]])
    negotiated = negotiate(conformance_target, uri, document, identity=full_identity)
    assert negotiated.status_code == 200
    session_id, expires1 = session_of(negotiated)

    data = b"renew-me-with-two-chunks"
    ref = dpe_hash.blob_ref(data)
    chunk_size = min(len(data) - 1, limits["blob_chunk_bytes"])
    assert clock_advance_hook.call({"advance_seconds": 60}).ok
    first_half = data[:chunk_size]
    chunk1 = conformance_target.send(
        "PUT",
        f"staging/{session_id}/blobs/{ref}",
        identity=full_identity,
        content=first_half,
        headers={"Content-Range": f"bytes 0-{chunk_size - 1}/{len(data)}"},
    )
    assert chunk1.status_code == 202, f"中间块应为 202，实际 {chunk1.status_code}"
    expires2 = expires_at_header(chunk1)
    assert expires2 > expires1, "成功的中间块（202）必须续期"

    probe1 = breakpoint_query(
        conformance_target, session_id, kind="blobs", ref=ref, identity=full_identity
    )
    assert probe1.status_code == 200
    assert expires_at_header(probe1) == expires2, "断点查询不续期（过期时间不变）"
    assert upload_offset(probe1) == len(first_half)

    assert clock_advance_hook.call({"advance_seconds": 60}).ok
    second_chunk = conformance_target.send(
        "PUT",
        f"staging/{session_id}/blobs/{ref}",
        identity=full_identity,
        content=data[chunk_size:],
        headers={"Content-Range": f"bytes {chunk_size}-{len(data) - 1}/{len(data)}"},
    )
    assert second_chunk.status_code == 201, f"末块应为 201，实际 {second_chunk.status_code}"
    assert expires_at_header(second_chunk) > expires2, "末块完成同样续期"

    probe2 = breakpoint_query(
        conformance_target, session_id, kind="blobs", ref=ref, identity=full_identity
    )
    assert upload_offset(probe2) == len(data), "到齐后断点给出总字节数"
    assert expires_at_header(probe2) == expires_at_header(second_chunk), "断点查询仍不续期"
