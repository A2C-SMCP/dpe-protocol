"""章：大文档与逐层协商（#54 全量 1/6）。

- CORE §3.2：规模边界——文档对象必须整体出现在 negotiate 与 commit 请求中，超过
  ``max_payload_bytes`` → ``DPE_PAYLOAD_TOO_LARGE``；页/元素/blob 各自的上限；
- HTTP §4.5：negotiate 的逐层缺失清单；只交文档对象即完成协商；
- HTTP §4.6：页/元素对象上传（201/200 语义、响应给出下一层缺失清单、按本会话判定）；
- HTTP §4.7：单页对象超限时的分块上传与断点续传（偏移、丢弃归零）；
- conformance/README「大文档与逐层协商」：暂存期间中间态对读接口不可见、插入一页只缺新页、
  对调两页 delta 为零。

规模由 ``capabilities.limits`` 推导（不写死数值）；构造会撞上其他限额（如 ``page_max_bytes``）
或超上传预算时，按「显式 skip 并写明原因」处理——合规的大规模服务端不得被判失败。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, cast

import dpe_hash
import httpx
import pytest
from dpe_hash import ElementObject
from dpe_hash.jcs import jcs

from dpe_conformance.config import Identity
from dpe_conformance.plugin import Target
from dpe_conformance.support import (
    WireDocument,
    assert_commit_success,
    assert_doc_headers,
    assert_head_error,
    assert_problem,
    breakpoint_query,
    build_document,
    chunk_upload,
    limits_of,
    negotiate,
    session_of,
    stage_all,
    text_element,
    upload_blob,
    upload_element,
    upload_offset,
    upload_page,
)

#: 逐层完成检查的元素上传预算：页对象超限时元素数可能极大（页对象体积由 hash 列表主导），
#: 超出预算只跑机制检查（test_page_object_over_limit_chunked_mechanics），本检查显式 skip。
_ELEMENT_UPLOAD_BUDGET = 2000


def _large_document(limits: Mapping[str, int]) -> WireDocument:
    """展开骨架超过 ``max_payload_bytes`` 的文档：少量「大元素」（单元素远小于上限）。"""
    per_element = max(limits["max_payload_bytes"] // 8, 1)
    count = (limits["max_payload_bytes"] * 3 // 2) // per_element + 1
    prefix = len(str(count))  # 等长且互异的文本：逐层上传覆盖「多个不同大元素」
    texts = [f"{index}".ljust(prefix, "0") + "x" * (per_element - prefix) for index in range(count)]
    return build_document([[text_element(text) for text in texts]])


def _oversized_page(limits: Mapping[str, int]) -> tuple[dict[str, Any], str, int, list[str]]:
    """页对象本身超过 ``max_payload_bytes`` 的构造：返回 (页对象, page_hash, 元素数, hash 列表)。"""
    hash_entry = 64 + 8  # 引号 + dpe1: + 64 hex + 引号 + 逗号 ≈ 73 字节
    count = limits["max_payload_bytes"] * 11 // 10 // hash_entry + 1
    hashes = ["dpe1:" + format(index, "064x") for index in range(count)]
    page: dict[str, Any] = {"elements": hashes}
    return page, dpe_hash.page_hash({}, hashes), count, hashes


def _chunk_size(limits: Mapping[str, int], total: int) -> int:
    # 单块请求体同时受 blob_chunk_bytes 与 max_payload_bytes 约束（HTTP §4.7 第 0/5 步）
    return min(limits["blob_chunk_bytes"], limits["max_payload_bytes"], max(total, 1))


def _assert_chunked_page_missing(
    body: Mapping[str, Any], expected: Sequence[str], max_payload_bytes: int
) -> None:
    """分块上传的页对象的缺失清单（HTTP §4.6）：MUST 带 ``missing_truncated``；为 false 时与实际
    缺失一致；为 true 时是实际缺失的非空子集，且只在未截断的响应体按 JCS 序列化后超过
    ``max_payload_bytes`` 时才允许截断。"""
    truncated = body.get("missing_truncated")
    assert isinstance(truncated, bool), f"页对象响应必须带布尔的 missing_truncated：{truncated!r}"
    listed = body["missing_content_hashes"]
    if not truncated:
        assert set(listed) == set(expected), "未截断时缺失清单必须与实际缺失一致"
        return
    assert listed and set(listed) <= set(expected), "截断时缺失清单必须是实际缺失的非空子集"
    full = jcs({"missing_content_hashes": list(expected), "missing_truncated": False})
    assert len(full.encode("utf-8")) > max_payload_bytes, (
        "未截断的响应体放得进 max_payload_bytes 时 MUST NOT 截断"
    )


def _assert_missing_layer(problem: Mapping[str, Any], layer: str, expected: set[str]) -> None:
    """``DPE_MISSING_CONTENT`` 的某层清单：未截断时严格相等；截断（``missing_truncated`` 为
    **problem 体顶层成员**，HTTP §5）时只断言子集——引擎式截断按 pages → content_hashes →
    blobs 消耗预算，后层可被截空（CORE §3.3：MAY 截断并注明）。"""
    listed = set(problem["missing"][layer])
    if problem.get("missing_truncated"):
        assert listed <= expected, f"截断的 {layer} 清单必须是子集：{sorted(listed)}"
    else:
        assert listed == expected, f"{layer} 清单与实际缺失不符：{sorted(listed)}"


def test_large_document_layerwise_roundtrip(
    conformance_target: Target, full_identity: Identity
) -> None:
    """CORE §3.2 + HTTP §4.5/§4.6/§4.8：展开骨架超过 ``max_payload_bytes`` 的文档经逐层协商
    完成提交（negotiate 只交文档对象 → 页 → 元素 → commit 只带文档对象与会话），读回 hash 等价。"""
    limits = limits_of(conformance_target, identity=full_identity)
    uri = conformance_target.uri("large/layerwise")
    document = _large_document(limits)
    inline = json.dumps(document.commit_body()).encode()
    assert len(inline) > limits["max_payload_bytes"], "构造前提：内联快路径请求体会超限"

    negotiated = negotiate(
        conformance_target, uri, document, identity=full_identity, attach_pages=False
    )
    assert negotiated.status_code == 200, f"negotiate 返回 {negotiated.status_code}"
    body = negotiated.json()
    assert body["missing_pages"] == document.page_hashes, "只交文档对象时应缺全部页对象"
    assert body["missing_content_hashes"] == [], "未附带页对象时元素清单为空（HTTP §4.5）"
    session_id, _ = session_of(negotiated)

    stage_all(conformance_target, session_id, document, identity=full_identity)
    response = conformance_target.send(
        "PUT",
        "documents",
        identity=full_identity,
        params={"uri": uri},
        headers={"If-None-Match": "*"},
        json={"document": document.document, "staging_session": session_id},
    )
    commit_body = assert_commit_success(
        response, status=201, status_text="created", doc_hash=document.doc_hash
    )
    assert commit_body["delta"] == {"added": len(document.objects), "removed": 0, "retained": 0}

    readback = conformance_target.send(
        "GET", "documents", identity=full_identity, params={"uri": uri}
    )
    assert readback.status_code == 200, f"读回返回 {readback.status_code}"
    returned = readback.json()
    assert dpe_hash.object_hash(returned["document"], "document") == document.doc_hash, (
        "逐层提交后读回内容与推送内容不等价（HTTP §4.3 + CORE §2.7）"
    )


def test_document_object_over_limit_is_413(
    conformance_target: Target, full_identity: Identity
) -> None:
    """CORE §3.2：文档对象本身超过 ``max_payload_bytes`` 时，negotiate 与 commit 都 413。"""
    limits = limits_of(conformance_target, identity=full_identity)
    hash_entry = 64 + 8
    count = limits["max_payload_bytes"] * 11 // 10 // hash_entry + 1
    huge = {"file_type": "md", "pages": ["dpe1:" + format(i, "064x") for i in range(count)]}
    uri = conformance_target.uri("large/doc-413")

    negotiated = conformance_target.send(
        "POST", "negotiate", identity=full_identity, json={"file_uri": uri, "document": huge}
    )
    assert_problem(negotiated, status=413, code="DPE_PAYLOAD_TOO_LARGE", documents_path=False)

    committed = conformance_target.send(
        "PUT",
        "documents",
        identity=full_identity,
        params={"uri": uri},
        json={"document": huge, "force": True},
    )
    assert_problem(committed, status=413, code="DPE_PAYLOAD_TOO_LARGE")


def test_page_object_over_limit_chunked_mechanics(
    conformance_target: Target, full_identity: Identity
) -> None:
    """HTTP §4.7：单页对象超过 ``max_payload_bytes`` 时按分块上传——中间块 202 + 偏移，末块
    201 + 缺失元素清单（可截断，此处断言为本页元素集的子集且非空），断点查询给出总字节数。"""
    limits = limits_of(conformance_target, identity=full_identity)
    page, page_hash, _, hashes = _oversized_page(limits)
    data = json.dumps(page, separators=(",", ":")).encode()
    assert len(data) > limits["max_payload_bytes"], "构造前提：页对象超过 max_payload_bytes"
    if len(data) > limits["page_max_bytes"]:
        pytest.skip(
            f"页对象 {len(data)} 字节超过 page_max_bytes={limits['page_max_bytes']}，"
            "服务端会先按尺寸拒绝（README：分块页对象上限）"
        )
    uri = conformance_target.uri("large/page-chunk")
    document = {"file_type": "md", "pages": [page_hash]}
    negotiated = conformance_target.send(
        "POST", "negotiate", identity=full_identity, json={"file_uri": uri, "document": document}
    )
    assert negotiated.status_code == 200, f"negotiate 返回 {negotiated.status_code}"
    session_id, _ = session_of(negotiated)

    responses = chunk_upload(
        conformance_target,
        session_id,
        kind="pages",
        ref=page_hash,
        data=data,
        identity=full_identity,
        chunk_size=_chunk_size(limits, len(data)),
    )
    received = 0
    for response in responses[:-1]:
        assert response.status_code == 202, f"中间块应为 202，实际 {response.status_code}"
        assert response.content == b"", "202 无体"
        received += len(response.request.content or b"")
        assert upload_offset(response) == received, "中间块的偏移应为本会话已收字节数"
        assert response.headers.get("DPE-Session-Expires") is not None, "202 带会话过期时间头"
    final = responses[-1]
    assert final.status_code == 201, f"末块应为 201，实际 {final.status_code}"
    _assert_chunked_page_missing(final.json(), hashes, limits["max_payload_bytes"])

    probe = breakpoint_query(
        conformance_target, session_id, kind="pages", ref=page_hash, identity=full_identity
    )
    assert probe.status_code == 200, f"断点查询返回 {probe.status_code}"
    assert upload_offset(probe) == len(data), "到齐后断点查询给出该对象已收总字节数"


def test_page_object_over_limit_completes(
    conformance_target: Target, full_identity: Identity
) -> None:
    """HTTP §4.7 + §4.8：分块上传页对象后补齐元素并 commit，读回 hash 等价。

    页对象体积由元素 hash 列表主导（约 73 字节/元素），大限额服务端的元素数会很大——超过
    上传预算时显式 skip 并计入计数（机制已由分块检查覆盖）。"""
    limits = limits_of(conformance_target, identity=full_identity)
    _, _, count, _ = _oversized_page(limits)
    if count > _ELEMENT_UPLOAD_BUDGET:
        pytest.skip(f"元素数 {count} 超过上传预算 {_ELEMENT_UPLOAD_BUDGET}（机制见分块检查）")
    # 完成路径需要「路径 hash = 对象真实 hash 一致」的元素：逐个构造真实元素（数量已受预算约束）
    elements = [{"category": "NarrativeText", "text": f"x{index}"} for index in range(count)]
    hashes = [dpe_hash.content_hash(cast(ElementObject, element)) for element in elements]
    page: dict[str, Any] = {"elements": hashes}
    page_hash = dpe_hash.page_hash({}, hashes)
    data = json.dumps(page, separators=(",", ":")).encode()
    assert len(data) > limits["max_payload_bytes"], "构造前提：页对象超过 max_payload_bytes"
    if len(data) > limits["page_max_bytes"]:
        pytest.skip(
            f"页对象 {len(data)} 字节超过 page_max_bytes={limits['page_max_bytes']}，无法完成"
        )
    uri = conformance_target.uri("large/page-complete")
    document = {"file_type": "md", "pages": [page_hash]}
    negotiated = conformance_target.send(
        "POST", "negotiate", identity=full_identity, json={"file_uri": uri, "document": document}
    )
    assert negotiated.status_code == 200, f"negotiate 返回 {negotiated.status_code}"
    session_id, _ = session_of(negotiated)

    responses = chunk_upload(
        conformance_target,
        session_id,
        kind="pages",
        ref=page_hash,
        data=data,
        identity=full_identity,
        chunk_size=_chunk_size(limits, len(data)),
    )
    assert responses[-1].status_code == 201, f"末块应为 201，实际 {responses[-1].status_code}"

    for element, element_hash in zip(elements, hashes, strict=True):
        response = upload_element(
            conformance_target, session_id, element, element_hash, identity=full_identity
        )
        assert response.status_code in (200, 201), (
            f"元素上传失败：{response.status_code} {response.text[:200]}"
        )
    committed = conformance_target.send(
        "PUT",
        "documents",
        identity=full_identity,
        params={"uri": uri},
        headers={"If-None-Match": "*"},
        json={"document": document, "staging_session": session_id},
    )
    assert_commit_success(
        committed,
        status=201,
        status_text="created",
        doc_hash=dpe_hash.doc_hash({"file_type": "md"}, [page_hash]),
    )


def test_commit_missing_content_reports_layers(
    conformance_target: Target, full_identity: Identity
) -> None:
    """CORE §3.3 + HTTP §4.8：``DPE_MISSING_CONTENT`` 的缺失清单按层给出，并与实际上传的
    对象互相印证——补传一层，缺失清单收窄一层，直至成功。"""
    uri = conformance_target.uri("large/missing-layers")
    image_bytes = b"\x89PNG fake"
    blob_ref_value = dpe_hash.blob_ref(image_bytes)
    document = build_document(
        [
            [
                text_element("alpha"),
                text_element("beta"),
                {"category": "Image", "mime_type": "image/png", "blob": blob_ref_value},
            ]
        ]
    )
    negotiated = negotiate(
        conformance_target, uri, document, identity=full_identity, attach_pages=False
    )
    assert negotiated.status_code == 200, f"negotiate 返回 {negotiated.status_code}"
    session_id, _ = session_of(negotiated)

    def commit_with_session() -> httpx.Response:
        return conformance_target.send(
            "PUT",
            "documents",
            identity=full_identity,
            params={"uri": uri},
            headers={"If-None-Match": "*"},
            json={"document": document.document, "staging_session": session_id},
        )

    first = commit_with_session()
    assert first.status_code == 400, f"缺内容时应 400，实际 {first.status_code}"
    missing = first.json()["missing"]
    assert missing["pages"] == document.page_hashes, "页对象尚未上传：缺失清单的 pages 层必须列出它"
    assert missing["content_hashes"] == [], (
        "页对象缺失时其元素不可枚举（分层清单语义，HTTP §4.5/§4.6）"
    )

    page_response = upload_page(
        conformance_target,
        session_id,
        document.pages[0],
        document.page_hashes[0],
        identity=full_identity,
    )
    assert page_response.status_code == 201
    assert set(page_response.json()["missing_content_hashes"]) == {
        dpe_hash.content_hash(cast(ElementObject, element)) for element in document.objects
    }, "页上传响应必须按本会话给出该页仍缺的元素对象"
    assert page_response.json().get("missing_truncated") is False, (
        "页对象响应 MUST 带 missing_truncated；未分块上传的页对象 MUST NOT 截断（HTTP §4.6）"
    )

    for element in document.objects:
        response = upload_element(
            conformance_target,
            session_id,
            element,
            dpe_hash.content_hash(cast(ElementObject, element)),
            identity=full_identity,
        )
        assert response.status_code == 201
    second = commit_with_session()
    second_problem = assert_problem(second, status=400, code="DPE_MISSING_CONTENT")
    _assert_missing_layer(second_problem, "blobs", {blob_ref_value})

    blob_response = upload_blob(conformance_target, session_id, image_bytes, identity=full_identity)
    assert blob_response.status_code == 201, f"blob 上传失败：{blob_response.status_code}"
    final = commit_with_session()
    assert_commit_success(final, status=201, status_text="created", doc_hash=document.doc_hash)


def test_staging_is_invisible_to_reads(conformance_target: Target, full_identity: Identity) -> None:
    """conformance/README + plan §13：暂存期间的中间态对 head / get_skeleton / list 不可见。"""
    namespace = conformance_target.uri("large/invisible/")
    uri = f"{namespace}doc"
    document = build_document([[text_element("hidden")]])
    negotiated = negotiate(conformance_target, uri, document, identity=full_identity)
    assert negotiated.status_code == 200, f"negotiate 返回 {negotiated.status_code}"
    session_id, _ = session_of(negotiated)
    stage_all(conformance_target, session_id, document, identity=full_identity)

    head = conformance_target.send("HEAD", "documents", identity=full_identity, params={"uri": uri})
    assert_head_error(head, status=404, code="DPE_NOT_FOUND")
    skeleton = conformance_target.send(
        "GET", "documents", identity=full_identity, params={"uri": uri}
    )
    assert_problem(skeleton, status=404, code="DPE_NOT_FOUND")
    listed = conformance_target.send(
        "GET", "documents", identity=full_identity, params={"prefix": namespace}
    )
    assert listed.status_code == 200
    assert uri not in {entry["file_uri"] for entry in listed.json()["documents"]}, (
        "暂存中的文档不得出现在 list 中（可见中间态即跑偏）"
    )

    committed = conformance_target.send(
        "PUT",
        "documents",
        identity=full_identity,
        params={"uri": uri},
        headers={"If-None-Match": "*"},
        json={"document": document.document, "staging_session": session_id},
    )
    assert_commit_success(committed, status=201, status_text="created", doc_hash=document.doc_hash)
    after = conformance_target.send(
        "HEAD", "documents", identity=full_identity, params={"uri": uri}
    )
    assert after.status_code == 200, "提交后应可见"
    assert_doc_headers(after, document.doc_hash)


def test_inserted_page_is_the_only_missing_page(
    conformance_target: Target, full_identity: Identity
) -> None:
    """conformance/README：在中间插入一页后再次提交——``missing_pages`` 只含新页，其余页对象
    不重传；commit 返回 updated，doc_hash 变化。"""
    uri = conformance_target.uri("large/insert")
    version1 = build_document([[text_element("a")], [text_element("b")]])
    first = conformance_target.send(
        "PUT",
        "documents",
        identity=full_identity,
        params={"uri": uri},
        headers={"If-None-Match": "*"},
        json=version1.commit_body(),
    )
    assert first.status_code == 201, f"首次提交失败：{first.status_code}"

    version2 = build_document([[text_element("a")], [text_element("x")], [text_element("b")]])
    negotiated = negotiate(
        conformance_target, uri, version2, identity=full_identity, attach_pages=False
    )
    assert negotiated.status_code == 200, f"negotiate 返回 {negotiated.status_code}"
    assert negotiated.json()["missing_pages"] == [version2.page_hashes[1]], "只缺中间的新页"
    session_id, _ = session_of(negotiated)

    page_response = upload_page(
        conformance_target,
        session_id,
        version2.pages[1],
        version2.page_hashes[1],
        identity=full_identity,
    )
    assert page_response.status_code == 201
    new_element = version2.objects[1]  # 展平后：页 1 的元素、新页的元素、页 2 的元素
    new_ref = dpe_hash.content_hash(cast(ElementObject, new_element))
    assert page_response.json()["missing_content_hashes"] == [new_ref], "新页只缺它自己的元素"
    assert page_response.json().get("missing_truncated") is False, (
        "页对象响应 MUST 带 missing_truncated；未分块上传的页对象不截断（HTTP §4.6）"
    )
    uploaded = upload_element(
        conformance_target, session_id, new_element, new_ref, identity=full_identity
    )
    assert uploaded.status_code == 201
    committed = conformance_target.send(
        "PUT",
        "documents",
        identity=full_identity,
        params={"uri": uri},
        headers={"If-Match": f'"{version1.doc_hash}"'},
        json={"document": version2.document, "staging_session": session_id},
    )
    assert_commit_success(committed, status=200, status_text="updated", doc_hash=version2.doc_hash)


def test_swapped_pages_delta_zero(conformance_target: Target, full_identity: Identity) -> None:
    """conformance/README + CORE §3.3：对调两页——``missing_pages`` 为空、commit 返回 updated、
    delta 为零，但 doc_hash 变化（顺序属于内容）。"""
    uri = conformance_target.uri("large/swap")
    version1 = build_document([[text_element("a")], [text_element("b")]])
    first = conformance_target.send(
        "PUT",
        "documents",
        identity=full_identity,
        params={"uri": uri},
        headers={"If-None-Match": "*"},
        json=version1.commit_body(),
    )
    assert first.status_code == 201, f"首次提交失败：{first.status_code}"

    version2 = build_document([[text_element("b")], [text_element("a")]])
    assert version2.doc_hash != version1.doc_hash, "对调后的 doc_hash 必然变化"
    negotiated = negotiate(
        conformance_target, uri, version2, identity=full_identity, attach_pages=False
    )
    assert negotiated.status_code == 200, f"negotiate 返回 {negotiated.status_code}"
    assert negotiated.json()["missing_pages"] == [], "对调后无缺失页（内容寻址）"
    session_id, _ = session_of(negotiated)

    committed = conformance_target.send(
        "PUT",
        "documents",
        identity=full_identity,
        params={"uri": uri},
        headers={"If-Match": f'"{version1.doc_hash}"'},
        json={"document": version2.document, "staging_session": session_id},
    )
    body = assert_commit_success(
        committed, status=200, status_text="updated", doc_hash=version2.doc_hash
    )
    assert body["delta"] == {"added": 0, "removed": 0, "retained": 2}, (
        f"对调两页的 delta 必须为零（多集语义）：{body['delta']!r}"
    )


def test_upload_hash_mismatch_is_rejected(
    conformance_target: Target, full_identity: Identity
) -> None:
    """HTTP §4.6：上传对象的 hash 与路径不符 → ``DPE_HASH_MISMATCH``（不是 200 重复）。"""
    uri = conformance_target.uri("large/mismatch")
    document = build_document([[text_element("m")]])
    negotiated = negotiate(conformance_target, uri, document, identity=full_identity)
    assert negotiated.status_code == 200
    session_id, _ = session_of(negotiated)

    wrong_ref = "dpe1:" + "0" * 64
    response = upload_page(
        conformance_target, session_id, document.pages[0], wrong_ref, identity=full_identity
    )
    assert_problem(response, status=400, code="DPE_HASH_MISMATCH", documents_path=False)


def test_completed_resend_ignores_content_range(
    conformance_target: Target, full_identity: Identity
) -> None:
    """HTTP §4.7：本会话中已完成的 hash 重复上传 MUST 成功（200 + 缺失清单），不校验
    ``Content-Range`` 与请求体——故意用非法 Content-Range 与乱码体重发来证明。"""
    uri = conformance_target.uri("large/resend")
    document = build_document([[text_element("r")]])
    negotiated = negotiate(conformance_target, uri, document, identity=full_identity)
    assert negotiated.status_code == 200
    session_id, _ = session_of(negotiated)
    data = b"blob-bytes"
    ref = dpe_hash.blob_ref(data)
    first = upload_blob(conformance_target, session_id, data, identity=full_identity)
    assert first.status_code == 201, f"整体上传失败：{first.status_code}"

    resend = conformance_target.send(
        "PUT",
        f"staging/{session_id}/blobs/{ref}",
        identity=full_identity,
        content=b"garbage",
        headers={"Content-Range": "bytes 99-100/999999"},
    )
    assert resend.status_code == 200, (
        f"已完成的 hash 重复上传必须 200（不校验 Content-Range），实际 {resend.status_code}"
    )


def test_offset_discontinuity_reports_offset(
    conformance_target: Target, full_identity: Identity
) -> None:
    """HTTP §4.7：偏移不连续 → 400 + ``DPE_VALIDATION``，
    响应带 ``DPE-Upload-Offset`` 给出已收字节数。"""
    uri = conformance_target.uri("large/offset")
    document = build_document([[text_element("o")]])
    negotiated = negotiate(conformance_target, uri, document, identity=full_identity)
    assert negotiated.status_code == 200
    session_id, _ = session_of(negotiated)
    limits = limits_of(conformance_target, identity=full_identity)

    total = min(4096, limits["blob_max_bytes"])
    data = (bytes(range(256)) * 16)[:total]
    chunk = max(1, min(limits["blob_chunk_bytes"], total // 4))
    ref = dpe_hash.blob_ref(data)
    first_chunk = data[:chunk]
    start = conformance_target.send(
        "PUT",
        f"staging/{session_id}/blobs/{ref}",
        identity=full_identity,
        content=first_chunk,
        headers={"Content-Range": f"bytes 0-{chunk - 1}/{len(data)}"},
    )
    assert start.status_code == 202, f"首块应为 202，实际 {start.status_code}"
    assert upload_offset(start) == len(first_chunk)

    skipped_from = chunk * 2
    skipped_to = min(skipped_from + chunk, total) - 1
    skipped = conformance_target.send(
        "PUT",
        f"staging/{session_id}/blobs/{ref}",
        identity=full_identity,
        content=data[skipped_from : skipped_to + 1],
        headers={"Content-Range": f"bytes {skipped_from}-{skipped_to}/{len(data)}"},
    )
    assert_problem(skipped, status=400, code="DPE_VALIDATION", documents_path=False)
    assert upload_offset(skipped) == len(first_chunk), "偏移不连续响应必须带实际已收字节数"


def test_element_object_has_no_breakpoint(
    conformance_target: Target, full_identity: Identity
) -> None:
    """HTTP §4.6：元素对象没有断点查询——``HEAD`` → 405 + ``Allow: PUT``。"""
    uri = conformance_target.uri("large/no-head")
    document = build_document([[text_element("h")]])
    negotiated = negotiate(conformance_target, uri, document, identity=full_identity)
    assert negotiated.status_code == 200
    session_id, _ = session_of(negotiated)
    ref = dpe_hash.content_hash(cast(ElementObject, document.objects[0]))
    response = breakpoint_query(
        conformance_target, session_id, kind="objects", ref=ref, identity=full_identity
    )
    assert response.status_code == 405, f"预期 405，实际 {response.status_code}"
    assert response.headers.get("Allow") == "PUT", (
        f"Allow 头应为 PUT：{response.headers.get('Allow')!r}"
    )


def test_breakpoint_offset_states(conformance_target: Target, full_identity: Identity) -> None:
    """HTTP §4.7：断点查询三态——无进度 0、中间已收字节数、到齐校验失败丢弃后回 0。"""
    uri = conformance_target.uri("large/breakpoint")
    document = build_document([[text_element("b")]])
    negotiated = negotiate(conformance_target, uri, document, identity=full_identity)
    assert negotiated.status_code == 200
    session_id, _ = session_of(negotiated)

    truth = b"the-real-bytes"
    ref = dpe_hash.blob_ref(truth)
    initial = breakpoint_query(
        conformance_target, session_id, kind="blobs", ref=ref, identity=full_identity
    )
    assert initial.status_code == 200, f"无进度查询应为 200，实际 {initial.status_code}"
    assert upload_offset(initial) == 0, "无进度以偏移 0 表示（不是 404）"
    assert initial.headers.get("DPE-Session-Expires") is not None, "断点查询带会话过期时间头"

    partial = b"x" * len(truth)  # 与真字节等长但内容不同（到齐校验必然失败）
    declared = len(truth)
    chunk_one = conformance_target.send(
        "PUT",
        f"staging/{session_id}/blobs/{ref}",
        identity=full_identity,
        content=partial[:5],
        headers={"Content-Range": f"bytes 0-4/{declared}"},
    )
    assert chunk_one.status_code == 202, f"中间块应为 202，实际 {chunk_one.status_code}"
    mid = breakpoint_query(
        conformance_target, session_id, kind="blobs", ref=ref, identity=full_identity
    )
    assert upload_offset(mid) == 5, "中间态断点给出已收字节数"

    final = conformance_target.send(
        "PUT",
        f"staging/{session_id}/blobs/{ref}",
        identity=full_identity,
        content=partial[5:],
        headers={"Content-Range": f"bytes 5-{declared - 1}/{declared}"},
    )
    assert_problem(final, status=400, code="DPE_HASH_MISMATCH", documents_path=False)
    discarded = breakpoint_query(
        conformance_target, session_id, kind="blobs", ref=ref, identity=full_identity
    )
    assert upload_offset(discarded) == 0, "到齐校验失败后，该对象已收内容被丢弃、偏移回 0"
