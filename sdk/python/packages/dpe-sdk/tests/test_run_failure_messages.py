"""校验失败消息不回显输入值（connector 契约 §4.3「不外泄」，#156）。

消息会进入日志与 §7.4 轮报告：只写位置（JSON Pointer、成员名、下标）与约束（关键字及其参数），
不写取值；未通过校验的串不当作名字写出。

- 实例配置：逐例消费两个 SDK 共用的 ``sdk/testdata/config_failure_messages.json``（Rust 侧
  ``tests/manifest.rs`` 同表）——同一措辞、同一不回显口径；
- 清单：对照 Rust ``tests/manifest.rs::failure_messages_do_not_echo_input_values``（#114）。

每例既断言哨兵不出现，也断言消息含预期片段——确认走到了目标分支，避免日后被更早的校验拦下而
同义反复地通过。
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from dpe_sdk.run import InstanceFailure
from dpe_sdk.run.manifest import parse_manifest, validate_config

TABLE = json.loads(
    (Path(__file__).resolve().parents[4] / "testdata" / "config_failure_messages.json").read_text(
        encoding="utf-8"
    )
)
SENTINELS: list[str] = TABLE["sentinels"]
SENTINEL = "s3cret-sentinel"

EXAMPLE: dict[str, Any] = {
    "manifest_version": 1,
    "name": "git-connector",
    "version": "0.1.0",
    "protocol_versions": ["dpe-connector/1"],
    "config_schema": {"type": "object"},
}


def with_schema(schema: Any) -> dict[str, Any]:
    data = copy.deepcopy(EXAMPLE)
    data["config_schema"] = schema
    return data


def assert_no_echo(message: str) -> None:
    for sentinel in [*SENTINELS, "S3CRET"]:  # 凭证名用例的哨兵须全大写才能通过名字格式
        assert sentinel not in message, f"回显了输入值：{message}"


@pytest.mark.parametrize("case", TABLE["cases"], ids=[c["name"] for c in TABLE["cases"]])
def test_config_failure_messages(case: dict[str, Any]) -> None:
    manifest = parse_manifest(with_schema(case["schema"]))
    with pytest.raises(InstanceFailure) as info:
        validate_config(manifest, case["config"])
    assert info.value.code == "config_schema_violation"
    message = str(info.value)
    assert case["expected"] in message, f"走到了别的分支：{message}"
    assert_no_echo(message)


def _schema_member(key: str, value: Any) -> dict[str, Any]:
    return with_schema({"type": "object", "properties": {"repo": {"type": "string", key: value}}})


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (_schema_member("$ref", f"#/{SENTINEL}"), "$ref 形式不合法"),
        (_schema_member("$ref", f"#/%zz{SENTINEL}"), "$ref 形式不合法"),
        (_schema_member("$ref", f"#/$defs/{SENTINEL}"), "$ref 的目标不存在"),
        (_schema_member("pattern", f"{SENTINEL}(?=x)"), "pattern 超出 §4.1.1 子集（"),
        (
            with_schema(
                {"type": "object", "$schema": f"https://example.com/{SENTINEL}"},
            ),
            "$schema 必须缺省或为",
        ),
        (
            with_schema(
                {
                    "type": "object",
                    "properties": {"r": {"$schema": f"https://example.com/{SENTINEL}"}},
                }
            ),
            "$schema",
        ),
        # draft 2020-12 元校验：jsonschema 的英文消息会内嵌取值，现只报位置与关键字
        (_schema_member("minLength", SENTINEL), "不是合法的 draft 2020-12 schema（"),
        ({**EXAMPLE, "manifest_version": 31337}, "manifest_version 只能为 1"),
        ({**EXAMPLE, "description": {"token": SENTINEL}}, "description"),
        ({**EXAMPLE, "secrets": [{"name": f"ghp_{SENTINEL}"}]}, "凭证名须匹配"),
        ({**EXAMPLE, "secrets": [{"name": "DPE_S3CRET_TOKEN"}]}, "凭证名不得以 DPE_ 开头"),
        ({**EXAMPLE, "secrets": [{"name": "PATH"}]}, "凭证名不得与基础环境变量同名"),
    ],
)
def test_manifest_failure_messages(data: dict[str, Any], expected: str) -> None:
    with pytest.raises(InstanceFailure) as info:
        parse_manifest(data)
    assert info.value.code == "manifest_invalid"
    message = str(info.value)
    assert expected in message, f"走到了别的分支：{message}"
    assert_no_echo(message)


def test_pattern_properties_key_is_a_position() -> None:
    """``patternProperties`` 的键属于 schema 结构（位置），写出；超出子集时只报原因标记。"""
    schema = {"type": "object", "patternProperties": {"^a\\q": {"type": "string"}}}
    with pytest.raises(InstanceFailure) as info:
        parse_manifest(with_schema(schema))
    message = str(info.value)
    assert "patternProperties: 键" in message and "unknown_escape" in message, message
