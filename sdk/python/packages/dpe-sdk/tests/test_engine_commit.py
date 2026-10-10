"""参考服务端引擎 commit（core §3.3，#63 修订后的求值顺序）。"""

from __future__ import annotations

import copy
import json
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import dpe_hash
import pytest
from dpe_sdk import errors
from dpe_sdk.testing import BaseHash, Engine, IfAbsent, Precondition, PrefixAuthorizer
from engine_helpers import Inline, inline, make_engine, negotiate_body, text_doc

URI = "test://docs/a"
C = dpe_hash.CONTRACT
#: 格式合法、但不属于任何已存内容的 hash
ABSENT_HASH = "dpe1:" + "0" * 64


def _vectors(vectors_dir: Path, kind: str) -> list[dict[str, Any]]:
    out = []
    for f in sorted(vectors_dir.glob("*.json")):
        if f.name == "manifest.json":
            continue
        vec = json.loads(f.read_text(encoding="utf-8"))
        if vec["kind"] == kind:
            out.append(vec)
    assert out
    return out


def _create(engine: Engine, doc: dict[str, Any], uri: str = URI, caller: str = "u") -> Inline:
    req = inline(doc)
    result = engine.commit(caller, uri, req.body(), C, IfAbsent())
    assert result.status == "created"
    return req


# ---------------------------------------------------------------------------
# 向量：doc_hash 与读回
# ---------------------------------------------------------------------------


def test_document_vectors_via_fast_path(vectors_dir: Path) -> None:
    """每篇向量文档经快路径提交：服务端回报的 doc_hash 等于向量值，读回内容等价。"""
    engine = make_engine(contracts=("dpe1", dpe_hash.DRILL_CONTRACT))
    n = 0
    for vec in _vectors(vectors_dir, "document"):
        for key, document in vec["documents"].items():
            uri = f"test://vectors/{vec['name']}/{key}"
            for contract, expected in vec["expected"][key].items():
                req = inline(document, contract)
                assert req.doc_hash == expected["doc_hash"]
                blobs = [e["blob"] for e in req.objects if e.get("blob") is not None]
                if blobs:
                    # blob 不能内联（core §6 DPE_MISSING_CONTENT 的恢复动作）：快路径如实报缺；
                    # 经暂存会话上传后提交由 test_engine_session 覆盖（#43）
                    with pytest.raises(errors.MissingContentError) as info:
                        engine.commit("u", uri, req.body(), contract, IfAbsent())
                    assert info.value.missing.blobs == list(dict.fromkeys(blobs))
                    continue
                result = engine.commit("u", uri, req.body(), contract, IfAbsent())
                assert result.doc_hash == expected["doc_hash"], uri
                head = engine.head("u", uri, contract)
                assert head is not None and head.doc_hash == expected["doc_hash"]
                skeleton = engine.get_skeleton("u", uri, contract)
                assert skeleton is not None
                assert skeleton.document.doc_hash(contract) == expected["doc_hash"]
                assert [p.page_hash(contract) for p in skeleton.pages] == [
                    p["page_hash"] for p in expected["pages"]
                ]
                # 两个契约下读到的是同一份内容（契约 1 §6 原位重算）
                for other, other_expected in vec["expected"][key].items():
                    other_head = engine.head("u", uri, other)
                    assert other_head is not None
                    assert other_head.doc_hash == other_expected["doc_hash"]
                engine.delete("u", uri, contract, result.doc_hash)
                n += 1
    assert n > 0


def test_invalid_object_vectors_in_every_position(vectors_dir: Path) -> None:
    """拒绝类向量放进 document / pages / objects，错误码与向量一致（core §2.8）。"""
    engine = make_engine()
    valid_doc = inline(text_doc(["x"]))
    member = {"document", "page", "element"}
    for vec in _vectors(vectors_dir, "invalid"):
        for case in vec["cases"]:
            # expanded_document 是展开视图的用例，线上没有这种对象
            if case["contract"] != C or "input" not in case or case["object_kind"] not in member:
                continue
            kind = case["object_kind"]
            if kind == "document":
                payload = {"document": case["input"]}
            elif kind == "page":
                payload = {"document": valid_doc.document, "pages": [case["input"]]}
            else:
                payload = {"document": valid_doc.document, "objects": [case["input"]]}
            body = json.dumps(payload).encode()
            with pytest.raises(errors.DpeError) as info:
                engine.commit("u", URI, body, C, IfAbsent())
            assert info.value.code == case["code"], case["name"]


