"""sans-IO 协议核心（#49）：暂存路径——协商、上传、分块断点续传与会话（core §3.2–§3.4）。

响应按 HTTP 绑定 §4.5–§4.7、§5 的报文形状手工构造，用脚本化 ``send`` 驱动操作；分块续传的
三态（无进度 / 部分进度 / 已完成）与两类恢复（``DPE_MISSING_CONTENT`` 补传、``DPE_SESSION_
EXPIRED`` 重开）逐条断言请求序列。真实参考服务端的用例在 ``test_protocol_staging_http.py``。
"""

from __future__ import annotations

import io
import json
from datetime import datetime
from typing import Any

import dpe_hash
import pytest
from dpe_sdk import Document, ElementObject, PageObject, errors
from dpe_sdk.protocol import (
    CONTRACT_HEADER,
    BaseHash,
    BlobRange,
    BlobSource,
    Force,
    IfAbsent,
    ProtocolCore,
    Request,
    Response,
    Session,
    drive,
)
from engine_helpers import inline, text_doc
from protocol_helpers import (
    H1,
    H2,
    H_DRILL,
    SESSION_EXPIRES,
    Script,
    caps,
    chunk_ranges,
    core,
    gateway,
    negotiated,
    offset_problem,
    ok,
    problem,
    resume,
    run,
    uploaded,
)

URI = "s3://bucket/a.md"
TARGET = "documents?uri=s3%3A%2F%2Fbucket%2Fa.md"
EXPIRES = datetime.fromisoformat(SESSION_EXPIRES)
SESSION = Session("st-1", URI, EXPIRES)
REF = dpe_hash.blob_ref(b"binary-image")
IMAGE = b"binary-image"


def doc(*pages: list[str]) -> Document:
    return Document.model_validate(text_doc(*pages))


def image_doc() -> Document:
    return Document.model_validate(
        {
            "file_type": "md",
            "pages": [{"elements": [{"category": "Image", "blob": REF, "mime_type": "image/png"}]}],
        }
    )


def committed(document: Document, status: str, added: int, removed: int, retained: int) -> Response:
    body = {
        "status": status,
        "doc_hash": document.doc_hash(),
        "delta": {"added": added, "removed": removed, "retained": retained},
    }
    return ok(body, 201 if status == "created" else 200, {"DPE-Doc-Hash": document.doc_hash()})


def wire_json(data: Any) -> bytes:
    """与 ``ProtocolCore._json_body`` 相同的序列化（断言请求体与尺寸用）。"""
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def staged_core(**limits: Any) -> ProtocolCore:
    """小限额的核心：内联体放不下，强制走暂存路径。"""
    return ProtocolCore(caps(limits={"max_payload_bytes": 120, **limits}))


# ---------------------------------------------------------------------- 构造与本地校验


def test_blob_source_from_bytes_and_seekable() -> None:
    source = BlobSource.from_bytes(b"hello")
    assert (source.ref, source.size) == (dpe_hash.blob_ref(b"hello"), 5)
    assert source.read(1, 3) == b"ell"
    assert source.read(4, 10) == b"o"  # 越过后缀的读取由调用方按 size 约束
    stream = BlobSource.from_seekable(io.BytesIO(b"abcdef"), "sha256:" + "0" * 64, 6)
    assert (stream.ref, stream.size) == ("sha256:" + "0" * 64, 6)
    assert stream.read(2, 3) == b"cde"
    assert stream.read(5, 1) == b"f"


def test_request_body_bytes_resolves_blob_ranges() -> None:
    request = Request("PUT", "x", {}, BlobRange(BlobSource.from_bytes(b"abcdef"), 2, 3))
    assert request.body_bytes() == b"cde"
    assert Request("PUT", "x", {}, b"raw").body_bytes() == b"raw"
    assert Request("GET", "x").body_bytes() is None
    short = Request(
        "PUT", "x", {}, BlobRange(BlobSource("sha256:" + "0" * 64, 5, lambda o, n: b"ab"), 0, 5)
    )
    with pytest.raises(ValueError, match="字节"):
        short.body_bytes()


def test_upload_blob_validates_the_source_locally() -> None:
    with pytest.raises(errors.PayloadTooLargeError, match="blob_max_bytes"):
        next(core(limits={"blob_max_bytes": 4}).upload_blob(SESSION, b"12345"))
    with pytest.raises(ValueError, match="sha256"):
        next(core().upload_blob(SESSION, BlobSource("dpe1:" + "0" * 64, 1, lambda o, n: b"x")))
    with pytest.raises(TypeError, match="BlobSource"):
        next(core().upload_blob(SESSION, "not a source"))  # type: ignore[arg-type]


