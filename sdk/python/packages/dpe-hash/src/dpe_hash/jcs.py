"""RFC 8785 JCS 序列化（spec/hash-contract-1.md §3.3）。

按规范文本独立实现，与 ``scripts/gen_vectors.py`` 的一致由向量逐字节校验，而不是共享代码。

输入是 JSON 值：``None`` / ``bool`` / ``int`` / ``float`` / ``str``，对象为 ``Mapping``（键必须是
``str``），数组为 ``list`` 或 ``tuple``。其余类型、NaN / Infinity、超过 2^53−1 的整数、含孤立代理项
的字符串一律拒绝，异常带出错位置的 JSON Pointer。
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from json.encoder import encode_basestring
from typing import Any

from dpe_hash.errors import (
    DpeHashError,
    IntegerOutOfRangeError,
    InvalidUnicodeError,
    ValidationError,
)

__all__ = ["has_invalid_unicode", "jcs"]

_MAX_SAFE_INTEGER = 2**53 - 1
_SURROGATE = re.compile("[\ud800-\udfff]")


class _Invalid(Exception):
    """序列化中途的错误：逐层回溯时收集路径分量，最外层再转换为公开异常。"""

    def __init__(self, error: type[DpeHashError], message: str) -> None:
        self.error = error
        self.message = message
        self.segments: list[str | int] = []


def has_invalid_unicode(value: Any) -> bool:
    """I-JSON 检查：字符串或对象键中是否含孤立代理项（core §2.8 第 0 步）。"""
    if isinstance(value, str):
        return _SURROGATE.search(value) is not None
    if isinstance(value, Mapping):
        return any(
            (isinstance(k, str) and _SURROGATE.search(k) is not None) or has_invalid_unicode(v)
            for k, v in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(has_invalid_unicode(v) for v in value)
    return False


def utf16_key(key: str) -> bytes:
    return key.encode("utf-16-be", "surrogatepass")  # 大端字节序比较 = UTF-16 码元序


def pointer(*segments: str | int) -> str:
    """把路径分量拼成 RFC 6901 JSON Pointer。"""
    return "".join(
        "/" + (str(s) if isinstance(s, int) else s.replace("~", "~0").replace("/", "~1"))
        for s in segments
    )


def _number(x: int | float) -> str:
    """ECMAScript ``Number::toString``（base 10）。"""
    if isinstance(x, int):
        if abs(x) > _MAX_SAFE_INTEGER:
            raise _Invalid(IntegerOutOfRangeError, f"整数超出 ±(2^53−1)：{int.__repr__(x)}")
        return int.__repr__(x)  # 不走子类（如 IntEnum）的 __str__
    if math.isnan(x) or math.isinf(x):
        raise _Invalid(ValidationError, "JCS 不允许 NaN / Infinity")
    if x == 0:
        return "0"  # 含 -0.0
    sign = x < 0
    # float.__repr__ 即最短往返十进制（如 "0.1"、"1e-07"、"1.5e+22"）。直接解析该字符串，
    # 不经 Decimal：Decimal 运算受线程全局的 decimal 上下文（精度）影响，会静默改变 hash。
    mantissa, _, exponent = float.__repr__(abs(x)).partition("e")
    int_part, _, frac_part = mantissa.partition(".")
    digits = int_part + frac_part
    n = len(int_part) + int(exponent or "0")  # value = 0.digits × 10^n
    stripped = digits.lstrip("0")
    n -= len(digits) - len(stripped)
    s = stripped.rstrip("0")
    k = len(s)
    if k <= n <= 21:
        body = s + "0" * (n - k)
    elif 0 < n <= 21:
        body = s[:n] + "." + s[n:]
    elif -6 < n <= 0:
        body = "0." + "0" * (-n) + s
    else:
        mantissa = s[0] + ("." + s[1:] if k > 1 else "")
        e = n - 1
        body = f"{mantissa}e{'+' if e >= 0 else '-'}{abs(e)}"
    return ("-" if sign else "") + body


def _string(s: str) -> str:
    if _SURROGATE.search(s):
        raise _Invalid(InvalidUnicodeError, "字符串含孤立代理项，不是合法的 Unicode")
    # 标准库（ensure_ascii=False 时）的转义恰好是 JCS 要求的集合：
    # " \ \b \f \n \r \t 与其余 < 0x20 的小写 \u00XX，其他字符原样输出
    return encode_basestring(s)


def _serialize(value: Any, strip_nulls: bool) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return _string(value)
    if isinstance(value, (int, float)):
        return _number(value)
    if isinstance(value, (dict, Mapping)):  # dict 在前：常见情形免走 ABC 检查
        for key in value:
            if not isinstance(key, str):
                raise _Invalid(
                    ValidationError, f"对象的键必须是字符串，实际为 {type(key).__name__}"
                )
            if _SURROGATE.search(key):
                err = _Invalid(InvalidUnicodeError, "键含孤立代理项，不是合法的 Unicode")
                err.segments.append(key)
                raise err
        keys = sorted(
            (k for k, v in value.items() if not (strip_nulls and v is None)), key=utf16_key
        )
        parts = []
        for key in keys:
            try:
                parts.append(f"{_string(key)}:{_serialize(value[key], strip_nulls)}")
            except _Invalid as err:
                err.segments.append(key)
                raise
        return "{" + ",".join(parts) + "}"
    if isinstance(value, (list, tuple)):
        items = []
        for i, item in enumerate(value):  # 数组元素不受 null 删除影响
            try:
                items.append(_serialize(item, strip_nulls))
            except _Invalid as err:
                err.segments.append(i)
                raise
        return "[" + ",".join(items) + "]"
    raise _Invalid(ValidationError, f"不是 JSON 值：{type(value).__name__}")


def canonical(value: Any, *, strip_nulls: bool = False, at: str = "") -> str:
    """JCS 规范化字符串；``strip_nulls`` 为 True 时同时递归删除值为 null 的键（契约 1 §3.2）。

    ``at`` 是 ``value`` 自身的 JSON Pointer，用作异常路径的前缀。
    """
    try:
        return _serialize(value, strip_nulls)
    except _Invalid as err:
        raise err.error(err.message, at + pointer(*reversed(err.segments))) from None


def jcs(value: Any) -> str:
    """把 JSON 值序列化为 RFC 8785 规范化字符串（不做 null 键删除等任何 DPE 规范化）。"""
    return canonical(value)
