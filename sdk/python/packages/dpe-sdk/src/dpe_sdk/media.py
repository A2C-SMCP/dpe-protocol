"""媒体上传辅助（#15）：把调用方显式给出的字节来源适配成核心要的 ``BlobSource``。

只做**来源适配**：给定本地路径、可 seek 的字节流或 connector 契约 §6.7 的 ``read_blob`` 回调
（句柄由调用方绑定），算出 ``sha256:`` 引用与总长，并给出按任意偏移读取的 ``read``。分块、
断点续传与「400 + ``DPE-Upload-Offset`` 重同步」都在核心（``ProtocolCore.upload_blob`` /
``deliver`` 的 ``blobs`` 参数，见 ``dpe_sdk.protocol`` 模块说明），本模块不重复实现；内存字节
直接用 ``BlobSource.from_bytes``。

**不解析 url、不识别 metadata 里的路径字符串**：来源一律由调用方显式给出——元素对象的
``blob`` 字段只认 ``sha256:`` 引用（core §2.3），url 的解引用与凭证属于 connector
（契约 1 §4.2）。

**内存不随文件大小增长**：路径与流分块流式哈希（内存 O(``chunk_bytes``)）；``read_blob``
回调的来源分两趟——先分块读一遍算出摘要与总长，``read`` 再按偏移请求同一份快照（契约 §6.7
保证句柄支持任意偏移、同一偏移重复读取结果一致），不整读、不落盘。摘要与总长在上传开始前
确定，符合 §6.7 对运行器的约束（上传路径含 sha256、``Content-Range`` 含 total）。
"""

from __future__ import annotations

import io
import os
from collections.abc import Callable
from typing import BinaryIO, TypeAlias

from dpe_hash import BlobHasher

from dpe_sdk.protocol import BlobSource

__all__ = ["ReadAt", "source_from_path", "source_from_read_blob", "source_from_stream"]

#: 流式哈希与 ``read_blob`` 单次请求的缺省块大小（1 MiB）
_CHUNK_BYTES = 1 << 20


#: ``read_blob`` 形状的任意偏移读取（connector 契约 §6.7，句柄由调用方绑定）：
#: ``read(offset, max_bytes)`` 返回 ``(data, eof)``——``data`` 不超过 ``max_bytes``，
#: 空 ``data`` 必带 ``eof=True``；``offset`` 等于或超过总长时返回空块 + ``eof=True``。
ReadAt: TypeAlias = Callable[[int, int], tuple[bytes, bool]]


def source_from_path(
    path: str | os.PathLike[str], *, chunk_bytes: int = _CHUNK_BYTES
) -> BlobSource:
    """本地路径 → ``BlobSource``：分块流式哈希算出引用与总长（不整读进内存）。

    ``read`` 每次按偏移重新打开文件读取：要求文件在投递期间保持不变，内容变化会以
    ``DPE_HASH_MISMATCH`` 如实上报（core §3.4），不会被静默接受。
    """
    _check_chunk(chunk_bytes)
    with open(path, "rb") as stream:
        ref, size = _hash_stream(stream, chunk_bytes)

    def read(offset: int, length: int) -> bytes:
        block = bytearray()
        with open(path, "rb") as stream:
            stream.seek(offset)
            while len(block) < length:
                piece = stream.read(length - len(block))
                if not piece:
                    break
                block.extend(piece)
        return bytes(block)

    return BlobSource(ref, size, read)


def source_from_stream(stream: BinaryIO, *, chunk_bytes: int = _CHUNK_BYTES) -> BlobSource:
    """可 seek 的二进制流（``open(…, "rb")``、``io.BytesIO`` 等）→ ``BlobSource``。

    分块流式哈希后委托 ``BlobSource.from_seekable``：流须支持任意偏移的 seek / read，且由
    调用方负责在投递完成前保持打开（读取发生在传输适配层调用 ``read`` 时，可能在工作线程）。
    不可 seek 的流请改用 ``source_from_read_blob``（connector 的 ``read_blob`` 回调）或先落为
    临时文件。
    """
    _check_chunk(chunk_bytes)
    seek = getattr(stream, "seek", None)
    if seek is None:
        raise ValueError(
            "流必须可 seek（任意偏移读取）；不可 seek 的流改用 read_blob 回调或先落为临时文件"
        )
    try:
        seek(0)
    except io.UnsupportedOperation as exc:  # 管道等：seek 存在但明确不支持
        raise ValueError(
            "流必须可 seek（任意偏移读取）；不可 seek 的流改用 read_blob 回调或先落为临时文件："
            f"{exc}"
        ) from exc
    except (OSError, ValueError) as exc:  # 已关闭或底层失效（io.UnsupportedOperation 已被上面接住）
        raise ValueError(f"流不可用（已关闭或读取失败），需要可 seek 的打开句柄：{exc}") from exc
    ref, size = _hash_stream(stream, chunk_bytes)
    return BlobSource.from_seekable(stream, ref, size)