def test_upload_page_and_element_reject_oversized_objects_locally() -> None:
    page = PageObject.model_validate({"title": "x" * 300, "elements": []})
    with pytest.raises(errors.PayloadTooLargeError, match="page_max_bytes"):
        next(core(limits={"page_max_bytes": 128}).upload_page(SESSION, page))
    element = ElementObject.model_validate({"category": "NarrativeText", "text": "x" * 300})
    with pytest.raises(errors.PayloadTooLargeError, match="max_payload_bytes"):
        next(core(limits={"max_payload_bytes": 128}).upload_element(SESSION, element))


# ---------------------------------------------------------------------- negotiate


def test_negotiate_request_shape_and_result() -> None:
    document = doc(["x"])
    parts = inline(text_doc(["x"]))
    result, server = run(
        core().negotiate(URI, document), negotiated(missing_pages=[parts.page_hashes[0]])
    )
    (request,) = server.requests
    assert (request.method, request.target) == ("POST", "negotiate")
    assert request.headers[CONTRACT_HEADER] == "dpe1"
    assert request.headers["Content-Type"] == "application/json"
    assert json.loads(request.body_bytes() or b"") == {
        "file_uri": URI,
        "document": parts.document,
    }
    assert result.missing_pages == [parts.page_hashes[0]]
    assert result.missing_content_hashes == []
    assert (result.session.id, result.session.uri) == ("st-1", URI)
    assert result.session.expires_at == EXPIRES


def test_negotiate_normalizes_the_session_uri() -> None:
    result, _ = run(core().negotiate("S3://bucket/a.md", doc(["x"])), negotiated())
    assert result.session.uri == URI


def test_negotiate_rejects_response_hashes_outside_the_contract() -> None:
    with pytest.raises(errors.UnexpectedResponseError):
        run(core().negotiate(URI, doc(["x"])), negotiated(missing_pages=[H_DRILL]))
    with pytest.raises(errors.UnexpectedResponseError):
        run(core().negotiate(URI, doc(["x"])), negotiated(missing_content_hashes=[H_DRILL]))


def test_negotiate_rejects_a_non_rfc3339_expiry() -> None:
    with pytest.raises(errors.UnexpectedResponseError, match="RFC 3339"):
        run(core().negotiate(URI, doc(["x"])), negotiated(expires_at="whenever"))


@pytest.mark.parametrize("uri", ["no scheme", "feishu://doc/ 空格"])
def test_negotiate_validates_the_uri_locally(uri: str) -> None:
    with pytest.raises(dpe_hash.ValidationError):
        next(core().negotiate(uri, doc(["x"])))


# ---------------------------------------------------------------------- 上传原语


def test_upload_page_whole_request_and_missing_elements() -> None:
    page = PageObject.model_validate({"title": "t", "elements": [H1, H2]})
    missing, server = run(
        core().upload_page(SESSION, page), uploaded({"missing_content_hashes": [H1]})
    )
    (request,) = server.requests
    assert (request.method, request.target) == ("PUT", f"staging/st-1/pages/{page.page_hash()}")
    assert request.headers["Content-Type"] == "application/json"
    assert "Content-Range" not in request.headers
    assert json.loads(request.body_bytes() or b"") == {"title": "t", "elements": [H1, H2]}
    assert missing == [H1]


def test_upload_page_over_max_payload_is_chunked() -> None:
    page = PageObject.model_validate({"title": "y" * 500, "elements": []})
    payload = wire_json(page.model_dump())
    c = core(limits={"max_payload_bytes": 200, "blob_chunk_bytes": 100})
    ranges = chunk_ranges(len(payload), 100)
    steps = [uploaded(status=202, offset=start + length) for start, length in ranges[:-1]]
    missing, server = run(
        c.upload_page(SESSION, page), *steps, uploaded({"missing_content_hashes": []})
    )
    assert missing == []
    assert [(r.headers.get("Content-Range"), r.body_bytes()) for r in server.requests] == [
        (f"bytes {start}-{start + length - 1}/{len(payload)}", payload[start : start + length])
        for start, length in ranges
    ]


def test_upload_page_duplicate_and_created_are_both_success() -> None:
    page = PageObject.model_validate({"elements": [H1]})
    missing, _ = run(core().upload_page(SESSION, page), uploaded({"missing_content_hashes": [H2]}))
    assert missing == [H2]
    missing, _ = run(
        core().upload_page(SESSION, page), uploaded({"missing_content_hashes": []}, status=200)
    )
    assert missing == []