def test_invalid_ijson_vectors(vectors_dir: Path) -> None:
    """以原始 JSON 文本给出的用例（I-JSON 与数值越界），按对象层级放进请求体对应位置。"""
    engine = make_engine()
    valid_doc = json.dumps(inline(text_doc(["x"])).document)
    member = {"element": "objects", "page": "pages"}
    for vec in _vectors(vectors_dir, "invalid"):
        for case in vec["cases"]:
            if "input_json" not in case:
                continue
            if case["object_kind"] == "document":
                body = ('{"document": ' + case["input_json"] + "}").encode()
            else:
                key = member[case["object_kind"]]
                text = f'{{"document": {valid_doc}, "{key}": [{case["input_json"]}]}}'
                body = text.encode()
            with pytest.raises(errors.DpeError) as info:
                engine.commit("u", URI, body, C, IfAbsent())
            assert info.value.code == case["code"], case["name"]


# ---------------------------------------------------------------------------
# 报文校验（第 0–1 步）
# ---------------------------------------------------------------------------


def test_payload_too_large_comes_first() -> None:
    engine = make_engine(max_payload_bytes=10, authorizer=PrefixAuthorizer({}))
    with pytest.raises(errors.PayloadTooLargeError):
        engine.commit("u", URI, b"{" * 11, None, None)
    # 恰好等于上限不触发 413
    with pytest.raises(errors.ValidationError):
        engine.commit("u", URI, b"[1,2,3,45]", C, None)


@pytest.mark.parametrize(
    ("body", "code"),
    [
        (b"{", "DPE_VALIDATION"),
        (b'{"document": {}, "document": {}}', "DPE_VALIDATION"),  # 重复键
        (b'{"document": {"file_type": "md", "title": "\\ud800", "pages": []}}', "DPE_VALIDATION"),
        (b"\xff", "DPE_VALIDATION"),
    ],
)
def test_ijson_before_contract(body: bytes, code: str) -> None:
    engine = make_engine()
    with pytest.raises(errors.DpeError) as info:
        engine.commit("u", URI, body, "dpe9", None)
    assert info.value.code == code


def test_contract_before_envelope() -> None:
    engine = make_engine()
    for contract in (None, "dpe9", "dpe2"):
        with pytest.raises(errors.ContractUnsupportedError):
            engine.commit("u", URI, b"[]", contract, None)


@pytest.mark.parametrize(
    "payload",
    [
        [],
        "x",
        {},
        {"document": None},
        {"document": {"file_type": "md", "pages": []}, "extra": 1},
        {"document": {"file_type": "md", "pages": []}, "pages": {}},
        {"document": {"file_type": "md", "pages": []}, "objects": "x"},
        {"document": {"file_type": "md", "pages": []}, "staging_session": 1},
        {"document": {"file_type": "md", "pages": []}, "force": 1},
        {"document": {"file_type": "md", "pages": []}, "force": "true"},
        {"document": {"file_type": "md", "pages": []}, "base_hash": ABSENT_HASH},
    ],
)
def test_envelope(payload: Any) -> None:
    engine = make_engine()
    with pytest.raises(errors.ValidationError):
        engine.commit("u", URI, json.dumps(payload).encode(), C, IfAbsent())


def test_envelope_location_is_rfc6901_and_utf16_ordered() -> None:
    engine = make_engine()
    doc = {"file_type": "md", "pages": []}
    # 码点序 U+FF5E < U+1F600，UTF-16 码元序相反（后者是代理对 0xD83D 0xDE00）
    payload = {"document": doc, "a/b~": 1, "\U0001f600": 2, "\uff5e": 3}
    with pytest.raises(errors.ValidationError) as info:
        engine.commit("u", URI, json.dumps(payload).encode(), C, IfAbsent())
    assert info.value.path == "/a~1b~0"
    del payload["a/b~"]
    with pytest.raises(errors.ValidationError) as info:
        engine.commit("u", URI, json.dumps(payload).encode(), C, IfAbsent())
    assert info.value.path == "/\U0001f600"


@pytest.mark.parametrize(
    ("member", "value", "path"),
    [
        ("force", "yes", "/force"),
        ("pages", {}, "/pages"),
        ("staging_session", 1, "/staging_session"),
    ],
)
def test_envelope_member_type_location(member: str, value: Any, path: str) -> None:
    engine = make_engine()
    payload = {"document": {"file_type": "md", "pages": []}, member: value}
    with pytest.raises(errors.ValidationError) as info:
        engine.commit("u", URI, json.dumps(payload).encode(), C, IfAbsent())
    assert info.value.path == path


