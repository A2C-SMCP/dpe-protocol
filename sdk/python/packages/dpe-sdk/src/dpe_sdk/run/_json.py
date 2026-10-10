"""运行器读取的 JSON 载体（清单、实例定义、线协议报文）共用的 I-JSON 检查与封闭模型基类。"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from dpe_hash import ValidationError
from pydantic import BaseModel, ConfigDict, model_validator

from dpe_sdk import _ijson

__all__ = [
    "MAX_JSON_DEPTH",
    "ClosedModel",
    "has_lone_surrogate",
    "has_numeric_violation",
    "json_depth",
    "load_json_file",
]

#: §4.1 / §4.4：清单与实例定义文件的 JSON 嵌套深度上限
MAX_JSON_DEPTH = 64

#: core §2.6：整数绝对值上限（与 JCS 一致）
_MAX_SAFE_INTEGER = 2**53 - 1


def has_lone_surrogate(value: Any) -> bool:
    """值中是否有含孤立代理项的字符串（含对象键）；I-JSON 禁止（RFC 7493 §2.1）。

    ``dpe_sdk._ijson`` 把孤立代理项留给 dpe_hash 在 core 校验时判定，这里的载体不经 core 校验，
    须自行检查。
    """
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            try:
                item.encode("utf-8")
            except UnicodeEncodeError:
                return True
        elif isinstance(item, dict):
            stack.extend(item.keys())
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return False


def has_numeric_violation(value: Any) -> bool:
    """值中是否有越界数值（core §2.6）：整数绝对值超过 2^53−1，或超出 double 范围的数（如
    ``1e400``）。``dpe_sdk._ijson`` 在解析阶段不拒绝它们（超长整数解析为越界哨兵、``1e400`` 解析为
    ``inf``），不经 core 校验的载体须自行检查，否则配置会被静默改写或在编码时才失败。"""
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, bool):
            continue
        if isinstance(item, int):
            if abs(item) > _MAX_SAFE_INTEGER:
                return True
        elif isinstance(item, float):
            if not math.isfinite(item):
                return True
        elif isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return False


def json_depth(value: Any) -> int:
    """JSON 值的嵌套深度（对象与数组各计一层，标量计 1；迭代实现，不受递归限额影响）。

    §4.1 / §4.4：清单与实例定义文件的嵌套深度 MUST ≤ 64——上界使合法文件在各实现的默认 JSON
    解析限额（如 serde_json 的递归限额 128）内也能被读取。
    """
    depth = 0
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        item, level = stack.pop()
        depth = max(depth, level)
        if isinstance(item, dict):
            stack.extend((child, level + 1) for child in item.values())
        elif isinstance(item, list):
            stack.extend((child, level + 1) for child in item)
    return depth


def load_json_file(path: Path) -> Any:
    """读取 UTF-8 I-JSON 文件；读不到或不合法时抛 ``ValueError``（消息不含文件内容）。"""
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"无法读取 {path}：{exc.strerror or exc}") from None
    try:
        value = _ijson.loads(data)
    except ValidationError as exc:
        raise ValueError(f"{path} 不是合法的 I-JSON：{exc}") from None
    if has_lone_surrogate(value):
        raise ValueError(f"{path} 含孤立代理项，不是 I-JSON")
    if has_numeric_violation(value):
        raise ValueError(f"{path} 含越界数值（整数绝对值超过 2^53−1 或超出 double 范围）")
    return value


class ClosedModel(BaseModel):
    """清单与实例定义的封闭对象：未定义成员即拒绝；没有任何成员以 ``null`` 为合法值——可选成员
    只能缺省，显式的 ``null`` 是类型不符（§4.1、§4.4）。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    @model_validator(mode="before")
    @classmethod
    def _reject_null(cls, data: Any) -> Any:
        if isinstance(data, dict):
            for key, value in data.items():
                if value is None:
                    raise ValueError(f"{key} 不得为 null")
        return data