def test_upload_element_request_and_missing_blobs() -> None:
    element = ElementObject.model_validate(
        {"category": "Image", "blob": REF, "mime_type": "image/png"}
    )
    missing, server = run(
        core().upload_element(SESSION, element), uploaded({"missing_blobs": [REF]})
    )
    (request,) = server.requests
    assert (request.method, request.target) == (
        "PUT",
        f"staging/st-1/objects/{element.content_hash()}",
    )
    assert missing == [REF]


def test_upload_element_rejects_a_malformed_blob_ref_in_the_response() -> None:
    element = ElementObject.model_validate({"category": "NarrativeText", "text": "x"})
    with pytest.raises(errors.UnexpectedResponseError):
        run(core().upload_element(SESSION, element), uploaded({"missing_blobs": ["dpe1:x"]}))


def test_upload_session_expired_is_reported() -> None:
    page = PageObject.model_validate({"elements": []})
    with pytest.raises(errors.SessionExpiredError):
        run(core().upload_page(SESSION, page), problem("DPE_SESSION_EXPIRED", 410))


def test_upload_blob_whole_request_uses_the_media_type() -> None:
    _, server = run(
        core().upload_blob(SESSION, BlobSource.from_bytes(IMAGE), media_type="image/png"),
        uploaded({}),
    )
    (request,) = server.requests
    assert request.target == f"staging/st-1/blobs/{REF}"
    assert request.headers["Content-Type"] == "image/png"
    assert "Content-Range" not in request.headers
    assert request.body_bytes() == IMAGE
    _, server = run(core().upload_blob(SESSION, BlobSource.from_bytes(IMAGE)), uploaded({}))
    assert server.requests[0].headers["Content-Type"] == "application/octet-stream"


# ---------------------------------------------------------------------- blob 分块与断点续传

#: 250 字节、单块 100：块为 [0-99]、[100-199]、[200-249]
BLOB = b"x" * 250


def chunked_core(*, max_resends: int = 2) -> ProtocolCore:
    """250 字节的单块上限 100：块为 [0-99]、[100-199]、[200-249]。"""
    return ProtocolCore(
        caps(limits={"max_payload_bytes": 100, "blob_chunk_bytes": 100}), max_resends=max_resends
    )


def test_blob_chunk_lost_response_resumes_from_the_server_offset() -> None:
    """响应丢失且服务端未收到该块：先断点查询，再从已收偏移续传（core §3.4）。"""
    lost = ConnectionError("块 2 的响应丢失")
    server = Script(
        uploaded(status=202, offset=100),
        lost,
        resume(100),
        uploaded(status=202, offset=200),
        uploaded({}),
    )
    assert drive(chunked_core().upload_blob(SESSION, BLOB, media_type="image/png"), server) is None
    first, resend, head, resumed, last = server.requests
    assert [r.method for r in server.requests] == ["PUT", "PUT", "HEAD", "PUT", "PUT"]
    assert first.headers["Content-Range"] == "bytes 0-99/250"
    assert resend is not None and resend.headers["Content-Range"] == "bytes 100-199/250"
    assert head.target == first.target and "Content-Range" not in head.headers
    assert resumed.headers["Content-Range"] == "bytes 100-199/250"  # 从偏移 100 续传，不是从零
    assert resumed.body_bytes() == BLOB[100:200]
    assert last.headers["Content-Range"] == "bytes 200-249/250"
    assert last.headers["Content-Type"] == "image/png"


def test_blob_chunk_completed_before_resume_resends_the_last_chunk() -> None:
    """响应丢失但服务端已收完：偏移等于总量即已完成，重发末块（非空区间）取缺失清单。"""
    server = Script(
        uploaded(status=202, offset=100),
        uploaded(status=202, offset=200),
        ConnectionError("末块的响应丢失"),
        resume(250),
        uploaded({}),
    )
    drive(chunked_core().upload_blob(SESSION, BLOB), server)
    third, head, resent = server.requests[2], server.requests[3], server.requests[4]
    assert (head.method, head.target) == ("HEAD", third.target)
    assert resent.headers["Content-Range"] == "bytes 200-249/250"  # 末块原样，不发空区间
    assert resent.body_bytes() == BLOB[200:250]