def test_envelope_before_objects() -> None:
    """信封违例（VALIDATION）先于对象违例（CATEGORY_UNKNOWN）。"""
    engine = make_engine()
    payload = {
        "document": {"file_type": "md", "pages": []},
        "objects": [{"category": "Nope"}],
        "force": "yes",
    }
    with pytest.raises(errors.ValidationError):
        engine.commit("u", URI, json.dumps(payload).encode(), C, None)


def test_envelope_null_members_are_absent() -> None:
    engine = make_engine()
    payload = {
        "document": {"file_type": "md", "pages": []},
        "pages": None,
        "objects": None,
        "staging_session": None,
        "force": None,
    }
    result = engine.commit("u", URI, json.dumps(payload).encode(), C, IfAbsent())
    assert result.status == "created"


def test_objects_validated_document_then_pages_then_elements() -> None:
    engine = make_engine()
    bad_page = {"elements": [], "page_number": 1}
    bad_element = {"category": "Nope"}
    empty = {"file_type": "md", "pages": []}
    cases: list[tuple[dict[str, Any], str, str]] = [
        (
            {"document": {**empty, "x": 1}, "pages": [bad_page], "objects": [bad_element]},
            "DPE_VALIDATION",
            "/document/x",
        ),
        (
            {"document": empty, "pages": [bad_page], "objects": [bad_element]},
            "DPE_VALIDATION",
            "/pages/0/page_number",
        ),
        (
            {"document": empty, "objects": [{"category": "Title"}, bad_element]},
            "DPE_CATEGORY_UNKNOWN",
            "/objects/1/category",
        ),
    ]
    for payload, code, path in cases:
        with pytest.raises(errors.DpeError) as info:
            engine.commit("u", URI, json.dumps(payload).encode(), C, IfAbsent())
        assert (info.value.code, info.value.path) == (code, path)


def test_force_with_precondition_before_authorization() -> None:
    engine = make_engine(authorizer=PrefixAuthorizer({}))
    body = inline(text_doc(["x"])).body(force=True)
    for pre in (IfAbsent(), BaseHash(ABSENT_HASH)):
        with pytest.raises(errors.ValidationError):
            engine.commit("u", URI, body, C, pre)


# ---------------------------------------------------------------------------
# 授权（第 2 步）
# ---------------------------------------------------------------------------


def test_validation_precedes_authorization() -> None:
    """#63：无写授权的调用者提交不合法的报文，先得到校验错误。"""
    engine = make_engine(authorizer=PrefixAuthorizer({}))
    with pytest.raises(errors.ValidationError):
        engine.commit("u", URI, b'{"document": 1}', C, IfAbsent())
    with pytest.raises(errors.ContractUnsupportedError):
        engine.commit("u", URI, inline(text_doc(["x"])).body(), "dpe9", IfAbsent())


def test_forbidden_regardless_of_document_state() -> None:
    auth = PrefixAuthorizer({"owner": ("test://",)})
    engine = make_engine(authorizer=auth)
    req = _create(engine, text_doc(["x"]), caller="owner")
    other = inline(text_doc(["y"]))
    # 内容未变、前置条件不满足、文档不存在、无前置条件：无写授权一律 403
    cases: list[tuple[str, bytes, Precondition | None]] = [
        (URI, req.body(), BaseHash(req.doc_hash)),
        (URI, other.body(), BaseHash(ABSENT_HASH)),
        (URI, other.body(), IfAbsent()),
        ("test://docs/none", other.body(), BaseHash(ABSENT_HASH)),
        (URI, req.body(), None),
        (URI, req.body(staging_session="s"), BaseHash(req.doc_hash)),
    ]
    for uri, body, pre in cases:
        with pytest.raises(errors.ForbiddenError):
            engine.commit("intruder", uri, body, C, pre)


def test_force_permission() -> None:
    auth = PrefixAuthorizer({"w": ("test://",), "f": ("test://",)}, force=frozenset({"f"}))
    engine = make_engine(authorizer=auth)
    body = inline(text_doc(["x"])).body(force=True)
    with pytest.raises(errors.ForbiddenError):
        engine.commit("w", URI, body, C, None)
    assert engine.commit("f", URI, body, C, None).status == "created"
    # force: false 不需要 force 权限
    assert (
        engine.commit(
            "w", "test://b", inline(text_doc(["x"])).body(force=False), C, IfAbsent()
        ).status
        == "created"
    )


