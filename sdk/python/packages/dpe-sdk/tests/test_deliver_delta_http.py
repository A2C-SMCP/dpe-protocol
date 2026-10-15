"""更新已存在的文档时只传缺失（#212，core §3.2）：对真实参考服务端核对投递路径与请求体。

选路（两个 SDK 一致）：新建与小文档（整篇内联不超过 ``SMALL_UPDATE_BYTES``）的更新走快路径；
更新较大的文档走差量路径，只传缺失、往返数有上限——``negotiate`` 只交文档对象得到缺失的页；
有缺失的页时再 ``negotiate`` 一次、只附带这些页（存入会话），得到其中缺失的元素；``commit``
引用会话并只内联缺失的元素。缺失的 blob 由 commit 的缺失清单给出，补传后重提。已有的页与元素
一次都不出现在请求里。
"""

from __future__ import annotations

import json
from typing import Any

import dpe_hash
import pytest
from dpe_sdk import Document
from dpe_sdk.protocol import (
    SMALL_UPDATE_BYTES,
    BaseHash,
    Force,
    IfAbsent,
    Request,
    adrive,
)
from engine_helpers import FakeClock, id_factory, make_engine
from http_helpers import Recorder, remote, session_ids

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


URI = "test://docs/delta"
IMAGE = b"\x89PNG" + b"y" * 96
REF = dpe_hash.blob_ref(IMAGE)


def _element(tag: str) -> dict[str, Any]:
    return {"category": "NarrativeText", "text": f"{tag} " + "lorem ipsum dolor sit amet " * 3}


def _page(tag: str, count: int) -> dict[str, Any]:
    return {"elements": [_element(f"{tag}-{i}") for i in range(count)]}


def _doc(pages: list[dict[str, Any]]) -> Document:
    return Document.model_validate({"file_type": "md", "pages": pages})


def _large(extra: list[dict[str, Any]] | None = None, *, at: int = 1) -> Document:
    """三页、整篇内联超过 ``SMALL_UPDATE_BYTES`` 的文档；``extra`` 插入到第 ``at`` 页之前。"""
    pages = [_page("a", 250), _page("b", 250), _page("c", 250)]
    pages[at:at] = extra or []
    return _doc(pages)


def _body(request: Request) -> dict[str, Any]:
    raw = request.body_bytes()
    assert raw is not None
    parsed: dict[str, Any] = json.loads(raw)
    return parsed


def _negotiates(sent: list[Request]) -> list[Request]:
    return [request for request in sent if request.target == "negotiate"]


def _commits(sent: list[Request]) -> list[Request]:
    return [request for request in sent if request.target.startswith("documents?")]


def _content_hashes(document: Document, page_index: int) -> list[str]:
    hashes: list[str] = document.hashes()["pages"][page_index]["elements"]
    return hashes


def _sent_pages(sent: list[Request]) -> set[str]:
    """请求里出现过的全部页对象（negotiate 附带、commit 内联与暂存上传）的 page_hash。"""
    hashes: set[str] = set()
    for request in sent:
        if request.target == "negotiate" or request.target.startswith("documents?"):
            hashes |= {
                dpe_hash.object_hash(page, "page") for page in _body(request).get("pages") or []
            }
        elif "/pages/" in request.target:
            hashes.add(request.target.rsplit("/", 1)[1])
    return hashes


def _old_pages(document: Document) -> set[str]:
    return {page["page_hash"] for page in document.hashes()["pages"]}


def _inline_hashes(body: dict[str, Any]) -> list[str]:
    return [dpe_hash.content_hash(element) for element in body.get("objects") or []]


async def test_inserting_a_middle_page_sends_only_the_new_objects() -> None:
    """北极星验收：中间插入一页，只传新增的内容对象——negotiate 只交文档对象，再只附带新页；
    commit 引用会话并只内联新页的元素；旧页与旧元素一次都不出现在请求里。"""
    v1 = _large()
    v2 = _large([_page("new", 3)])
    full = {"document": {}, "pages": v2.model_dump(mode="json")["pages"]}
    assert len(json.dumps(full)) > SMALL_UPDATE_BYTES  # 整篇内联超过阈值：走差量路径
    async with remote(make_engine(session_id_factory=id_factory())) as r:
        core = await r.core()
        assert (await r.run(core.deliver(URI, v1, IfAbsent()))).status == "created"
        recorder = Recorder(r.send)
        result = await adrive(core.deliver(URI, v2, BaseHash(v1.doc_hash())), recorder)
        assert (result.status, result.delta.added, result.delta.removed) == ("updated", 3, 0)
        # 三次往返：只交文档对象 → 只附带新页 → commit
        assert [request.target.split("?")[0] for request in recorder.sent] == [
            "negotiate",
            "negotiate",
            "documents",
        ]
        first, second = _negotiates(recorder.sent)
        assert "pages" not in _body(first)
        new_page = v2.hashes()["pages"][1]["page_hash"]
        assert [dpe_hash.object_hash(page, "page") for page in _body(second)["pages"]] == [new_page]
        (commit,) = _commits(recorder.sent)
        body = _body(commit)
        assert not body.get("pages")  # 新页已随第二次 negotiate 存入会话，不再内联
        assert sorted(_inline_hashes(body)) == sorted(_content_hashes(v2, 1))
        assert not _sent_pages(recorder.sent) & _old_pages(v1)  # 旧页对象一次都不出现
        skeleton = await r.run(core.get_skeleton(URI))
        assert skeleton is not None and skeleton.doc_hash == v2.doc_hash()