def test_blob_chunk_lost_first_response_restarts_from_zero() -> None:
    server = Script(
        ConnectionError("首块响应丢失"),
        resume(0),
        uploaded(status=202, offset=100),
        uploaded({}),
    )
    assert drive(chunked_core().upload_blob(SESSION, b"x" * 120), server) is None
    assert [(r.method, r.headers.get("Content-Range")) for r in server.requests] == [
        ("PUT", "bytes 0-99/120"),
        ("HEAD", None),
        ("PUT", "bytes 0-99/120"),
        ("PUT", "bytes 100-119/120"),
    ]


def test_blob_chunk_offset_mismatch_resyncs_from_the_header_without_head() -> None:
    """偏移不连续的 400 带 ``DPE-Upload-Offset``：直接据它重同步，免一次断点查询。"""
    server = Script(uploaded(status=202, offset=100), offset_problem(200), uploaded({}))
    assert drive(chunked_core().upload_blob(SESSION, BLOB), server) is None
    assert [r.method for r in server.requests] == ["PUT", "PUT", "PUT"]  # 无 HEAD
    assert server.requests[2].headers["Content-Range"] == "bytes 200-249/250"


def test_blob_chunk_recovery_budget_is_bounded() -> None:
    lost = ConnectionError("又丢了")
    server = Script(ConnectionError("丢了"), resume(0), lost)
    with pytest.raises(ConnectionError) as info:
        drive(chunked_core(max_resends=1).upload_blob(SESSION, BLOB), server)
    assert info.value is lost
    assert [r.method for r in server.requests] == ["PUT", "HEAD", "PUT"]


def test_blob_chunk_gateway_lost_resumes_via_head() -> None:
    """无错误码的 504（网关已转发、结果未知）：与响应丢失同处理——先断点查询再续传。"""
    server = Script(gateway(504), resume(0), uploaded(status=202, offset=100), uploaded({}))
    assert drive(chunked_core().upload_blob(SESSION, b"x" * 120), server) is None
    assert [r.method for r in server.requests] == ["PUT", "HEAD", "PUT", "PUT"]


def test_blob_chunk_gateway_budget_exhausted_is_reported() -> None:
    server = Script(gateway(504), resume(0), gateway(504))
    with pytest.raises(errors.UnexpectedResponseError) as info:
        drive(chunked_core(max_resends=1).upload_blob(SESSION, BLOB), server)
    assert info.value.status == 504
    assert [r.method for r in server.requests] == ["PUT", "HEAD", "PUT"]


def test_blob_chunk_resume_session_expired_is_reported() -> None:
    """断点查询得 410：会话不可用如实抛出（HEAD 只有错误码头），由 deliver 重开。"""
    server = Script(ConnectionError("块响应丢失"), problem("DPE_SESSION_EXPIRED", 410))
    with pytest.raises(errors.SessionExpiredError):
        drive(chunked_core().upload_blob(SESSION, BLOB), server)
    assert [r.method for r in server.requests] == ["PUT", "HEAD"]


def test_blob_chunk_resync_hint_beyond_the_total_is_rejected() -> None:
    """偏移重同步值超过总量：判不合规，不把它当作「已收」。"""
    server = Script(offset_problem(999))
    with pytest.raises(errors.UnexpectedResponseError, match="超过总量"):
        drive(chunked_core().upload_blob(SESSION, BLOB), server)
    assert [r.method for r in server.requests] == ["PUT"]


def test_blob_chunk_absurd_offset_header_is_rejected() -> None:
    """超长数字串不逃出 SDK 的错误分类：按「头不可用」判不合规，不抛裸 ValueError。"""
    server = Script(Response(202, {"DPE-Upload-Offset": "1" * 5000}))
    with pytest.raises(errors.UnexpectedResponseError) as info:
        drive(chunked_core().upload_blob(SESSION, BLOB), server)
    # 环境放开 int() 位数上限（PYTHONINTMAXSTRDIGITS=0）时走 202 的偏移守卫，同样是不合规响应；
    # 断言不与该上限耦合
    assert info.value.status == 202


def test_blob_chunk_resend_after_completion_counts_against_the_budget() -> None:
    """已完成后的重发再丢：恢复预算耗尽的如实抛出（不无限重发）。"""
    server = Script(
        uploaded(status=202, offset=100),
        uploaded(status=202, offset=200),
        ConnectionError("末块丢失"),
        resume(250),
        ConnectionError("末块重发又丢失"),
    )
    with pytest.raises(ConnectionError, match="又丢失"):
        drive(chunked_core(max_resends=1).upload_blob(SESSION, BLOB), server)


