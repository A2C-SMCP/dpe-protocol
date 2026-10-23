"""I-JSON 严格解析（RFC 7493；core §2.8 第 0 步的解析阶段）。

标准库 ``json`` 默认放行重复键（后写覆盖）、``NaN`` / ``Infinity`` 字面量与孤立代理项，
这里把三者都在解析阶段拒绝（孤立代理项由解析后的整值扫描判定）。数值不在解析阶段拒绝
（core §2.8）：超出 ``int`` 字符串位数上限的超长整数返回越界哨兵，其余越界整数按原值保留，
``1e400`` 解析为 ``inf``，统一由 dpe_hash 在出错的值上报告。

与 Rust 的 ``dpe_hash::parse_ijson`` 行为对等；解析深度不设显式上限，Python 由标准库的递归
上限决定（core §2.8 只要求不低于「64 + 信封层数」）。
"""

from __future__ import annotations

import json
from typing import Any

from dpe_hash.errors import InvalidUnicodeError, ValidationError
from dpe_hash.jcs import has_invalid_unicode

__all__ = ["parse_ijson"]


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


def parse_ijson(data: str | bytes | bytearray) -> Any:
    """把 JSON 文本严格解析为值（core §2.8 第 0 步的解析部分）。

    拒绝重复键、``NaN`` / ``Infinity`` / ``-Infinity`` 字面量与孤立代理项；不是 UTF-8 编码的
    合法 JSON 同样拒绝。全部违例抛 ``ValidationError`` 或其子类（``DPE_VALIDATION``），位置为
    对象自身（空串）。数值越界不在解析阶段拒绝：超出 ``int`` 字符串位数上限的超长整数返回
    越界哨兵，其余越界整数按原值保留，``1e400`` 解析为 ``inf``，交给对象校验在出错的值上报告。
    """
    try:
        text = data if isinstance(data, str) else bytes(data).decode("utf-8")
        value = json.loads(
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
    if has_invalid_unicode(value):
        raise InvalidUnicodeError("报文不是 I-JSON：字符串或对象键含孤立代理项")
    return value
