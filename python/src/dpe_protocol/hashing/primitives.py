"""hash-contract-v1 §2 / §6 通用原语。"""

import hashlib
import json
from typing import Any


def concat_parts(parts: list[bytes]) -> bytes:
    """长度前缀拼接：每个 part 前置 4 字节大端无符号长度，消除 ``["ab","c"]`` / ``["a","bc"]`` 歧义。"""
    return b"".join(len(p).to_bytes(4, "big") + p for p in parts)


def digest(data: bytes) -> str:
    """``sha256`` 十六进制摘要的前 32 个字符（128 位）。"""
    return hashlib.sha256(data).hexdigest()[:32]


def text_bytes(value: str | None) -> bytes:
    """``None`` → 空串，再以 UTF-8 编码。禁止 ``str(value)`` 式隐式字符串化（会产出 ``"None"``）。"""
    return (value or "").encode("utf-8")


def json_stable(value: Any) -> str:
    """稳定 JSON：键按 Unicode 码点递归排序、分隔符 ``", "`` / ``": "``（带空格）、非 ASCII 不转义。

    这里显式写出 ``separators``，即使它等于 Python 默认值，以免被误「优化」为紧凑格式。
    """
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(", ", ": "))
