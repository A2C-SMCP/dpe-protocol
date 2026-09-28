"""dpe-push/1 错误模型（push-protocol-v1 §7）以及客户端本地校验错误。"""

from enum import StrEnum
from typing import Any


class DPEErrorCode(StrEnum):
    MANIFEST_INVALID = "DPE_MANIFEST_INVALID"
    CONTENT_HASH_MISMATCH = "DPE_CONTENT_HASH_MISMATCH"
    HASH_STRATEGY_UNSUPPORTED = "DPE_HASH_STRATEGY_UNSUPPORTED"
    INSUFFICIENT_CONTENT = "DPE_INSUFFICIENT_CONTENT"
    PAYLOAD_TOO_LARGE = "DPE_PAYLOAD_TOO_LARGE"
    UNSUPPORTED_PROTOCOL_VERSION = "DPE_UNSUPPORTED_PROTOCOL_VERSION"
    RATE_LIMITED = "DPE_RATE_LIMITED"
    ROBOT_UNAVAILABLE = "DPE_ROBOT_UNAVAILABLE"


RETRYABLE_CODES = frozenset({DPEErrorCode.RATE_LIMITED, DPEErrorCode.ROBOT_UNAVAILABLE})


class DPEError(Exception):
    """本 SDK 全部异常的根。"""


class DPEPushError(DPEError):
    """服务端返回的协议错误。``code`` 可能是协议外的值（如网关错误），故保留原始字符串。"""

    def __init__(
        self, code: str, message: str, *, http_status: int, data: Any = None, retry_after: float | None = None
    ) -> None:
        super().__init__(f"[{http_status} {code}] {message}")
        self.code = code
        self.message = message
        self.http_status = http_status
        self.data = data
        self.retry_after = retry_after

    @property
    def retryable(self) -> bool:
        return self.code in RETRYABLE_CODES


class DPEValidationError(DPEError):
    """投递前的本地校验失败：文档不满足 dpe-push/1 的约束，发出去也会被拒绝。"""

    def __init__(self, violations: list[str]) -> None:
        super().__init__("; ".join(violations))
        self.violations = violations


class NoCompatibleHashStrategyError(DPEError):
    """SDK 支持的 hash 策略与服务端 allowlist 无交集。"""