# ---------------------------------------------------------------------------
# 前置条件存在性、unchanged、前置条件求值（第 3–5 步）
# ---------------------------------------------------------------------------


def test_precondition_required_even_if_unchanged() -> None:
    engine = make_engine()
    req = _create(engine, text_doc(["x"]))
    with pytest.raises(errors.PreconditionRequiredError):
        engine.commit("u", URI, req.body(), C, None)


def test_unchanged_wins_over_preconditions_and_skips_inline_and_session() -> None:
    engine = make_engine()
    req = _create(engine, text_doc(["x", "y"]))
    extra = {"category": "Title", "text": "unused"}
    body = req.body(objects=[*req.objects, extra], staging_session="whatever")
    for pre in (IfAbsent(), BaseHash(ABSENT_HASH), BaseHash(req.doc_hash)):
        result = engine.commit("u", URI, body, C, pre)
        assert result.status == "unchanged"
        assert result.doc_hash == req.doc_hash
        assert result.delta.model_dump() == {"added": 0, "removed": 0, "retained": 2}
    assert engine.commit("u", URI, req.body(force=True), C, None).status == "unchanged"


def test_cas_matrix() -> None:
    """{不存在, 存在} × {if_absent, base_hash 匹配 / 不匹配 / 非法, force} × {同内容, 新内容}。"""
    engine = make_engine()
    base = text_doc(["base"])
    new = inline(text_doc(["new"]))
    created = inline(base)
    preconditions: dict[str, Callable[[], tuple[Precondition | None, bool]]] = {
        "if_absent": lambda: (IfAbsent(), False),
        "base_match": lambda: (BaseHash(created.doc_hash), False),
        "base_mismatch": lambda: (BaseHash(ABSENT_HASH), False),
        "base_garbage": lambda: (BaseHash("not-a-hash"), False),
        "base_unknown_contract": lambda: (BaseHash("dpe9:" + "a" * 64), False),
        "force": lambda: (None, True),
    }
    expected = {
        # (文档存在, 前置条件, 提交内容) → 结果
        (False, "if_absent", "new"): "created",
        (False, "base_match", "new"): "DPE_NOT_FOUND",
        (False, "base_mismatch", "new"): "DPE_NOT_FOUND",
        (False, "base_garbage", "new"): "DPE_NOT_FOUND",
        (False, "base_unknown_contract", "new"): "DPE_NOT_FOUND",
        (False, "force", "new"): "created",
        (True, "if_absent", "new"): "DPE_ALREADY_EXISTS",
        (True, "base_match", "new"): "updated",
        (True, "base_mismatch", "new"): "DPE_PRECONDITION_FAILED",
        (True, "base_garbage", "new"): "DPE_PRECONDITION_FAILED",
        (True, "base_unknown_contract", "new"): "DPE_PRECONDITION_FAILED",
        (True, "force", "new"): "updated",
        **{(True, p, "same"): "unchanged" for p in preconditions},
    }
    for (exists, name, content), outcome in expected.items():
        uri = f"test://cas/{exists}/{name}/{content}"
        if exists:
            engine.commit("u", uri, created.body(), C, IfAbsent())
        pre, force = preconditions[name]()
        req = new if content == "new" else created
        body = req.body(force=True) if force else req.body()
        status: str
        try:
            status = engine.commit("u", uri, body, C, pre).status
        except errors.DpeError as exc:
            status = exc.code
        assert status == outcome, (exists, name, content)
        # 失败的写入不改变状态
        head = engine.head("u", uri, C)
        if status.startswith("DPE_"):
            assert (head is None) == (not exists)
            if exists:
                assert head is not None and head.doc_hash == created.doc_hash


def test_base_hash_matches_any_supported_contract() -> None:
    engine = make_engine(contracts=("dpe1", dpe_hash.DRILL_CONTRACT))
    doc = text_doc(["x"])
    engine.commit("u", URI, inline(doc).body(), C, IfAbsent())
    dpe2_hash = inline(doc, dpe_hash.DRILL_CONTRACT).doc_hash
    result = engine.commit("u", URI, inline(text_doc(["y"])).body(), C, BaseHash(dpe2_hash))
    assert result.status == "updated"


