"""参考服务端引擎的暂存路径（#43）：negotiate、上传、分块与断点（core §3.4、http §4.5–§4.7）。"""

from __future__ import annotations

import json
from typing import Any

import dpe_hash
import pytest
from dpe_sdk import errors
from dpe_sdk.testing import BaseHash, DedupScope, Engine, IfAbsent, UploadChunk, UploadOffsetError
from dpe_sdk.wire import ElementUploadMissing, PageUploadMissing
from engine_helpers import (
    FakeClock,
    PrefixAuthorizer,
    chunks,
    commit_body,
    id_factory,
    inline,
    make_engine,
    negotiate_body,
)

URI = "test://docs/a"
OTHER_URI = "test://docs/b"
C = dpe_hash.CONTRACT


def _element(text: str = "x") -> tuple[dict[str, Any], str]:
    body = {"category": "NarrativeText", "text": text}
    return body, dpe_hash.object_hash(body, "element", C)


def _image(blob: str) -> tuple[dict[str, Any], str]:
    body = {"category": "Image", "blob": blob, "mime_type": "image/png"}
    return body, dpe_hash.object_hash(body, "element", C)


def _page(elements: list[str], **fields: Any) -> tuple[dict[str, Any], str]:
    body: dict[str, Any] = {**fields, "elements": elements}
    return body, dpe_hash.object_hash(body, "page", C)


def _open(engine: Engine, uri: str = URI, caller: str = "u") -> str:
    """开一个空文档的会话并返回会话 id。"""
    result = engine.negotiate(caller, negotiate_body(uri, {"file_type": "md", "pages": []}), C)
    return result.staging_session.id


def _commit_fast(
    engine: Engine, expanded: dict[str, Any], uri: str = URI, caller: str = "u"
) -> Any:
    req = inline(expanded)
    return engine.commit(caller, uri, req.body(), C, IfAbsent())


# ---------------------------------------------------------------------------
# negotiate（HTTP 绑定 §4.5）
# ---------------------------------------------------------------------------


def test_negotiate_missing_layers_and_scope() -> None:
    """页面层与附带页的元素层：范围内已存的不列，缺的按文档顺序列出。"""
    engine = make_engine()
    x, xh = _element("x")
    _commit_fast(engine, {"file_type": "md", "pages": [{"elements": [x]}]})  # v1：页 A 引用 x
    _, ah = _page([xh])
    _, yh = _element("y")
    b, bh = _page([xh, yh])
    _, ch = _page([yh])
    document = {"file_type": "md", "pages": [ah, bh, ch]}
    result = engine.negotiate("u", negotiate_body(URI, document, pages=[b]), C)
    # A 在本文档当前状态（去重范围下界）中；B 已附带；C 缺失
    assert result.missing_pages == [ch]
    # 附带页 B 的 x 在范围内、y 缺失；未附带页 C 的元素不在 negotiate 列出
    assert result.missing_content_hashes == [yh]


def test_negotiate_page_swap_and_insertion() -> None:
    """对调两页 missing_pages 为空；中间插入一页时只含新页（conformance「大文档与逐层协商」）。"""
    engine = make_engine(session_id_factory=id_factory())
    a, ah = _page([], title="A")
    b, bh = _page([], title="B")
    _commit_fast(
        engine,
        {"file_type": "md", "pages": [a, b]},
    )  # 注：expanded 视图的页就是页对象本身
    _head = engine.head("u", URI, C)
    assert _head is not None
    swapped = engine.negotiate("u", negotiate_body(URI, {"file_type": "md", "pages": [bh, ah]}), C)
    assert swapped.missing_pages == []  # 两页都在本文档当前状态中
    assert swapped.missing_content_hashes == []
    swapped_doc = {"file_type": "md", "pages": [bh, ah]}
    swap = engine.commit(
        "u",
        URI,
        commit_body(swapped_doc, staging_session=swapped.staging_session.id),
        C,
        BaseHash(_head.doc_hash),
    )
    assert swap.status == "updated"
    assert (swap.delta.added, swap.delta.removed, swap.delta.retained) == (0, 0, 0)
    inserted, ih = _page([], title="New")
    doc = {"file_type": "md", "pages": [ah, ih, bh]}
    result = engine.negotiate("u", negotiate_body(URI, doc), C)
    assert result.missing_pages == [ih]
    engine.upload_page("u", result.staging_session.id, ih, json.dumps(inserted).encode(), C)
    body = commit_body(doc, staging_session=result.staging_session.id)
    commit = engine.commit("u", URI, body, C, BaseHash(swap.doc_hash))
    assert commit.status == "updated"


