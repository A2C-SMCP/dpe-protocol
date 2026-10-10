"""服务端自身写入 ``Engine.server_write``（core §2.4 同级写入）与强制过期钩子（#45）。

同名写入与 ``commit`` 共用同一条求值顺序：报文校验 → （commit 才做授权）→ 前置条件存在性 →
CAS → 会话与可得性 → 原子切换。差别只有两处：不询问对外授权器；去重范围上界取全部持有文档。
"""

from __future__ import annotations

import json
from typing import Any

import dpe_hash
import pytest
from dpe_sdk import errors
from dpe_sdk.testing import BaseHash, DedupScope, Engine, IfAbsent, PrefixAuthorizer
from engine_helpers import commit_body, id_factory, inline, make_engine, negotiate_body, text_doc

C = dpe_hash.CONTRACT
URI = "test://docs/server"
ABSENT = "dpe1:" + "0" * 64


def _write(
    engine: Engine,
    uri: str = URI,
    text: str = "x",
    *,
    contract: str | None = None,
    precondition: Any = None,
) -> Any:
    return engine.server_write(uri, inline(text_doc([text])).body(), contract, precondition)


def _put(engine: Engine, uri: str, text: str = "x", caller: str = "u") -> str:
    return engine.commit(caller, uri, inline(text_doc([text])).body(), C, IfAbsent()).doc_hash


# ---------------------------------------------------------------------------
# 授权与求值顺序
# ---------------------------------------------------------------------------


def test_server_write_is_not_subject_to_the_authorizer() -> None:
    """服务端自身写入不问对外授权器；同一请求由普通调用者提交则 403（授权先于 CAS）。"""
    engine = make_engine(authorizer=PrefixAuthorizer({}))
    result = _write(engine, precondition=IfAbsent())
    assert result.status == "created"
    with pytest.raises(errors.ForbiddenError):
        engine.commit("u", URI, inline(text_doc(["y"])).body(), C, IfAbsent())


def test_server_write_requires_a_precondition_and_allows_explicit_force() -> None:
    engine = make_engine()
    with pytest.raises(errors.PreconditionRequiredError):
        _write(engine)
    forced = engine.server_write(URI, inline(text_doc(["forced"])).body(force=True))
    assert forced.status == "created"


def test_server_write_cas_matrix() -> None:
    engine = make_engine()
    first = _write(engine, text="first", precondition=IfAbsent())
    with pytest.raises(errors.AlreadyExistsError):
        _write(engine, text="second", precondition=IfAbsent())
    with pytest.raises(errors.PreconditionFailedError):
        _write(engine, text="second", precondition=BaseHash(ABSENT))
    second = _write(engine, text="second", precondition=BaseHash(first.doc_hash))
    assert second.status == "updated" and second.doc_hash != first.doc_hash
    # 同样内容重复写入：unchanged，不论前置条件是否仍然成立（core §5.2）
    again = _write(engine, text="second", precondition=BaseHash(first.doc_hash))
    assert again.status == "unchanged" and again.doc_hash == second.doc_hash


def test_server_write_body_validation_precedes_cas() -> None:
    engine = make_engine()
    _write(engine, precondition=IfAbsent())
    bad = json.dumps({"document": {"file_type": "md", "pages": []}, "nope": 1}).encode()
    with pytest.raises(errors.ValidationError):
        engine.server_write(URI, bad, None, BaseHash(ABSENT))  # 报文校验先于前置条件求值
    inside = json.dumps(
        {"document": {"file_type": "md", "pages": []}, "staging_session": "st-1"}
    ).encode()
    with pytest.raises(errors.ValidationError) as info:
        engine.server_write(URI, inside, None, IfAbsent())
    assert "staging_session" in str(info.value)


def test_server_write_sees_every_document_in_the_dedup_scope() -> None:
    """去重范围上界 = 全部持有文档（core §3.3）：他人排他持有的内容，服务端自身写入可得。"""
    auth = PrefixAuthorizer({"u": ("test://mine/",), "other": ("test://other/",)})
    engine = make_engine(authorizer=auth, dedup_scope=DedupScope.WRITABLE)
    shared = inline(text_doc(["shared"]))
    _put(engine, "test://other/a", "shared", caller="other")
    # 只交文档对象（页与元素在他人文档的闭包里）；普通调用者按去重范围上界判缺失
    body = commit_body(shared.document)
    with pytest.raises(errors.MissingContentError):
        engine.commit("u", "test://mine/b", body, C, IfAbsent())
    result = engine.server_write("test://mine/b", body, C, IfAbsent())
    assert result.status == "created"


