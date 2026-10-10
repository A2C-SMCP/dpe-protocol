"""参考服务端引擎：file_uri 的校验与规范化（core §1.1），以及绑定层无法解释的条件头 / 分块
参数在求值顺序中的位置（#44）。"""

from __future__ import annotations

import json
from collections.abc import Callable

import dpe_hash
import pytest
from dpe_sdk import errors
from dpe_sdk.testing import (
    BaseHash,
    Engine,
    IfAbsent,
    IfNoneMatch,
    InvalidChunk,
    InvalidPrecondition,
    PrefixAuthorizer,
    UploadChunk,
)
from engine_helpers import inline, make_engine, negotiate_body, text_doc

C = dpe_hash.CONTRACT
RAW = "FEISHU://Doc.Example/%61%2f"
NORMALIZED = "feishu://doc.example/a%2F"
INVALID = ("no-scheme", "test://a b", "test://é", "test://%zz", "")


def _put(engine: Engine, uri: str, text: str = "x") -> str:
    return engine.commit("u", uri, inline(text_doc([text])).body(), C, IfAbsent()).doc_hash


def _move_body(from_uri: str, to_uri: str, base_hash: str) -> bytes:
    return json.dumps({"from_uri": from_uri, "to_uri": to_uri, "base_hash": base_hash}).encode()


def test_normalized_form_is_identity() -> None:
    engine = make_engine()
    doc_hash = _put(engine, RAW)
    assert engine.head("u", NORMALIZED, C) is not None
    skeleton = engine.get_skeleton("u", RAW, C)
    assert skeleton is not None and skeleton.file_uri == NORMALIZED
    assert [d.file_uri for d in engine.list_documents("u", C).documents] == [NORMALIZED]
    heads = engine.batch_head("u", json.dumps({"uris": [RAW, NORMALIZED]}).encode(), C).heads
    assert [h and h.doc_hash for h in heads] == [doc_hash, doc_hash]
    # 等价写法即同一文档：if_absent 冲突、base_hash 写入作用于同一文档
    with pytest.raises(errors.AlreadyExistsError):
        _put(engine, NORMALIZED, "other")
    engine.delete("u", NORMALIZED, C, doc_hash)
    assert engine.head("u", RAW, C) is None


@pytest.mark.parametrize("uri", INVALID)
def test_invalid_uri_rejected_at_every_entry(uri: str) -> None:
    engine = make_engine()
    body = inline(text_doc(["x"])).body()
    doc = {"file_type": "md", "pages": []}
    calls: list[Callable[[], object]] = [
        lambda: engine.head("u", uri, C),
        lambda: engine.get_skeleton("u", uri, C),
        lambda: engine.commit("u", uri, body, C, IfAbsent()),
        lambda: engine.delete("u", uri, C, "dpe1:" + "0" * 64),
        lambda: engine.negotiate("u", negotiate_body(uri, doc), C),
        lambda: engine.batch_head("u", json.dumps({"uris": ["test://ok", uri]}).encode(), C),
        lambda: engine.move("u", _move_body(uri, "test://b", "dpe1:" + "0" * 64), C),
        lambda: engine.move("u", _move_body("test://b", uri, "dpe1:" + "0" * 64), C),
    ]
    for call in calls:
        with pytest.raises(errors.ValidationError):
            call()


def test_invalid_uri_position_in_order() -> None:
    """位置：契约声明之后、与请求信封同一步（HTTP 绑定 §3.1、§4.5）。"""
    engine = make_engine()
    body = inline(text_doc(["x"])).body()
    with pytest.raises(errors.ContractUnsupportedError):
        engine.commit("u", "bad", body, "dpe9", IfAbsent())
    with pytest.raises(errors.ContractUnsupportedError):
        engine.head("u", "bad", None)
    with pytest.raises(errors.ContractUnsupportedError):
        engine.get_skeleton("u", "bad", None)
    with pytest.raises(errors.ContractUnsupportedError):
        engine.delete("u", "bad", None, None)
    with pytest.raises(errors.ValidationError) as info:
        engine.batch_head("u", json.dumps({"uris": ["test://ok", "bad"]}).encode(), C)
    assert info.value.path == "/uris/1"
    with pytest.raises(errors.ValidationError) as info:
        engine.move("u", _move_body("test://a", "bad", "x"), C)
    assert info.value.path == "/to_uri"
    # 前缀不规范化、不校验（core §3）
    assert engine.list_documents("u", C, prefix="bad prefix é").documents == []


