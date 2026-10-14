"""章：安全与去重范围（#57 全量 4/6）。

- core §3.3 第 6 步 + §8：**去重范围固定为本文档**——其他文档持有的对象一律视为缺失。
  调用者提交的内容 X 已存于另一篇文档时（无论调用者对那篇文档有无写授权），缺失清单仍按 X
  所在的层列出它，引用而不上传必须得到 ``DPE_MISSING_CONTENT``，在自己会话中上传 X 首次是
  ``201``；否则服务端就把「别处有没有这份内容」泄露给了调用者；
- HTTP §4.6、§4.7：201 / 200 与断点查询的偏移**只按本会话**判定（同一文档的另一个会话也不算）；
- core §3.3 第 2 步 + §8：无写授权的请求一律 ``403``——不因内容与当前相同而回 ``unchanged``，
  不因文档存在与否而改变结论（delete、move 两侧的各种状态）；
- 去重范围下界：本文档当前状态可得的对象不列为缺失（页层见 large 章，元素层与 blob 层在本章）。

「无写授权」用 scoped 身份；未配置时依赖它的检查显式 skip 并计入计数。
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, cast

import dpe_hash
import httpx
import pytest
from dpe_hash import ElementObject

from dpe_conformance.config import Identity
from dpe_conformance.plugin import Target
from dpe_conformance.support import (
    WRONG_HASH,
    WireDocument,
    assert_commit_success,
    assert_problem,
    breakpoint_query,
    build_document,
    commit,
    commit_with_session,
    delete,
    inside_scoped,
    move,
    negotiate,
    outside_scoped,
    seed,
    session_of,
    stage_all,
    text_element,
    upload_blob,
    upload_element,
    upload_offset,
    upload_page,
)


def _image(blob_ref: str, **extra: Any) -> dict[str, Any]:
    return {"category": "Image", "mime_type": "image/png", "blob": blob_ref, **extra}


def _content_hash(element: Mapping[str, Any]) -> str:
    return dpe_hash.content_hash(cast(ElementObject, dict(element)))


@dataclass(frozen=True)
class _Shared:
    """存于另一篇文档的内容：一页（文本元素 E + 引用 blob B 的 Image 元素 I）。"""

    document: WireDocument
    blob: bytes
    blob_ref: str
    text_hash: str
    image: dict[str, Any]
    image_hash: str


def _unique_blob() -> tuple[bytes, str]:
    """每次唯一的 blob 字节与其引用：断言不受其他用例留下的对象影响。"""
    blob = f"blob-{uuid.uuid4().hex}".encode()
    return blob, dpe_hash.blob_ref(blob)


def _shared_content() -> _Shared:
    blob, blob_ref = _unique_blob()
    text = text_element(f"secret-{uuid.uuid4().hex}")
    image = _image(blob_ref)
    return _Shared(
        document=build_document([[text, image]]),
        blob=blob,
        blob_ref=blob_ref,
        text_hash=_content_hash(text),
        image=image,
        image_hash=_content_hash(image),
    )


def _publish(
    target: Target,
    identity: Identity,
    uri: str,
    document: WireDocument,
    blobs: Mapping[str, bytes],
) -> None:
    """经暂存路径完整提交一篇新文档（含 blob 字节）。"""
    negotiated = negotiate(target, uri, document, identity=identity)
    assert negotiated.status_code == 200, f"negotiate 失败：{negotiated.status_code}"
    session_id, _ = session_of(negotiated)
    stage_all(target, session_id, document, identity=identity)
    for data in blobs.values():
        uploaded = upload_blob(target, session_id, data, identity=identity)
        assert uploaded.status_code in (200, 201), f"blob 上传失败：{uploaded.status_code}"
    committed = commit_with_session(target, uri, document, session_id, identity=identity)
    assert_commit_success(committed, status=201, status_text="created", doc_hash=document.doc_hash)


def _assert_offset_zero(response: httpx.Response, why: str) -> None:
    assert response.status_code == 200, f"断点查询应为 200，实际 {response.status_code}"
    assert upload_offset(response) == 0, why


# ---------------------------------------------------------------------- 去重范围固定为本文档


@pytest.fixture(params=["writer", "no-write"])
def caller(
    request: pytest.FixtureRequest, conformance_target: Target, full_identity: Identity
) -> tuple[Identity, Callable[[str], str], str]:
    """(调用者, 生成它自己可写的新 URI 的函数, 存放共享内容的另一篇文档的 URI)。

    ``writer``：调用者对另一篇文档也有写授权（full 身份）；``no-write``：调用者对它没有写授权
    （scoped 身份，另一篇文档在其可写前缀之外，自己的 URI 在前缀之下）。
    """
    if request.param == "writer":
        return (
            full_identity,
            lambda name: conformance_target.uri(f"security/{name}/{uuid.uuid4().hex}"),
            conformance_target.uri(f"security/other/{uuid.uuid4().hex}"),
        )
    scoped: Identity = request.getfixturevalue("scoped_identity")
    return (
        scoped,
        lambda name: inside_scoped(scoped, f"security/{name}"),
        outside_scoped(conformance_target, scoped, "security/other"),
    )


def test_content_held_elsewhere_is_still_missing(
    conformance_target: Target,
    full_identity: Identity,
    caller: tuple[Identity, Callable[[str], str], str],
) -> None:
    """core §3.3 第 6 步 + §8：内容已存于另一篇文档，对调用者仍按所在层列为缺失——
    页在 negotiate 的 ``missing_pages``；元素在附带页的 negotiate 与页上传响应的
    ``missing_content_hashes``；blob 在元素上传响应的 ``missing_blobs``；上传它们首次都是
    ``201``，断点查询的偏移为 0；不上传而直接引用 → ``DPE_MISSING_CONTENT``。

    每一步用各自的新 URI：会话绑定 (file_uri, 调用者)，不依赖「每次 negotiate 必开新会话」。
    """
    identity, mine, other = caller
    shared = _shared_content()
    document = shared.document
    page_hash = document.page_hashes[0]
    _publish(conformance_target, full_identity, other, document, {shared.blob_ref: shared.blob})

    # 不上传任何内容就引用 → 缺失（别处有不算数），且页层列出
    referenced = conformance_target.send(
        "PUT",
        "documents",
        identity=identity,
        params={"uri": mine("referenced")},
        headers={"If-None-Match": "*"},
        json={"document": document.document},
    )
    problem = assert_problem(referenced, status=400, code="DPE_MISSING_CONTENT")
    assert page_hash in problem["missing"]["pages"], f"页必须列为缺失：{problem['missing']!r}"

    # 元素层：附带页的 negotiate
    attached = negotiate(conformance_target, mine("attached"), document, identity=identity)
    assert attached.status_code == 200, f"negotiate 返回 {attached.status_code}"
    # negotiate 的清单不截断（HTTP §4.5）：附带页的元素清单与实际缺失一致
    listed = set(attached.json()["missing_content_hashes"])
    assert listed == {shared.text_hash, shared.image_hash}, (
        f"存于他处的元素仍须列入 missing_content_hashes，且不多列：{sorted(listed)}"
    )

    # 页层：negotiate 只交文档对象
    bare = negotiate(
        conformance_target, mine("staged"), document, identity=identity, attach_pages=False
    )
    assert bare.status_code == 200, f"negotiate 返回 {bare.status_code}"
    assert page_hash in bare.json()["missing_pages"], "存于他处的页仍须列入 missing_pages"
    session_id, _ = session_of(bare)

    # 页上传：首次 201（不因别处已有而 200），下一层清单完整列出两个元素（未分块不截断）
    page_response = upload_page(
        conformance_target, session_id, document.pages[0], page_hash, identity=identity
    )
    assert page_response.status_code == 201, f"首次上传页应为 201，实际 {page_response.status_code}"
    page_body = page_response.json()
    assert page_body["missing_truncated"] is False, "未分块上传的页响应不得截断（HTTP §4.6）"
    assert set(page_body["missing_content_hashes"]) == {shared.text_hash, shared.image_hash}, (
        f"页上传响应的元素清单不符：{page_body!r}"
    )

    # 元素上传：blob 层列出 B
    element_response = upload_element(
        conformance_target, session_id, shared.image, shared.image_hash, identity=identity
    )
    assert element_response.status_code == 201, (
        f"首次上传元素应为 201，实际 {element_response.status_code}"
    )
    assert element_response.json()["missing_blobs"] == [shared.blob_ref], (
        f"存于他处的 blob 仍须列入 missing_blobs：{element_response.json()!r}"
    )

    # blob：断点偏移只反映本会话（0），整体上传首次 201
    head = breakpoint_query(
        conformance_target, session_id, kind="blobs", ref=shared.blob_ref, identity=identity
    )
    _assert_offset_zero(head, "断点偏移只反映本会话，不反映他处已存")
    uploaded = upload_blob(conformance_target, session_id, shared.blob, identity=identity)
    assert uploaded.status_code == 201, f"首次上传 blob 应为 201，实际 {uploaded.status_code}"


def test_upload_state_is_per_session(conformance_target: Target, full_identity: Identity) -> None:
    """HTTP §4.6、§4.7 + core §3.3 第 6 步：201 / 200、断点偏移与缺失清单只按**本会话**
    （与本文档当前状态）判定——同一文档、同一调用者另一个未提交会话里上传的元素与 blob，在新会话
    中首次上传仍为 201、偏移仍为 0，且 blob 仍列入 ``missing_blobs``。

    服务端对同一 (file_uri, 调用者) 复用已打开的会话时无法构造「另一个会话」，如实 skip。
    """
    uri = conformance_target.uri(f"security/per-session/{uuid.uuid4().hex}")
    shared = _shared_content()

    def open_session() -> str:
        response = negotiate(
            conformance_target, uri, shared.document, identity=full_identity, attach_pages=False
        )
        assert response.status_code == 200, f"negotiate 返回 {response.status_code}"
        return session_of(response)[0]

    def put_image(session_id: str) -> httpx.Response:
        return upload_element(
            conformance_target, session_id, shared.image, shared.image_hash, identity=full_identity
        )

    first = open_session()
    first_image = put_image(first)
    assert first_image.status_code == 201, f"会话 1 上传元素失败：{first_image.status_code}"
    first_blob = upload_blob(conformance_target, first, shared.blob, identity=full_identity)
    assert first_blob.status_code == 201, f"会话 1 上传 blob 失败：{first_blob.status_code}"

    second = open_session()
    if second == first:
        pytest.skip("服务端对同一 (file_uri, 调用者) 复用会话，无法构造另一个会话")
    second_image = put_image(second)
    assert second_image.status_code == 201, (
        f"另一会话已传过的元素，本会话首次上传仍应为 201，实际 {second_image.status_code}"
    )
    assert second_image.json()["missing_blobs"] == [shared.blob_ref], (
        f"另一会话的 blob 不在本会话的去重范围内：{second_image.json()!r}"
    )
    head = breakpoint_query(
        conformance_target, second, kind="blobs", ref=shared.blob_ref, identity=full_identity
    )
    _assert_offset_zero(head, "另一会话的进度不得计入本会话")
    uploaded = upload_blob(conformance_target, second, shared.blob, identity=full_identity)
    assert uploaded.status_code == 201, f"本会话首次上传应为 201，实际 {uploaded.status_code}"


# ---------------------------------------------------------------------- 无写授权一律 403


def test_unchanged_content_without_write_access_is_forbidden(
    conformance_target: Target, full_identity: Identity, scoped_identity: Identity
) -> None:
    """core §3.3 第 2 步先于第 4 步 + §8：对无写授权的 URI 提交与其当前内容**相同**的 commit
    （带正确的 ``If-Match``）→ 403，不是 ``unchanged``——否则无权者能借此探知文档内容。"""
    uri = outside_scoped(conformance_target, scoped_identity, "security/same-content")
    document = build_document([[text_element(f"same-{uuid.uuid4().hex}")]])
    seed(conformance_target, uri, document, identity=full_identity)
    response = commit(
        conformance_target, uri, document, identity=scoped_identity, if_match=document.doc_hash
    )
    assert_problem(response, status=403, code="DPE_FORBIDDEN")


@pytest.mark.parametrize("state", ["absent", "existing"])
def test_delete_without_write_access_is_forbidden(
    conformance_target: Target, full_identity: Identity, scoped_identity: Identity, state: str
) -> None:
    """core §5 总则 + §8：无写授权的 delete 无论文档是否存在都是 403（不泄露存在性）。"""
    uri = outside_scoped(conformance_target, scoped_identity, f"security/delete-{state}")
    document = build_document([[text_element("delete")]])
    if state == "existing":
        seed(conformance_target, uri, document, identity=full_identity)
    response = delete(conformance_target, uri, identity=scoped_identity, if_match=document.doc_hash)
    assert_problem(response, status=403, code="DPE_FORBIDDEN")


@pytest.mark.parametrize("target_exists", [False, True], ids=["to-absent", "to-existing"])
@pytest.mark.parametrize("source_exists", [False, True], ids=["from-absent", "from-existing"])
@pytest.mark.parametrize("denied", ["source", "target"])
def test_move_without_write_access_is_forbidden_in_any_state(
    conformance_target: Target,
    full_identity: Identity,
    scoped_identity: Identity,
    denied: str,
    source_exists: bool,
    target_exists: bool,
) -> None:
    """core §5.2 + §8：move 的源或目标任一侧无写授权，无论两侧文档是否存在都是 403——
    授权先于「源不存在 → 404」「目标已存在 → 409」「base_hash 不符 → 409」等依赖状态的判定
    （这里 base_hash 故意不符）。源存在、目标不存在的两例与 order 章的授权用例重合，此处为
    凑齐状态矩阵保留。"""

    def side(name: str, is_denied: bool) -> str:
        if is_denied:
            return outside_scoped(conformance_target, scoped_identity, name)
        return inside_scoped(scoped_identity, name)

    source = side("security/move-src", denied == "source")
    target = side("security/move-dst", denied == "target")
    if source_exists:
        seed(
            conformance_target,
            source,
            build_document([[text_element("s")]]),
            identity=full_identity,
        )
    if target_exists:
        seed(
            conformance_target,
            target,
            build_document([[text_element("t")]]),
            identity=full_identity,
        )
    response = move(
        conformance_target, source, target, identity=scoped_identity, base_hash=WRONG_HASH
    )
    assert_problem(response, status=403, code="DPE_FORBIDDEN", documents_path=False)


# ---------------------------------------------------------------------- 去重范围下界


def test_objects_held_by_current_state_are_not_missing(
    conformance_target: Target, full_identity: Identity
) -> None:
    """core §3.3 第 6 步（下界与闭包不变式）：本文档当前状态引用的对象不列为缺失。新版本加一页，
    页内复用当前状态已有的元素 I，并新增引用同一 blob 的元素 I2：页上传响应只列 I2（元素层），
    I2 上传响应的 ``missing_blobs`` 为空（blob 层），无需重传字节。页层下界见 large 章。"""
    uri = conformance_target.uri(f"security/lower-bound/{uuid.uuid4().hex}")
    blob, blob_ref = _unique_blob()
    image = _image(blob_ref)
    version1 = build_document([[image]])
    _publish(conformance_target, full_identity, uri, version1, {blob_ref: blob})

    second_image = _image(blob_ref, metadata={"variant": 2})
    second_hash = _content_hash(second_image)
    version2 = build_document([[image], [image, second_image]])
    negotiated = negotiate(
        conformance_target, uri, version2, identity=full_identity, attach_pages=False
    )
    assert negotiated.status_code == 200, f"negotiate 返回 {negotiated.status_code}"
    assert negotiated.json()["missing_pages"] == [version2.page_hashes[1]], "只缺新页"
    session_id, _ = session_of(negotiated)
    page_response = upload_page(
        conformance_target,
        session_id,
        version2.pages[1],
        version2.page_hashes[1],
        identity=full_identity,
    )
    assert page_response.status_code == 201, (
        f"新页首次上传应为 201，实际 {page_response.status_code}"
    )
    assert page_response.json()["missing_content_hashes"] == [second_hash], (
        f"当前状态已有的元素不得列为缺失，新页只缺新元素：{page_response.json()!r}"
    )
    element_response = upload_element(
        conformance_target, session_id, second_image, second_hash, identity=full_identity
    )
    assert element_response.status_code == 201, (
        f"新元素首次上传应为 201，实际 {element_response.status_code}"
    )
    assert element_response.json()["missing_blobs"] == [], (
        f"本文档当前状态已有的 blob 不得列为缺失：{element_response.json()!r}"
    )
    committed = commit_with_session(
        conformance_target,
        uri,
        version2,
        session_id,
        identity=full_identity,
        if_match=version1.doc_hash,
    )
    assert_commit_success(committed, status=200, status_text="updated", doc_hash=version2.doc_hash)