def test_blob_chunk_stalled_202_is_rejected() -> None:
    """不前进的 202（服务端重复旧状态）：判不合规，不无上界地重发同一块。"""
    server = Script(uploaded(status=202, offset=0), uploaded(status=202, offset=0))
    with pytest.raises(errors.UnexpectedResponseError, match="202"):
        drive(chunked_core().upload_blob(SESSION, BLOB), server)
    assert [r.method for r in server.requests] == ["PUT"]  # 第一块即判，不再重发


def test_blob_chunk_202_offset_beyond_the_total_is_rejected() -> None:
    server = Script(uploaded(status=202, offset=251))
    with pytest.raises(errors.UnexpectedResponseError, match="未到齐"):
        drive(chunked_core().upload_blob(SESSION, BLOB), server)
    assert [r.method for r in server.requests] == ["PUT"]


def test_blob_chunk_202_after_completion_is_rejected() -> None:
    """已完成后重发末块又得 202：到齐应是 201/200，202 语义不自洽，判不合规。"""
    server = Script(
        uploaded(status=202, offset=100),
        uploaded(status=202, offset=200),
        ConnectionError("末块响应丢失"),
        resume(250),
        uploaded(status=202, offset=250),
    )
    with pytest.raises(errors.UnexpectedResponseError, match="未到齐"):
        drive(chunked_core().upload_blob(SESSION, BLOB), server)
    assert [r.method for r in server.requests] == ["PUT", "PUT", "PUT", "HEAD", "PUT"]


# ---------------------------------------------------------------------- deliver：快路径


def test_deliver_fast_path_is_a_single_commit() -> None:
    document = doc(["x", "y"])
    result, server = run(
        core().deliver(URI, document, IfAbsent()), committed(document, "created", 2, 0, 0)
    )
    assert result.status == "created"
    (request,) = server.requests
    assert (request.method, request.target) == ("PUT", TARGET)
    assert "staging_session" not in json.loads(request.body_bytes() or b"")


def test_deliver_fast_path_force_has_no_condition_header() -> None:
    document = doc(["x", "y"])
    _, server = run(core().deliver(URI, document, Force()), committed(document, "updated", 1, 1, 1))
    (request,) = server.requests
    assert "If-Match" not in request.headers and "If-None-Match" not in request.headers
    assert json.loads(request.body_bytes() or b"")["force"] is True


def test_deliver_recovers_a_missing_blob_into_a_session() -> None:
    """blob 永远不能内联：开会话上传后，以内联 + ``staging_session`` 重新 commit（core §6）。"""
    document = image_doc()
    parts = inline(image_doc().model_dump())
    missing = problem(
        "DPE_MISSING_CONTENT", 400, missing={"pages": [], "content_hashes": [], "blobs": [REF]}
    )
    server = Script(
        missing,
        negotiated(),
        uploaded({}),
        committed(document, "created", 1, 0, 0),
    )
    result = drive(core().deliver(URI, document, IfAbsent(), blobs=lambda ref: IMAGE), server)
    assert result.status == "created"
    first, negotiate_req, blob_req, commit_req = server.requests
    assert "staging_session" not in json.loads(first.body_bytes() or b"")
    assert (negotiate_req.method, negotiate_req.target) == ("POST", "negotiate")
    assert blob_req.target == f"staging/st-1/blobs/{REF}"
    assert blob_req.body_bytes() == IMAGE
    assert blob_req.headers["Content-Type"] == "image/png"  # 元素带的 mime_type
    body = json.loads(commit_req.body_bytes() or b"")
    assert body["staging_session"] == "st-1"
    assert body["document"] == parts.document and body["objects"] == parts.objects  # 其余对象不重传
    assert "force" not in body


def test_deliver_missing_blob_without_a_provider_is_reported() -> None:
    document = image_doc()
    server = Script(problem("DPE_MISSING_CONTENT", 400, missing={"blobs": [REF]}), negotiated())
    with pytest.raises(errors.MissingContentError) as info:
        drive(core().deliver(URI, document, IfAbsent()), server)
    assert info.value.missing.blobs == [REF]
    assert info.value.session is not None and info.value.session.id == "st-1"
    assert [r.method for r in server.requests] == ["PUT", "POST"]  # 未上传任何字节


