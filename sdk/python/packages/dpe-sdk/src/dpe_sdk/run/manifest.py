"""插件清单与实例配置校验（connector 契约 §4.1–§4.2）。

清单是静态声明，不启动插件即可读取。``config_schema`` 按 draft 2020-12 语义校验实例配置，且
MUST 自包含：读取清单时就静态解析 schema 中的每个 ``$ref`` / ``$dynamicRef``——只在 schema
文档自身（含其中以 ``$id`` 标识的嵌入资源）内查找，外部引用与元模式 URI 一律判清单不合法，
从不访问网络或文件。因此清单是否合法只取决于清单本身，与配置内容无关。``pattern`` 按本实现的
正则方言（Python ``re``）在读取时编译，无法编译同样判清单不合法。不启用 ``format`` 校验（只作
注解）。配置原样传给插件，不填充 ``default``、不做任何改写。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match
from pydantic import Field, field_validator
from pydantic import ValidationError as PydanticValidationError
from referencing import Registry, Resource
from referencing.exceptions import Unresolvable
from referencing.jsonschema import DRAFT202012

from dpe_sdk.run._json import ClosedModel, has_numeric_violation, load_json_file
from dpe_sdk.run.errors import InstanceFailure
from dpe_sdk.run.secrets import is_base_env_name

__all__ = [
    "MANIFEST_FILENAME",
    "Manifest",
    "SecretDeclaration",
    "load_manifest",
    "parse_manifest",
    "validate_config",
]

#: 清单的文件名（§4.1）；``plugin.manifest`` 指向的文件不强制此名
MANIFEST_FILENAME = "dpe-connector.json"

_DRAFT_2020_12 = "https://json-schema.org/draft/2020-12/schema"
_SECRET_NAME = re.compile(r"[A-Z][A-Z0-9_]*")
_META_VALIDATOR = Draft202012Validator(Draft202012Validator.META_SCHEMA)

_NonEmpty = Annotated[str, Field(strict=True, min_length=1)]


class SecretDeclaration(ClosedModel):
    """清单 ``secrets`` 的一项（§4.1）。"""

    name: Annotated[str, Field(strict=True)]
    description: Annotated[str, Field(strict=True)] | None = None
    required: Annotated[bool, Field(strict=True)] = True

    @field_validator("name")
    @classmethod
    def _check_name(cls, name: str) -> str:
        if not _SECRET_NAME.fullmatch(name):
            raise ValueError("凭证名须匹配 ^[A-Z][A-Z0-9_]*$")
        if name.startswith("DPE_"):
            raise ValueError("凭证名不得以 DPE_ 开头（保留给运行器）")
        if is_base_env_name(name):
            raise ValueError("凭证名不得与基础环境变量同名")
        return name


class Manifest(ClosedModel):
    """插件清单 ``dpe-connector.json``（§4.1），封闭 schema。"""

    manifest_version: Annotated[int, Field(strict=True)]
    name: _NonEmpty
    version: _NonEmpty
    description: Annotated[str, Field(strict=True)] | None = None
    protocol_versions: Annotated[list[Annotated[str, Field(strict=True)]], Field(min_length=1)]
    config_schema: dict[str, Any]
    secrets: tuple[SecretDeclaration, ...] = ()

    @field_validator("manifest_version")
    @classmethod
    def _check_version(cls, version: int) -> int:
        if version != 1:
            raise ValueError(f"不认识的 manifest_version {version}")
        return version

    @field_validator("config_schema")
    @classmethod
    def _check_schema(cls, schema: dict[str, Any]) -> dict[str, Any]:
        if has_numeric_violation(schema):
            raise ValueError("含越界数值（整数绝对值超过 2^53−1 或超出 double 范围）")
        if schema.get("type") != "object":
            raise ValueError('根必须为 "type": "object"')
        if schema.get("$schema", _DRAFT_2020_12) != _DRAFT_2020_12:
            raise ValueError(f"$schema 必须缺省或为 {_DRAFT_2020_12}")
        try:
            # 同 check_schema，但不启用 format 校验（format 只作注解，对 schema 本身同样如此）
            error = best_match(_META_VALIDATOR.iter_errors(schema))
            if error is not None:
                raise ValueError(f"不是合法的 draft 2020-12 schema：{error.message}")
            _check_self_contained(schema)
        except RecursionError:
            raise ValueError("嵌套过深，无法校验") from None
        return schema

    @field_validator("secrets")
    @classmethod
    def _unique_secrets(
        cls, secrets: tuple[SecretDeclaration, ...]
    ) -> tuple[SecretDeclaration, ...]:
        names = [s.name for s in secrets]
        if len(set(names)) != len(names):
            raise ValueError("凭证名在清单内必须唯一")
        return secrets

    @property
    def secret_names(self) -> frozenset[str]:
        return frozenset(s.name for s in self.secrets)


def _check_self_contained(schema: dict[str, Any]) -> None:
    """静态检查 ``config_schema`` 自包含（§4.1）：每个引用都能在文档内解析，每个 ``pattern`` 都能
    编译。解析器只含本文档的资源（不含 jsonschema 自带的元模式），因此元模式 URI 也算外部引用。"""
    root = DRAFT202012.create_resource(schema)
    registry: Registry[Any] = Registry().with_resource(root.id() or "", root).crawl()
    _walk(root, registry.resolver(base_uri=root.id() or ""))


def _walk(resource: Resource[Any], resolver: Any) -> None:
    contents = resource.contents
    if isinstance(contents, dict):
        for keyword in ("$ref", "$dynamicRef"):
            ref = contents.get(keyword)
            if isinstance(ref, str):
                try:
                    resolver.lookup(ref)
                except Unresolvable:
                    raise ValueError(f"{keyword} {ref!r} 无法在文档内解析") from None
        patterns = [contents.get("pattern")]
        if isinstance(contents.get("patternProperties"), dict):
            patterns.extend(contents["patternProperties"])
        for pattern in patterns:
            if isinstance(pattern, str):
                try:
                    re.compile(pattern)
                except re.error as exc:
                    raise ValueError(f"pattern {pattern!r} 无法编译：{exc}") from None
    for sub in resource.subresources():
        _walk(sub, resolver.in_subresource(sub))


def _pydantic_details(exc: PydanticValidationError) -> str:
    return "; ".join(
        f"{'.'.join(map(str, e['loc'])) or '<root>'}: {e['msg']}" for e in exc.errors()
    )


def parse_manifest(data: Any) -> Manifest:
    """校验已解析的清单；不合法时抛 ``InstanceFailure("manifest_invalid")``。"""
    try:
        return Manifest.model_validate(data)
    except PydanticValidationError as exc:
        raise InstanceFailure("manifest_invalid", f"清单不合法：{_pydantic_details(exc)}") from None


def load_manifest(path: Path) -> Manifest:
    """读取并校验清单文件；不可读或不合法时抛 ``InstanceFailure("manifest_invalid")``。"""
    try:
        data = load_json_file(path)
    except ValueError as exc:
        raise InstanceFailure("manifest_invalid", str(exc)) from None
    return parse_manifest(data)


def validate_config(manifest: Manifest, config: dict[str, Any]) -> None:
    """按清单的 ``config_schema`` 校验实例配置（§4.2）；不改写 ``config``。

    不通过时抛 ``InstanceFailure("config_schema_violation")``。引用无法在文档内解析的清单在读取
    时已被拒绝（``_check_self_contained``），校验因此不会访问网络或文件。
    """
    validator = Draft202012Validator(manifest.config_schema, registry=Registry())
    try:
        error = best_match(validator.iter_errors(config))
    except RecursionError:
        raise InstanceFailure("config_schema_violation", "实例配置嵌套过深，无法校验") from None
    if error is not None:
        where = error.json_path
        raise InstanceFailure(
            "config_schema_violation", f"实例配置未通过 config_schema（{where}）：{error.message}"
        )
