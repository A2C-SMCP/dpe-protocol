"""暂存路径对真实参考服务端（#49、#50）：页分块、blob 断点续传、会话过期与缺失恢复。

``ProtocolCore`` 产出的请求经 ``ASGITransport`` 发给 ``dpe_sdk.testing`` 的参考服务端；
「响应丢失」用包装器模拟：请求照常到达引擎（状态已变），客户端拿不到结果。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import timedelta

import dpe_hash
import pytest
from dpe_sdk import Document, PageObject, errors
from dpe_sdk.protocol import BaseHash, BlobSource, IfAbsent, Request, Response, adrive
from engine_helpers import FakeClock, id_factory
from http_helpers import (
    Lossy,
    Recorder,
    Send,
    chunk_spans,
    problem,
    remote,
    session_ids,
    small_engine,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


URI = "test://docs/staged"
TARGET = "documents?uri=test%3A%2F%2Fdocs%2Fstaged"
IMAGE = b"\x89PNG" + b"x" * 246  # 250 字节
REF = dpe_hash.blob_ref(IMAGE)


def image_doc() -> Document:
    return Document.model_validate(
        {
            "file_type": "png",
            "title": "图片",
            "pages": [{"elements": [{"category": "Image", "blob": REF, "mime_type": "image/png"}]}],
        }
    )


def many_elements_doc(count: int = 10) -> Document:
    """一页多元素：页对象超过 ``max_payload_bytes`` 而每个元素不超（走页分块）。"""
    return Document.model_validate(
        {
            "file_type": "md",
            "pages": [
                {
                    "elements": [
                        {"category": "NarrativeText", "text": f"sentence {i} " * 4}
                        for i in range(count)
                    ]
                }
            ],
        }
    )


class Expire(Recorder):
    """命中 ``match`` 的第一次请求转发前拨快时钟：会话在服务端立即过期。"""

    def __init__(
        self, send: Send, match: Callable[[Request], bool], clock: FakeClock, seconds: float
    ) -> None:
        super().__init__(send)
        self._match = match
        self._clock = clock
        self._seconds = seconds
        self.expired = 0

    async def __call__(self, request: Request) -> Response:
        if self.expired == 0 and self._match(request):
            self.expired += 1
            self._clock.advance(self._seconds)
        return await super().__call__(request)


class Tick(Recorder):
    """每个请求发出前拨快时钟 ``seconds`` 秒（会话只在成功的上传时续期）。"""

    def __init__(self, send: Send, clock: FakeClock, seconds: float) -> None:
        super().__init__(send)
        self._clock = clock
        self._seconds = seconds

    async def __call__(self, request: Request) -> Response:
        self._clock.advance(self._seconds)
        return await super().__call__(request)


class Recorded(Recorder):
    """记录每个请求与它收到的响应（断言「核心确实收到了这条错误」）。"""

    def __init__(self, send: Send) -> None:
        super().__init__(send)
        self.responses: list[Response] = []

    async def __call__(self, request: Request) -> Response:
        response = await super().__call__(request)
        self.responses.append(response)
        return response


async def test_deliver_chunks_a_large_page_against_the_reference_server() -> None:
    document = many_elements_doc()
    async with remote(small_engine()) as r:
        core = await r.core()
        recorder = Recorder(r.send)
        result = await adrive(core.deliver(URI, document, IfAbsent()), recorder)
        assert result.status == "created"
        assert (result.delta.added, result.delta.removed) == (10, 0)
        skeleton = await r.run(core.get_skeleton(URI))
        assert skeleton is not None
        assert skeleton.doc_hash == document.doc_hash()
        assert len(skeleton.pages) == 1 and len(skeleton.pages[0].elements) == 10

        spans = chunk_spans(recorder.sent)
        assert spans, "页对象应当分块上传"
        total = spans[0][2]
        assert all(item[2] == total for item in spans)
        assert spans[0][0] == 0 and spans[-1][1] + 1 == total
        assert all(span[0] == spans[i - 1][1] + 1 for i, span in enumerate(spans) if i)
        for request in recorder.sent:
            body = request.body_bytes()
            if "Content-Range" in request.headers:  # 分块请求：单块不超过 blob_chunk_bytes
                assert body is not None and len(body) <= 100


async def test_deliver_resumes_a_lost_blob_chunk_against_the_reference_server() -> None:
    """某一块的响应丢失：先断点查询、按已收偏移续传（core §3.4 的客户端义务）。"""
    document = image_doc()

    def drop_second_chunk(request: Request) -> bool:
        return (
            request.method == "PUT"
            and "/blobs/" in request.target
            and request.headers.get("Content-Range", "").startswith("bytes 100-")
        )

    async with remote(small_engine()) as r:
        core = await r.core()
        lossy = Lossy(r.send, drop_second_chunk)
        result = await adrive(
            core.deliver(URI, document, IfAbsent(), blobs=lambda ref: BlobSource.from_bytes(IMAGE)),
            lossy,
        )
        assert result.status == "created"
        assert lossy.dropped == 1
        dropped_at = next(i for i, request in enumerate(lossy.sent) if drop_second_chunk(request))
        head = lossy.sent[dropped_at + 1]
        assert (head.method, head.target) == ("HEAD", lossy.sent[dropped_at].target)
        skeleton = await r.run(core.get_skeleton(URI))
        assert skeleton is not None and skeleton.doc_hash == document.doc_hash()


async def test_deliver_renegotiates_when_the_session_expires_mid_flow() -> None:
    document = many_elements_doc()
    clock = FakeClock()
    engine = small_engine(clock=clock, staging_ttl_seconds=3600)
    async with remote(engine) as r:
        core = await r.core()
        expiring = Expire(
            r.send,
            lambda request: request.method == "PUT" and "/pages/" in request.target,
            clock,
            3601,
        )
        result = await adrive(core.deliver(URI, document, IfAbsent()), expiring)
        assert result.status == "created"
        assert expiring.expired == 1
        assert len(session_ids(expiring.sent)) == 2  # 过期后重开：旧会话的请求被拒、新会话完成投递
        last_session = json.loads(expiring.sent[-1].body_bytes() or b"")["staging_session"]
        assert last_session in session_ids(expiring.sent)
        skeleton = await r.run(core.get_skeleton(URI))
        assert skeleton is not None and skeleton.doc_hash == document.doc_hash()


async def test_deliver_reports_a_server_side_blob_hash_mismatch() -> None:
    """声明了正确的 ref 但字节不符：服务端到齐校验失败，如实上报（带会话句柄）。"""
    document = image_doc()
    wrong = b"y" * len(IMAGE)
    source = BlobSource(REF, len(IMAGE), lambda offset, length: wrong[offset : offset + length])
    async with remote(small_engine()) as r:
        core = await r.core()
        with pytest.raises(errors.HashMismatchError) as info:
            await adrive(core.deliver(URI, document, IfAbsent(), blobs=lambda ref: source), r.send)
        assert info.value.session is not None


async def test_deliver_recovers_a_missing_blob_via_a_session() -> None:
    """快路径缺 blob（blob 永远不能内联）：开会话补传后以「内联 + 会话」重提（core §6）。"""
    document = image_doc()
    async with remote() as r:
        core = await r.core()
        recorder = Recorder(r.send)
        result = await adrive(
            core.deliver(URI, document, IfAbsent(), blobs=lambda ref: IMAGE), recorder
        )
        assert result.status == "created"
        methods = [(request.method, request.target) for request in recorder.sent]
        assert methods[0] == ("PUT", TARGET)  # 先快路径
        assert methods[1][0] == "POST" and methods[1][1] == "negotiate"
        assert any("/blobs/" in target for _, target in methods)
        body = json.loads(recorder.sent[-1].body_bytes() or b"")
        assert body["staging_session"] and body["pages"] and body["objects"]  # 内联 + 会话
        skeleton = await r.run(core.get_skeleton(URI))
        assert skeleton is not None and skeleton.doc_hash == document.doc_hash()


async def test_deliver_reuses_the_session_after_a_cas_failure() -> None:
    """CAS 冲突后以新前置条件引用同一会话重提，已上传内容不重传（core §3.4）。"""
    document = many_elements_doc()
    changed = many_elements_doc(9)
    async with remote(small_engine()) as r:
        core = await r.core()
        assert (await adrive(core.deliver(URI, document, IfAbsent()), r.send)).status == "created"
        # 前置条件不满足（如他人已写入）：冲突原样上报，异常带仍可复用的会话
        with pytest.raises(errors.PreconditionFailedError) as info:
            await adrive(core.deliver(URI, changed, BaseHash(changed.doc_hash())), r.send)
        session = info.value.session
        assert session is not None
        # 重新读取后引用同一会话重提：只 commit，不再 negotiate、不重传页与元素
        recorder = Recorder(r.send)
        result = await adrive(
            core.deliver(URI, changed, BaseHash(document.doc_hash()), session=session), recorder
        )
        assert result.status == "updated"
        assert [request.method for request in recorder.sent] == ["PUT"]
        skeleton = await r.run(core.get_skeleton(URI))
        assert skeleton is not None and skeleton.doc_hash == changed.doc_hash()


# ---------------------------------------------------------------------- 集成场景（#50）


def incremental_docs() -> tuple[Document, Document]:
    """V1 → V2：第一页追加一个文本与一个带 blob 的图片元素，第二页不动（未变的层不应重传）。"""
    image = {"category": "Image", "blob": REF, "mime_type": "image/png"}
    texts = [{"category": "NarrativeText", "text": text} for text in ("x", "a")]
    v1 = Document.model_validate(
        {
            "file_type": "md",
            "pages": [
                {"elements": texts},
                {"elements": [{"category": "NarrativeText", "text": "b"}]},
            ],
        }
    )
    v2 = Document.model_validate(
        {
            "file_type": "md",
            "pages": [
                {"elements": [*texts, {"category": "NarrativeText", "text": "c"}, image]},
                {"elements": [{"category": "NarrativeText", "text": "b"}]},
            ],
        }
    )
    return v1, v2


async def test_deliver_uploads_only_the_changed_layers_on_an_incremental_update() -> None:
    """增量投递：去重范围固定为本文档（core §3.3），只传变更的页、元素与 blob。

    ``max_payload_bytes=240``：negotiate 与 commit 请求体放得下，而快路径内联体放不下（必然走
    暂存路径）、变更页（302 字节）与 blob（250 字节）都放不下（必然分块）；会话 id 用确定性的
    短值，尺寸可推算。
    """
    v1, v2 = incremental_docs()
    changed_page = v2.hashes()["pages"][0]["page_hash"]
    new_text, new_image = v2.hashes()["pages"][0]["elements"][2:]
    async with remote(small_engine(max_payload_bytes=240, session_id_factory=id_factory())) as r:
        core = await r.core()
        assert (await r.run(core.deliver(URI, v1, IfAbsent()))).status == "created"

        recorder = Recorder(r.send)
        result = await adrive(
            core.deliver(URI, v2, BaseHash(v1.doc_hash()), blobs=lambda ref: IMAGE), recorder
        )
        assert (
            result.status,
            result.delta.added,
            result.delta.removed,
            result.delta.retained,
        ) == ("updated", 2, 0, 3)
        sent = recorder.sent
        (sid,) = session_ids(sent)  # 全程一个会话；重开会多出一个，这里直接失败
        # 只有变更的页被上传（未变的第二页不重传），页对象按分块走完
        pages = [request for request in sent if "/pages/" in request.target]
        assert {request.target for request in pages} == {f"staging/{sid}/pages/{changed_page}"}
        spans = chunk_spans(pages)
        assert spans[0][0] == 0 and spans[-1][1] + 1 == spans[0][2]
        assert all(span[0] == spans[i - 1][1] + 1 for i, span in enumerate(spans) if i)
        # 只有两个新元素被上传（未变元素不重传；顺序无规范约束，按集合断言）
        assert {request.target for request in sent if "/objects/" in request.target} == {
            f"staging/{sid}/objects/{new_text}",
            f"staging/{sid}/objects/{new_image}",
        }
        blobs = [request for request in sent if "/blobs/" in request.target]
        assert {request.target for request in blobs} == {f"staging/{sid}/blobs/{REF}"}
        blob_spans = chunk_spans(blobs)
        assert all(span[2] == len(IMAGE) for span in blob_spans)
        assert blob_spans[0][0] == 0 and blob_spans[-1][1] + 1 == len(IMAGE)
        # 最后的 commit 引用会话，不内联页与元素
        commit = sent[-1]
        assert (commit.method, commit.target) == ("PUT", TARGET)
        body = json.loads(commit.body_bytes() or b"")
        assert body["staging_session"] == sid
        assert "pages" not in body and "objects" not in body
        skeleton = await r.run(core.get_skeleton(URI))
        assert skeleton is not None and skeleton.doc_hash == v2.doc_hash()
        assert len(skeleton.pages) == 2


async def test_deliver_renews_the_session_past_the_ttl() -> None:
    """会话续期（core §3.4）：每次成功上传把过期时间推后——总耗时超过 TTL 的长流程，
    仍以同一个会话完成，不重开 negotiate。"""
    clock = FakeClock()
    async with remote(small_engine(clock=clock, staging_ttl_seconds=3600)) as r:
        core = await r.core()
        start = clock.now
        # 每个请求前拨 1000 秒：单步间隔小于 TTL，但流程总耗时远大于 TTL
        tick = Tick(r.send, clock, 1000)
        result = await adrive(core.deliver(URI, many_elements_doc(), IfAbsent()), tick)
        assert result.status == "created"
        assert clock.now - start > timedelta(seconds=3600)
        assert len(session_ids(tick.sent)) == 1
        skeleton = await r.run(core.get_skeleton(URI))
        assert skeleton is not None and skeleton.doc_hash == many_elements_doc().doc_hash()


async def test_blind_resend_of_an_applied_middle_chunk_is_rejected_with_the_offset() -> None:
    """#73（HTTP 绑定 §4.7）：中间块已被接受后盲目重发同一块 → 400 + ``DPE-Upload-Offset``
    （本会话已收字节数），按该偏移续传成功；断点查询（HEAD，只读、不续期）给出同一偏移。

    这是服务端侧的对照；核心收到该 400 后自己按偏移续传见
    ``test_blob_upload_resyncs_from_the_offset_in_a_400``。"""
    clock = FakeClock()
    async with remote(small_engine(clock=clock, staging_ttl_seconds=3600)) as r:
        core = await r.core()
        negotiated = await r.run(core.negotiate(URI, image_doc()))
        base = f"staging/{negotiated.session.id}/blobs/{REF}"
        # 第一块已被接受（202 的响应在客户端看来「丢失」）
        first = await r.raw(
            "PUT", base, headers={"Content-Range": f"bytes 0-99/{len(IMAGE)}"}, content=IMAGE[:100]
        )
        assert (first.status_code, first.headers["dpe-upload-offset"]) == (202, "100")
        # 盲目重发同一块：不命中「已完成」的 200，而是 400 + 本会话已收字节数
        blind = await r.raw(
            "PUT", base, headers={"Content-Range": f"bytes 0-99/{len(IMAGE)}"}, content=IMAGE[:100]
        )
        assert (blind.status_code, problem(blind)["code"]) == (400, "DPE_VALIDATION")
        assert blind.headers["dpe-upload-offset"] == "100"
        # 断点查询给出同一偏移，且不续期（拨钟后过期时间不变）
        clock.advance(60)
        head = await r.raw("HEAD", base)
        assert (head.status_code, head.headers["dpe-upload-offset"]) == (200, "100")
        assert head.headers["dpe-session-expires"] == first.headers["dpe-session-expires"]
        # 按已收偏移续传剩余两块
        middle = await r.raw(
            "PUT", base, headers={"Content-Range": "bytes 100-199/250"}, content=IMAGE[100:200]
        )
        assert (middle.status_code, middle.headers["dpe-upload-offset"]) == (202, "200")
        last = await r.raw(
            "PUT", base, headers={"Content-Range": "bytes 200-249/250"}, content=IMAGE[200:]
        )
        assert last.status_code == 201
        done = await r.raw("HEAD", base)
        assert done.headers["dpe-upload-offset"] == str(len(IMAGE))


async def test_blob_upload_resyncs_from_the_offset_in_a_400() -> None:
    """#73 客户端侧（HTTP 绑定 §4.7）：本会话已有进度时，核心的上传从 0 开始，收到
    400 + ``DPE-Upload-Offset`` 后**直接按该偏移续传**（不回退重发、不重开会话），完成后可提交。"""
    document = image_doc()
    async with remote(small_engine()) as r:
        core = await r.core()
        negotiated = await r.run(core.negotiate(URI, document))
        base = f"staging/{negotiated.session.id}/blobs/{REF}"
        # 服务端已有第一块（对客户端是「别处传来的既成进度」）：核心上传前并不知道
        first = await r.raw(
            "PUT", base, headers={"Content-Range": "bytes 0-99/250"}, content=IMAGE[:100]
        )
        assert first.status_code == 202

        recorder = Recorded(r.send)
        await adrive(core.upload_blob(negotiated.session, BlobSource.from_bytes(IMAGE)), recorder)
        # 第一条 PUT 从 0 开始并收到 400 + 偏移 100；第二条直接从 100 继续（不重发 0-99）
        assert recorder.responses[0].status == 400
        assert recorder.responses[0].header("DPE-Error-Code") == "DPE_VALIDATION"
        assert recorder.responses[0].header("DPE-Upload-Offset") == "100"
        assert chunk_spans(recorder.sent) == [(0, 99, 250), (100, 199, 250), (200, 249, 250)]
        # 全程一个会话，不开新会话
        assert session_ids(recorder.sent) == {negotiated.session.id}
        # 完成后偏移等于总字节数；补齐文档其余层后，引用同一会话提交成功、内容一致
        done = await r.raw("HEAD", base)
        assert done.headers["dpe-upload-offset"] == str(len(IMAGE))
        page = document.pages[0]
        await r.run(
            core.upload_page(
                negotiated.session,
                PageObject(elements=[element.content_hash() for element in page.elements]),
            )
        )
        await r.run(core.upload_element(negotiated.session, page.elements[0]))
        result = await r.run(core.commit(URI, document, IfAbsent(), session=negotiated.session))
        assert (result.status, result.doc_hash) == ("created", document.doc_hash())
        skeleton = await r.run(core.get_skeleton(URI))
        assert skeleton is not None and skeleton.doc_hash == document.doc_hash()


async def test_resume_query_reports_the_three_states_and_resets_after_discard() -> None:
    """#73（HTTP 绑定 §4.7）：断点查询三状态——本会话无进度 → 200 + 偏移 0（不得 404）、
    已完成 → 该对象总字节数、到齐校验失败被丢弃 → 归 0。"""
    async with remote(small_engine()) as r:
        core = await r.core()
        negotiated = await r.run(core.negotiate(URI, image_doc()))
        base = f"staging/{negotiated.session.id}/blobs/{REF}"
        # ① 无进度：200 + 偏移 0（绝不是 404）
        empty = await r.raw("HEAD", base)
        assert (empty.status_code, empty.headers["dpe-upload-offset"]) == (200, "0")
        # ② 到齐校验失败被丢弃：已收的中间态先有偏移，失败后归 0
        part = await r.raw(
            "PUT", base, headers={"Content-Range": "bytes 0-99/250"}, content=IMAGE[:100]
        )
        assert part.status_code == 202
        assert (await r.raw("HEAD", base)).headers["dpe-upload-offset"] == "100"
        second = await r.raw(
            "PUT", base, headers={"Content-Range": "bytes 100-199/250"}, content=b"y" * 100
        )
        assert second.status_code == 202
        wrong = await r.raw(
            "PUT", base, headers={"Content-Range": "bytes 200-249/250"}, content=b"y" * 50
        )
        assert (wrong.status_code, problem(wrong)["code"]) == (400, "DPE_HASH_MISMATCH")
        discarded = await r.raw("HEAD", base)
        assert discarded.headers["dpe-upload-offset"] == "0"
        # ③ 完成：偏移 = 该对象总字节数
        statuses = []
        for start in range(0, len(IMAGE), 100):
            piece = IMAGE[start : start + 100]
            response = await r.raw(
                "PUT",
                base,
                headers={"Content-Range": f"bytes {start}-{start + len(piece) - 1}/250"},
                content=piece,
            )
            statuses.append(response.status_code)
        assert statuses[-1] == 201
        done = await r.raw("HEAD", base)
        assert done.headers["dpe-upload-offset"] == str(len(IMAGE))
