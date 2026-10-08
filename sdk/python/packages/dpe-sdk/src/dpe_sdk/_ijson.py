"""严格的 I-JSON 解析（RFC 7493；core §2.8 第 0 步的解析阶段）。

标准库 ``json`` 默认放行重复键（后写覆盖）与 ``NaN`` / ``Infinity`` 字面量，这里把两者都拒绝；
孤立代理项照常解析成字符串，由 dpe_hash 在校验第 0 步统一判定，位置同样是对象自身。
数值不在解析阶段拒绝（core §2.8）：超大整数解析为 ``int``、``1e400`` 解析为 ``inf``，
由 dpe_hash 在出错的值上报告。
"""

from __future__ import annotations

import json
from typing import Any

from dpe_hash import ValidationError

__all__ = ["loads"]


class _NotIJson(Exception):
    """解析中途发现的 I-JSON 违例（从 ``json`` 的回调里抛出，最外层转换为公开异常）。"""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    obj = dict(pairs)
    if len(obj) != len(pairs):
        raise _NotIJson("同一对象内有重复的键")
    return obj


def _reject_constant(name: str) -> Any:
    raise _NotIJson(f"{name} 不是 JSON 数值")


#: 不少于 17 位的整数必然超出 ±(2^53−1)，同号的 2^53 足以让 dpe_hash 在原位置判定越界
_OUT_OF_RANGE = 2**53


def _parse_int(literal: str) -> int:
    # Python 3.11+ 的 int() 对超过 sys.get_int_max_str_digits() 位的串抛 ValueError；
    # 不能因此在解析阶段拒绝（core §2.8），换成必然越界的哨兵值交给 dpe_hash 判定
    try:
        return int(literal)
    except ValueError:
        return -_OUT_OF_RANGE if literal.startswith("-") else _OUT_OF_RANGE


def loads(data: str | bytes | bytearray) -> Any:
    """把报文解析为 JSON 值；不是 UTF-8 编码的合法 JSON、或违反 I-JSON 时抛 ``ValidationError``
    （``DPE_VALIDATION``，位置为对象自身）。"""
    try:
        text = data if isinstance(data, str) else bytes(data).decode("utf-8")
        return json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
            parse_int=_parse_int,
        )
    except _NotIJson as exc:
        raise ValidationError(f"报文不是 I-JSON：{exc}") from None
    # JSONDecodeError / UnicodeDecodeError 均为 ValueError；嵌套过深时 json 抛 RecursionError
    except (ValueError, RecursionError) as exc:
        raise ValidationError(f"报文不是合法的 JSON：{exc}") from None
