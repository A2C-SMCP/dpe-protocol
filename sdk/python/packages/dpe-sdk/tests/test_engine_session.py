"""参考服务端引擎的会话生命周期与 commit 集成（#43）：绑定、过期、消费与原子切换。"""

from __future__ import annotations

import json
from typing import Any

import dpe_hash
import pytest
from dpe_sdk import errors
from dpe_sdk.testing import BaseHash, DedupScope, Engine, IfAbsent
from engine_helpers import (
    FakeClock,
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


def _page(elements: list[str], **fields: Any) -> tuple[dict[str, Any], str]:
    body: dict[str, Any] = {**fields, "elements": elements}
    return body, dpe_hash.object_hash(body, "page", C)


def _text_doc(*texts: str) -> dict[str, Any]:
    elements = [{"category": "NarrativeText", "text": t} for t in texts]
    return {"file_type": "md", "pages": [{"elements": elements}]}


def _open(engine: Engine, uri: str = URI, caller: str = "u") -> str:
    result = engine.negotiate(caller, negotiate_body(uri, {"file_type": "md", "pages": []}), C)
    return result.staging_session.id


def _stage_all(engine: Engine, sid: str, req: Any) -> None:
    """把快路径请求的全部页对象与元素对象上传到会话。"""
    for page in req.pages:
        ph = dpe_hash.object_hash(page, "page", C)
        engine.upload_page("u", sid, ph, json.dumps(page).encode(), C)
    for element in req.objects:
        eh = dpe_hash.object_hash(element, "element", C)
        engine.upload_element("u", sid, eh, json.dumps(element).encode(), C)


# ---------------------------------------------------------------------------
# 绑定与隔离（core §3.4）
# ---------------------------------------------------------------------------


def test_session_binding_isolation() -> None:
    engine = make_engine(session_id_factory=id_factory())
    sid = _open(engine)
    element, eh = _element()
    raw = json.dumps(element).encode()
    with pytest.raises(errors.SessionExpiredError):  # 他人会话
        engine.upload_element("v", sid, eh, raw, C)
    with pytest.raises(errors.SessionExpiredError):
        engine.upload_offset("v", sid, eh)
    with pytest.raises(errors.SessionExpiredError):  # 伪造 id
        engine.upload_element("u", "st-none", eh, raw, C)
    # 属于同一调用者、但属于另一 file_uri 的会话：commit 时 410
    with pytest.raises(errors.SessionExpiredError):
        engine.commit(
            "u",
            OTHER_URI,
            commit_body({"file_type": "md", "pages": []}, staging_session=sid),
            C,
            IfAbsent(),
        )


class _FlipAuthorizer:
    """可在测试中途翻转的授权器。"""

    def __init__(self) -> None:
        self.allow = True

    def can_write(self, caller: str, uri: str) -> bool:
        return self.allow

    def can_force(self, caller: str, uri: str) -> bool:
        return False


def test_upload_not_reauthorized_but_commit_is() -> None:
    """negotiate 后授权被撤销：upload 不重复判定（会话可用即放行），commit 才 403（§5 总则）。"""
    authorizer = _FlipAuthorizer()
    engine = make_engine(authorizer=authorizer, session_id_factory=id_factory())
    sid = _open(engine)
    authorizer.allow = False
    element, eh = _element()
    upload = engine.upload_element("u", sid, eh, json.dumps(element).encode(), C)
    assert upload.outcome == "created"
    body = commit_body({"file_type": "md", "pages": []}, staging_session=sid)
    with pytest.raises(errors.ForbiddenError):
        engine.commit("u", URI, body, C, IfAbsent())


def test_mixed_contract_staging_is_conservative() -> None:
    """同一会话混用契约：未按提交契约上传的暂存对象视为缺失（方向安全，模块文档化的行为）。"""
    drill = dpe_hash.DRILL_CONTRACT
    engine = make_engine(contracts=(C, drill), session_id_factory=id_factory())
    element, _ = _element()
    eh1 = dpe_hash.object_hash(element, "element", C)
    page_body = {"elements": [eh1]}
    ph1 = dpe_hash.object_hash(page_body, "page", C)
    eh2 = dpe_hash.object_hash(element, "element", drill)
    ph2 = dpe_hash.object_hash({"elements": [eh2]}, "page", drill)
    sid = _open(engine)
    engine.upload_page("u", sid, ph1, json.dumps(page_body).encode(), C)
    engine.upload_element("u", sid, eh1, json.dumps(element).encode(), C)
    doc2 = {"file_type": "md", "pages": [ph2]}
    with pytest.raises(errors.MissingContentError) as info:
        engine.commit("u", URI, commit_body(doc2, staging_session=sid), drill, IfAbsent())
    assert info.value.missing.pages == [ph2]  # dpe2 下直查会话未命中 → 视为缺失


def test_session_expiry_and_renewal() -> None:
    clock = FakeClock()
    engine = make_engine(clock=clock, staging_ttl_seconds=3600, session_id_factory=id_factory())
    sid = _open(engine)
    element, eh = _element()
    raw = json.dumps(element).encode()
    clock.advance(3600)  # now >= expires_at → 过期
    with pytest.raises(errors.SessionExpiredError):
        engine.upload_element("u", sid, eh, raw, C)
    sid2 = _open(engine)
    clock.advance(3500)
    assert engine.upload_element("u", sid2, eh, raw, C).outcome == "created"  # 成功操作续期
    clock.advance(3599)  # 距上次成功约 3599 秒，仍在 TTL 内
    assert engine.upload_element("u", sid2, eh, raw, C).outcome == "duplicate"
    clock.advance(3601)
    with pytest.raises(errors.SessionExpiredError):
        engine.upload_element("u", sid2, eh, raw, C)


def test_expired_session_restart_from_empty() -> None:
    """会话过期后重开 negotiate，新会话从空开始：过期会话中的内容不再可得。"""
    clock = FakeClock()
    engine = make_engine(clock=clock, staging_ttl_seconds=3600, session_id_factory=id_factory())
    sid = _open(engine)
    _, xh = _element()
    page, ph = _page([xh])
    engine.upload_page("u", sid, ph, json.dumps(page).encode(), C)
    clock.advance(3601)
    r = engine.negotiate("u", negotiate_body(URI, {"file_type": "md", "pages": [ph]}), C)
    assert r.missing_pages == [ph]  # 新会话从空开始：过期会话中的内容不再可得
    with pytest.raises(errors.MissingContentError) as info:
        engine.commit(
            "u",
            URI,
            commit_body({"file_type": "md", "pages": [ph]}, staging_session=r.staging_session.id),
            C,
            IfAbsent(),
        )
    assert info.value.missing.pages == [ph]


# ---------------------------------------------------------------------------
# 消费语义（core §3.4）
# ---------------------------------------------------------------------------


def test_commit_consumes_session_and_unchanged_replay() -> None:
    engine = make_engine(session_id_factory=id_factory())
    sid = _open(engine)
    document = {"file_type": "md", "pages": []}
    body = commit_body(document, staging_session=sid)
    result = engine.commit("u", URI, body, C, IfAbsent())
    assert result.status == "created"
    with pytest.raises(errors.SessionExpiredError):  # 已消费
        engine.upload_offset("u", sid, "sha256:" + "0" * 64)
    # 原样重试（同一 staging_session）：unchanged 在会话检查之前，200 而不是 410
    replay = engine.commit("u", URI, body, C, IfAbsent())
    assert replay.status == "unchanged"


def test_unchanged_does_not_consume_session() -> None:
    engine = make_engine(session_id_factory=id_factory())
    v1 = inline(_text_doc("same"))
    engine.commit("u", URI, v1.body(), C, IfAbsent())
    sid = _open(engine)
    result = engine.commit("u", URI, commit_body(v1.document, staging_session=sid), C, IfAbsent())
    assert result.status == "unchanged"
    # 会话未被消费：仍可上传
    element, eh = _element()
    assert engine.upload_element("u", sid, eh, json.dumps(element).encode(), C).outcome == "created"


def test_failed_commit_keeps_session_then_412_reuse() -> None:
    """验收：412 后以新前置条件引用同一会话重新 commit 成功，无需重传。"""
    engine = make_engine(session_id_factory=id_factory())
    v1 = inline(_text_doc("one"))
    first = engine.commit("u", URI, v1.body(), C, IfAbsent())
    v2 = inline(_text_doc("two"))
    r = engine.negotiate("u", negotiate_body(URI, v2.document), C)
    sid = r.staging_session.id
    _stage_all(engine, sid, v2)
    # 他人写入
    v1b = inline(_text_doc("middle"))
    other = engine.commit("u", URI, v1b.body(), C, BaseHash(first.doc_hash))
    # 旧前置条件 → 412，会话保持
    stale = commit_body(v2.document, staging_session=sid)
    with pytest.raises(errors.PreconditionFailedError):
        engine.commit("u", URI, stale, C, BaseHash(first.doc_hash))
    # 新前置条件引用同一会话 → 成功（内容已在会话中，无需重传）
    result = engine.commit(
        "u", URI, commit_body(v2.document, staging_session=sid), C, BaseHash(other.doc_hash)
    )
    assert result.status == "updated"
    assert result.doc_hash == v2.doc_hash
    skeleton = engine.get_skeleton("u", URI, C)
    assert skeleton is not None and [p.page_hash(C) for p in skeleton.pages] == v2.page_hashes


def test_missing_content_layers_then_upload_same_session() -> None:
    """DPE_MISSING_CONTENT 逐层清单：补传到同一会话后重新 commit 成功（core §3.3 第 6 步）。"""
    engine = make_engine(session_id_factory=id_factory())
    sid = _open(engine)
    blob = b"\x00pic"
    ref = dpe_hash.blob_ref(blob)
    image = {"category": "Image", "blob": ref, "mime_type": "image/png"}
    ih = dpe_hash.object_hash(image, "element", C)
    page1, ph1 = _page([ih])
    page2, ph2 = _page([ih], title="P2")
    document = {"file_type": "md", "pages": [ph1, ph2]}
    engine.upload_page("u", sid, ph1, json.dumps(page1).encode(), C)
    engine.upload_element("u", sid, ih, json.dumps(image).encode(), C)
    with pytest.raises(errors.MissingContentError) as info:
        engine.commit("u", URI, commit_body(document, staging_session=sid), C, IfAbsent())
    assert info.value.missing.pages == [ph2]  # 页层
    assert info.value.missing.blobs == [ref]  # blob 层
    engine.upload_page("u", sid, ph2, json.dumps(page2).encode(), C)
    engine.upload_blob("u", sid, ref, blob, C)
    result = engine.commit("u", URI, commit_body(document, staging_session=sid), C, IfAbsent())
    assert result.status == "created"


# ---------------------------------------------------------------------------
# 原子切换与验收（#43）
# ---------------------------------------------------------------------------


def test_staging_invisible_until_commit() -> None:
    """验收：分批期间读接口看不到中间态，commit 后可见。"""
    engine = make_engine(session_id_factory=id_factory())
    v1 = inline(_text_doc("old"))
    first = engine.commit("u", URI, v1.body(), C, IfAbsent())
    v2 = inline(_text_doc("new"))
    sid = _open(engine)
    _stage_all(engine, sid, v2)
    # 中间态：读接口仍看到 v1
    head = engine.head("u", URI, C)
    assert head is not None and head.doc_hash == first.doc_hash
    skeleton = engine.get_skeleton("u", URI, C)
    assert skeleton is not None
    assert [p.page_hash(C) for p in skeleton.pages] == v1.page_hashes
    listed = engine.list_documents("u", C, prefix="test://")
    assert [entry.doc_hash for entry in listed.documents] == [first.doc_hash]
    # commit 后可见
    result = engine.commit(
        "u", URI, commit_body(v2.document, staging_session=sid), C, BaseHash(first.doc_hash)
    )
    assert result.status == "updated"
    assert result.delta.added == 1 and result.delta.removed == 1 and result.delta.retained == 0
    after = engine.head("u", URI, C)
    assert after is not None and after.doc_hash == v2.doc_hash


def test_staged_commit_promotes_blob_and_dedups_across_documents() -> None:
    """会话中的 blob 随提交进入对象存储并按引用计数回收；可写范围内跨文档去重。"""
    engine = make_engine(dedup_scope=DedupScope.WRITABLE, session_id_factory=id_factory())
    blob = b"\x89PNG-dedup"
    ref = dpe_hash.blob_ref(blob)
    image = {"category": "Image", "blob": ref, "mime_type": "image/png"}
    ih = dpe_hash.object_hash(image, "element", C)
    page, ph = _page([ih])
    sid = _open(engine)
    engine.upload_page("u", sid, ph, json.dumps(page).encode(), C)
    engine.upload_element("u", sid, ih, json.dumps(image).encode(), C)
    engine.upload_blob("u", sid, ref, blob, C)
    doc = {"file_type": "md", "pages": [ph]}
    first = engine.commit("u", URI, commit_body(doc, staging_session=sid), C, IfAbsent())
    # 第二篇文档走快路径复用同一元素与 blob（在可写范围内，无需再传）
    v2 = inline({"file_type": "md", "pages": [{"elements": [image]}]})
    second = engine.commit("u", OTHER_URI, v2.body(), C, IfAbsent())
    assert engine._store.blobs[ref].refs == 2
    engine.delete("u", URI, C, first.doc_hash)
    assert engine._store.blobs[ref].refs == 1
    engine.delete("u", OTHER_URI, C, second.doc_hash)
    assert ref not in engine._store.blobs


def test_session_page_representation_wins_over_scope() -> None:
    """页既在范围内又在会话时，本次上传的原样表示生效（§2.7 SHOULD；内联 > 会话 > 旧表示）。"""
    engine = make_engine(session_id_factory=id_factory())
    element, eh = _element("repr")
    ph = dpe_hash.object_hash({"elements": [eh]}, "page", C)
    v1 = inline({"file_type": "md", "pages": [{"elements": [element]}]})
    first = engine.commit("u", URI, v1.body(), C, IfAbsent())
    page_new = {"title": None, "elements": [eh]}  # 与缺省 title 等价（core §2.7），同 hash
    assert dpe_hash.object_hash(page_new, "page", C) == ph
    sid = _open(engine)
    engine.upload_page("u", sid, ph, json.dumps(page_new).encode(), C)
    doc2 = {"file_type": "md", "title": "V2", "pages": [ph]}
    engine.commit("u", URI, commit_body(doc2, staging_session=sid), C, BaseHash(first.doc_hash))
    skeleton = engine.get_skeleton("u", URI, C)
    assert skeleton is not None
    assert "title" in skeleton.pages[0].model_fields_set  # 读回的是会话中的表示（显式 null）
    assert skeleton.pages[0].title is None


def test_commit_session_with_writable_scope_second_pass() -> None:
    """会话页的元素在会话、其 blob 仅由另一可写文档持有：二阶段授权后 commit 成功。"""
    engine = make_engine(dedup_scope=DedupScope.WRITABLE, session_id_factory=id_factory())
    blob = b"\x00two-phase"
    ref = dpe_hash.blob_ref(blob)
    image = {"category": "Image", "blob": ref, "mime_type": "image/png"}
    ih = dpe_hash.object_hash(image, "element", C)
    page, ph = _page([ih])
    doc = {"file_type": "md", "pages": [ph]}
    # 持有文档（blob 经它自己的会话进入存储）
    holder_sid = _open(engine, uri=OTHER_URI)
    engine.upload_page("u", holder_sid, ph, json.dumps(page).encode(), C)
    engine.upload_element("u", holder_sid, ih, json.dumps(image).encode(), C)
    engine.upload_blob("u", holder_sid, ref, blob, C)
    engine.commit("u", OTHER_URI, commit_body(doc, staging_session=holder_sid), C, IfAbsent())
    # 目标文档：页与元素在会话，blob 仅由持有文档持有（可写）→ 第一遍缺 blob，二阶段后成功
    sid = _open(engine)
    engine.upload_page("u", sid, ph, json.dumps(page).encode(), C)
    engine.upload_element("u", sid, ih, json.dumps(image).encode(), C)
    result = engine.commit("u", URI, commit_body(doc, staging_session=sid), C, IfAbsent())
    assert result.status == "created"


def test_commit_session_page_traversal_reports_deep_missing() -> None:
    """会话页的子层不闭合：只传文档与页时，元素与 blob 逐层列出。"""
    engine = make_engine(session_id_factory=id_factory())
    sid = _open(engine)
    blob = b"\x00deep"
    ref = dpe_hash.blob_ref(blob)
    image = {"category": "Image", "blob": ref, "mime_type": "image/png"}
    ih = dpe_hash.object_hash(image, "element", C)
    page, ph = _page([ih])
    document = {"file_type": "md", "pages": [ph]}
    # 只传文档对象与页对象
    engine.upload_page("u", sid, ph, json.dumps(page).encode(), C)
    with pytest.raises(errors.MissingContentError) as info:
        engine.commit("u", URI, commit_body(document, staging_session=sid), C, IfAbsent())
    assert info.value.missing.pages == []
    assert info.value.missing.content_hashes == [ih]
    engine.upload_element("u", sid, ih, json.dumps(image).encode(), C)
    with pytest.raises(errors.MissingContentError) as info:
        engine.commit("u", URI, commit_body(document, staging_session=sid), C, IfAbsent())
    assert info.value.missing.blobs == [ref]
    engine.upload_blob("u", sid, ref, blob, C)
    body = commit_body(document, staging_session=sid)
    assert engine.commit("u", URI, body, C, IfAbsent()).status == "created"
