"""一轮内的 blob 读取与暂存（connector 契约 §6.7）。

句柄 blob 按块 ``read_blob`` 读入本地暂存（``SpooledTemporaryFile``：小对象在内存、大对象落临时
文件），边读边算 sha256；读完才得到引用与总长，上传在此之后由核心按需进行（§6.7「摘要与总长
确定前 MUST NOT 开始上传」）。插件的句柄只在本轮内有效，所以读取在轮内完成、不跨下一次轮首
``scan``；暂存在轮结束时统一释放。

失败一律是条目级（§7.1 ``blob`` 来源），由调用方让所在文档整篇失败；只有宿主的致命失败
（协议违例、插件崩溃、请求超时后不响应 ``cancel``）向上抛出，由编排层按轮级 / 协议级处理。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import tempfile
import threading
from dataclasses import dataclass
from typing import IO, Any

from dpe_sdk.protocol import BlobSource
from dpe_sdk.run.errors import ItemFailure, RpcError
from dpe_sdk.run.host import PluginHost, RequestTimedOut
from dpe_sdk.run.items import BlobSpec

__all__ = ["BlobStage"]

#: 单次 ``read_blob`` 的原始字节上限（再受 ``max_message_bytes`` 约束）
_MAX_CHUNK = 1 << 20
#: 响应信封（``jsonrpc`` / ``id`` / ``result`` / ``eof``）预留的字节数
_ENVELOPE_RESERVE = 4096
#: 单份暂存留在内存中的上限，超过即落临时文件
_SPOOL_MEMORY = 1 << 20

_BLOB_PREFIX = "sha256:"


def chunk_bytes(max_message_bytes: int) -> int:
    """在 ``max_message_bytes`` 内能承载的单次读取原始字节数（base64 膨胀 4/3）。"""
    return max(1, min(_MAX_CHUNK, (max_message_bytes - _ENVELOPE_RESERVE) * 3 // 4))


@dataclass(frozen=True)
class _Failed:
    """一次读取失败的不可变描述：同一句柄被多篇文档引用时，每篇各得一个新的 ``ItemFailure``
    （各自补上自己的 URI）。"""

    code: str
    message: str
    retryable: bool = False

    def failure(self) -> ItemFailure:
        return ItemFailure(self.code, self.message, source="blob", retryable=self.retryable)


def _failure(code: str, message: str, *, retryable: bool = False) -> _Failed:
    return _Failed(code, message, retryable)


class _Spooled:
    """一份已读全的字节暂存；多个上传线程可能并发读取同一份，读取串行化。"""

    def __init__(self, stream: IO[bytes], ref: str, size: int) -> None:
        self._stream = stream
        self._lock = threading.Lock()
        self.source = BlobSource(ref, size, self._read)

    def _read(self, offset: int, length: int) -> bytes:
        with self._lock:
            self._stream.seek(offset)
            return self._stream.read(length)

    def close(self) -> None:
        self._stream.close()


class BlobStage:
    """一轮的 blob 暂存：``resolve`` 取得字节并返回引用，``lookup`` 供核心上传时取字节。

    同一句柄只读一次（§6.7 SHOULD：同一轮内相同内容只读取与上传一次）；不同句柄读出相同内容
    时只保留一份暂存。
    """

    def __init__(
        self,
        host: PluginHost,
        *,
        blob_max_bytes: int | None,
        timeout: float,
    ) -> None:
        self._host = host
        self._limit = blob_max_bytes
        self._timeout = timeout
        self._chunk = chunk_bytes(host.max_message_bytes)
        self._sources: dict[str, BlobSource | _Spooled] = {}
        self._handles: dict[str, str | _Failed] = {}

    def lookup(self, ref: str) -> BlobSource:
        """核心 ``deliver`` 的 ``blobs`` 回调：按引用给出本轮读到的字节。"""
        entry = self._sources[ref]
        return entry.source if isinstance(entry, _Spooled) else entry

    def close(self) -> None:
        """释放全部暂存。编排层在每批落地完成后调用（暂存不跨批累积，内存与临时文件随批
        回收）；同一句柄在之后的批中再被引用时重新读取（句柄在本轮内有效，§6.7）。"""
        for entry in self._sources.values():
            if isinstance(entry, _Spooled):
                entry.close()
        self._sources.clear()
        self._handles.clear()

    async def resolve(self, spec: BlobSpec) -> str | ItemFailure:
        """取得一个字节来源的 ``sha256:`` 引用；失败返回条目级失败（调用方补上 URI）。"""
        if spec.inline is not None:
            if self._limit is not None and len(spec.inline) > self._limit:
                return _failure(
                    "blob_too_large", f"内联 blob {len(spec.inline)} 字节，超过 {self._limit}"
                ).failure()
            source = BlobSource.from_bytes(spec.inline)
            self._sources.setdefault(source.ref, source)
            return source.ref
        assert spec.handle is not None
        outcome = self._handles.get(spec.handle)
        if outcome is None:
            outcome = await self._read(spec.handle, spec.size)
            self._handles[spec.handle] = outcome
        return outcome.failure() if isinstance(outcome, _Failed) else outcome

    async def _read(self, handle: str, size: int | None) -> str | _Failed:
        if size is not None and self._limit is not None and size > self._limit:
            return _failure("blob_too_large", f"句柄 blob 声明 {size} 字节，超过 {self._limit}")
        stream: IO[bytes] = tempfile.SpooledTemporaryFile(max_size=_SPOOL_MEMORY)  # noqa: SIM115
        hasher = hashlib.sha256()
        total = 0
        try:
            while True:
                chunk = await self._read_chunk(handle, total)
                if isinstance(chunk, _Failed):
                    stream.close()
                    return chunk
                data, eof = chunk
                hasher.update(data)
                stream.write(data)
                total += len(data)
                if size is not None and total > size:
                    stream.close()
                    return _failure("blob_size_mismatch", f"句柄读到的字节已超过声明的 {size}")
                if self._limit is not None and total > self._limit:
                    stream.close()
                    return _failure("blob_too_large", f"句柄 blob 超过 {self._limit} 字节")
                if eof:
                    break
        except BaseException:
            stream.close()
            raise
        if size is not None and total != size:
            stream.close()
            return _failure("blob_size_mismatch", f"句柄读到 {total} 字节，声明为 {size}")
        ref = _BLOB_PREFIX + hasher.hexdigest()
        if ref in self._sources:
            stream.close()  # 相同内容已有一份暂存
        else:
            self._sources[ref] = _Spooled(stream, ref, total)
        return ref

    async def _read_chunk(self, handle: str, offset: int) -> tuple[bytes, bool] | _Failed:
        try:
            result: Any = await self._host.request(
                "read_blob",
                {"handle": handle, "offset": offset, "max_bytes": self._chunk},
                timeout=self._timeout,
            )
        except RpcError as exc:
            # 取消视为可重试，句柄失效不可重试，源端失败取插件给出的 data.retryable（§7.1）
            retryable = True if exc.name == "cancelled" else exc.retryable
            if exc.name == "unknown_handle":
                retryable = False
            return _failure(
                "blob_read_failed",
                f"read_blob 失败（{exc.name}）：{exc.message}",
                retryable=retryable,
            )
        except RequestTimedOut:
            return _failure(
                "blob_read_failed", f"read_blob 在 {self._timeout} 秒内未完成", retryable=True
            )
        return self._parse_chunk(result)

    def _parse_chunk(self, result: Any) -> tuple[bytes, bool] | _Failed:
        if (
            not isinstance(result, dict)
            or set(result) != {"data", "eof"}
            or not isinstance(result["data"], str)
            or not isinstance(result["eof"], bool)
        ):
            return _failure("blob_read_failed", "read_blob 响应必须恰为 {data, eof}")
        encoded: str = result["data"]
        try:
            data = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            return _failure("blob_read_failed", "read_blob 的 data 不是标准 base64")
        if len(encoded) % 4:
            return _failure("blob_read_failed", "read_blob 的 data 不是带填充的标准 base64")
        if len(data) > self._chunk:
            return _failure("blob_read_failed", "read_blob 返回的字节超过 max_bytes")
        if not data and not result["eof"]:
            return _failure("blob_read_failed", "read_blob 返回空块却未标记 eof")
        return data, result["eof"]