# ---------------------------------------------------------------------------
# 会话与可得性（第 6 步）
# ---------------------------------------------------------------------------


def test_session_reference_expired_only_at_step_6() -> None:
    engine = make_engine()
    req = inline(text_doc(["x"]))
    with pytest.raises(errors.SessionExpiredError):
        engine.commit("u", URI, req.body(staging_session="st-1"), C, IfAbsent())
    # 前置条件失败先于会话
    _create(engine, text_doc(["y"]))
    with pytest.raises(errors.PreconditionFailedError):
        engine.commit("u", URI, req.body(staging_session="st-1"), C, BaseHash(ABSENT_HASH))


def test_precondition_failure_precedes_missing_content() -> None:
    engine = make_engine()
    _create(engine, text_doc(["x"]))
    lonely = inline(text_doc(["z"]))
    body = json.dumps({"document": lonely.document}).encode()
    with pytest.raises(errors.PreconditionFailedError):
        engine.commit("u", URI, body, C, BaseHash(ABSENT_HASH))
    with pytest.raises(errors.MissingContentError):
        engine.commit("u", URI, body, C, BaseHash(_head(engine)))


def _head(engine: Engine, uri: str = URI) -> str:
    head = engine.head("u", uri, C)
    assert head is not None
    return head.doc_hash


def test_missing_content_is_layered() -> None:
    engine = make_engine()
    req = inline(text_doc(["a", "b"], ["c"], ["a", "b"]))
    # 只给文档对象：缺全部页（重复的页 hash 只列一次）
    with pytest.raises(errors.MissingContentError) as info:
        engine.commit("u", URI, json.dumps({"document": req.document}).encode(), C, IfAbsent())
    assert info.value.missing.pages == [req.page_hashes[0], req.page_hashes[1]]
    assert info.value.missing.content_hashes == []
    assert info.value.truncated is False
    # 给了页、没给元素：缺页引用的元素（同一元素只列一次）
    body = json.dumps({"document": req.document, "pages": req.pages}).encode()
    with pytest.raises(errors.MissingContentError) as info:
        engine.commit("u", URI, body, C, IfAbsent())
    assert info.value.missing.pages == []
    assert info.value.missing.content_hashes == [*req.content_hashes[0], *req.content_hashes[1]]
    assert engine.head("u", URI, C) is None


def test_missing_blob() -> None:
    engine = make_engine()
    blob = "sha256:" + "b" * 64
    doc = {
        "file_type": "png",
        "pages": [{"elements": [{"category": "Image", "blob": blob, "mime_type": "image/png"}]}],
    }
    with pytest.raises(errors.MissingContentError) as info:
        engine.commit("u", URI, inline(doc).body(), C, IfAbsent())
    assert info.value.missing.blobs == [blob]


def test_missing_truncated() -> None:
    engine = make_engine(missing_max=2)
    req = inline(text_doc(["a"], ["b"], ["c"]))
    with pytest.raises(errors.MissingContentError) as info:
        engine.commit("u", URI, json.dumps({"document": req.document}).encode(), C, IfAbsent())
    assert info.value.missing.pages == req.page_hashes[:2]
    assert info.value.truncated is True


def test_fast_path_reuses_current_document_objects() -> None:
    """去重范围固定为本文档：本文档当前状态引用的对象不必重传。"""
    engine = make_engine()
    first = _create(engine, text_doc(["a", "b"], ["c"]))
    second = inline(text_doc(["a", "b"], ["c"], ["d"]))
    # 只内联新页与新元素
    body = json.dumps(
        {"document": second.document, "pages": [second.pages[2]], "objects": [second.objects[3]]}
    ).encode()
    result = engine.commit("u", URI, body, C, BaseHash(first.doc_hash))
    assert result.status == "updated"
    assert result.delta.model_dump() == {"added": 1, "removed": 0, "retained": 3}


def test_missing_list_ignores_objects_held_by_other_documents() -> None:
    """去重范围固定为本文档（core §3.3）：另一篇文档已存有同一对象时，缺失清单仍列出它。"""
    engine = make_engine()
    other = _create(engine, text_doc(["shared"]), uri="test://docs/other")
    body = json.dumps({"document": other.document}).encode()
    with pytest.raises(errors.MissingContentError) as info:
        engine.commit("u", URI, body, C, IfAbsent())
    assert info.value.missing.pages == other.page_hashes


