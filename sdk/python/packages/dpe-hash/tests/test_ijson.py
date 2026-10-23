"""``parse_ijson``（core §2.8 第 0 步的解析部分）与 0.4.0 语义的保留。

与 Rust 的 ``sdk/rust/crates/dpe-hash/tests/ijson.rs``（``v0_4_semantics_preserved``）使用同一组
输入，两侧判定一致；仅 Python 可构造的 bytes / 原始代理项 / 无显式深度上限用例只在本文件。
"""

from __future__ import annotations

import json
import math
from typing import Any

import dpe_hash
import pytest
from dpe_hash import (
    InvalidUnicodeError,
    ValidationError,
    parse_ijson,
)

V = "DPE_VALIDATION"

#: 与 Rust ``tests/ijson.rs`` 的 ``v0_4_semantics_preserved`` 保持同一组输入
#: （值为期望的异常类型：孤立代理项必须与其余违例区分，且都是 ``ValidationError`` 的子类）
V0_4_REJECTED: list[tuple[str, type[ValidationError]]] = [
    # 重复键（任意深度）
    ('{"category": "Title", "category": "Video"}', ValidationError),
    ('{"a": {"b": 1, "b": 2}}', ValidationError),
    ('[{"a": 1, "a": 1}]', ValidationError),
    # 非 JSON 数值字面量
    ('{"x": NaN}', ValidationError),
    ('{"x": Infinity}', ValidationError),
    ('{"x": -Infinity}', ValidationError),
    # 坏 JSON：结构、转义、控制字符
    ('{"category": "Title"', ValidationError),
    ('{"a": 1,}', ValidationError),
    ("[1,]", ValidationError),
    ('{"a" 1}', ValidationError),
    ("{a: 1}", ValidationError),
    ("[1 2]", ValidationError),
    ("{,}", ValidationError),
    ("", ValidationError),
    ("   ", ValidationError),
    ("1 2", ValidationError),
    ("truex", ValidationError),
    ('{"a":}', ValidationError),
    ("{", ValidationError),
    ('{"a": "\\x"}', ValidationError),
    ('{"a": "\\u12G4"}', ValidationError),
    ('{"a": "\\u123"}', ValidationError),
    # 孤立代理项在解析阶段以 InvalidUnicodeError 拒绝
    ('{"a": "\\uD834"}', InvalidUnicodeError),
    ('{"a": "\\uDD1E"}', InvalidUnicodeError),
    ('{"a": "\\uD834A"}', InvalidUnicodeError),
    ('{"a": "\t"}', ValidationError),
    ('{"a": "\x00"}', ValidationError),
    # 数字语法
    ("01", ValidationError),
    ("-", ValidationError),
    ("+1", ValidationError),
    (".5", ValidationError),
    ("1.", ValidationError),
    ("1e", ValidationError),
    ("1e+", ValidationError),
    ("- 1", ValidationError),
    ("0x1", ValidationError),
    ("1_000", ValidationError),
]

#: 与 Rust 同一组输入
V0_4_ACCEPTED = [
    '{"a": [1, 2.5, "x", null, true], "b": {"c": []}, "d": 9007199254740991}',
    '{"s": "中文 emoji 😀 A 𝄞 \\" \\\\ \\/ \\b \\f \\n \\r \\t"}',
    '{"n": [-0, 0.5, 1e2, 1E+2, 1e-2, 1.0, 0.1]}',
    "[]",
    "{}",
    "[[[[[1]]]]]",
    '{"k": "\x7f\U0010ffff"}',
    '{"a": "\\u0000"}',
    '{"𝄞": "𝄞"}',
    '{"a": "𝄞 x\\ty", "b": "A"}',
    "0",
    "-0",
    "0.5",
    "1e2",
    "1E+2",
    "1e-2",
    "9007199254740993",
    "1e400",
]


@pytest.mark.parametrize(("text", "error"), V0_4_REJECTED)
def test_v0_4_semantics_preserved_rejects(text: str, error: type[ValidationError]) -> None:
    """0.4.0 的拒绝语义原样保留：异常类型精确对应（孤立代理项为 ``InvalidUnicodeError``），
    错误码 ``DPE_VALIDATION``、位置 ""。"""
    with pytest.raises(error) as info:
        parse_ijson(text)
    assert type(info.value) is error
    assert info.value.code == V
    assert info.value.path == ""


@pytest.mark.parametrize("text", V0_4_ACCEPTED)
def test_v0_4_semantics_preserved_accepts(text: str) -> None:
    """0.4.0 的接受语义原样保留：合法 JSON 的解析结果与标准库一致。"""
    assert parse_ijson(text) == json.loads(text)


def test_v0_4_semantics_preserved_bytes_and_numbers() -> None:
    """bytes 入口、非 UTF-8 拒绝、数值越界不在解析阶段拒绝（换成哨兵 / ``inf``）。"""
    assert parse_ijson(b'{"a": 1}') == {"a": 1}
    assert parse_ijson(bytearray(b'{"a": 1}')) == {"a": 1}
    with pytest.raises(ValidationError) as info:
        parse_ijson(b'{"a": "\xff"}')
    assert (info.value.code, info.value.path) == (V, "")

    huge = "9" * 5000
    value = parse_ijson('{"n": ' + huge + "}")
    assert value == {"n": 2**53}  # 越界哨兵，交给对象校验在原位置判定
    assert math.isinf(parse_ijson('{"n": 1e400}')["n"])


def test_lone_surrogate_rejected_at_parse_phase() -> None:
    """唯一的语义变化：孤立代理项在解析阶段对整个报文拒绝（含非 DPE 字段与对象键）。"""
    raw = '{"bogus": "' + "\ud800" + '"}'
    for data in (
        '{"category": "Title", "text": "\\ud800"}',
        '{"category": "Title", "metadata": {"\\udc00": 1}}',
        '{"bogus": "\\ud800"}',
        raw,
        b'{"bogus": "\\ud800"}',
    ):
        with pytest.raises(InvalidUnicodeError) as info:
            parse_ijson(data)
        assert info.value.code == V
        assert info.value.path == ""


def test_public_export() -> None:
    """公开面：``from dpe_hash import parse_ijson``。"""
    assert dpe_hash.parse_ijson is parse_ijson
    assert "parse_ijson" in dpe_hash.__all__


def test_no_explicit_depth_limit() -> None:
    """不照抄 Rust 的 68 层防护：更深的合法 JSON 仍可解析（core §2.8 只要求不低于 64 + 信封）。"""
    for levels in (69, 200):
        text = "[" * levels + "1" + "]" * levels
        value: Any = parse_ijson(text)
        assert isinstance(value, list)
