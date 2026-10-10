"""file_type 的语法校验（core.md §2.5）：唯一实现，内核与各服务端实现直接引用。

file_type 是开放取值（推荐登记表见 ``RECOMMENDED_FILE_TYPES``）：协议对取值的唯一约束是语法
``^[a-z0-9][a-z0-9_]{0,31}$``——ASCII 小写字母、数字与下划线，首字符是字母或数字，长度不超过
32。接收方 MUST NOT 因取值未登记而拒收；语法校验因此与推荐表无关，未登记的取值同样合法。
"""

from __future__ import annotations

import re

from dpe_hash.errors import FileTypeInvalidError

__all__ = ["is_valid_file_type", "validate_file_type"]

#: 语法（core.md §2.5）；全串匹配见 ``is_valid_file_type``。
_FILE_TYPE = re.compile(r"[a-z0-9][a-z0-9_]{0,31}")


def is_valid_file_type(value: object) -> bool:
    """``value`` 是否满足 core.md §2.5 的 file_type 语法；不抛异常（非字符串为 ``False``）。

    只看语法，不看推荐登记表：未登记的取值同样是合法取值。
    """
    return isinstance(value, str) and _FILE_TYPE.fullmatch(value) is not None


def validate_file_type(value: object) -> None:
    """校验 core.md §2.5 的 file_type 语法；不合法抛 ``FileTypeInvalidError``。

    错误码为 ``DPE_VALIDATION``。入口形态供内核等写入路径直接引用：只按语法校验，
    不按取值集合拒收。
    """
    if not isinstance(value, str):
        raise FileTypeInvalidError(f"file_type 必须是字符串，实际为 {type(value).__name__}")
    if not is_valid_file_type(value):
        raise FileTypeInvalidError(f"file_type 不合语法：{value!r}")