async def test_appending_one_element_sends_only_that_element() -> None:
    v1 = _large()
    pages = [_page("a", 250), _page("b", 250), _page("c", 250)]
    pages[2]["elements"].append(_element("tail"))
    v2 = _doc(pages)
    async with remote(make_engine()) as r:
        core = await r.core()
        await r.run(core.deliver(URI, v1, IfAbsent()))
        recorder = Recorder(r.send)
        result = await adrive(core.deliver(URI, v2, BaseHash(v1.doc_hash())), recorder)
        assert (result.status, result.delta.added) == ("updated", 1)
        (commit,) = _commits(recorder.sent)
        assert _inline_hashes(_body(commit)) == [_content_hashes(v2, 2)[-1]]
        assert _sent_pages(recorder.sent) == {v2.hashes()["pages"][2]["page_hash"]}


async def test_a_new_blob_is_uploaded_from_the_commit_manifest() -> None:
    """新元素引用新 blob：blob 不能内联，由 commit 的缺失清单给出、补传到同一会话后重提；
    两次 commit 都只内联新元素。"""
    v1 = _large()
    image = {"elements": [{"category": "Image", "blob": REF, "mime_type": "image/png"}]}
    v2 = _large([image])
    async with remote(make_engine()) as r:
        core = await r.core()
        await r.run(core.deliver(URI, v1, IfAbsent()))
        recorder = Recorder(r.send)
        result = await adrive(
            core.deliver(URI, v2, BaseHash(v1.doc_hash()), blobs=lambda ref: IMAGE), recorder
        )
        assert (result.status, result.delta.added) == ("updated", 1)
        commits = _commits(recorder.sent)
        assert len(commits) == 2
        expected = _content_hashes(v2, 1)
        assert all(_inline_hashes(_body(commit)) == expected for commit in commits)
        (sid,) = session_ids(recorder.sent)
        assert [request.target for request in recorder.sent if "/blobs/" in request.target] == [
            f"staging/{sid}/blobs/{REF}"
        ]
        assert not _sent_pages(recorder.sent) & _old_pages(v1)


async def test_force_update_of_a_large_document_also_sends_only_missing() -> None:
    v1 = _large()
    v2 = _large([_page("new", 2)])
    async with remote(make_engine()) as r:
        core = await r.core()
        await r.run(core.deliver(URI, v1, IfAbsent()))
        recorder = Recorder(r.send)
        result = await adrive(core.deliver(URI, v2, Force()), recorder)
        assert (result.status, result.delta.added) == ("updated", 2)
        (commit,) = _commits(recorder.sent)
        body = _body(commit)
        assert body["force"] is True
        assert sorted(_inline_hashes(body)) == sorted(_content_hashes(v2, 1))


async def test_creating_a_large_document_is_still_a_single_commit() -> None:
    """新建：内容必然全部缺失，整篇内联放得下就走快路径（一次往返）。"""
    async with remote(make_engine()) as r:
        core = await r.core()
        recorder = Recorder(r.send)
        result = await adrive(core.deliver(URI, _large(), IfAbsent()), recorder)
        assert result.status == "created"
        assert [request.target for request in recorder.sent] == [
            request.target for request in _commits(recorder.sent)
        ]
        assert len(recorder.sent) == 1


async def test_updating_a_small_document_is_still_a_single_commit() -> None:
    """整篇内联不超过 ``SMALL_UPDATE_BYTES`` 的更新：重传代价与多一次往返相当，仍走快路径。"""
    v1 = _doc([_page("a", 3)])
    v2 = _doc([_page("a", 3), _page("b", 1)])
    async with remote(make_engine()) as r:
        core = await r.core()
        await r.run(core.deliver(URI, v1, IfAbsent()))
        recorder = Recorder(r.send)
        result = await adrive(core.deliver(URI, v2, BaseHash(v1.doc_hash())), recorder)
        assert result.status == "updated"
        assert len(recorder.sent) == 1 and len(_commits(recorder.sent)) == 1