def source_from_read_blob(
    read: ReadAt, *, expected_size: int | None = None, chunk_bytes: int = _CHUNK_BYTES
) -> BlobSource:
    """connector 契约 §6.7 的 ``read_blob``（句柄已绑定）→ ``BlobSource``。

    先分块读一遍算出引用与总长（§6.7：摘要与总长确定后才开始上传），``read`` 再按偏移请求
    同一份快照。``expected_size`` 给出时校验实算总长与它一致（§6.7 的 ``size`` 一致性），
    不符抛 ``ValueError``；回调违反契约（单次返回超过 ``max_bytes``、空块不带 ``eof``）时
    同样抛 ``ValueError``。读取中途提前到末尾（快照变化）时 ``read`` 返回短块，由
    ``Request.body_bytes`` 按「字节数与声明的区间不符」如实拒绝。

    **句柄声明了 ``size`` 时（契约 §6.6）请一律传 ``expected_size``**：既做 §6.7 的
    一致性校验，也让「回调始终不返回 ``eof``」的违约来源在总长超过声明时立即失败（不必等
    读取超时）；不带时本地没有上界可判，这类来源只能靠调用方自己的读取超时兜底（§6.7 把
    超时职责放在运行器）。
    """
    _check_chunk(chunk_bytes)
    if expected_size is not None and (
        isinstance(expected_size, bool) or not isinstance(expected_size, int) or expected_size < 0
    ):
        raise ValueError(f"expected_size 必须是非负整数，实际为 {expected_size!r}")
    hasher = BlobHasher()
    while True:
        data, eof = _read_once(read, hasher.length, chunk_bytes)
        hasher.update(data)
        total = hasher.length
        if expected_size is not None and total > expected_size:
            raise ValueError(f"read_blob 读到的总长已超过声明的 {expected_size}")
        if eof:
            break
    if expected_size is not None and total != expected_size:
        raise ValueError(f"read_blob 读到的总长 {total} 与声明的 {expected_size} 不符")
    ref = hasher.finish()

    def read_range(offset: int, length: int) -> bytes:
        block = bytearray()
        while len(block) < length:
            data, eof = _read_once(read, offset + len(block), min(chunk_bytes, length - len(block)))
            block.extend(data)
            if eof:
                break
        return bytes(block)

    return BlobSource(ref, total, read_range)


def _hash_stream(stream: BinaryIO, chunk_bytes: int) -> tuple[str, int]:
    """分块流式哈希：内存 O(chunk_bytes)，不整读（路径与流共用）。"""
    hasher = BlobHasher()
    while True:
        chunk = stream.read(chunk_bytes)
        if not chunk:
            break
        hasher.update(chunk)
    return hasher.finish(), hasher.length


def _read_once(read: ReadAt, offset: int, max_bytes: int) -> tuple[bytes, bool]:
    """一次 ``read_blob`` 调用并校验其契约（``data`` ≤ ``max_bytes``；空块必带 ``eof``）。"""
    data, eof = read(offset, max_bytes)
    if len(data) > max_bytes:
        raise ValueError(
            f"read_blob 在偏移 {offset} 上请求至多 {max_bytes} 字节，返回了 {len(data)} 字节"
        )
    if not data and not eof:
        raise ValueError(f"read_blob 在偏移 {offset} 返回空块且 eof 为 false（空块必带 eof: true）")
    return data, eof


def _check_chunk(chunk_bytes: int) -> None:
    if isinstance(chunk_bytes, bool) or not isinstance(chunk_bytes, int) or chunk_bytes < 1:
        raise ValueError(f"chunk_bytes 必须是正整数，实际为 {chunk_bytes!r}")
