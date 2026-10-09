"""线协议编解码（connector 契约 §6.2–§6.3）：sans-IO，不触碰进程与流。

- 分帧：NDJSON——一条消息 = 一行紧凑 JSON（UTF-8、LF 结尾），单条消息（含 LF）不超过
  ``max_message_bytes``。空行、无法解析的行、超限行都是协议错误，MUST NOT 跳过。
- 报文：I-JSON（无重复键、无孤立代理项）、单个 JSON-RPC 2.0 对象（不接受批量数组）。
- 方向：v1 只有运行器发起请求（§6.1），所以插件发来的每条消息都必须是响应；请求或通知都是违例。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Annotated, Any

from dpe_hash import ValidationError
from pydantic import BaseModel, ConfigDict, Field
from pydantic import ValidationError as PydanticValidationError

from dpe_sdk import _ijson
from dpe_sdk.run._json import has_lone_surrogate
from dpe_sdk.run.errors import ProtocolFailure

__all__ = [
    "PROTOCOL_VERSION",
    "InitializeResult",
    "LineDecoder",
    "PluginCapabilities",
    "PluginIdentity",
    "Response",
    "encode_notification",
    "encode_request",
    "parse_initialize_result",
    "parse_response",
]

#: 本实现支持的线协议版本（§6.2），与 DPE 协议版本、hash 契约版本相互独立
PROTOCOL_VERSION = "dpe-connector/1"


def _encode(message: dict[str, Any], max_message_bytes: int) -> bytes:
    line = json.dumps(message, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    data = line.encode("utf-8") + b"\n"
    if len(data) > max_message_bytes:
        # 运行器自己构造的消息超限是调用方的缺陷，不是插件违例
        raise ValueError(f"消息 {len(data)} 字节，超过 max_message_bytes {max_message_bytes}")
    return data


def encode_request(id_: int, method: str, params: dict[str, Any], max_message_bytes: int) -> bytes:
    """一条请求的线上字节（含行尾 LF）。"""
    return _encode(
        {"jsonrpc": "2.0", "id": id_, "method": method, "params": params}, max_message_bytes
    )


def encode_notification(method: str, params: dict[str, Any], max_message_bytes: int) -> bytes:
    """一条通知（无 ``id``，如 ``cancel``）的线上字节。"""
    return _encode({"jsonrpc": "2.0", "method": method, "params": params}, max_message_bytes)


class LineDecoder:
    """把 stdout 字节流切成消息行；违例即抛 ``ProtocolFailure``，之后不应再使用。"""

    def __init__(self, max_message_bytes: int) -> None:
        if max_message_bytes < 2:
            raise ValueError("max_message_bytes 至少容纳一个字节加行尾 LF")
        self._max = max_message_bytes
        self._buffer = bytearray()
        #: 缓冲区中已确认不含 LF 的前缀长度：长行分块到达时只扫描新增字节
        self._scanned = 0

    def feed(self, data: bytes) -> list[bytes]:
        """喂入一段字节，返回其中完整的行（不含 LF）。"""
        self._buffer += data
        lines: list[bytes] = []
        start = 0
        search_from = self._scanned
        while True:
            end = self._buffer.find(b"\n", search_from)
            if end == -1:
                break
            if end - start + 1 > self._max:
                raise self._oversize()
            if end == start:
                raise ProtocolFailure("插件 stdout 出现空行（§6.2）")
            lines.append(bytes(self._buffer[start:end]))
            start = search_from = end + 1
        del self._buffer[:start]
        self._scanned = len(self._buffer)
        # 未见 LF 时已有的字节加上行尾必然超限：不必等到行结束，也不无限缓冲
        if len(self._buffer) + 1 > self._max:
            raise self._oversize()
        return lines

    @property
    def pending(self) -> int:
        """尚未凑成整行的字节数；流结束时非零表示最后一条消息被截断。"""
        return len(self._buffer)

    def _oversize(self) -> ProtocolFailure:
        return ProtocolFailure(f"插件消息超过 max_message_bytes {self._max}（§6.2）")


@dataclass(frozen=True)
class Response:
    """插件对某个请求的响应：``error`` 为 ``None`` 时 ``result`` 有效。"""

    id: int
    result: Any = None
    error: tuple[int, str, Any] | None = None


def parse_response(line: bytes) -> Response:
    """解析插件发来的一行；不是合法的 JSON-RPC 2.0 响应即抛 ``ProtocolFailure``。"""
    try:
        message = _ijson.loads(line)
    except ValidationError as exc:
        raise ProtocolFailure(f"插件消息不是合法的 I-JSON（§6.2）：{exc}") from None
    if has_lone_surrogate(message):
        raise ProtocolFailure("插件消息含孤立代理项，不是 I-JSON（§6.2）")
    if isinstance(message, list):
        raise ProtocolFailure("插件发送了 JSON-RPC 批量数组（§6.2）")
    if not isinstance(message, dict):
        raise ProtocolFailure("插件消息不是 JSON 对象（§6.2）")
    if message.get("jsonrpc") != "2.0":
        raise ProtocolFailure('插件消息缺少 "jsonrpc": "2.0"')
    if "method" in message:
        raise ProtocolFailure("插件主动发起了请求或通知（§6.1）")
    id_ = message.get("id")
    has_result, has_error = "result" in message, "error" in message
    if has_result == has_error:
        raise ProtocolFailure("插件响应必须恰含 result 与 error 之一")
    if has_error and id_ is None:
        # 只有无法识别请求 id 时才会出现（JSON-RPC 2.0 §5）；运行器发出的请求总是合法的
        # 不回显插件文本：它可能含凭证，且 repr 转义后无法按字面值脱敏（§4.3）
        raise ProtocolFailure("插件返回了无 id 的错误响应")
    if not isinstance(id_, int) or isinstance(id_, bool):
        raise ProtocolFailure(f"插件响应的 id 不是整数（{type(id_).__name__}）")
    if has_result:
        return Response(id=id_, result=message["result"])
    error = message["error"]
    if not isinstance(error, dict):
        raise ProtocolFailure("插件响应的 error 不是对象")
    code, text = error.get("code"), error.get("message")
    if not isinstance(code, int) or isinstance(code, bool) or not isinstance(text, str):
        raise ProtocolFailure("插件响应的 error 缺少整数 code 或字符串 message")
    return Response(id=id_, error=(code, text, error.get("data")))


_NonEmpty = Annotated[str, Field(strict=True, min_length=1)]


class PluginIdentity(BaseModel):
    """插件自报的 ``{name, version}``（§6.3），须与清单相同（§4.1）。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: _NonEmpty
    version: _NonEmpty


class PluginCapabilities(BaseModel):
    """initialize 响应的 ``capabilities``（§6.3）：运行器 MUST 忽略不认识的键。"""

    model_config = ConfigDict(extra="ignore", frozen=True)

    supports_cursor: Annotated[bool, Field(strict=True)] = False


class InitializeResult(BaseModel):
    """initialize 响应的 ``result``（§6.3）。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol_version: Annotated[str, Field(strict=True)]
    plugin: PluginIdentity
    capabilities: PluginCapabilities = PluginCapabilities()


def parse_initialize_result(result: Any) -> InitializeResult:
    """校验 initialize 的 ``result``；缺少必填字段或类型不符是握手违例（协议级，§7.1）。"""
    try:
        return InitializeResult.model_validate(result)
    except PydanticValidationError as exc:
        details = "; ".join(
            f"{'.'.join(map(str, e['loc'])) or '<result>'}: {e['msg']}" for e in exc.errors()
        )
        raise ProtocolFailure(f"initialize 响应不合法（§6.3）：{details}") from None
