"""运行器读取的 JSON 载体（清单、实例定义、线协议报文）共用的 I-JSON 检查与封闭模型基类。"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from dpe_hash import ValidationError, parse_ijson
from pydantic import BaseModel, ConfigDict, model_validator

__all__ = [
    "ClosedModel",
    "has_numeric_violation",
    "load_json_file",
]

#: core §2.6：整数绝对值上限（与 JCS 一致）
_MAX_SAFE_INTEGER = 2**53 - 1


def has_numeric_violation(value: Any) -> bool:
    """值中是否有越界数值（core §2.6）：整数绝对值超过 2^53−1，或超出 double 范围的数（如
    ``1e400``）。``dpe_hash.parse_ijson`` 在解析阶段不拒绝它们（超长整数解析为越界哨兵、
    ``1e400`` 解析为 ``inf``），不经 core 校验的载体须自行检查，否则配置会被静默改写或在编码
    时才失败。"""
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


def load_json_file(path: Path) -> Any:
    """读取 UTF-8 I-JSON 文件；读不到或不合法时抛 ``ValueError``（消息不含文件内容）。"""
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"无法读取 {path}：{exc.strerror or exc}") from None
    try:
        value = parse_ijson(data)
    except ValidationError as exc:
        raise ValueError(f"{path} 不是合法的 I-JSON：{exc}") from None
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
