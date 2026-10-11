"""参考服务端集成回归（#46）：向量一致性、暂存与存储混合的闭包、中间态不可见、会话跨 CAS 复用。

全部经 HTTP 绑定（ASGI）驱动 ``Engine``，与 conformance 跑分器及 Rust SDK（#25）看到的表面一致。

#16 原有验收逐条映射：

- 参考服务端算出的 doc_hash 与向量一致：本文件
  ``test_document_vectors_via_the_staging_path``（暂存路径）；快路径见
  ``test_engine_commit.test_document_vectors_via_fast_path``；
- Rust SDK 的集成测试可以对它运行（#25 以此为对手）：ASGI 可进程内挂载（本文件、
  ``http_helpers.remote``），独立监听与跨进程可达见
  ``test_http_server.test_standalone_server_is_reachable_from_another_process``；
- ASGI 应用可进程内挂载：``http_helpers.remote``；
- 可注入时钟与会话 id 生成器：
  ``test_hooks.test_clock_advance_hook_renews_and_expires``、``test_engine_session``；
- 限额全部可配：``http_helpers.small_engine`` 等；
- 可插拔授权器：``test_engine_uri``（``PrefixAuthorizer``）、``test_http_server``；
- 模拟服务端自身写入（core §2.4）：
  ``test_hooks.test_self_write_hook_runs_the_commit_path``、``test_engine_server_write``；
- 断点查询 HEAD 带 ``DPE-Session-Expires`` 且不续期（规范 0.1.4）：
  ``test_http_staging.test_staging_path_end_to_end``（HTTP 级）、
  ``test_engine_staging.test_upload_offset_three_states_and_no_renewal``（引擎级）；
  #50 的 ``test_protocol_staging_http`` 另覆盖核心/服务端两侧的偏移续传与三状态；
- 去重范围策略（文档级 / 写授权级可配）：已被 #97 取代，范围固定为本文档（core §3.3），
  无开关；``test_engine_staging.test_staging_manifest_ignores_objects_held_by_other_documents``；
- 中间态不可见 / 闭包不变式 / 会话跨 CAS 复用：本文件其余用例。

每个成功提交的文档向量表项都按引擎声明的两个契约读回（HEAD/GET 自洽 + 页与文档 hash 原位
重算；向量表声明处比对期望值），覆盖 #45 双契约钩子面的交叉回归（契约 1 §6）；带 blob 的
文档按 core §6 如实报缺、不读回。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import quote

import dpe_hash
import httpx
import pytest
from engine_helpers import Inline, commit_body, inline, make_engine, negotiate_body
from http_helpers import Remote, problem, remote

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


C = dpe_hash.CONTRACT
DRILL = dpe_hash.DRILL_CONTRACT
URI = "test://regression/a"
URI2 = "test://regression/b"
IMAGE = b"\x89PNG" + b"x" * 246
REF = dpe_hash.blob_ref(IMAGE)


def target(uri: str) -> str:
    return "documents?uri=" + quote(uri, safe="")


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


def _page_raw(elements: list[str]) -> dict[str, Any]:
    return {"elements": elements}


def _element_hash(element: dict[str, Any]) -> str:
    return dpe_hash.object_hash(element, "element", C)


def _page_hash(elements: list[str]) -> str:
    return dpe_hash.object_hash(_page_raw(elements), "page", C)


def _text(text: str) -> dict[str, Any]:
    return {"category": "NarrativeText", "text": text}


async def _negotiate(r: Remote, uri: str, document: dict[str, Any], contract: str = C) -> str:
    response = await r.raw(
        "POST", "negotiate", contract=contract, content=negotiate_body(uri, document)
    )
    assert response.status_code == 200, response.text
    sid: str = response.json()["staging_session"]["id"]
    return sid


async def _upload(r: Remote, path: str, body: bytes, contract: str = C) -> httpx.Response:
    return await r.raw("PUT", path, contract=contract, content=body)


async def _upload_pages_and_elements(
    r: Remote,
    sid: str,
    pages: list[dict[str, Any]],
    elements: list[dict[str, Any]],
    contract: str = C,
) -> None:
    """把页对象与元素对象逐个传进会话（重复上传是 200）。"""
    for page in pages:
        response = await _upload(
            r,
            f"staging/{sid}/pages/{_page_hash(page['elements'])}",
            json.dumps(page).encode(),
            contract,
        )
        assert response.status_code in (200, 201), response.text
    for element in elements:
        response = await _upload(
            r,
            f"staging/{sid}/objects/{_element_hash(element)}",
            json.dumps(element).encode(),
            contract,
        )
        assert response.status_code in (200, 201), response.text


async def _commit(
    r: Remote,
    uri: str,
    document: dict[str, Any],
    *,
    sid: str,
    precondition: str,
    contract: str = C,
) -> httpx.Response:
    """commit 引用暂存会话；``precondition`` 直接给条件头（``If-None-Match: *`` 或 If-Match）。"""
    name, _, value = precondition.partition(": ")
    return await r.raw(
        "PUT",
        target(uri),
        contract=contract,
        headers={name: value},
        content=commit_body(document, staging_session=sid),
    )


# ---------------------------------------------------------------------------
# 向量：doc_hash 与向量一致（暂存路径）
# ---------------------------------------------------------------------------


async def _stage_document(r: Remote, uri: str, doc: Inline, contract: str) -> httpx.Response:
    """经暂存路径提交一篇文档：negotiate → 上传页 → 上传元素 → commit 引用会话。

    页与元素在到齐时由服务端重算 hash 并与路径比较（Rule 0），上传本身就是 hash 一致性检查。
    """
    sid = await _negotiate(r, uri, doc.document, contract)
    for page, page_hash in zip(doc.pages, doc.page_hashes, strict=True):
        response = await _upload(
            r, f"staging/{sid}/pages/{page_hash}", json.dumps(page).encode(), contract
        )
        assert response.status_code in (200, 201), response.text
    hashes = [h for page in doc.content_hashes for h in page]
    for element, content_hash in zip(doc.objects, hashes, strict=True):
        response = await _upload(
            r, f"staging/{sid}/objects/{content_hash}", json.dumps(element).encode(), contract
        )
        assert response.status_code in (200, 201), response.text
    return await _commit(
        r, uri, doc.document, sid=sid, precondition="If-None-Match: *", contract=contract
    )


async def test_document_vectors_via_the_staging_path(vectors_dir: Path) -> None:
    """#16 验收：暂存路径提交的 doc_hash 与向量一致，且按全部声明契约读回同一份内容。

    带 blob 的文档没有字节来源（向量只给引用）：按 core §6 如实报缺，缺失清单必须与向量引用
    一致（这些文档的 doc_hash 一致性由 ``dpe_hash`` 层的向量测试覆盖）。
    """
    engine = make_engine(contracts=(C, DRILL))
    committed = 0
    async with remote(engine) as r:
        for vec in _vectors(vectors_dir, "document"):
            for key, document in vec["documents"].items():
                for contract, expected in vec["expected"][key].items():
                    uri = f"test://vectors/{vec['name']}/{key}/{contract}"
                    doc = inline(document, contract)
                    assert doc.doc_hash == expected["doc_hash"], uri  # 客户端本地算值也与向量一致
                    blobs = [e["blob"] for e in doc.objects if e.get("blob") is not None]
                    response = await _stage_document(r, uri, doc, contract)
                    if blobs:
                        body = problem(response)
                        assert (response.status_code, body["code"]) == (
                            400,
                            "DPE_MISSING_CONTENT",
                        )
                        # 页与元素都已入会话：完整清单只剩向量引用的 blob，且未被截断
                        assert body["missing"] == {
                            "pages": [],
                            "content_hashes": [],
                            "blobs": list(dict.fromkeys(blobs)),
                        }
                        assert body["missing_truncated"] is False
                        continue
                    assert response.status_code == 201, response.text
                    assert response.json()["doc_hash"] == expected["doc_hash"], uri
                    # 全部声明契约下读回：自洽（契约 1 §6 原位重算）＋向量声明处比对期望值
                    for other in (C, DRILL):
                        head = await r.raw("HEAD", target(uri), contract=other)
                        assert head.status_code == 200
                        skeleton = (await r.raw("GET", target(uri), contract=other)).json()
                        page_hashes = [
                            dpe_hash.object_hash(page, "page", other) for page in skeleton["pages"]
                        ]
                        assert skeleton["document"]["pages"] == page_hashes
                        assert (
                            dpe_hash.object_hash(skeleton["document"], "document", other)
                            == skeleton["doc_hash"]
                            == head.headers["dpe-doc-hash"]
                        )
                        if other in vec["expected"][key]:
                            declared = vec["expected"][key][other]
                            assert skeleton["doc_hash"] == declared["doc_hash"], uri
                            assert page_hashes == [p["page_hash"] for p in declared["pages"]]
                    committed += 1
    assert committed > 0


# ---------------------------------------------------------------------------
# 闭包不变式：暂存与存储混合
# ---------------------------------------------------------------------------


async def test_closure_holds_when_stored_and_staged_content_are_mixed() -> None:
    """闭包不变式（core §3.3 第 6 步）：新状态由存储中的页与会话中的页、元素、blob 混合而成。

    去重范围固定为本文档：存储中的页与元素不必重传（缺失清单里不出现）；提交成功后读回是闭合的
    （每页引用的元素按存储或上传的原样表示可解析），delta 按元素多重集计算。
    """
    e1, e2, e3 = (
        _text("one"),
        _text("two"),
        {"category": "Image", "blob": REF, "mime_type": "image/png"},
    )
    h1, h2 = _element_hash(e1), _element_hash(e2)
    p1 = _page_raw([h1, h2])
    p2 = _page_raw([h2, _element_hash(e3)])
    p1_hash, p2_hash = _page_hash(p1["elements"]), _page_hash(p2["elements"])
    v1 = {"file_type": "md", "pages": [p1_hash]}
    v2 = {"file_type": "md", "pages": [p1_hash, p2_hash]}

    async with remote() as r:
        created = await r.raw(
            "PUT",
            target(URI),
            headers={"If-None-Match": "*"},
            content=json.dumps({"document": v1, "pages": [p1], "objects": [e1, e2]}).encode(),
        )
        assert created.status_code == 201
        v1_hash = created.json()["doc_hash"]

        # 缺失清单：存储中已存的页不算缺；会话页给出缺失元素（e2 在范围里）；元素给出缺失 blob
        negotiated = await r.raw(
            "POST", "negotiate", content=json.dumps({"file_uri": URI, "document": v2}).encode()
        )
        assert negotiated.json()["missing_pages"] == [p2_hash]
        sid: str = negotiated.json()["staging_session"]["id"]
        uploaded_page = await _upload(r, f"staging/{sid}/pages/{p2_hash}", json.dumps(p2).encode())
        assert uploaded_page.status_code == 201
        assert uploaded_page.json()["missing_content_hashes"] == [_element_hash(e3)]
        uploaded_element = await _upload(
            r, f"staging/{sid}/objects/{_element_hash(e3)}", json.dumps(e3).encode()
        )
        assert uploaded_element.status_code == 201
        assert uploaded_element.json()["missing_blobs"] == [REF]
        assert (await _upload(r, f"staging/{sid}/blobs/{REF}", IMAGE)).status_code == 201

        # 提交：新状态混合存储（P1、e1、e2）与会话（P2、e3、blob），delta 按多重集
        updated = await _commit(r, URI, v2, sid=sid, precondition=f'If-Match: "{v1_hash}"')
        assert updated.status_code == 200, updated.text
        result = updated.json()
        assert result["status"] == "updated"
        assert (
            result["delta"]["added"],
            result["delta"]["removed"],
            result["delta"]["retained"],
        ) == (2, 0, 2)  # e2（第二页再引用一次）与 e3 新增

        # 读回是闭合且原样的：P1 保持存储中的表示，P2 是本次上传的表示
        skeleton = (await r.raw("GET", target(URI))).json()
        assert skeleton["doc_hash"] == result["doc_hash"]
        assert skeleton["pages"] == [p1, p2]


# ---------------------------------------------------------------------------
# 中间态不可见
# ---------------------------------------------------------------------------


async def _listed(r: Remote, prefix: str) -> dict[str, str]:
    page = (await r.raw("GET", "documents?prefix=" + quote(prefix, safe=""))).json()
    return {entry["file_uri"]: entry["doc_hash"] for entry in page["documents"]}


async def test_intermediate_state_is_invisible_until_commit() -> None:
    """暂存的内容对一切读接口不可见（core §3.4）：更新到一半时读到的仍是旧内容，
    新建到一半时文档根本不存在；commit 之后才可见。"""
    old, fresh = _text("old"), _text("fresh")
    p1 = _page_raw([_element_hash(old)])
    p2 = _page_raw([_element_hash(old), _element_hash(fresh)])
    a1 = {"file_type": "md", "pages": [_page_hash(p1["elements"])]}
    a2 = {"file_type": "md", "pages": [_page_hash(p2["elements"])]}

    async with remote() as r:
        created = await r.raw(
            "PUT",
            target(URI),
            headers={"If-None-Match": "*"},
            content=json.dumps({"document": a1, "pages": [p1], "objects": [old]}).encode(),
        )
        a1_hash = created.json()["doc_hash"]

        # 为「更新」与「新建」各开一个会话并把内容传完，都先不 commit
        sid_update = await _negotiate(r, URI, a2)
        await _upload_pages_and_elements(r, sid_update, [p2], [old, fresh])
        sid_new = await _negotiate(r, URI2, a1)
        await _upload_pages_and_elements(r, sid_new, [p1], [old])

        # 更新中的文档读到的仍是旧内容
        assert (await r.raw("HEAD", target(URI))).headers["dpe-doc-hash"] == a1_hash
        assert (await r.raw("GET", target(URI))).json()["doc_hash"] == a1_hash
        # 新建中的文档不存在，也不出现在 list 与 batch_head
        assert (await r.raw("HEAD", target(URI2))).status_code == 404
        assert (await r.raw("GET", target(URI2))).status_code == 404
        assert await _listed(r, "test://regression/") == {URI: a1_hash}
        heads = (
            await r.raw("POST", "heads", content=json.dumps({"uris": [URI, URI2]}).encode())
        ).json()["heads"]
        assert heads == [{"doc_hash": a1_hash}, None]

        # commit 之后才可见
        committed_update = await _commit(
            r, URI, a2, sid=sid_update, precondition=f'If-Match: "{a1_hash}"'
        )
        assert committed_update.status_code == 200
        new_head = (await r.raw("HEAD", target(URI))).headers["dpe-doc-hash"]
        assert new_head == committed_update.json()["doc_hash"] != a1_hash
        committed_new = await _commit(r, URI2, a1, sid=sid_new, precondition="If-None-Match: *")
        assert committed_new.status_code == 201
        assert (await r.raw("HEAD", target(URI2))).headers["dpe-doc-hash"] == a1_hash


# ---------------------------------------------------------------------------
# 会话跨 CAS 复用
# ---------------------------------------------------------------------------


async def test_session_survives_a_failed_commit_and_is_reused() -> None:
    """失败的 commit 不消费会话（core §3.4）：412 之后会话与已传内容仍可用，
    换一个前置条件引用同一会话重提即可，无需重传任何对象。"""
    old, new1, new2 = _text("old"), _text("n1"), _text("n2")
    p1 = _page_raw([_element_hash(old)])
    p2 = _page_raw([_element_hash(new1), _element_hash(new2)])
    a1 = {"file_type": "md", "pages": [_page_hash(p1["elements"])]}
    a2 = {"file_type": "md", "pages": [_page_hash(p2["elements"])]}
    page_body = json.dumps(p2).encode()

    async with remote() as r:
        created = await r.raw(
            "PUT",
            target(URI),
            headers={"If-None-Match": "*"},
            content=json.dumps({"document": a1, "pages": [p1], "objects": [old]}).encode(),
        )
        a1_hash = created.json()["doc_hash"]

        sid = await _negotiate(r, URI, a2)
        page_path = f"staging/{sid}/pages/{_page_hash(p2['elements'])}"
        assert (await _upload(r, page_path, page_body)).status_code == 201
        for element in (new1, new2):
            uploaded = await _upload(
                r, f"staging/{sid}/objects/{_element_hash(element)}", json.dumps(element).encode()
            )
            assert uploaded.status_code == 201, uploaded.text

        # CAS 失败：文档已存在（If-None-Match: *），412 不消费会话、不改变文档
        failed = await _commit(r, URI, a2, sid=sid, precondition="If-None-Match: *")
        assert (failed.status_code, problem(failed)["code"]) == (412, "DPE_ALREADY_EXISTS")
        assert (await r.raw("HEAD", target(URI))).headers["dpe-doc-hash"] == a1_hash
        held = await r.raw("HEAD", page_path)
        assert (held.status_code, held.headers["dpe-upload-offset"]) == (200, str(len(page_body)))

        # 重新读取后换正确的前置条件，引用同一会话重提：不重传、一次写成
        updated = await _commit(r, URI, a2, sid=sid, precondition=f'If-Match: "{a1_hash}"')
        assert updated.status_code == 200, updated.text
        assert updated.json()["delta"] == {"added": 2, "removed": 1, "retained": 0}
        assert (await r.raw("GET", target(URI))).json()["pages"] == [p2]

        # 成功的 commit 消费会话：之后一律 410（HEAD 无体，错误码在头里）
        consumed = await r.raw("HEAD", page_path)
        assert consumed.status_code == 410
        assert consumed.headers["dpe-error-code"] == "DPE_SESSION_EXPIRED"