def test_negotiate_attached_pages_enter_session() -> None:
    engine = make_engine(session_id_factory=id_factory())
    _, xh = _element()
    b, bh = _page([xh])
    result = engine.negotiate(
        "u", negotiate_body(URI, {"file_type": "md", "pages": [bh]}, pages=[b]), C
    )
    assert result.missing_pages == []
    upload = engine.upload_page("u", result.staging_session.id, bh, json.dumps(b).encode(), C)
    assert upload.outcome == "duplicate"  # 附带页已存入会话


def test_negotiate_requires_write_authorization() -> None:
    engine = make_engine(
        authorizer=PrefixAuthorizer({"u": ("s3://ok/",)}), session_id_factory=id_factory()
    )
    document = {"file_type": "md", "pages": []}
    with pytest.raises(errors.ForbiddenError):
        engine.negotiate("u", negotiate_body(URI, document), C)
    # 报文校验先于授权：非法体即使无授权也是 DPE_VALIDATION
    with pytest.raises(errors.ValidationError):
        engine.negotiate("u", b"[]", C)
    # 未通过授权的调用者未获得会话：下一个会话 id 仍是一号
    ok = engine.negotiate("u", negotiate_body("s3://ok/a", document), C)
    assert ok.staging_session.id == "st-0"


def test_negotiate_validation_ladder() -> None:
    engine = make_engine()
    document = {"file_type": "md", "pages": []}
    cases: list[tuple[bytes, type[errors.DpeError]]] = [
        (b"not json", errors.ValidationError),
        (json.dumps({}).encode(), errors.ValidationError),  # 缺 file_uri
        (
            json.dumps({"file_uri": URI, "document": document, "x": 1}).encode(),
            errors.ValidationError,  # 未定义成员
        ),
        (
            json.dumps({"file_uri": URI, "document": document, "pages": {}}).encode(),
            errors.ValidationError,  # pages 不是数组
        ),
        (negotiate_body("docs/a", document), errors.ValidationError),  # 相对 URI
        (negotiate_body("", document), errors.ValidationError),  # 空串
        (negotiate_body(URI, {"pages": []}), errors.ValidationError),  # 文档缺 file_type
        (negotiate_body(URI, document, pages=[{}]), errors.ValidationError),  # 附带页不合法
    ]
    for body, expected in cases:
        with pytest.raises(expected):
            engine.negotiate("u", body, C)
    with pytest.raises(errors.ContractUnsupportedError):
        engine.negotiate("u", negotiate_body(URI, document), "dpe9")
    with pytest.raises(errors.PayloadTooLargeError):
        make_engine(max_payload_bytes=10).negotiate("u", negotiate_body(URI, document), C)


def test_negotiate_clock_and_session_id() -> None:
    clock = FakeClock()
    engine = make_engine(clock=clock, staging_ttl_seconds=3600, session_id_factory=id_factory())
    result = engine.negotiate("u", negotiate_body(URI, {"file_type": "md", "pages": []}), C)
    assert result.staging_session.id == "st-0"
    assert result.staging_session.expires_at == "2026-01-01T13:00:00Z"


# ---------------------------------------------------------------------------
# 上传：非分块（HTTP 绑定 §4.6）
# ---------------------------------------------------------------------------