def test_deleted_objects_are_not_available() -> None:
    engine = make_engine()
    req = _create(engine, text_doc(["x"]))
    engine.delete("u", URI, C, req.doc_hash)
    body = json.dumps({"document": req.document}).encode()
    with pytest.raises(errors.MissingContentError):
        engine.commit("u", URI, body, C, IfAbsent())


# ---------------------------------------------------------------------------
# delta 与读回
# ---------------------------------------------------------------------------


def test_delta_is_multiset_difference() -> None:
    engine = make_engine()
    first = _create(engine, text_doc(["a", "a", "b"]))
    second = inline(text_doc(["a", "c", "c"]))
    result = engine.commit("u", URI, second.body(), C, BaseHash(first.doc_hash))
    assert result.delta.model_dump() == {"added": 2, "removed": 2, "retained": 1}


def test_reorder_updates_with_zero_delta() -> None:
    engine = make_engine()
    first = _create(engine, text_doc(["他们离婚了", "A 与 C 再婚了"]))
    swapped = inline(text_doc(["A 与 C 再婚了", "他们离婚了"]))
    result = engine.commit("u", URI, swapped.body(), C, BaseHash(first.doc_hash))
    assert result.status == "updated"
    assert result.delta.model_dump() == {"added": 0, "removed": 0, "retained": 2}


def test_skeleton_reads_back_latest_representation() -> None:
    engine = make_engine()
    doc = text_doc(["x"], title="t")
    doc["pages"][0]["page_metadata"] = {"label": "i", "gone": None}
    req = _create(engine, doc)
    skeleton = engine.get_skeleton("u", URI, C)
    assert skeleton is not None
    assert skeleton.file_uri == URI
    assert skeleton.doc_hash == req.doc_hash
    assert skeleton.document.model_dump() == req.document
    assert [p.model_dump() for p in skeleton.pages] == req.pages


def test_open_file_type_is_accepted_and_read_back() -> None:
    """file_type 是开放取值（core §2.5）：未登记的合法取值照常写入、读回、进 doc_hash。"""
    engine = make_engine()
    req = inline(text_doc(["x"], file_type="custom_format_v2"))
    result = engine.commit("u", URI, req.body(), C, IfAbsent())
    assert result.status == "created"
    skeleton = engine.get_skeleton("u", URI, C)
    assert skeleton is not None
    assert skeleton.doc_hash == req.doc_hash
    assert skeleton.document.file_type == "custom_format_v2"


def test_invalid_file_type_syntax_is_rejected() -> None:
    """不合语法的 file_type → `DPE_VALIDATION`，位置在文档对象内；协商与提交两条入口一致。"""
    engine = make_engine()
    body = json.dumps({"document": {"file_type": "Markdown", "pages": []}}).encode()
    with pytest.raises(errors.ValidationError) as info:
        engine.commit("u", URI, body, C, IfAbsent())
    assert info.value.path == "/document/file_type"
    with pytest.raises(errors.ValidationError) as info:
        engine.negotiate("u", negotiate_body(URI, {"file_type": "Markdown", "pages": []}), C)
    assert info.value.path == "/document/file_type"


def test_duplicate_pages_read_back_in_order() -> None:
    engine = make_engine()
    req = _create(engine, text_doc(["a"], ["b"], ["a"]))
    skeleton = engine.get_skeleton("u", URI, C)
    assert skeleton is not None
    assert [p.page_hash() for p in skeleton.pages] == req.page_hashes
    assert req.page_hashes[0] == req.page_hashes[2]


def test_objects_are_released_with_their_documents() -> None:
    engine = make_engine()
    store = engine._store
    first = _create(engine, text_doc(["a"], ["b"]))
    _create(engine, text_doc(["a"]), uri="test://docs/b")
    second = inline(text_doc(["c"]))
    engine.commit("u", URI, second.body(), C, BaseHash(first.doc_hash))
    # 页 ["a"] 仍被 docs/b 引用，页 ["b"] 与元素 "b" 已回收
    assert first.page_hashes[0] in store.objects
    assert first.page_hashes[1] not in store.objects
    assert first.content_hashes[1][0] not in store.objects
    assert all(r.refs == 1 for r in store.objects.values())


# ---------------------------------------------------------------------------
# 原样表示归属文档、失败无副作用
# ---------------------------------------------------------------------------


