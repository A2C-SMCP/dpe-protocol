"""把 JSON 值（含 Pydantic 模型）迭代地转成纯 Python 值。

模型的校验入口在校验前需要一份与调用方隔离的普通 JSON 值；本模块用显式栈完成这步深拷贝，
不递归，深度只受内存限制。深度口径不在这里定义：core §2.8 第 0 步由 dpe-hash 的迭代校验判定。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from dpe_hash import ValidationError
from pydantic import BaseModel

__all__ = ["plain"]


def _leaf(value: Any) -> Any:
    """BaseModel 先取 ``model_dump``；其余值原样。"""
    return value.model_dump() if isinstance(value, BaseModel) else value


def _is_container(value: Any) -> bool:
    return isinstance(value, (Mapping, list, tuple))


def _new_container(value: Any) -> dict[str, Any] | list[Any]:
    return {} if isinstance(value, Mapping) else []


def plain(value: Any) -> Any:
    """迭代深拷贝为纯 JSON 值（BaseModel 经 ``model_dump``），不递归。

    输入须为无环的 JSON 值；环状引用（无法由 JSON 文本产生）抛 ``ValidationError``
    （``DPE_VALIDATION``），而不是像递归实现那样耗尽解释器栈或无限循环。
    """
    root_value = _leaf(value)
    if not _is_container(root_value):
        return root_value
    root = _new_container(root_value)
    stack: list[tuple[Any, Any, bool]] = [(root_value, root, False)]
    active: set[int] = set()

    def child_container(item: Any) -> Any:
        if _is_container(item) and id(item) in active:
            raise ValidationError("输入含循环引用，不是 JSON 值")
        return item

    while stack:
        source, target, leaving = stack.pop()
        if leaving:
            active.discard(id(source))
            continue
        active.add(id(source))
        stack.append((source, target, True))
        if isinstance(source, Mapping):
            for key, item in source.items():
                child = child_container(_leaf(item))
                if _is_container(child):
                    new = _new_container(child)
                    target[key] = new
                    stack.append((child, new, False))
                else:
                    target[key] = child
        else:
            for item in source:
                child = child_container(_leaf(item))
                if _is_container(child):
                    new = _new_container(child)
                    target.append(new)
                    stack.append((child, new, False))
                else:
                    target.append(child)
    return root