def test_upload_page_manifest_and_duplicate() -> None:
    engine = make_engine(session_id_factory=id_factory())
    sid = _open(engine)
    x, xh = _element()
    _, yh = _element("y")
    page, ph = _page([xh, yh])
    raw = json.dumps(page).encode()
    upload = engine.upload_page("u", sid, ph, raw, C)
    assert upload.outcome == "created"
    assert isinstance(upload.missing, PageUploadMissing)
    assert upload.missing.missing_content_hashes == [xh, yh]
    engine.upload_element("u", sid, xh, json.dumps(x).encode(), C)
    again = engine.upload_page("u", sid, ph, raw, C)
    assert again.outcome == "duplicate"
    assert isinstance(again.missing, PageUploadMissing)
    assert again.missing.missing_content_hashes == [yh]


def test_upload_page_hash_mismatch() -> None:
    engine = make_engine(session_id_factory=id_factory())
    sid = _open(engine)
    _, xh = _element()
    page, ph = _page([xh])
    engine.upload_page("u", sid, ph, json.dumps(page).encode(), C)
    # 同一路径、被篡改的请求体：DPE_HASH_MISMATCH，而不是 200 重复（core §3.4）
    tampered = json.dumps({"title": "T", "elements": [xh]}).encode()
    with pytest.raises(errors.HashMismatchError):
        engine.upload_page("u", sid, ph, tampered, C)


def test_upload_element_manifest_blobs_and_duplicate() -> None:
    engine = make_engine(session_id_factory=id_factory())
    sid = _open(engine)
    blob = b"\x89PNG-staging"
    ref = dpe_hash.blob_ref(blob)
    image, ih = _image(ref)
    raw = json.dumps(image).encode()
    upload = engine.upload_element("u", sid, ih, raw, C)
    assert upload.outcome == "created"
    assert isinstance(upload.missing, ElementUploadMissing)
    assert upload.missing.missing_blobs == [ref]
    engine.upload_blob("u", sid, ref, blob, C)
    again = engine.upload_element("u", sid, ih, raw, C)
    assert again.outcome == "duplicate"
    assert isinstance(again.missing, ElementUploadMissing)
    assert again.missing.missing_blobs == []
    # 元素不分块，超过 max_payload_bytes → 413（限额恰好容下 negotiate 的请求体）
    negotiate_size = len(negotiate_body(URI, {"file_type": "md", "pages": []}))
    small = make_engine(max_payload_bytes=negotiate_size, session_id_factory=id_factory())
    sid2 = _open(small)
    assert len(raw) > negotiate_size
    with pytest.raises(errors.PayloadTooLargeError):
        small.upload_element("u", sid2, ih, raw, C)


def test_upload_page_page_max_bytes() -> None:
    engine = make_engine(page_max_bytes=50, session_id_factory=id_factory())
    sid = _open(engine)
    _, xh = _element()
    page, ph = _page([xh], title="T" * 100)
    with pytest.raises(errors.PayloadTooLargeError):
        engine.upload_page("u", sid, ph, json.dumps(page).encode(), C)


# ---------------------------------------------------------------------------
# 上传：blob（HTTP 绑定 §4.7 整体上传）
# ---------------------------------------------------------------------------


def test_upload_blob_whole() -> None:
    engine = make_engine(session_id_factory=id_factory())
    sid = _open(engine)
    blob = b"\x00\x01binary"
    ref = dpe_hash.blob_ref(blob)
    with pytest.raises(errors.ValidationError):
        engine.upload_blob("u", sid, "sha256:zz", blob, C)  # 路径引用格式
    with pytest.raises(errors.HashMismatchError):
        engine.upload_blob("u", sid, dpe_hash.blob_ref(b"other"), blob, C)
    assert engine.upload_blob("u", sid, ref, blob, C).outcome == "created"
    assert engine.upload_blob("u", sid, ref, blob, C).outcome == "duplicate"
    small = make_engine(blob_max_bytes=4, session_id_factory=id_factory())
    sid2 = _open(small)
    with pytest.raises(errors.PayloadTooLargeError):
        small.upload_blob("u", sid2, ref, blob, C)


# ---------------------------------------------------------------------------
# 分块与断点（HTTP 绑定 §4.7）
# ---------------------------------------------------------------------------


