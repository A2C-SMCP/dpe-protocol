"""公开异常：调用方按类型或 ``code`` 分派，不解析消息字符串。

``code`` 取规范错误码（core.md §6），服务端可直接映射为 problem 体；``path`` 是出错位置的
RFC 6901 JSON Pointer，相对于传入函数的那个对象（如 ``/pages/0/elements/2/text_as_html``），
出错位置就是对象本身时为空串。
"""

from __future__ import annotations

from typing import ClassVar

__all__ = [
    "CategoryUnknownError",
    "ContractUnsupportedError",
    "DpeHashError",
    "FileTypeUnknownError",
    "IntegerOutOfRangeError",
    "InvalidUnicodeError",
    "UndefinedFieldError",
    "ValidationError",
]


class DpeHashError(ValueError):
    """dpe-hash 全部异常的共同基类。"""

    code: ClassVar[str]

    def __init__(self, message: str, path: str = "") -> None:
        super().__init__(f"{message}（位置 {path}）" if path else message)
        self.message = message
        self.path = path


class ValidationError(DpeHashError):
    """输入不合法（``DPE_VALIDATION``）：类型错误、非法 hash / blob 引用、契约混用等。"""

    code = "DPE_VALIDATION"


class UndefinedFieldError(ValidationError):
    """封闭 schema 违例：对象出现规范未定义（或其 category 未允许）的字段（core.md §2）。"""


class FileTypeUnknownError(ValidationError):
    """file_type 不在封闭枚举内（core.md §2.5）。"""


class InvalidUnicodeError(ValidationError):
    """报文不是 I-JSON：字符串或对象键含孤立代理项（core §2.8 第 0 步，先于其他所有校验）。"""


class IntegerOutOfRangeError(ValidationError):
    """整数字面量绝对值超过 2^53−1（契约 1 §3.3、core.md §2.6）。"""


class CategoryUnknownError(DpeHashError):
    """category 不在契约的封闭枚举内（``DPE_CATEGORY_UNKNOWN``，契约 1 §4）。"""

    code = "DPE_CATEGORY_UNKNOWN"


class ContractUnsupportedError(DpeHashError):
    """hash 契约不被支持，或 hash 值缺少 / 带未知契约前缀（``DPE_CONTRACT_UNSUPPORTED``）。"""

    code = "DPE_CONTRACT_UNSUPPORTED"
