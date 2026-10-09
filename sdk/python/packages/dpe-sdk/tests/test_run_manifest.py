"""清单与实例配置校验（connector 契约 §4.1–§4.2）。"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from dpe_sdk.run import InstanceFailure, load_manifest, validate_config
from dpe_sdk.run.manifest import parse_manifest

# §4.1 的示例清单
EXAMPLE: dict[str, Any] = {
    "manifest_version": 1,
    "name": "git-connector",
    "version": "0.1.0",
    "description": "以 Git 仓库为数据源",
    "protocol_versions": ["dpe-connector/1"],
    "config_schema": {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {"repo": {"type": "string"}, "ref": {"type": "string", "default": "main"}},
        "required": ["repo"],
        "additionalProperties": False,
    },
    "secrets": [{"name": "GIT_TOKEN", "description": "读取私有仓库的访问令牌", "required": False}],
}


def with_(path: str, value: Any) -> dict[str, Any]:
    data = copy.deepcopy(EXAMPLE)
    *parents, last = path.split(".")
    target: Any = data
    for key in parents:
        target = target[int(key)] if isinstance(target, list) else target[key]
    if value is _DELETE:
        del target[last]
    elif isinstance(target, list):
        target[int(last)] = value
    else:
        target[last] = value
    return data


_DELETE = object()


def test_example_manifest_is_valid() -> None:
    manifest = parse_manifest(EXAMPLE)
    assert manifest.secret_names == {"GIT_TOKEN"}
    assert manifest.secrets[0].required is False


def test_secret_required_defaults_to_true() -> None:
    manifest = parse_manifest(with_("secrets", [{"name": "TOKEN"}]))
    assert manifest.secrets[0].required is True
    assert parse_manifest(with_("secrets", _DELETE)).secrets == ()


@pytest.mark.parametrize(
    "data",
    [
        with_("extra", 1),  # 封闭 schema
        with_("secrets.0.extra", 1),  # secrets 项同样封闭
        with_("manifest_version", 2),
        with_("manifest_version", True),
        with_("manifest_version", "1"),
        with_("name", ""),
        with_("version", 1),
        with_("protocol_versions", []),
        with_("protocol_versions", [1]),
        with_("description", None),
        with_("config_schema", _DELETE),
        with_("config_schema", {"type": "string"}),
        with_("config_schema", {"properties": {}}),
        with_("config_schema.$schema", "http://json-schema.org/draft-07/schema#"),
        with_("config_schema.properties", {"repo": {"type": 5}}),  # 元 schema 不通过
        with_("secrets.0.name", "git_token"),
        with_("secrets.0.name", "DPE_TOKEN"),
        with_("secrets.0.name", "PATH"),
        with_("secrets.0.name", "LC_TOKEN"),
        with_("secrets.0.name", "SYSTEMROOT"),  # Windows 基础环境，任何平台都拒绝
        with_("secrets.0.required", "yes"),
        with_("secrets", [{"name": "A"}, {"name": "A"}]),
    ],
)
def test_invalid_manifest(data: dict[str, Any]) -> None:
    with pytest.raises(InstanceFailure) as info:
        parse_manifest(data)
    assert (info.value.code, info.value.level) == ("manifest_invalid", "instance")


def test_load_manifest(tmp_path: Path) -> None:
    path = tmp_path / "dpe-connector.json"
    path.write_text(json.dumps(EXAMPLE), encoding="utf-8")
    assert load_manifest(path).name == "git-connector"


@pytest.mark.parametrize(
    "content",
    [b"{", b'{"name": "a", "name": "b"}', b"\xff\xfe", b'{"name": "\\ud800"}'],
)
def test_load_manifest_rejects_non_ijson(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "dpe-connector.json"
    path.write_bytes(content)
    with pytest.raises(InstanceFailure) as info:
        load_manifest(path)
    assert info.value.code == "manifest_invalid"


def test_load_manifest_missing_file(tmp_path: Path) -> None:
    with pytest.raises(InstanceFailure) as info:
        load_manifest(tmp_path / "absent.json")
    assert info.value.code == "manifest_invalid"


def test_config_validation() -> None:
    manifest = parse_manifest(EXAMPLE)
    config = {"repo": "https://example.com/r.git"}
    validate_config(manifest, config)
    assert config == {"repo": "https://example.com/r.git"}  # 原样，不填充 default
    for bad in ({}, {"repo": 1}, {"repo": "r", "token": "x"}):
        with pytest.raises(InstanceFailure) as info:
            validate_config(manifest, bad)
        assert info.value.code == "config_schema_violation"


def test_format_is_annotation_only() -> None:
    schema = {"type": "object", "properties": {"mail": {"type": "string", "format": "email"}}}
    validate_config(parse_manifest(with_("config_schema", schema)), {"mail": "not-an-email"})


def test_internal_ref_resolves() -> None:
    schema = {
        "type": "object",
        "$defs": {"name": {"type": "string"}},
        "properties": {"repo": {"$ref": "#/$defs/name"}},
    }
    manifest = parse_manifest(with_("config_schema", schema))
    validate_config(manifest, {"repo": "r"})
    with pytest.raises(InstanceFailure) as info:
        validate_config(manifest, {"repo": 1})
    assert info.value.code == "config_schema_violation"


_DRAFT = "https://json-schema.org/draft/2020-12/schema"


@pytest.mark.parametrize(
    "schema",
    [
        # 配置不触及该属性时也必须拒绝：清单合法性只取决于清单本身
        {"type": "object", "properties": {"repo": {"$ref": "https://example.com/s.json"}}},
        {"type": "object", "properties": {"repo": {"$ref": "other.json#/x"}}},
        # 元模式 URI 同样是文档外引用（jsonschema 自带的元模式不算本文档）
        {"type": "object", "properties": {"repo": {"$ref": _DRAFT}}},
        {"type": "object", "properties": {"repo": {"$ref": "#/$defs/absent"}}},
        {"type": "object", "properties": {"repo": {"type": "string", "pattern": "\\p{L}"}}},
        {"type": "object", "patternProperties": {"(": {}}},
        {"type": "object", "properties": {"n": {"maximum": 1e400}}},
    ],
)
def test_schema_must_be_self_contained(schema: dict[str, Any]) -> None:
    with pytest.raises(InstanceFailure) as info:
        parse_manifest(with_("config_schema", schema))
    assert info.value.code == "manifest_invalid"


def test_embedded_resources_resolve() -> None:
    schema = {
        "type": "object",
        "$defs": {"name": {"$id": "urn:example:name", "type": "string", "minLength": 2}},
        "properties": {"a": {"$ref": "urn:example:name"}, "b": {"$ref": "#/$defs/name"}},
    }
    manifest = parse_manifest(with_("config_schema", schema))
    validate_config(manifest, {"a": "ok", "b": "ok"})
    with pytest.raises(InstanceFailure) as info:
        validate_config(manifest, {"a": "x"})
    assert info.value.code == "config_schema_violation"


def test_schema_format_is_not_checked() -> None:
    # 元模式中 pattern 带 format: regex；不启用 format 校验时 schema 本身也不按 format 判定
    schema = {"type": "object", "properties": {"d": {"type": "string", "format": "unknown"}}}
    parse_manifest(with_("config_schema", schema))


def test_load_manifest_rejects_out_of_range_numbers(tmp_path: Path) -> None:
    path = tmp_path / "dpe-connector.json"
    path.write_text(json.dumps(EXAMPLE).replace('"version": "0.1.0"', '"version": 1e400'))
    with pytest.raises(InstanceFailure) as info:
        load_manifest(path)
    assert info.value.code == "manifest_invalid"


def test_deeply_nested_schema_and_config_are_classified() -> None:
    deep: dict[str, Any] = {"type": "object"}
    for _ in range(3000):
        deep = {"type": "object", "properties": {"a": deep}}
    with pytest.raises(InstanceFailure) as info:
        parse_manifest(with_("config_schema", deep))
    assert info.value.code == "manifest_invalid"

    nested: dict[str, Any] = {"type": "object", "properties": {"a": {"$ref": "#"}}}
    manifest = parse_manifest(with_("config_schema", nested))
    config: dict[str, Any] = {}
    for _ in range(3000):
        config = {"a": config}
    with pytest.raises(InstanceFailure) as info:
        validate_config(manifest, config)
    assert info.value.code == "config_schema_violation"