def test_page_chunked_flow() -> None:
    engine = make_engine(session_id_factory=id_factory(), blob_chunk_bytes=40)
    sid = _open(engine)
    _, xh = _element("分块")
    page, ph = _page([xh])
    raw = json.dumps(page).encode()
    parts = chunks(raw, 40)
    assert len(parts) >= 2
    for piece, part in parts[:-1]:
        upload = engine.upload_page("u", sid, ph, piece, C, chunk=part)
        assert upload.outcome == "partial"
        assert upload.offset == part.to_byte + 1
    piece, part = parts[-1]
    final = engine.upload_page("u", sid, ph, piece, C, chunk=part)
    assert final.outcome == "created"
    assert isinstance(final.missing, PageUploadMissing)
    assert final.missing.missing_content_hashes == [xh]
    assert engine.upload_offset("u", sid, ph, C, kind="page").offset == len(raw)


def test_completed_chunk_upload_skips_validation() -> None:
    engine = make_engine(session_id_factory=id_factory())
    sid = _open(engine)
    blob = b"abcdefgh"
    ref = dpe_hash.blob_ref(blob)
    engine.upload_blob("u", sid, ref, blob, C)
    # 任意块、任意范围、任意体：已完成的 hash 幂等成功，不校验 Content-Range 与请求体
    upload = engine.upload_blob("u", sid, ref, b"garbage", C, chunk=UploadChunk(0, 6, 7))
    assert upload.outcome == "duplicate"
    # 页对象同理
    _, xh = _element()
    page, ph = _page([xh])
    engine.upload_page("u", sid, ph, json.dumps(page).encode(), C)
    upload = engine.upload_page("u", sid, ph, b"junk", C, chunk=UploadChunk(5, 2, 3))
    assert upload.outcome == "duplicate"


def test_chunk_consistency_violations() -> None:
    engine = make_engine(session_id_factory=id_factory())
    sid = _open(engine)
    blob = b"0123456789"
    ref = dpe_hash.blob_ref(blob)
    with pytest.raises(errors.ValidationError):
        engine.upload_blob("u", sid, ref, b"012", C, chunk=UploadChunk(0, 5, 10))  # 块长不符
    with pytest.raises(errors.ValidationError):
        engine.upload_blob("u", sid, ref, b"012", C, chunk=UploadChunk(2, 1, 10))  # 起点大于终点
    with pytest.raises(errors.ValidationError):
        engine.upload_blob("u", sid, ref, b"012", C, chunk=UploadChunk(8, 10, 10))  # 超出 total
    engine.upload_blob("u", sid, ref, blob[:4], C, chunk=UploadChunk(0, 3, 10))
    with pytest.raises(errors.ValidationError):
        engine.upload_blob("u", sid, ref, blob[4:8], C, chunk=UploadChunk(4, 7, 11))  # total 不一致


def test_chunk_offset_error_carries_offset() -> None:
    engine = make_engine(session_id_factory=id_factory())
    sid = _open(engine)
    blob = b"0123456789"
    ref = dpe_hash.blob_ref(blob)
    engine.upload_blob("u", sid, ref, blob[:4], C, chunk=UploadChunk(0, 3, 10))
    engine.upload_blob("u", sid, ref, blob[4:8], C, chunk=UploadChunk(4, 7, 10))
    with pytest.raises(UploadOffsetError) as info:
        engine.upload_blob("u", sid, ref, blob[:4], C, chunk=UploadChunk(0, 3, 10))
    assert info.value.code == "DPE_VALIDATION"
    assert info.value.offset == 8  # 本会话已收字节数，供重新同步


def test_chunk_limits_reject_first_chunk_without_receiving() -> None:
    chunk_engine = make_engine(session_id_factory=id_factory(), blob_chunk_bytes=4)
    sid = _open(chunk_engine)
    blob = b"0123456789"
    ref = dpe_hash.blob_ref(blob)
    with pytest.raises(errors.PayloadTooLargeError):  # 单块超限
        chunk_engine.upload_blob("u", sid, ref, blob[:8], C, chunk=UploadChunk(0, 7, 10))
    total_engine = make_engine(session_id_factory=id_factory(), blob_max_bytes=6)
    sid2 = _open(total_engine)
    with pytest.raises(errors.PayloadTooLargeError):  # 声明的总量首块即判
        total_engine.upload_blob("u", sid2, ref, blob[:4], C, chunk=UploadChunk(0, 3, 10))
    assert chunk_engine.upload_offset("u", sid, ref, C, kind="blob").offset == 0  # 未接收任何字节
    assert total_engine.upload_offset("u", sid2, ref, C, kind="blob").offset == 0