def test_move_to_equivalent_uri() -> None:
    """规范化后相等：源存在即目标已存在（core §5.2），base_hash 不符时 PRECONDITION_FAILED 优先。"""
    engine = make_engine()
    doc_hash = _put(engine, "feishu://doc/a")
    with pytest.raises(errors.PreconditionFailedError):
        engine.move("u", _move_body("FEISHU://doc/%61", "feishu://doc/a", "dpe1:" + "0" * 64), C)
    with pytest.raises(errors.AlreadyExistsError):
        engine.move("u", _move_body("FEISHU://doc/%61", "feishu://doc/a", doc_hash), C)
    assert engine.head("u", "feishu://doc/a", C) is not None


def test_invalid_precondition_position() -> None:
    """条件头形式非法：报文校验的末步（与 force 冲突同一步），先于授权与一切状态判定。"""
    engine = make_engine()
    req = inline(text_doc(["x"]))
    bad = InvalidPrecondition("If-Match 不得为 *")
    # 对象校验先于它
    with pytest.raises(errors.CategoryUnknownError):
        broken = {"document": req.document, "objects": [{"category": "Nope"}]}
        engine.commit("u", "test://a", json.dumps(broken).encode(), C, bad)
    with pytest.raises(errors.ValidationError, match="不得为"):
        engine.commit("u", "test://a", req.body(), C, bad)
    # 内容未变（unchanged 在后）也不放行
    engine.commit("u", "test://a", req.body(), C, IfAbsent())
    with pytest.raises(errors.ValidationError):
        engine.commit("u", "test://a", req.body(), C, bad)
    # delete：契约 → uri → 形式 → 授权
    with pytest.raises(errors.ContractUnsupportedError):
        engine.delete("u", "test://a", None, bad)
    with pytest.raises(errors.ValidationError, match="不得为"):
        engine.delete("u", "test://a", C, bad)


def test_invalid_precondition_before_authorization() -> None:
    """形式判定先于授权（HTTP 绑定 §3.2）：无权 + 条件头形式非法时得到形式错误，不是 403。"""
    engine = make_engine(authorizer=PrefixAuthorizer({}))
    bad = InvalidPrecondition("If-Match 不得为 *")
    req = inline(text_doc(["x"]))
    with pytest.raises(errors.ValidationError, match="不得为"):
        engine.commit("u", "test://a", req.body(), C, bad)
    # 形式合法时才轮到授权（缺前置条件的 428 也在授权之后）
    with pytest.raises(errors.ForbiddenError):
        engine.commit("u", "test://a", req.body(), C, None)
    with pytest.raises(errors.ValidationError, match="不得为"):
        engine.delete("u", "test://a", C, bad)
    with pytest.raises(errors.ForbiddenError):
        engine.delete("u", "test://a", C, None)


def test_skeleton_condition_syntax_after_uri() -> None:
    """GET 条件头的语法与 uri 同层：契约 → uri → 条件头，先于文档状态（HTTP 绑定 §3.3）。"""
    engine = make_engine()
    req = inline(text_doc(["x"]))
    engine.commit("u", "test://a", req.body(), C, IfAbsent())
    bad = InvalidPrecondition("If-None-Match 语法非法")
    with pytest.raises(errors.ContractUnsupportedError):
        engine.get_skeleton("u", "test://a", None, bad)
    with pytest.raises(errors.ValidationError, match="语法非法"):
        engine.get_skeleton("u", "test://a", C, bad)
    # 合法形态照常返回；`*` 表示空 tuple，不参与取值比较（比较在绑定层按弱比较做）
    skeleton = engine.get_skeleton("u", "test://a", C, IfNoneMatch(()))
    assert skeleton is not None and skeleton.doc_hash == req.doc_hash