async def test_a_delta_that_does_not_fit_falls_back_to_layered_uploads() -> None:
    """差量请求体放不下 ``max_payload_bytes``：按 negotiate 的清单逐个上传缺失元素，commit
    只引用会话。"""
    v1 = _doc([_page("a", 1), _page("b", 1)])
    big = {"elements": [{"category": "NarrativeText", "text": f"{i} " + "x" * 1200} for i in "12"]}
    v2 = _doc([_page("a", 1), big, _page("b", 1)])
    async with remote(make_engine(max_payload_bytes=2000)) as r:
        core = await r.core()
        await r.run(core.deliver(URI, v1, IfAbsent()))
        recorder = Recorder(r.send)
        result = await adrive(core.deliver(URI, v2, BaseHash(v1.doc_hash())), recorder)
        assert (result.status, result.delta.added) == ("updated", 2)
        first, second = _negotiates(recorder.sent)
        assert "pages" not in _body(first) and len(_body(second)["pages"]) == 1
        uploads = [request for request in recorder.sent if "/objects/" in request.target]
        assert len(uploads) == 2
        (commit,) = _commits(recorder.sent)
        assert "objects" not in _body(commit) and "pages" not in _body(commit)


async def test_an_expired_delta_session_is_renegotiated_with_pages() -> None:
    """差量路径的会话在 commit 前过期：重走两次 negotiate，重算只含缺失对象的请求体。"""
    clock = FakeClock()
    v1 = _large()
    v2 = _large([_page("new", 2)])
    engine = make_engine(clock=clock, staging_ttl_seconds=3600, session_id_factory=id_factory())
    async with remote(engine) as r:
        core = await r.core()
        await r.run(core.deliver(URI, v1, IfAbsent()))

        class ExpireBeforeFirstCommit(Recorder):
            async def __call__(self, request: Request) -> Any:
                if request.target.startswith("documents?") and not _commits(self.sent):
                    clock.advance(3601)
                return await super().__call__(request)

        recorder = ExpireBeforeFirstCommit(r.send)
        result = await adrive(core.deliver(URI, v2, BaseHash(v1.doc_hash())), recorder)
        assert (result.status, result.delta.added) == ("updated", 2)
        negotiates = _negotiates(recorder.sent)
        assert [len(_body(n).get("pages") or []) for n in negotiates] == [0, 1, 0, 1]
        commits = _commits(recorder.sent)
        assert len(commits) == 2
        assert len({_body(commit)["staging_session"] for commit in commits}) == 2
        assert sorted(_inline_hashes(_body(commits[-1]))) == sorted(_content_hashes(v2, 1))
        assert not _sent_pages(recorder.sent) & _old_pages(v1)


async def test_reordering_pages_needs_no_objects() -> None:
    """只调整页序：没有缺失的页，negotiate 只交文档对象后直接 commit 引用会话（两次往返），
    不传任何页或元素。"""
    v1 = _large()
    pages = [_page("a", 250), _page("b", 250), _page("c", 250)]
    v2 = _doc([pages[2], pages[0], pages[1]])
    async with remote(make_engine()) as r:
        core = await r.core()
        await r.run(core.deliver(URI, v1, IfAbsent()))
        recorder = Recorder(r.send)
        result = await adrive(core.deliver(URI, v2, BaseHash(v1.doc_hash())), recorder)
        assert (result.status, result.delta.added, result.delta.removed) == ("updated", 0, 0)
        assert [request.target.split("?")[0] for request in recorder.sent] == [
            "negotiate",
            "documents",
        ]
        (commit,) = _commits(recorder.sent)
        assert "objects" not in _body(commit) and "pages" not in _body(commit)
        assert not _sent_pages(recorder.sent)


async def test_a_new_page_too_large_to_attach_is_uploaded_by_layers() -> None:
    """缺失的页连附带进第二次 negotiate 都放不下：按第一次 negotiate 的清单逐层上传——新页经
    暂存上传（响应给出其中缺失的元素），旧页一次都不出现。"""
    v1 = _doc([_page("a", 1), _page("b", 1)])
    wide = {"elements": [{"category": "NarrativeText", "text": f"w{i}"} for i in range(40)]}
    v2 = _doc([_page("a", 1), wide, _page("b", 1)])
    async with remote(make_engine(max_payload_bytes=2000)) as r:
        core = await r.core()
        await r.run(core.deliver(URI, v1, IfAbsent()))
        recorder = Recorder(r.send)
        result = await adrive(core.deliver(URI, v2, BaseHash(v1.doc_hash())), recorder)
        assert (result.status, result.delta.added) == ("updated", 40)
        (negotiate,) = _negotiates(recorder.sent)
        assert "pages" not in _body(negotiate)
        new_page = v2.hashes()["pages"][1]["page_hash"]
        assert _sent_pages(recorder.sent) == {new_page}
        uploaded = {r.target.rsplit("/", 1)[1] for r in recorder.sent if "/objects/" in r.target}
        assert uploaded == set(_content_hashes(v2, 1))  # 只有新页的元素
        # 分块上传的页的缺失清单可能截断，余下的由 commit 的缺失清单逐轮给出（core §3.3）
        assert all(
            "objects" not in _body(commit) and "pages" not in _body(commit)
            for commit in _commits(recorder.sent)
        )