def test_page_chunk_total_limit() -> None:
    page: dict[str, Any] = {"elements": []}
    raw = json.dumps(page).encode()
    engine = make_engine(session_id_factory=id_factory(), page_max_bytes=len(raw) - 1)
    sid = _open(engine)
    _, ph = _page([])
    with pytest.raises(errors.PayloadTooLargeError):
        engine.upload_page("u", sid, ph, raw[:4], C, chunk=UploadChunk(0, 3, len(raw)))
    assert engine.upload_offset("u", sid, ph, C, kind="page").offset == 0


def test_chunked_page_completion_failure_discards_and_recovers() -> None:
    engine = make_engine(session_id_factory=id_factory(), blob_chunk_bytes=8)
    sid = _open(engine)
    _, xh = _element()
    page, ph = _page([xh])
    raw = json.dumps(page).encode()
    parts = chunks(raw, 8)
    # 用错误的路径 hash：到齐校验不符 → HASH_MISMATCH 并丢弃已收内容
    wrong_hash = "dpe1:" + "0" * 64
    for piece, part in parts[:-1]:
        engine.upload_page("u", sid, wrong_hash, piece, C, chunk=part)
    piece, part = parts[-1]
    with pytest.raises(errors.HashMismatchError):
        engine.upload_page("u", sid, wrong_hash, piece, C, chunk=part)
    assert engine.upload_offset("u", sid, wrong_hash, C, kind="page").offset == 0  # 丢弃后归零
    # 以正确路径从零重传成功（补传收敛）
    upload = None
    for piece, part in parts:
        upload = engine.upload_page("u", sid, ph, piece, C, chunk=part)
    assert upload is not None and upload.outcome == "created"


def test_chunked_page_invalid_json_discards() -> None:
    engine = make_engine(session_id_factory=id_factory())
    sid = _open(engine)
    parts = chunks(b"not json bytes", 4)
    _, ph = _page([])
    for piece, part in parts[:-1]:
        engine.upload_page("u", sid, ph, piece, C, chunk=part)
    piece, part = parts[-1]
    with pytest.raises(errors.ValidationError):
        engine.upload_page("u", sid, ph, piece, C, chunk=part)
    assert engine.upload_offset("u", sid, ph, C, kind="page").offset == 0


def test_whole_upload_voids_partial_progress() -> None:
    engine = make_engine(session_id_factory=id_factory(), blob_chunk_bytes=8)
    sid = _open(engine)
    blob = b"abcdefghij"
    ref = dpe_hash.blob_ref(blob)
    engine.upload_blob("u", sid, ref, blob[:4], C, chunk=UploadChunk(0, 3, 10))
    assert engine.upload_offset("u", sid, ref, C, kind="blob").offset == 4
    upload = engine.upload_blob("u", sid, ref, blob, C)  # 整段上传：部分进度作废
    assert upload.outcome == "created"
    assert engine.upload_offset("u", sid, ref, C, kind="blob").offset == 10
    # 已完成：再来任意分块 → 重复
    again = engine.upload_blob("u", sid, ref, blob[4:], C, chunk=UploadChunk(4, 9, 10))
    assert again.outcome == "duplicate"


# ---------------------------------------------------------------------------
# 断点查询（HTTP 绑定 §4.7）：三状态、只读不续期
# ---------------------------------------------------------------------------


