"""参考服务端引擎 delete（core §5.1）与 move（core §5.2，#63 修订后的求值顺序）。"""

from __future__ import annotations

import json
from typing import Any

import dpe_hash
import pytest
from dpe_sdk import errors
from dpe_sdk.testing import BaseHash, Engine, IfAbsent
from engine_helpers import PrefixAuthorizer, inline, make_engine, text_doc

C = dpe_hash.CONTRACT
ABSENT_HASH = "dpe1:" + "0" * 64


def _put(engine: Engine, uri: str, text: str = "x", caller: str = "u") -> str:
    req = inline(text_doc([text]))
    return engine.commit(caller, uri, req.body(), C, IfAbsent()).doc_hash


def _move(engine: Engine, caller: str = "u", contract: str | None = C, **payload: Any) -> str:
    return engine.move(caller, json.dumps(payload).encode(), contract).result.doc_hash


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


def test_delete() -> None:
    engine = make_engine()
    doc_hash = _put(engine, "test://a")
    engine.delete("u", "test://a", C, doc_hash)
    assert engine.head("u", "test://a", C) is None
    assert engine.list_documents("u", C).documents == []


def test_delete_order() -> None:
    auth = PrefixAuthorizer({"u": ("test://",)})
    engine = make_engine(authorizer=auth)
    doc_hash = _put(engine, "test://a")
    # 契约声明 → 授权 → 前置条件存在性 → 存在性 → base_hash
    with pytest.raises(errors.ContractUnsupportedError):
        engine.delete("intruder", "test://a", None, None)
    for uri in ("test://a", "test://none"):
        with pytest.raises(errors.ForbiddenError):
            engine.delete("intruder", uri, C, doc_hash)
    with pytest.raises(errors.PreconditionRequiredError):
        engine.delete("u", "test://none", C, None)
    with pytest.raises(errors.NotFoundError):
        engine.delete("u", "test://none", C, ABSENT_HASH)
    with pytest.raises(errors.PreconditionFailedError):
        engine.delete("u", "test://a", C, ABSENT_HASH)
    assert engine.head("u", "test://a", C) is not None


def test_delete_then_stale_commit_is_not_found() -> None:
    """复活防护（core §4）：基于旧 doc_hash 的 commit 在删除后只得到 DPE_NOT_FOUND。"""
    engine = make_engine()
    doc_hash = _put(engine, "test://a")
    engine.delete("u", "test://a", C, doc_hash)
    with pytest.raises(errors.NotFoundError):
        engine.commit("u", "test://a", inline(text_doc(["y"])).body(), C, BaseHash(doc_hash))


# ---------------------------------------------------------------------------
# move
# ---------------------------------------------------------------------------


def test_move_keeps_doc_hash_and_leaves_no_trace() -> None:
    engine = make_engine()
    doc_hash = _put(engine, "test://from")
    before = engine.get_skeleton("u", "test://from", C)
    result = _move(engine, from_uri="test://from", to_uri="test://to", base_hash=doc_hash)
    assert result == doc_hash
    assert engine.head("u", "test://from", C) is None
    assert [d.file_uri for d in engine.list_documents("u", C).documents] == ["test://to"]
    after = engine.get_skeleton("u", "test://to", C)
    assert before is not None and after is not None
    assert (after.document, after.pages) == (before.document, before.pages)
    # 移动后的文档照常可写，对象没有被回收
    engine.commit("u", "test://to", inline(text_doc(["x"])).body(), C, BaseHash(doc_hash))


def test_move_retry_after_success() -> None:
    engine = make_engine()
    doc_hash = _put(engine, "test://from")
    payload = {"from_uri": "test://from", "to_uri": "test://to", "base_hash": doc_hash}
    _move(engine, **payload)
    # 响应丢失后原样重试：目标状态已达成即成功
    assert _move(engine, **payload) == doc_hash