def test_deliver_rejects_a_provider_source_with_the_wrong_ref() -> None:
    document = image_doc()
    server = Script(problem("DPE_MISSING_CONTENT", 400, missing={"blobs": [REF]}), negotiated())
    with pytest.raises(ValueError, match="不符"):
        drive(core().deliver(URI, document, IfAbsent(), blobs=lambda ref: b"other"), server)


def test_deliver_rejects_a_manifest_entry_not_in_the_document() -> None:
    document = doc(["x"])
    server = Script(
        problem("DPE_MISSING_CONTENT", 400, missing={"content_hashes": [H1]}), negotiated()
    )
    with pytest.raises(errors.UnexpectedResponseError, match="不是本次提交引用"):
        drive(core().deliver(URI, document, IfAbsent()), server)


def test_deliver_rejects_a_manifest_blob_not_in_the_document() -> None:
    other = dpe_hash.blob_ref(b"other")
    server = Script(problem("DPE_MISSING_CONTENT", 400, missing={"blobs": [other]}), negotiated())
    with pytest.raises(errors.UnexpectedResponseError, match="不是本次提交引用"):
        drive(core().deliver(URI, image_doc(), IfAbsent(), blobs=lambda ref: b"other"), server)


def test_deliver_rejects_a_malformed_manifest_blob() -> None:
    server = Script(
        problem("DPE_MISSING_CONTENT", 400, missing={"blobs": ["dpe1:" + "0" * 64]}),
        negotiated(),
    )
    with pytest.raises(errors.UnexpectedResponseError, match="不是合法的 blob 引用"):
        drive(core().deliver(URI, image_doc(), IfAbsent()), server)


def test_deliver_without_a_blob_source_fails_fast() -> None:
    """缺 blob 来源是本地判定：直接逃出恢复循环——已上传的页与元素不因空转而重传。"""
    document = image_doc()
    parts = inline(document.model_dump())
    c = ProtocolCore(caps(limits={"max_payload_bytes": 200}))  # 内联放不下 → 暂存路径
    server = Script(
        negotiated(missing_pages=[parts.page_hashes[0]]),
        uploaded({"missing_content_hashes": parts.content_hashes[0]}),
        uploaded({"missing_blobs": [REF]}),
    )
    with pytest.raises(errors.MissingContentError) as info:
        drive(c.deliver(URI, document, IfAbsent()), server)
    assert [r.method for r in server.requests] == ["POST", "PUT", "PUT"]  # 页与元素各传一次
    assert info.value.missing.blobs == [REF]
    assert info.value.session is not None and info.value.session.id == "st-1"


def test_deliver_rejects_an_out_of_document_blob_from_an_element_response() -> None:
    """元素上传响应给出的域外 blob 同样先过校验：不把未校验的引用交给取字节回调。"""
    document = doc(["x"])
    parts = inline(text_doc(["x"]))
    other = dpe_hash.blob_ref(b"other")
    server = Script(
        negotiated(missing_pages=[parts.page_hashes[0]]),
        uploaded({"missing_content_hashes": parts.content_hashes[0]}),
        uploaded({"missing_blobs": [other]}),
    )
    asked: list[str] = []

    def provider(ref: str) -> bytes:
        asked.append(ref)
        return b""

    with pytest.raises(errors.UnexpectedResponseError, match="不是本次提交引用"):
        drive(staged_core().deliver(URI, document, IfAbsent(), blobs=provider), server)
    assert asked == []


def test_deliver_fast_path_recovery_falls_back_when_the_body_overflows() -> None:
    """内联体贴着上限（加上会话 id 即超限）：恢复改走暂存路径，请求体只含文档对象。"""
    document = image_doc()
    parts = inline(document.model_dump())
    inline_body = {"document": parts.document, "pages": parts.pages, "objects": parts.objects}
    limit = len(wire_json(inline_body))  # 恰好放得下内联体，放不下「内联 + staging_session」
    c = ProtocolCore(caps(limits={"max_payload_bytes": limit}))
    server = Script(
        problem("DPE_MISSING_CONTENT", 400, missing={"blobs": [REF]}),
        negotiated("st-1"),
        problem("DPE_MISSING_CONTENT", 400, missing={"blobs": [REF]}),
        uploaded({}),
        committed(document, "created", 1, 0, 0),
    )
    result = drive(c.deliver(URI, document, IfAbsent(), blobs=lambda ref: IMAGE), server)
    assert result.status == "created"
    assert [r.method for r in server.requests] == ["PUT", "POST", "PUT", "PUT", "PUT"]
    first, _, staged_commit, blob_put, _ = server.requests
    assert len(first.body_bytes() or b"") == limit
    assert json.loads(staged_commit.body_bytes() or b"") == {
        "document": parts.document,
        "staging_session": "st-1",
    }
    assert blob_put.target == f"staging/st-1/blobs/{REF}"