def _snapshot(engine: Engine) -> tuple[Any, ...]:
    store = engine._store
    return (
        {h: (r.body, r.refs, dict(r.hashes)) for h, r in store.objects.items()},
        {c: dict(a) for c, a in store.alias.items()},
        dict(store.docs),
    )


def test_failed_commits_leave_store_untouched() -> None:
    engine = make_engine()
    first = _create(engine, text_doc(["a"]))
    before = copy.deepcopy(_snapshot(engine))  # 深拷贝：原地修改记录 body 也要能发现
    fresh = inline(text_doc(["b"], ["c"]))
    partial_body = json.dumps(
        {"document": fresh.document, "pages": fresh.pages[:1], "objects": fresh.objects[:1]}
    ).encode()
    failures: list[tuple[bytes, Precondition | None, type[errors.DpeError]]] = [
        (partial_body, BaseHash(first.doc_hash), errors.MissingContentError),
        (fresh.body(), BaseHash(ABSENT_HASH), errors.PreconditionFailedError),
        (fresh.body(staging_session="st"), BaseHash(first.doc_hash), errors.SessionExpiredError),
        (fresh.body(), IfAbsent(), errors.AlreadyExistsError),
    ]
    for body, pre, error in failures:
        with pytest.raises(error):
            engine.commit("u", URI, body, C, pre)
        assert _snapshot(engine) == before


def test_equivalent_write_does_not_change_other_documents_readback() -> None:
    """同 hash 的等价表示（显式 null）写进另一篇文档，不改变本文档的读回（core §3 单文档原子）。"""
    engine = make_engine()
    b = _create(engine, text_doc(["x"]), uri="test://b")
    variant = inline(text_doc(["x"]))
    variant.pages[0]["title"] = None
    assert inline(text_doc(["x"])).page_hashes == variant.page_hashes
    engine.commit("u", "test://a", variant.body(), C, IfAbsent())
    skeleton_b = engine.get_skeleton("u", "test://b", C)
    skeleton_a = engine.get_skeleton("u", "test://a", C)
    assert skeleton_b is not None and skeleton_a is not None
    assert [p.model_dump() for p in skeleton_b.pages] == b.pages
    assert [p.model_dump() for p in skeleton_a.pages] == variant.pages


def test_update_reads_back_latest_representation_of_retained_page() -> None:
    engine = make_engine()
    first = _create(engine, text_doc(["x"], ["y"]))
    second = inline(text_doc(["x"], ["z"]))
    second.pages[0]["page_metadata"] = {}  # 与原页等价的另一种表示
    assert second.page_hashes[0] == first.page_hashes[0]
    engine.commit("u", URI, second.body(), C, BaseHash(first.doc_hash))
    skeleton = engine.get_skeleton("u", URI, C)
    assert skeleton is not None
    assert [p.model_dump() for p in skeleton.pages] == second.pages
    # 未内联的保留页沿用本文档已有的表示
    third = inline(text_doc(["x"], ["w"]))
    body = json.dumps(
        {"document": third.document, "pages": [third.pages[1]], "objects": [third.objects[1]]}
    ).encode()
    engine.commit("u", URI, body, C, BaseHash(second.doc_hash))
    skeleton = engine.get_skeleton("u", URI, C)
    assert skeleton is not None
    assert skeleton.pages[0].model_dump() == second.pages[0]


def test_inline_page_with_elements_held_by_another_document_is_missing() -> None:
    """元素层：元素只存在于另一篇文档时，本文档的提交仍把它列为缺失（去重范围固定为本文档）。"""
    engine = make_engine()
    _create(engine, text_doc(["shared"]), uri="test://docs/other")
    # 新页内联且只补传新元素：已存于另一篇文档的 "shared" 不在本文档范围，列为缺失
    req = inline(text_doc(["shared", "new"]))
    body = json.dumps(
        {"document": req.document, "pages": req.pages, "objects": [req.objects[1]]}
    ).encode()
    with pytest.raises(errors.MissingContentError) as info:
        engine.commit("u", URI, body, C, IfAbsent())
    assert info.value.missing.content_hashes == [req.content_hashes[0][0]]


# ---------------------------------------------------------------------------
# 授权器在锁外调用
# ---------------------------------------------------------------------------


