"""写操作对真实参考服务端（#50）：CAS 全矩阵、commit 求值顺序、delete / move 及其重试。

``ProtocolCore`` 产出的请求经真实 HTTP 路径（ASGI）交给 ``dpe_sdk.testing`` 的引擎；断言落在
内容上——失败的前置条件必须不改变服务端内容、重试不改变最终状态——不只看状态码。「响应丢失」
用 ``Lossy`` 包装器模拟：请求照常到达引擎（状态已变），客户端拿不到结果。

快路径与暂存路径的选择、页分块、blob 断点续传与错误码的报文位置分别在 ``test_http_documents``、
``test_protocol_staging_http`` 覆盖；这里覆盖引擎里才有定论的求值顺序（core §3.3、§5.2）。
"""

from __future__ import annotations

import dpe_hash
import pytest
from dpe_sdk import Document, errors
from dpe_sdk.protocol import BaseHash, Force, IfAbsent, adrive
from engine_helpers import FakeClock, make_engine, text_doc
from http_helpers import Lossy, Recorder, remote

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


URI = "test://docs/a"
TARGET = "test://docs/b"
THIRD = "test://docs/c"
FRESH = "test://docs/fresh"
TARGET_QUERY = "documents?uri=test%3A%2F%2Fdocs%2Fa"
IMAGE = b"\x89PNG" + b"x" * 246  # 250 字节
REF = dpe_hash.blob_ref(IMAGE)


def doc(*texts: str) -> Document:
    return Document.model_validate(text_doc(list(texts)))


def image_doc() -> Document:
    """引用一个 blob 的文档：blob 永远不能内联（core §6），快路径提交必报缺内容。"""
    return Document.model_validate(
        {
            "file_type": "png",
            "pages": [{"elements": [{"category": "Image", "blob": REF, "mime_type": "image/png"}]}],
        }
    )


async def test_cas_matrix_over_the_reference_server() -> None:
    """CAS 全矩阵（core §3.3 第 4–5 步、§5）：三种前置条件的成功与失败分支，失败不改变内容。"""
    async with remote() as r:
        core = await r.core()
        first, changed, other = doc("a", "b"), doc("a", "c"), doc("z")

        # 快路径：一条 PUT 内联全部对象；IfAbsent 新建
        recorder = Recorder(r.send)
        created = await adrive(core.commit(URI, first, IfAbsent()), recorder)
        assert (created.status, created.delta.added) == ("created", 2)
        assert created.doc_hash == first.doc_hash()
        assert [(request.method, request.target) for request in recorder.sent] == [
            ("PUT", TARGET_QUERY)
        ]

        # IfAbsent + 不同内容 → ALREADY_EXISTS，服务端内容未被覆盖
        with pytest.raises(errors.AlreadyExistsError):
            await r.run(core.commit(URI, changed, IfAbsent()))
        skeleton = await r.run(core.get_skeleton(URI))
        assert skeleton is not None and skeleton.doc_hash == created.doc_hash

        # IfAbsent + 相同内容 → unchanged（第 4 步先于前置条件求值）
        replay = await r.run(core.commit(URI, first, IfAbsent()))
        assert (
            replay.status,
            replay.delta.added,
            replay.delta.removed,
            replay.delta.retained,
        ) == ("unchanged", 0, 0, 2)

        # BaseHash 命中 → updated，delta 按元素多重集
        updated = await r.run(core.commit(URI, changed, BaseHash(created.doc_hash)))
        assert (
            updated.status,
            updated.delta.added,
            updated.delta.removed,
            updated.delta.retained,
        ) == ("updated", 1, 1, 1)
        skeleton = await r.run(core.get_skeleton(URI))
        assert skeleton is not None and skeleton.doc_hash == changed.doc_hash()

        # BaseHash 过期 → PRECONDITION_FAILED，内容未被覆盖
        with pytest.raises(errors.PreconditionFailedError):
            await r.run(core.commit(URI, other, BaseHash(created.doc_hash)))
        skeleton = await r.run(core.get_skeleton(URI))
        assert skeleton is not None and skeleton.doc_hash == updated.doc_hash

        # BaseHash 指向不存在的文档 → NOT_FOUND
        with pytest.raises(errors.NotFoundError):
            await r.run(core.commit("test://docs/missing", other, BaseHash(other.doc_hash())))
        assert await r.run(core.head("test://docs/missing")) is None

        # Force：不看现状直接覆盖；相同内容仍是 unchanged；到不存在的文档是 created
        forced = await r.run(core.commit(URI, other, Force()))
        assert (forced.status, forced.doc_hash) == ("updated", other.doc_hash())
        assert (await r.run(core.commit(URI, other, Force()))).status == "unchanged"
        fresh = await r.run(core.commit("test://docs/missing", other, Force()))
        assert fresh.status == "created" and fresh.doc_hash == other.doc_hash()