def test_unparseable_base_hash_is_a_mismatch() -> None:
    """合法 entity-tag 内的值按值比较、不做格式校验（core §5.2）：比较不上即不匹配。"""
    engine = make_engine()
    req = inline(text_doc(["x"]))
    engine.commit("u", "test://a", req.body(), C, IfAbsent())
    other = inline(text_doc(["y"])).body()
    for value in ("abc", "dpe9:" + "0" * 64, 'W/"' + req.doc_hash + '"'):
        with pytest.raises(errors.PreconditionFailedError):
            engine.commit("u", "test://a", other, C, BaseHash(value))
        with pytest.raises(errors.PreconditionFailedError):
            engine.delete("u", "test://a", C, value)
    with pytest.raises(errors.NotFoundError):
        engine.commit("u", "test://none", other, C, BaseHash("abc"))


def test_invalid_chunk_after_completed_check() -> None:
    """无法解析的分块参数同属 §4.7 第 4 步：会话与「已完成 → 200」先于它。"""
    engine = make_engine()
    sid = engine.negotiate(
        "u", negotiate_body("test://a", {"file_type": "md", "pages": []}), C
    ).staging_session.id
    blob = b"abcdefgh"
    ref = dpe_hash.blob_ref(blob)
    bad = InvalidChunk("Content-Range 语法非法")
    with pytest.raises(errors.SessionExpiredError):
        engine.upload_blob("u", "st-none", ref, b"x", C, chunk=bad)
    with pytest.raises(errors.ValidationError, match="语法非法"):
        engine.upload_blob("u", sid, ref, b"x", C, chunk=bad)
    engine.upload_blob("u", sid, ref, blob, C)
    assert engine.upload_blob("u", sid, ref, b"x", C, chunk=bad).outcome == "duplicate"


def test_target_syntax_before_session() -> None:
    """分块与断点查询的路径 hash 语法：契约之后、会话之前（否则非法 hash 会得到 offset 0）。"""
    engine = make_engine()
    chunk = UploadChunk(0, 0, 2)
    for kind, target in (
        ("page", "sha256:" + "0" * 64),
        ("page", "dpe1:x"),
        ("blob", "dpe1:" + "0" * 64),
    ):
        with pytest.raises(errors.ContractUnsupportedError):
            engine.upload_offset("u", "st-none", target, None, kind=kind)  # type: ignore[arg-type]
        with pytest.raises(errors.ValidationError):
            engine.upload_offset("u", "st-none", target, C, kind=kind)  # type: ignore[arg-type]
    with pytest.raises(errors.ValidationError):
        engine.upload_page("u", "st-none", "sha256:" + "0" * 64, b"x", C, chunk=chunk)
    with pytest.raises(errors.ValidationError):
        engine.upload_blob("u", "st-none", "dpe1:" + "0" * 64, b"x", C, chunk=chunk)
    with pytest.raises(errors.SessionExpiredError):
        engine.upload_offset("u", "st-none", "dpe1:" + "0" * 64, C, kind="page")


def test_element_with_chunk_parameters_order() -> None:
    """元素对象不分块（#83）：带分块参数时 413 → 契约 → DPE_VALIDATION，先于 I-JSON；
    不带时按 §3.1（I-JSON 先于契约）。"""
    engine = make_engine(max_payload_bytes=8)
    bad = InvalidChunk("元素对象不分块")
    h = "dpe1:" + "0" * 64
    with pytest.raises(errors.PayloadTooLargeError):
        engine.upload_element("u", "st-none", h, b"{" * 9, None, chunk=bad)
    with pytest.raises(errors.ContractUnsupportedError):
        engine.upload_element("u", "st-none", h, b"{", None, chunk=bad)
    with pytest.raises(errors.ValidationError, match="不分块"):
        engine.upload_element("u", "st-none", h, b"{", C, chunk=bad)
    with pytest.raises(errors.ValidationError) as info:
        engine.upload_element("u", "st-none", h, b"{", None)
    assert "不分块" not in str(info.value)