@dataclass
class _ReentrantAuthorizer:
    """回调引擎的授权器（如按文档状态做 ACL）：在锁内调用会永久挂起。"""

    engine: Engine | None = None
    calls: list[str] = field(default_factory=list)

    def can_write(self, caller: str, uri: str) -> bool:
        self.calls.append(uri)
        assert self.engine is not None
        self.engine.head(caller, uri, C)
        return True

    def can_force(self, caller: str, uri: str) -> bool:
        return True


def test_authorizer_may_call_back_into_engine() -> None:
    """授权在锁外调用（core §5 总则）：授权器回调引擎时不得死锁，只为目标文档授权。"""
    auth = _ReentrantAuthorizer()
    engine = make_engine(authorizer=auth)
    auth.engine = engine
    done: list[str] = []
    worker = threading.Thread(
        target=lambda: done.append(
            engine.commit("u", URI, inline(text_doc(["fresh"])).body(), C, IfAbsent()).status
        )
    )
    worker.start()
    worker.join(timeout=5)
    assert not worker.is_alive(), "授权器回调引擎时死锁"
    assert done == ["created"]
    assert auth.calls == [URI]


@dataclass
class _WindowAuthorizer:
    """首次判定任一 URI 时执行一次钩子（模拟授权窗口内他人写入目标）。"""

    hook: Callable[[], object] | None = None

    def can_write(self, caller: str, uri: str) -> bool:
        hook, self.hook = self.hook, None
        if hook is not None:
            hook()
        return True

    def can_force(self, caller: str, uri: str) -> bool:
        return True


def test_cas_is_decided_after_the_authorization_window() -> None:
    """授权在锁外、CAS 在锁内裁决：窗口内他人写入目标，外层 commit 按最新状态返回 AlreadyExists。"""
    auth = _WindowAuthorizer()
    engine = make_engine(authorizer=auth)
    auth.hook = lambda: engine.commit("u", URI, inline(text_doc(["rival"])).body(), C, IfAbsent())
    with pytest.raises(errors.AlreadyExistsError):
        engine.commit("u", URI, inline(text_doc(["mine"])).body(), C, IfAbsent())


def test_target_reaching_same_content_in_window_is_unchanged() -> None:
    """窗口内他人写入与本次提交相同的内容：外层 commit 得到 unchanged（core §5.2）。"""
    auth = _WindowAuthorizer()
    engine = make_engine(authorizer=auth)
    same = inline(text_doc(["mine"]))
    auth.hook = lambda: engine.commit("u", URI, same.body(), C, IfAbsent())
    assert engine.commit("u", URI, same.body(), C, IfAbsent()).status == "unchanged"


def test_missing_content_is_decided_after_the_authorization_window() -> None:
    """授权在锁外、可得性在锁内按最新状态裁决：窗口内目标被改写后，本次提交依赖的页不再可得。"""
    auth = _WindowAuthorizer()
    engine = make_engine(authorizer=auth)
    first = _create(engine, text_doc(["keep"], ["drop"]))
    wanted = inline(text_doc(["keep"], ["new"]))
    assert wanted.page_hashes[0] == first.page_hashes[0]  # "keep" 页只在目标文档的当前状态里
    rewrite = inline(text_doc(["rewritten"]))
    auth.hook = lambda: engine.commit("u", URI, rewrite.body(), C, BaseHash(first.doc_hash))
    # force 跳过前置条件：可得性须按锁内最新状态判定，"keep" 页已随改写不可得
    body = json.dumps(
        {
            "document": wanted.document,
            "pages": [wanted.pages[1]],
            "objects": [wanted.objects[1]],
            "force": True,
        }
    ).encode()
    with pytest.raises(errors.MissingContentError) as info:
        engine.commit("u", URI, body, C, None)
    assert info.value.missing.pages == [wanted.page_hashes[0]]


def test_moved_document_keeps_serving_its_objects() -> None:
    """move 后目标 URI 的当前状态照常作为去重范围。"""
    engine = make_engine()
    req = _create(engine, text_doc(["x"], ["y"]), uri="test://from")
    engine.move(
        "u",
        json.dumps({"from_uri": "test://from", "to_uri": URI, "base_hash": req.doc_hash}).encode(),
        C,
    )
    reordered = inline(text_doc(["y"], ["x"]))
    body = json.dumps({"document": reordered.document}).encode()
    assert engine.commit("u", URI, body, C, BaseHash(req.doc_hash)).status == "updated"
    # 源 URI 不再提供任何对象
    with pytest.raises(errors.MissingContentError):
        engine.commit("u", "test://from", body, C, IfAbsent())