def test_upload_offset_three_states_and_no_renewal() -> None:
    clock = FakeClock()
    engine = make_engine(clock=clock, staging_ttl_seconds=3600, session_id_factory=id_factory())
    sid = _open(engine)
    _, xh = _element()
    page, ph = _page([xh])
    raw = json.dumps(page).encode()
    # 无进度 → 0（不得 404）
    assert engine.upload_offset("u", sid, ph, C, kind="page").offset == 0
    clock.advance(100)
    first = engine.upload_page("u", sid, ph, raw[:6], C, chunk=UploadChunk(0, 5, len(raw)))
    assert first.outcome == "partial"
    snapshot = engine.upload_offset("u", sid, ph, C, kind="page")
    assert snapshot.offset == 6
    # 成功上传续期到「此刻 + ttl」；断点查询不续期（过期时间不变）
    assert snapshot.expires_at == "2026-01-01T13:01:40Z"
    assert engine.upload_offset("u", sid, ph, C, kind="page").expires_at == snapshot.expires_at
    # 完成 → 总字节数
    engine.upload_page("u", sid, ph, raw[6:], C, chunk=UploadChunk(6, len(raw) - 1, len(raw)))
    assert engine.upload_offset("u", sid, ph, C, kind="page").offset == len(raw)


def test_upload_offset_bad_session() -> None:
    engine = make_engine(session_id_factory=id_factory())
    with pytest.raises(errors.SessionExpiredError):
        engine.upload_offset("u", "st-none", "dpe1:" + "0" * 64, C, kind="page")


# ---------------------------------------------------------------------------
# WRITABLE 去重范围的清单（core §3.3、§8）
# ---------------------------------------------------------------------------


def test_staging_manifest_writable_scope() -> None:
    x, xh = _element("shared")
    holder = make_engine(dedup_scope=DedupScope.WRITABLE)
    _commit_fast(holder, {"file_type": "md", "pages": [{"elements": [x]}]}, uri=OTHER_URI)
    b, bh = _page([xh])
    document = {"file_type": "md", "pages": [bh]}
    # WRITABLE：持有者文档可写 → 不列为缺失
    r1 = holder.negotiate("u", negotiate_body(URI, document, pages=[b]), C)
    assert r1.missing_pages == []
    assert r1.missing_content_hashes == []
    # DOCUMENT（默认）：范围只有目标文档当前状态（空）→ 附带页的元素缺失
    base = make_engine()
    r2 = base.negotiate("u", negotiate_body(URI, document, pages=[b]), C)
    assert r2.missing_pages == []
    assert r2.missing_content_hashes == [xh]


def test_writable_scope_unwritable_holder_is_missing() -> None:
    """调用者无写授权的文档中的对象一律视为缺失（core §8 防探测）。"""
    x, xh = _element("private")
    engine = make_engine(
        dedup_scope=DedupScope.WRITABLE,
        authorizer=PrefixAuthorizer({"u": ("test://docs/",), "v": ("test://private/",)}),
        session_id_factory=id_factory(),
    )
    _commit_fast(
        engine,
        {"file_type": "md", "pages": [{"elements": [x]}]},
        uri="test://private/p",
        caller="v",
    )
    b, bh = _page([xh])
    r = engine.negotiate("u", negotiate_body(URI, {"file_type": "md", "pages": [bh]}, pages=[b]), C)
    assert r.missing_content_hashes == [xh]


def test_writable_scope_authorizer_callback_into_engine() -> None:
    """授权器可回调引擎（在锁外调用，不得死锁）。"""

    class ReentrantAuthorizer:
        engine: Engine | None = None

        def __init__(self) -> None:
            self.calls: list[str] = []

        def can_write(self, caller: str, uri: str) -> bool:
            assert self.engine is not None
            self.engine.head(caller, uri, C)  # 回调引擎
            self.calls.append(uri)
            return True

        def can_force(self, caller: str, uri: str) -> bool:
            return True

    x, xh = _element("shared")
    authorizer = ReentrantAuthorizer()
    engine = make_engine(dedup_scope=DedupScope.WRITABLE, authorizer=authorizer)
    authorizer.engine = engine
    _commit_fast(engine, {"file_type": "md", "pages": [{"elements": [x]}]}, uri=OTHER_URI)
    b, bh = _page([xh])
    r = engine.negotiate("u", negotiate_body(URI, {"file_type": "md", "pages": [bh]}, pages=[b]), C)
    assert r.missing_content_hashes == []
    assert OTHER_URI in authorizer.calls
