"""协议错误（core §6）：每个错误码一个异常类，客户端与参考服务端共用这一份定义。

- ``code`` 是规范错误码，``retryable`` 只表示「原样重试同一请求可能成功」（core §6）；SDK 的通用
  重试逻辑只依据它，需要改变请求才能恢复的错误一律为 ``False``。
- ``path`` 是违例位置（RFC 6901 JSON Pointer，core §2.8），只用于诊断，HTTP problem 体不携带。
- 按 problem 体的 ``code`` 分派（``from_problem`` / ``error_class``），MUST NOT 按 HTTP 状态码分派
  （HTTP 绑定 §3.2）。全部错误类的构造签名一致：``(message="", path="", *, 关键字参数)``，
  关键字参数有通用的 ``retry_after``（秒，来自 ``Retry-After``），以及各类自有的参数；
  不在 core §6 中的码构造为 ``UnknownCodeError``（``code`` 为必填关键字），不丢弃。

本地构造模型（``dpe_sdk.models``）时的校验错误仍是 dpe_hash 的 ``DpeHashError``；
``from_hash_error`` 把它转换为对应的协议错误（服务端返回、或客户端统一上报时使用）。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from dpe_hash import DpeHashError
from pydantic import ValidationError as PydanticValidationError

from dpe_sdk.wire import Missing

__all__ = [
    "AlreadyExistsError",
    "CategoryUnknownError",
    "ContractUnsupportedError",
    "DpeError",
    "ForbiddenError",
    "HashMismatchError",
    "MissingContentError",
    "NotFoundError",
    "PayloadTooLargeError",
    "PreconditionFailedError",
    "PreconditionRequiredError",
    "RateLimitedError",
    "SessionExpiredError",
    "UnavailableError",
    "UnknownCodeError",
    "ValidationError",
    "error_class",
    "from_hash_error",
    "from_problem",
]


class DpeError(Exception):
    """全部协议错误的共同基类。"""

    code: str
    #: 已知码以本地码表（core §6）为准，不采信 problem 体声明的值
    retryable: bool = False

    def __init__(
        self, message: str = "", path: str = "", *, retry_after: float | None = None
    ) -> None:
        super().__init__(message)
        self.message = message
        self.path = path
        #: ``Retry-After`` 换算成的秒数（HTTP-date 形式由传输层换算）；只在可重试时有意义
        self.retry_after = retry_after

    def __str__(self) -> str:
        # 按当前的 message / path 生成，位置在构造后被补全时也一致；未设 code 的子类不让 str 失败
        text = self.message or getattr(self, "code", type(self).__name__)
        return f"{text}（位置 {self.path}）" if self.path else text


class ValidationError(DpeError):
    """报文不合法（``DPE_VALIDATION``）。"""

    code = "DPE_VALIDATION"


class ContractUnsupportedError(DpeError):
    """hash 契约版本不被支持（``DPE_CONTRACT_UNSUPPORTED``）。"""

    code = "DPE_CONTRACT_UNSUPPORTED"


class CategoryUnknownError(DpeError):
    """category 不在契约的封闭枚举内（``DPE_CATEGORY_UNKNOWN``）。"""

    code = "DPE_CATEGORY_UNKNOWN"


class PreconditionRequiredError(DpeError):
    """写操作缺少 CAS 前置条件（``DPE_PRECONDITION_REQUIRED``）。"""

    code = "DPE_PRECONDITION_REQUIRED"


class PreconditionFailedError(DpeError):
    """当前 doc_hash 与 ``base_hash`` 不符（``DPE_PRECONDITION_FAILED``）。"""

    code = "DPE_PRECONDITION_FAILED"


class AlreadyExistsError(DpeError):
    """``if_absent`` 冲突或 move 目标已存在（``DPE_ALREADY_EXISTS``）。"""

    code = "DPE_ALREADY_EXISTS"


class NotFoundError(DpeError):
    """文档不存在，含已删除（``DPE_NOT_FOUND``）。"""

    code = "DPE_NOT_FOUND"


class SessionExpiredError(DpeError):
    """暂存会话不可用：过期、已消费、不存在、不属于调用者或该 file_uri，不区分原因（core §3.4）。"""

    code = "DPE_SESSION_EXPIRED"


class HashMismatchError(DpeError):
    """暂存上传的对象与路径中声明的 hash 不符（``DPE_HASH_MISMATCH``，仅 upload）。"""

    code = "DPE_HASH_MISMATCH"


class MissingContentError(DpeError):
    """commit 引用了去重范围内不可得的对象（``DPE_MISSING_CONTENT``）；``missing`` 按层给出清单。"""

    code = "DPE_MISSING_CONTENT"

    def __init__(
        self,
        message: str = "",
        path: str = "",
        *,
        missing: Missing | None = None,
        truncated: bool = False,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message or "提交引用的对象不可得", path, retry_after=retry_after)
        self.missing = missing if missing is not None else Missing()
        self.truncated = truncated


class PayloadTooLargeError(DpeError):
    """请求、元素对象、分块页对象或 blob 超限（``DPE_PAYLOAD_TOO_LARGE``）。"""

    code = "DPE_PAYLOAD_TOO_LARGE"


class ForbiddenError(DpeError):
    """无权限，含前缀授权与 force 权限（``DPE_FORBIDDEN``）。"""

    code = "DPE_FORBIDDEN"


class _RetryAfterError(DpeError):
    retryable = True


class RateLimitedError(_RetryAfterError):
    """限流（``DPE_RATE_LIMITED``），按 ``retry_after`` 秒后原样重试。"""

    code = "DPE_RATE_LIMITED"


class UnavailableError(_RetryAfterError):
    """服务端暂时不可用（``DPE_UNAVAILABLE``），按 ``retry_after`` 秒后原样重试。"""

    code = "DPE_UNAVAILABLE"


class UnknownCodeError(DpeError):
    """problem 体或校验错误带了 core §6 之外的码：保留原码与对方声明的 ``retryable``。"""

    def __init__(
        self,
        message: str = "",
        path: str = "",
        *,
        code: str,
        retryable: bool = False,
        retry_after: float | None = None,
    ) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(message, path, retry_after=retry_after)


_BY_CODE: dict[str, type[DpeError]] = {
    cls.code: cls
    for cls in (
        ValidationError,
        ContractUnsupportedError,
        CategoryUnknownError,
        PreconditionRequiredError,
        PreconditionFailedError,
        AlreadyExistsError,
        NotFoundError,
        SessionExpiredError,
        HashMismatchError,
        MissingContentError,
        PayloadTooLargeError,
        ForbiddenError,
        RateLimitedError,
        UnavailableError,
    )
}


def error_class(code: str) -> type[DpeError] | None:
    """规范错误码对应的异常类；不是 core §6 定义的码返回 ``None``。"""
    return _BY_CODE.get(code)


def from_hash_error(exc: DpeHashError, prefix: str = "") -> DpeError:
    """把 dpe_hash 的校验错误转换为同码的协议错误。

    ``prefix`` 是被校验对象在外层报文中的位置（RFC 6901），拼在原位置之前，如 ``/objects/3``。
    """
    path = prefix + exc.path
    cls = _BY_CODE.get(exc.code)
    if cls is None:
        return UnknownCodeError(exc.message, path, code=exc.code)
    return cls(exc.message, path)


def _missing(raw: object) -> tuple[Missing, bool]:
    """解析 ``missing``，返回清单与它是否可信。

    规范要求 ``DPE_MISSING_CONTENT`` 带缺失清单（core §3.3）：缺省、形状不对、三层全空都视为
    不可信（调用方不能据此认为「什么都不缺」）。清单只用于补传，不能因为它不合法就丢掉错误码。
    """
    if isinstance(raw, Mapping):
        try:
            missing = Missing.model_validate(raw)
        except PydanticValidationError:
            return Missing(), False
        return missing, bool(missing.pages or missing.content_hashes or missing.blobs)
    return Missing(), False


def _message(problem: Mapping[str, Any]) -> str:
    """第一个非空字符串的 ``detail`` / ``title``（RFC 9457 两者都是字符串）。"""
    for key in ("detail", "title"):
        value = problem.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def from_problem(problem: Mapping[str, Any], retry_after: float | None = None) -> DpeError:
    """由 RFC 9457 problem 体（HTTP 绑定 §5）构造协议错误，按 ``code`` 分派。

    - ``retry_after``：响应 ``Retry-After`` 头换算成的秒数，传给任何码（含未知码）；
    - 已知码的 ``retryable`` 以本地码表为准；未知码只采信 problem 体中为 ``true`` 的声明；
    - ``DPE_MISSING_CONTENT`` 的 ``missing`` / ``missing_truncated`` 解析为 ``MissingContentError``
      的属性；``missing`` 缺省、形状不对或三层全空时降级为空清单并置 ``truncated``，错误码不丢；
    - problem 体不是对象、``code`` 缺失或不是非空字符串时抛 ``ValueError``：这不是 DPE 的
      problem 体，由调用方按 HTTP 绑定 §3.2 自行判定。其余字段不合法都只降级，不抛异常。
    """
    if not isinstance(problem, Mapping):
        raise ValueError("problem 体不是 JSON 对象，不是 DPE 错误")
    code = problem.get("code")
    if not isinstance(code, str) or not code:
        raise ValueError("problem 体缺少非空字符串 code，不是 DPE 错误")
    message = _message(problem)
    cls = _BY_CODE.get(code)
    if cls is None:
        return UnknownCodeError(
            message,
            code=code,
            retryable=problem.get("retryable") is True,
            retry_after=retry_after,
        )
    if cls is MissingContentError:
        missing, well_formed = _missing(problem.get("missing"))
        return MissingContentError(
            message,
            missing=missing,
            truncated=problem.get("missing_truncated") is True or not well_formed,
            retry_after=retry_after,
        )
    return cls(message, retry_after=retry_after)