# ---------------------------------------------------------------------- deliver：暂存路径


def test_deliver_staged_path_follows_the_layers() -> None:
    document = doc(["x", "y"])
    parts = inline(text_doc(["x", "y"]))
    page_hash, element_hashes = parts.page_hashes[0], parts.content_hashes[0]
    server = Script(
        negotiated(missing_pages=[page_hash]),
        uploaded({"missing_content_hashes": element_hashes}),
        uploaded({"missing_blobs": []}),
        uploaded({"missing_blobs": []}),
        committed(document, "created", 2, 0, 0),
    )
    result = drive(staged_core().deliver(URI, document, IfAbsent()), server)
    assert result.status == "created"
    negotiate_req, page_req, first, second, commit_req = server.requests
    assert negotiate_req.target == "negotiate"
    assert page_req.target == f"staging/st-1/pages/{page_hash}"
    assert (first.target, second.target) == tuple(
        f"staging/st-1/objects/{h}" for h in element_hashes
    )
    assert json.loads(commit_req.body_bytes() or b"") == {
        "document": parts.document,
        "staging_session": "st-1",
    }


def test_deliver_recovery_uploads_into_the_same_session() -> None:
    """commit 报 MISSING_CONTENT：按清单补进同一会话后重提（core §3.4）。"""
    document = doc(["x"])
    parts = inline(text_doc(["x"]))
    page_hash, element_hashes = parts.page_hashes[0], parts.content_hashes[0]
    server = Script(
        negotiated(),
        problem(
            "DPE_MISSING_CONTENT",
            400,
            missing={"pages": [page_hash], "content_hashes": [], "blobs": []},
        ),
        uploaded({"missing_content_hashes": element_hashes}),
        uploaded({"missing_blobs": []}),
        committed(document, "created", 1, 0, 0),
    )
    result = drive(staged_core().deliver(URI, document, IfAbsent()), server)
    assert result.status == "created"
    targets = [r.target for r in server.requests]
    assert targets[2] == f"staging/st-1/pages/{page_hash}"  # 同一会话 st-1
    assert targets[-1] == TARGET
    assert json.loads(server.requests[-1].body_bytes() or b"")["staging_session"] == "st-1"


def test_deliver_renegotiates_when_the_session_expires_mid_flow() -> None:
    document = doc(["x"])
    parts = inline(text_doc(["x"]))
    page_hash, element_hashes = parts.page_hashes[0], parts.content_hashes[0]
    server = Script(
        negotiated("st-1", missing_pages=[page_hash]),
        problem("DPE_SESSION_EXPIRED", 410),
        negotiated("st-2", missing_pages=[page_hash]),
        uploaded({"missing_content_hashes": element_hashes}),
        uploaded({"missing_blobs": []}),
        committed(document, "created", 1, 0, 0),
    )
    result = drive(staged_core().deliver(URI, document, IfAbsent()), server)
    assert result.status == "created"
    targets = [r.target for r in server.requests]
    assert targets[1] == f"staging/st-1/pages/{page_hash}"
    assert targets[3] == f"staging/st-2/pages/{page_hash}"
    assert json.loads(server.requests[-1].body_bytes() or b"")["staging_session"] == "st-2"


def test_deliver_recovery_is_bounded() -> None:
    """服务端始终报同一份缺失清单（无进展）：恢复轮耗尽后如实抛出，不无限重试。"""
    document = doc(["x"])
    parts = inline(text_doc(["x"]))
    page_hash = parts.page_hashes[0]
    missing = problem("DPE_MISSING_CONTENT", 400, missing={"pages": [page_hash]})
    server = Script(
        negotiated(missing_pages=[page_hash]),
        uploaded({"missing_content_hashes": []}),
        missing,
        uploaded({"missing_content_hashes": []}),
        missing,
    )
    core_one = ProtocolCore(caps(limits={"max_payload_bytes": 120}), max_resends=1)
    with pytest.raises(errors.MissingContentError):
        drive(core_one.deliver(URI, document, IfAbsent()), server)
    assert [r.method for r in server.requests] == ["POST", "PUT", "PUT", "PUT", "PUT"]


# ---------------------------------------------------------------------- deliver：会话复用与 CAS