def test_move_branches() -> None:
    engine = make_engine()
    h1 = _put(engine, "test://src", "1")
    h2 = _put(engine, "test://dst", "2")
    with pytest.raises(errors.PreconditionFailedError):
        _move(engine, from_uri="test://src", to_uri="test://new", base_hash=ABSENT_HASH)
    with pytest.raises(errors.AlreadyExistsError):
        _move(engine, from_uri="test://src", to_uri="test://dst", base_hash=h1)
    # 源不存在：目标 doc_hash 等于 base_hash 才成功，否则 NOT_FOUND
    assert _move(engine, from_uri="test://gone", to_uri="test://dst", base_hash=h2) == h2
    for to_uri, base in (("test://dst", h1), ("test://none", h1)):
        with pytest.raises(errors.NotFoundError):
            _move(engine, from_uri="test://gone", to_uri=to_uri, base_hash=base)
    # 失败的 move 不改变任何状态
    assert engine.head("u", "test://src", C) is not None
    assert engine.head("u", "test://new", C) is None


def test_move_to_itself_is_already_exists() -> None:
    engine = make_engine()
    doc_hash = _put(engine, "test://a")
    with pytest.raises(errors.AlreadyExistsError):
        _move(engine, from_uri="test://a", to_uri="test://a", base_hash=doc_hash)


def test_move_authorizes_both_sides_regardless_of_state() -> None:
    auth = PrefixAuthorizer({"admin": ("test://",), "u": ("test://mine/",)})
    engine = make_engine(authorizer=auth)
    h = _put(engine, "test://mine/a", caller="admin")
    _put(engine, "test://theirs/b", caller="admin")
    cases = [
        ("test://mine/a", "test://theirs/new", h),  # 目标侧无授权
        ("test://theirs/b", "test://mine/new", h),  # 源侧无授权
        ("test://theirs/gone", "test://mine/a", h),  # 目标状态已达成，也不得泄露
        ("test://mine/a", "test://theirs/b", h),  # 目标已存在，也不得泄露
    ]
    for from_uri, to_uri, base in cases:
        with pytest.raises(errors.ForbiddenError):
            _move(engine, "u", from_uri=from_uri, to_uri=to_uri, base_hash=base)
    # 无前置条件同样先 403
    with pytest.raises(errors.ForbiddenError):
        _move(engine, "u", from_uri="test://theirs/b", to_uri="test://mine/x")


def test_move_order() -> None:
    engine = make_engine(authorizer=PrefixAuthorizer({}), max_payload_bytes=128)
    with pytest.raises(errors.PayloadTooLargeError):
        engine.move("u", b" " * 129, None)
    with pytest.raises(errors.ValidationError):
        engine.move("u", b"{", None)
    with pytest.raises(errors.ContractUnsupportedError):
        engine.move("u", b"[]", "dpe9")
    # 信封违例先于授权（#63）
    payloads: list[Any] = [
        [],
        {"to_uri": "test://b", "base_hash": ABSENT_HASH},
        {"from_uri": "test://a", "base_hash": ABSENT_HASH},
        {"from_uri": 1, "to_uri": "test://b"},
        {"from_uri": "test://a", "to_uri": "test://b", "base_hash": 1},
        {"from_uri": "test://a", "to_uri": "test://b", "force": True},
    ]
    for payload in payloads:
        with pytest.raises(errors.ValidationError):
            engine.move("u", json.dumps(payload).encode(), C)
    with pytest.raises(errors.ForbiddenError):
        _move(engine, from_uri="test://a", to_uri="test://b")


def test_move_requires_base_hash() -> None:
    engine = make_engine()
    _put(engine, "test://a")
    for payload in (
        {"from_uri": "test://a", "to_uri": "test://b"},
        {"from_uri": "test://a", "to_uri": "test://b", "base_hash": None},
    ):
        with pytest.raises(errors.PreconditionRequiredError):
            engine.move("u", json.dumps(payload).encode(), C)


def test_move_base_hash_compared_by_value() -> None:
    engine = make_engine(contracts=("dpe1", dpe_hash.DRILL_CONTRACT))
    doc = text_doc(["x"])
    engine.commit("u", "test://a", inline(doc).body(), C, IfAbsent())
    for garbage in ("x", "dpe9:" + "a" * 64, ABSENT_HASH):
        with pytest.raises(errors.PreconditionFailedError):
            _move(engine, from_uri="test://a", to_uri="test://b", base_hash=garbage)
    # 任一受支持契约下的值都匹配；响应按请求声明的契约给出
    dpe2 = inline(doc, "dpe2").doc_hash
    assert (
        _move(engine, from_uri="test://a", to_uri="test://b", base_hash=dpe2)
        == inline(doc).doc_hash
    )
