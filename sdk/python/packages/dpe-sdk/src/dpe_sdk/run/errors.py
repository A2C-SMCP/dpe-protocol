"""运行器失败（connector 契约 §7.1、§7.4）：分层、来源与码。

``RunFailure`` 的字段与轮报告 ``failures`` 元素一一对应（§7.4），编排层（#52）直接据此写报告、
日志与退出码。三个子类按层级区分：实例级（不改动实例定义、清单或凭证就无法恢复，MUST NOT
自动重试）、协议级（终止实例，本轮作废）、轮级（本轮作废，退避后重试）。条目级失败不在
这里——它们不中断本轮，由编排层逐条记录。

``message`` MUST NOT 含凭证值（§4.3）：构造处只写凭证名，转发插件文本前由 ``Redactor`` 脱敏。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

__all__ = [
    "InstanceFailure",
    "Level",
    "ProtocolFailure",
    "RoundFailure",
    "RpcError",
    "RunFailure",
    "Source",
    "rpc_error_name",
]

Level = Literal["protocol", "instance", "round", "item"]
Source = Literal["plugin", "validation", "prefix", "blob", "remote", "runner"]

#: §6.5 的错误码闭集与 JSON-RPC 标准码在报告中的名称（§7.4 ``code``）
_RPC_ERROR_NAMES: dict[int, str] = {
    -32700: "parse_error",
    -32600: "invalid_request",
    -32601: "method_not_found",
    -32602: "invalid_params",
    -32603: "internal_error",
    -32001: "cancelled",
    -32002: "unknown_handle",
    -32003: "source_failed",
    -32004: "version_unsupported",
    -32005: "invalid_config",
}


def rpc_error_name(code: int) -> str:
    """插件 JSON-RPC 错误码在报告中的名称；不在闭集内的码记为 ``unknown_error``（§7.4）。"""
    return _RPC_ERROR_NAMES.get(code, "unknown_error")


class RunFailure(Exception):
    """一次失败的完整描述（§7.4 ``failures`` 元素）；``retryable`` 含义同 §6.5。"""

    level: Level

    def __init__(
        self,
        code: str,
        message: str,
        *,
        source: Source = "runner",
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.source: Source = source
        self.retryable = retryable

    def redact(self, redactor: Callable[[str], str]) -> None:
        """就地脱敏 ``message``（其中可能有插件控制的文本，§4.3「不外泄」）。"""
        self.message = redactor(self.message)
        self.args = (self.message,)

    def to_report(self) -> dict[str, Any]:
        """轮报告 ``failures`` 元素（非条目级失败不带 URI）。"""
        return {
            "level": self.level,
            "source": self.source,
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.code!r}, {self.message!r}, source={self.source!r})"


class InstanceFailure(RunFailure):
    """实例级失败（§7.1）：不启动插件或终止插件，MUST NOT 自动重试。"""

    level: Level = "instance"


class ProtocolFailure(RunFailure):
    """协议级失败（§7.1）：分帧、JSON-RPC 或握手违例、消息超限；终止实例，本轮作废。"""

    level: Level = "protocol"

    def __init__(self, message: str) -> None:
        super().__init__("protocol_error", message)


class RoundFailure(RunFailure):
    """轮级失败（§7.1）：本轮作废，退避后重试。"""

    level: Level = "round"


class RpcError(Exception):
    """插件对某个请求返回的 JSON-RPC ``error`` 响应（§6.5）。

    它本身不决定层级：同一个码在 ``scan`` 上是轮级、在 ``read_blob`` 上是条目级，由调用方归类。
    ``retryable`` 取 ``data.retryable``（缺省或非布尔视为 false）；未知码一律不可重试（§6.5）。
    """

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(f"{code} {message}")
        self.code = code
        self.message = message
        self.data = data

    @property
    def name(self) -> str:
        return rpc_error_name(self.code)

    @property
    def retryable(self) -> bool:
        if self.name == "unknown_error" or not isinstance(self.data, dict):
            return False
        return self.data.get("retryable") is True