async def test_commit_evaluation_order_over_the_reference_server() -> None:
    """commit 求值顺序（core §3.3）：unchanged 先于会话，前置条件先于会话与可得性。"""
    clock = FakeClock()
    async with remote(make_engine(clock=clock, staging_ttl_seconds=3600)) as r:
        core = await r.core()
        current, changed, image = doc("a", "b"), doc("a", "c"), image_doc()
        created = await r.run(core.commit(URI, current, IfAbsent()))

        # 开一个真实会话再拨过 TTL 让它过期：后续引用它时只有第 6 步会碰到会话
        negotiated = await r.run(core.negotiate(URI, changed))
        clock.advance(3601)

        # 第 4 步先于第 6 步：内容不变时即使引用过期会话也是 unchanged（不 410）
        replay = await r.run(
            core.commit(URI, current, BaseHash(created.doc_hash), session=negotiated.session)
        )
        assert replay.status == "unchanged"

        # 第 5 步先于第 6 步：前置条件失败（陈旧 base_hash）优先于会话过期
        stale = doc("z").doc_hash()
        with pytest.raises(errors.PreconditionFailedError):
            await r.run(core.commit(URI, changed, BaseHash(stale), session=negotiated.session))

        # 顺序确实走到了第 6 步：前置条件成立、内容有变时，同一会话报过期
        with pytest.raises(errors.SessionExpiredError):
            await r.run(
                core.commit(URI, changed, BaseHash(created.doc_hash), session=negotiated.session)
            )

        # 第 5 步先于缺内容：已存在时 IfAbsent 先失败，缺 blob 不参与
        with pytest.raises(errors.AlreadyExistsError):
            await r.run(core.commit(URI, image, IfAbsent()))

        # 前置条件成立时才轮到第 6 步：缺 blob 如实报缺（带分层清单）
        with pytest.raises(errors.MissingContentError) as info:
            await r.run(core.commit(FRESH, image, IfAbsent()))
        assert info.value.missing.blobs == [REF]


async def test_commit_retries_over_the_reference_server() -> None:
    """响应丢失后的重发（core §5.2）：CAS 提交重发得 unchanged；force 不重放、先 head 判定。"""
    async with remote() as r:
        core = await r.core()
        document = doc("a", "b")

        # 首次已生效、响应丢失 → 原样重发 → unchanged（首次的 delta 不可得）
        lossy = Lossy(r.send, lambda request: request.method == "PUT")
        created = await adrive(core.commit(URI, document, IfAbsent()), lossy)
        assert (created.status, created.doc_hash) == ("unchanged", document.doc_hash())
        assert [request.method for request in lossy.sent] == ["PUT", "PUT"]
        assert await r.run(core.head(URI)) == document.doc_hash()

        # force：响应丢失后不重放，先 head；等于提交内容即成功（不重发 PUT）
        lossy = Lossy(r.send, lambda request: request.method == "PUT")
        confirmed = await adrive(core.commit(URI, document, Force()), lossy)
        assert (confirmed.status, confirmed.doc_hash) == ("unchanged", document.doc_hash())
        assert [request.method for request in lossy.sent] == ["PUT", "HEAD"]


async def test_delete_and_move_retries_over_the_reference_server() -> None:
    """delete / move 的响应丢失重试与前置条件（core §4、§5.2），以最终内容断言。"""
    async with remote() as r:
        core = await r.core()
        document, other = doc("a", "b"), doc("z")
        created = await r.run(core.commit(URI, document, IfAbsent()))
        assert created.status == "created"

        # delete：首次已生效、响应丢失 → 重发得 NOT_FOUND → 视为成功
        lossy = Lossy(r.send, lambda request: request.method == "DELETE")
        await adrive(core.delete(URI, created.doc_hash), lossy)
        assert [request.method for request in lossy.sent] == ["DELETE", "DELETE"]
        assert await r.run(core.head(URI)) is None

        # 首次 delete 就 NOT_FOUND：如实抛出
        with pytest.raises(errors.NotFoundError):
            await r.run(core.delete(URI, created.doc_hash))

        # delete 前置条件不符 → 412，文档仍在（未被删）
        recreated = await r.run(core.commit(URI, document, IfAbsent()))
        with pytest.raises(errors.PreconditionFailedError):
            await r.run(core.delete(URI, other.doc_hash()))
        assert await r.run(core.head(URI)) == recreated.doc_hash

        # move：首次已生效、响应丢失 → 重发命中「目标状态已达成」→ 成功且内容完整
        lossy = Lossy(r.send, lambda request: request.target == "move")
        moved = await adrive(core.move(URI, TARGET, recreated.doc_hash), lossy)
        assert moved.doc_hash == recreated.doc_hash
        assert [request.target for request in lossy.sent] == ["move", "move"]
        assert await r.run(core.head(URI)) is None
        skeleton = await r.run(core.get_skeleton(TARGET))
        assert skeleton is not None and skeleton.doc_hash == recreated.doc_hash

        # move 前置条件不符（由请求体承载）→ 409 → PreconditionFailedError；两侧都未变
        with pytest.raises(errors.PreconditionFailedError):
            await r.run(core.move(TARGET, THIRD, other.doc_hash()))
        assert await r.run(core.head(TARGET)) == recreated.doc_hash
        assert await r.run(core.head(THIRD)) is None

        # move 到已存在的目标 → ALREADY_EXISTS（409），两侧都未变
        await r.run(core.commit(THIRD, other, IfAbsent()))
        with pytest.raises(errors.AlreadyExistsError):
            await r.run(core.move(TARGET, THIRD, recreated.doc_hash))
        assert await r.run(core.head(TARGET)) == recreated.doc_hash
        assert await r.run(core.head(THIRD)) == other.doc_hash()
