"""媒体来源适配对参考服务端（#15）：经核心分块上传、内存有界、续传只补剩余字节。

``BlobSource`` 由 ``dpe_sdk.media`` 的来源适配产出（路径 / ``read_blob`` 回调），交给
``ProtocolCore.deliver`` 的 ``blobs`` 参数；分块、断点续传与偏移重同步都走核心的既有路径。
"""

from __future__ import annotations

from pathlib import Path

import dpe_hash
import pytest
from dpe_sdk import Document
from dpe_sdk.media import source_from_path, source_from_read_blob
from dpe_sdk.protocol import BaseHash, BlobSource, IfAbsent, Request, adrive
from http_helpers import (
    Lossy,
    Recorder,
    RecordingReader,
    chunk_spans,
    recording_open,
    remote,
    small_engine,
)

pytestmark = pytest.mark.anyio

URI = "test://media/doc"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def media_doc(ref: str) -> Document:
    """引用一个 blob 的文档：blob 永远不能内联（core §6），必然经暂存路径上传。"""
    return Document.model_validate(
        {
            "file_type": "png",
            "pages": [{"elements": [{"category": "Image", "blob": ref, "mime_type": "image/png"}]}],
        }
    )


def media_bytes(size: int = 1024) -> bytes:
    return bytes(i % 251 for i in range(size))


async def test_deliver_uploads_a_path_source_in_bounded_chunks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """大于 ``blob_chunk_bytes`` 的文件：分块上传后 commit 成功；计数型断言证明哈希与各次读取
    都只请求有界块，从不整读文件（内存不随文件大小增长）。"""
    data = media_bytes()
    path = tmp_path / "media.bin"
    path.write_bytes(data)
    readers: list[RecordingReader] = []
    monkeypatch.setattr("dpe_sdk.media.open", recording_open(readers), raising=False)

    source = source_from_path(path, chunk_bytes=256)
    assert source.ref == dpe_hash.blob_ref(data)
    document = media_doc(source.ref)

    async with remote(small_engine()) as r:
        core = await r.core()
        recorder = Recorder(r.send)
        result = await adrive(
            core.deliver(URI, document, IfAbsent(), blobs=lambda ref: source), recorder
        )
        assert (result.status, result.delta.added) == ("created", 1)
        skeleton = await r.run(core.get_skeleton(URI))
        assert skeleton is not None and skeleton.doc_hash == document.doc_hash()

        blob_requests = [request for request in recorder.sent if "/blobs/" in request.target]
        spans = chunk_spans(blob_requests)
        assert spans and all(span[2] == len(data) for span in spans)
        assert spans[0][0] == 0 and spans[-1][1] + 1 == len(data)
        assert all(span[1] - span[0] + 1 <= 100 for span in spans)  # 单块 ≤ blob_chunk_bytes
        assert sum(span[1] - span[0] + 1 for span in spans) == len(data)  # 上传总字节 == 文件大小

    # 哈希一趟每次读 ≤ 256（构造时，readers[0]）；各次区间读取按调用方给的块长（≤ 100）
    assert readers[0].read_sizes and all(0 < size <= 256 for size in readers[0].read_sizes)
    assert all(all(0 < size <= 100 for size in reader.read_sizes) for reader in readers[1:])


async def test_lost_chunk_resumes_with_only_the_remaining_bytes(tmp_path: Path) -> None:
    """中断续传：丢掉第二块（100-199）的响应后，核心先断点查询、再从已收偏移续传——
    已收字节不重发、不开新会话，续传的字节数恰好是剩余部分。"""
    data = media_bytes(1024)
    path = tmp_path / "media.bin"
    path.write_bytes(data)
    source = source_from_path(path, chunk_bytes=128)
    document = media_doc(source.ref)

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
            core.deliver(URI, document, IfAbsent(), blobs=lambda ref: source), lossy
        )
        assert result.status == "created"
        skeleton = await r.run(core.get_skeleton(URI))
        assert skeleton is not None and skeleton.doc_hash == document.doc_hash()

    blob_requests = [request for request in lossy.sent if "/blobs/" in request.target]
    assert [request.method for request in blob_requests[:3]] == ["PUT", "PUT", "HEAD"]
    spans = chunk_spans(blob_requests)
    assert spans[:2] == [(0, 99, 1024), (100, 199, 1024)]  # 第二块已到达服务端、响应被丢弃
    # 断点查询之后的续传从 200 开始：不重发 0-199、也不从头重来
    expected = [
        (start, min(start + 99, len(data) - 1), len(data)) for start in range(200, len(data), 100)
    ]
    assert spans[2:] == expected
    assert sum(span[1] - span[0] + 1 for span in spans[2:]) == len(data) - 200
    assert lossy.dropped == 1


async def test_deliver_uses_a_read_blob_source_without_reading_it_whole() -> None:
    """计数型来源（``read_blob`` 回调）：哈希一趟与上传阶段的每次请求都 ≤ ``chunk_bytes``，
    从未整读；提交成功后读回与文档一致。"""
    data = b"\x89PNG" + media_bytes(597)  # 601 字节
    calls: list[tuple[int, int]] = []

    def read(offset: int, max_bytes: int) -> tuple[bytes, bool]:
        calls.append((offset, max_bytes))
        piece = data[offset : offset + max_bytes]
        return piece, offset + len(piece) >= len(data)

    source = source_from_read_blob(read, expected_size=len(data), chunk_bytes=64)
    hash_pass = list(calls)
    assert len(hash_pass) == -(-len(data) // 64)  # ceil：一趟覆盖全部字节
    assert all(max_bytes == 64 for _, max_bytes in hash_pass)
    document = media_doc(source.ref)

    async with remote(small_engine()) as r:
        core = await r.core()
        result = await adrive(
            core.deliver(URI, document, IfAbsent(), blobs=lambda ref: source), r.send
        )
        assert result.status == "created"
        skeleton = await r.run(core.get_skeleton(URI))
        assert skeleton is not None and skeleton.doc_hash == document.doc_hash()

    assert all(0 < max_bytes <= 64 for _, max_bytes in calls)  # 全程从未整读
    assert len(calls) > len(hash_pass)  # 上传阶段继续按偏移请求同一份快照


async def test_unchanged_documents_never_touch_the_source(tmp_path: Path) -> None:
    """未变文档（``unchanged``）不触碰来源：``blobs`` 回调不发生、也就不重新哈希
    ——来源读取可能很贵（大文件、``read_blob`` 往返）。"""
    data = media_bytes(512)
    path = tmp_path / "media.bin"
    path.write_bytes(data)
    ref = source_from_path(path, chunk_bytes=128).ref
    document = media_doc(ref)
    built: list[str] = []

    def blobs(blob_ref: str) -> BlobSource:
        built.append(blob_ref)
        return source_from_path(path, chunk_bytes=128)  # 真的被要求时才哈希

    async with remote(small_engine()) as r:
        core = await r.core()
        first = await r.run(core.deliver(URI, document, IfAbsent(), blobs=blobs))
        assert first.status == "created"
        assert built == [ref]
        second = await r.run(core.deliver(URI, document, BaseHash(first.doc_hash), blobs=blobs))
        assert second.status == "unchanged"
        assert built == [ref]  # 未变：来源没有被再次要求
