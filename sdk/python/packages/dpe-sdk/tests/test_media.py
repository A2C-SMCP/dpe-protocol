"""媒体来源适配（#15）：路径 / 可 seek 流 / ``read_blob`` 回调 → ``BlobSource``（本地计算）。"""

from __future__ import annotations

import hashlib
import io
from pathlib import Path
from typing import BinaryIO, cast

import dpe_hash
import pytest
from dpe_sdk.media import ReadAt, source_from_path, source_from_read_blob, source_from_stream
from http_helpers import RecordingReader, recording_open


def payload(size: int = 10244) -> bytes:
    """确定性的样例字节（跨块、含非 ASCII 值）。"""
    return bytes(i % 251 for i in range(size))


def read_at_factory(data: bytes) -> tuple[ReadAt, list[tuple[int, int]]]:
    """按契约实现的 ``read_blob``（快照 = ``data``），并记录每次 (offset, max_bytes)。"""
    calls: list[tuple[int, int]] = []

    def read(offset: int, max_bytes: int) -> tuple[bytes, bool]:
        calls.append((offset, max_bytes))
        piece = data[offset : offset + max_bytes]
        return piece, offset + len(piece) >= len(data)

    return read, calls


# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------


def test_source_from_path_matches_the_normative_ref(tmp_path: Path) -> None:
    data = payload()
    path = tmp_path / "media.bin"
    path.write_bytes(data)
    source = source_from_path(path, chunk_bytes=1024)
    assert source.ref == dpe_hash.blob_ref(data)
    assert source.ref == "sha256:" + hashlib.sha256(data).hexdigest()
    assert source.size == len(data)
    assert source.read(0, len(data)) == data
    assert source.read(0, 7) == data[:7]
    assert source.read(1024, 1024) == data[1024:2048]  # 跨块边界
    assert source.read(len(data) - 3, 3) == data[-3:]


def test_source_from_path_reads_and_hashes_in_bounded_chunks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """计数型断言：哈希与读取都只请求有界块，从不整读（内存不随文件大小增长）。"""
    data = payload(100_000)
    path = tmp_path / "big.bin"
    path.write_bytes(data)
    readers: list[RecordingReader] = []
    monkeypatch.setattr("dpe_sdk.media.open", recording_open(readers), raising=False)

    source = source_from_path(path, chunk_bytes=4096)
    assert (source.ref, source.size) == (dpe_hash.blob_ref(data), len(data))
    assert source.read(0, 100) == data[:100]

    hash_sizes = readers[0].read_sizes
    assert hash_sizes  # 哈希一趟
    assert all(0 < size <= 4096 for size in hash_sizes)
    # 读取按调用方给的区间；本次只请求 100 字节
    assert readers[1].read_sizes == [100]


# ---------------------------------------------------------------------------
# 可 seek 的流
# ---------------------------------------------------------------------------


def test_source_from_stream_hashes_and_serves_offsets() -> None:
    data = payload()
    stream = io.BytesIO(data)
    stream.seek(123)  # 当前位置无关：构造时回到起点
    source = source_from_stream(stream, chunk_bytes=512)
    assert (source.ref, source.size) == (dpe_hash.blob_ref(data), len(data))
    assert source.read(500, 100) == data[500:600]
    assert source.read(0, len(data)) == data


class Seekless:
    """不可 seek 的只读流（``seek`` 抛 ``io.UnsupportedOperation``）。"""

    def __init__(self, data: bytes) -> None:
        self._data = io.BytesIO(data)

    def read(self, size: int = -1) -> bytes:
        return self._data.read(size)

    def seek(self, offset: int, whence: int = 0) -> int:
        raise io.UnsupportedOperation("seek")