def test_deliver_with_a_provided_session_commits_directly() -> None:
    document = doc(["x"])
    parts = inline(text_doc(["x"]))
    session = Session("st-9", URI, EXPIRES)
    _, server = run(
        core().deliver(URI, document, IfAbsent(), session=session),
        committed(document, "created", 1, 0, 0),
    )
    (request,) = server.requests
    assert (request.method, request.target) == ("PUT", TARGET)
    assert json.loads(request.body_bytes() or b"") == {
        "document": parts.document,
        "staging_session": "st-9",
    }


def test_deliver_with_an_expired_session_renegotiates() -> None:
    """会话只作缓存：传入失效会话时重开 negotiate，结果与不复用时一致（core §3.4）。"""
    document = doc(["x"])
    parts = inline(text_doc(["x"]))
    page_hash, element_hashes = parts.page_hashes[0], parts.content_hashes[0]
    session = Session("st-9", URI, EXPIRES)
    server = Script(
        problem("DPE_SESSION_EXPIRED", 410),
        negotiated("st-2", missing_pages=[page_hash]),
        uploaded({"missing_content_hashes": element_hashes}),
        uploaded({"missing_blobs": []}),
        committed(document, "created", 1, 0, 0),
    )
    result = drive(staged_core().deliver(URI, document, IfAbsent(), session=session), server)
    assert result.status == "created"
    assert json.loads(server.requests[-1].body_bytes() or b"")["staging_session"] == "st-2"


def test_deliver_fast_path_renegotiates_when_the_session_expires() -> None:
    """快路径开会话后上传遇 410：重开 negotiate，补传保留清单，以「内联 + 新会话」重提。"""
    document = image_doc()
    server = Script(
        problem("DPE_MISSING_CONTENT", 400, missing={"blobs": [REF]}),
        negotiated("st-1"),
        problem("DPE_SESSION_EXPIRED", 410),
        negotiated("st-2"),
        uploaded({}),
        committed(document, "created", 1, 0, 0),
    )
    result = drive(core().deliver(URI, document, IfAbsent(), blobs=lambda ref: IMAGE), server)
    assert result.status == "created"
    targets = [r.target for r in server.requests]
    assert targets[2] == f"staging/st-1/blobs/{REF}"
    assert targets[4] == f"staging/st-2/blobs/{REF}"  # 新会话里重新补传
    body = json.loads(server.requests[-1].body_bytes() or b"")
    assert body["staging_session"] == "st-2"
    assert body["pages"] and body["objects"]  # 仍是「内联 + 会话」的请求体


@pytest.mark.parametrize(
    ("code", "status", "error"),
    [
        ("DPE_PRECONDITION_FAILED", 412, errors.PreconditionFailedError),
        ("DPE_ALREADY_EXISTS", 412, errors.AlreadyExistsError),
        ("DPE_NOT_FOUND", 412, errors.NotFoundError),
    ],
)
def test_deliver_reports_cas_failures_as_is_with_the_session(
    code: str, status: int, error: type[Exception]
) -> None:
    """CAS 三类原样抛出（绝不自动 force），异常带当前会话供上层复用（core §3.4、§5.2）。"""
    document = doc(["x"])
    session = Session("st-9", URI, EXPIRES)
    server = Script(problem(code, status))
    with pytest.raises(error) as info:
        drive(staged_core().deliver(URI, document, BaseHash(H1), session=session), server)
    assert info.value.session is session  # type: ignore[attr-defined]
    assert [r.method for r in server.requests] == ["PUT"]  # 不重发、不 head、不 force


def test_deliver_cas_failure_without_a_session_has_none() -> None:
    document = doc(["x", "y"])
    server = Script(problem("DPE_PRECONDITION_FAILED", 412))
    with pytest.raises(errors.PreconditionFailedError) as info:
        drive(core().deliver(URI, document, BaseHash(H1)), server)
    assert info.value.session is None


def test_deliver_validates_locally_without_requests() -> None:
    with pytest.raises(TypeError, match="Document"):
        next(core().deliver(URI, text_doc(["x"]), IfAbsent()))  # type: ignore[arg-type]
    with pytest.raises(dpe_hash.ValidationError):
        next(core().deliver("no scheme", doc(["x"]), IfAbsent()))
    with pytest.raises(TypeError, match="前置条件"):
        next(core().deliver(URI, doc(["x"]), None))  # type: ignore[arg-type]
    with pytest.raises(dpe_hash.ContractUnsupportedError):
        next(core().deliver(URI, doc(["x"]), BaseHash(H2.replace("dpe1", "dpe2"))))