def test_same_level_write_then_source_overwrites_with_new_base_hash() -> None:
    """一致性测试的「同级写入」场景：服务端写入使旧 base_hash 失效，来源按新值覆盖。"""
    engine = make_engine()
    h1 = _put(engine, URI, "first")
    server = _write(engine, text="server", precondition=BaseHash(h1))
    assert server.status == "updated"
    assert engine.head("u", URI, C) is not None
    with pytest.raises(errors.PreconditionFailedError):
        engine.commit("u", URI, inline(text_doc(["late"])).body(), C, BaseHash(h1))
    overwritten = engine.commit(
        "u", URI, inline(text_doc(["late"])).body(), C, BaseHash(server.doc_hash)
    )
    assert overwritten.status == "updated"


# ---------------------------------------------------------------------------
# 运行时契约口径（core §3.1：受支持 = 服务端声明的集合）
# ---------------------------------------------------------------------------


def test_declared_contracts_classify_drill_child_as_mixed() -> None:
    """声明 dpe1 + dpe2 的服务端：dpe1 页引用 dpe2 子 hash 是契约混用，不是「不受支持」。"""
    engine = make_engine(contracts=("dpe1", dpe_hash.DRILL_CONTRACT))
    child = f"{dpe_hash.DRILL_CONTRACT}:{'a' * 64}"
    body = json.dumps(
        {
            "document": {"file_type": "md", "pages": [f"{C}:{'0' * 64}"]},
            "pages": [{"elements": [child]}],
        }
    ).encode()
    with pytest.raises(errors.ValidationError):
        engine.commit("u", URI, body, C, IfAbsent())
    # 单契约服务端同一输入是「不受支持的契约」（与向量同口径）
    single = make_engine()
    with pytest.raises(errors.ContractUnsupportedError):
        single.commit("u", URI, body, C, IfAbsent())


def test_server_write_defaults_to_primary_contract() -> None:
    engine = make_engine(contracts=("dpe1", dpe_hash.DRILL_CONTRACT))
    primary = _write(engine, precondition=IfAbsent())
    assert primary.doc_hash.startswith(f"{C}:")
    drill = engine.server_write(
        "test://docs/drill",
        inline(text_doc(["p"]), dpe_hash.DRILL_CONTRACT).body(),
        dpe_hash.DRILL_CONTRACT,
        IfAbsent(),
    )
    assert drill.doc_hash.startswith(f"{dpe_hash.DRILL_CONTRACT}:")
    with pytest.raises(errors.ContractUnsupportedError):
        _write(engine, "test://docs/x", contract="dpe9", precondition=IfAbsent())


# ---------------------------------------------------------------------------
# 强制过期钩子
# ---------------------------------------------------------------------------


def _element(text: str = "x") -> tuple[dict[str, Any], str]:
    body = {"category": "NarrativeText", "text": text}
    return body, dpe_hash.object_hash(body, "element", C)


def test_expire_session_ends_only_that_session() -> None:
    engine = make_engine(session_id_factory=id_factory())
    first = engine.negotiate("u", negotiate_body(URI, {"file_type": "md", "pages": []}), C)
    second = engine.negotiate(
        "u", negotiate_body("test://docs/other", {"file_type": "md", "pages": []}), C
    )
    element, eh = _element()
    assert engine.expire_session(first.staging_session.id) is True
    assert engine.expire_session(first.staging_session.id) is False  # 已过期
    assert engine.expire_session("st-forged") is False
    with pytest.raises(errors.SessionExpiredError):
        engine.upload_element("u", first.staging_session.id, eh, json.dumps(element).encode(), C)
    with pytest.raises(errors.SessionExpiredError):
        engine.commit(
            "u",
            URI,
            commit_body({"file_type": "md", "pages": []}, staging_session=first.staging_session.id),
            C,
            IfAbsent(),
        )
    # 另一会话照常可用
    engine.upload_element("u", second.staging_session.id, eh, json.dumps(element).encode(), C)