def test_source_from_stream_rejects_seekless_streams() -> None:
    with pytest.raises(ValueError, match="可 seek"):
        source_from_stream(cast(BinaryIO, Seekless(payload(64))))

    closed = io.BytesIO(b"data")
    closed.close()
    with pytest.raises(ValueError, match="不可用"):
        source_from_stream(closed)

    class ReadOnly:
        """只有 ``read``、没有 ``seek`` 的鸭子类型流。"""

        def read(self, size: int = -1) -> bytes:
            return b""

    with pytest.raises(ValueError, match="可 seek"):
        source_from_stream(cast(BinaryIO, ReadOnly()))


# ---------------------------------------------------------------------------
# read_blob 回调
# ---------------------------------------------------------------------------


def test_source_from_read_blob_hashes_and_serves_offsets() -> None:
    data = payload()
    read, calls = read_at_factory(data)
    source = source_from_read_blob(read, expected_size=len(data), chunk_bytes=1024)
    assert (source.ref, source.size) == (dpe_hash.blob_ref(data), len(data))
    # 哈希一趟：每次请求都以 chunk_bytes 为界，从不整读
    assert calls
    assert all(max_bytes == 1024 for _, max_bytes in calls)
    assert sum(min(n, len(data) - o) for o, n in calls) == len(data)

    calls.clear()
    assert source.read(1000, 2000) == data[1000:3000]  # 跨请求边界连续读取
    assert all(0 < max_bytes <= 1024 for _, max_bytes in calls)
    assert source.read(len(data) - 5, 5) == data[-5:]
    assert source.read(len(data), 10) == b""  # 末尾之后：空块 + eof
    assert source.read(0, 0) == b""


def test_source_from_read_blob_rejects_contract_violations() -> None:
    def oversized(offset: int, max_bytes: int) -> tuple[bytes, bool]:
        return b"x" * (max_bytes + 1), True

    with pytest.raises(ValueError, match="返回了"):
        source_from_read_blob(oversized, chunk_bytes=16)

    def empty_without_eof(offset: int, max_bytes: int) -> tuple[bytes, bool]:
        return b"", False

    with pytest.raises(ValueError, match="eof"):
        source_from_read_blob(empty_without_eof, chunk_bytes=16)

    data = payload(64)
    read, _ = read_at_factory(data)
    with pytest.raises(ValueError, match="与声明的"):
        source_from_read_blob(read, expected_size=len(data) + 1, chunk_bytes=1024)

    def endless(offset: int, max_bytes: int) -> tuple[bytes, bool]:
        return b"x" * max_bytes, False  # 声称没到末尾：读出超过声明长度即拒绝，不无限循环

    with pytest.raises(ValueError, match="超过声明的"):
        source_from_read_blob(endless, expected_size=16, chunk_bytes=8)

    with pytest.raises(ValueError, match="expected_size"):
        source_from_read_blob(read, expected_size=-1)


@pytest.mark.parametrize("bad", [0, -1, True, 1.5])
def test_chunk_bytes_must_be_a_positive_int(bad: object) -> None:
    with pytest.raises(ValueError, match="chunk_bytes"):
        source_from_read_blob(lambda offset, max_bytes: (b"", True), chunk_bytes=cast(int, bad))


def test_read_blob_serving_a_changed_snapshot_returns_a_short_block() -> None:
    """句柄快照变化（契约禁止，但本地读不出错就照读）：提前到末尾时返回短块，
    由 ``Request.body_bytes`` 按「字节数与声明的区间不符」如实拒绝，不静默补零。"""
    data = payload(2048)
    current = {"data": data}

    def read(offset: int, max_bytes: int) -> tuple[bytes, bool]:
        piece = current["data"][offset : offset + max_bytes]
        return piece, offset + len(piece) >= len(current["data"])

    source = source_from_read_blob(read, chunk_bytes=256)
    assert source.size == len(data)
    current["data"] = data[:1000]  # 快照变了
    assert source.read(0, 1000) == data[:1000]
    assert source.read(900, 400) == data[900:1000]  # 短块，由上层如实拒绝
