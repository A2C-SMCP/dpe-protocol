"""插件清单与实例配置校验（connector 契约 §4.1–§4.2）。

清单是静态声明，不启动插件即可读取。``config_schema`` 按 draft 2020-12 语义校验实例配置，且
按 §4.1.1 是**封闭 schema**：只允许列出的关键字，``$ref`` 只有 ``#/$defs/<名字>`` 一种形式
（``$defs`` 仅根、引用必须无环，无嵌入资源、锚点与方言切换），读取清单即完成全部静态检查，
从不访问网络或文件。因此清单是否合法只取决于清单本身，与配置内容无关。``pattern`` 与
``patternProperties`` 的键按 §4.1.1 的**可移植子集**判定（``dpe_sdk.run._pattern``）：合法性
只由文法与结构上界决定，超出即判清单不合法；匹配一律在「解析 → 转译为显式码点区间」之后进行
（``validate_config`` 先在 schema 副本上替换 pattern 与 patternProperties 键），不依赖 Python
正则的字符类语义。不启用 ``format`` 校验（只作注解）。配置原样传给插件，不填充 ``default``、
不做任何改写。
"""

from __future__ import annotations

import copy
import re
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import unquote_to_bytes

from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match
from pydantic import Field, PrivateAttr, field_validator, model_validator
from pydantic import ValidationError as PydanticValidationError
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from dpe_sdk.run._json import (
    MAX_JSON_DEPTH,
    ClosedModel,
    has_numeric_violation,
    json_depth,
    load_json_file,
)
from dpe_sdk.run._pattern import PatternSubsetError, check_pattern, translate_pattern
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
        dialect = schema.get("$schema", _DRAFT_2020_12)
        if dialect not in (_DRAFT_2020_12, _DRAFT_2020_12 + "#"):
            raise ValueError(f"$schema 必须缺省或为 {_DRAFT_2020_12}")
        try:
            # 同 check_schema，但不启用 format 校验（format 只作注解，对 schema 本身同样如此）
            error = best_match(_META_VALIDATOR.iter_errors(schema))
            if error is not None:
                raise ValueError(f"不是合法的 draft 2020-12 schema：{error.message}")
            _check_closed_schema(schema)
        except RecursionError:
            # 同上：JSON 深度上界（_check_json_depth）在前，合法清单不会触达
            raise RuntimeError(
                "config_schema 校验超出解释器递归限额（内部错误；合法清单不应触达）"
            ) from None
        return schema

    @model_validator(mode="before")
    @classmethod
    def _check_json_depth(cls, data: Any) -> Any:
        # §4.1：清单文件的 JSON 嵌套深度 ≤ 64（读取阶段判定，与语义校验无关）
        if isinstance(data, dict) and json_depth(data) > MAX_JSON_DEPTH:
            raise ValueError(f"清单的 JSON 嵌套深度超过 {MAX_JSON_DEPTH}（§4.1）")
        return data

    @field_validator("secrets")
    @classmethod
    def _unique_secrets(
        cls, secrets: tuple[SecretDeclaration, ...]
    ) -> tuple[SecretDeclaration, ...]:
        names = [s.name for s in secrets]
        if len(set(names)) != len(names):
            raise ValueError("凭证名在清单内必须唯一")
        return secrets

    #: 求值用转译副本与「转译后 → 原文」映射；清单不可变，转译只做一次（§4.1.1）
    _translated: tuple[dict[str, Any], dict[str, list[str]]] = PrivateAttr()

    def model_post_init(self, __context: Any) -> None:
        # 此时字段校验已完成（pattern 均属子集），转译不会失败
        self._translated = _translate_schema(self.config_schema)

    def model_copy(
        self, *, update: Mapping[str, Any] | None = None, deep: bool = False
    ) -> Manifest:
        """``update`` 时经 ``model_validate`` 重建：派生态（``_translated``）与字段不得脱钩。"""
        if not update:
            return super().model_copy(deep=deep)
        data = _prune_absent(self.model_dump())
        try:
            return type(self).model_validate({**data, **update})
        except PydanticValidationError as exc:
            raise InstanceFailure(
                "manifest_invalid", f"清单不合法：{_pydantic_details(exc)}"
            ) from None

    @property
    def secret_names(self) -> frozenset[str]:
        return frozenset(s.name for s in self.secrets)


#: §4.1.1：schema 的展开深度上限（根计 1，schema 位置与 `$ref` 跳转各 +1）
_MAX_EXPANDED_DEPTH = 64

#: §4.1.1 封闭关键字子集：允许的关键字（位置约束在检查器里另行判定）
_ALLOWED_KEYWORDS = frozenset(
    {
        "type",
        "properties",
        "patternProperties",
        "additionalProperties",
        "required",
        "propertyNames",
        "items",
        "allOf",
        "anyOf",
        "oneOf",
        "not",
        "if",
        "then",
        "else",
        "$defs",
        "$ref",
        "$schema",
        "enum",
        "const",
        "pattern",
        "minLength",
        "maxLength",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "minItems",
        "maxItems",
        "uniqueItems",
        "minProperties",
        "maxProperties",
        "dependentRequired",
        "title",
        "description",
        "default",
        "examples",
        "deprecated",
        "readOnly",
        "writeOnly",
        "format",
        "contentEncoding",
        "contentMediaType",
        "$comment",
    }
)

#: 值为 schema 的关键字（数组见 _SUBSCHEMA_LIST，映射见 _SUBSCHEMA_MAP）
_SUBSCHEMA_VALUE = (
    "additionalProperties",
    "propertyNames",
    "items",
    "not",
    "if",
    "then",
    "else",
)
_SUBSCHEMA_LIST = ("allOf", "anyOf", "oneOf")
_SUBSCHEMA_MAP = ("properties", "patternProperties")


def _parse_defs_ref(ref: str) -> str:
    """解析 §4.1.1 的 `$ref` 唯一形式，返回解码后的名字；不合形式抛 ``ValueError``。"""
    if not ref.startswith("#/"):
        raise ValueError(f'$ref {ref!r} 只允许 "#/$defs/<名字>" 一种形式（§4.1.1）')
    fragment = ref[2:]
    # 非法百分号序列（`%` 后不是两位十六进制）在 RFC 3986 下无效，一律拒绝
    if re.search(r"%(?![0-9A-Fa-f]{2})", fragment):
        raise ValueError(f"$ref {ref!r} 含非法百分号序列（§4.1.1）")
    # RFC 6901 §6：先百分号解码，再按 "/" 拆分，最后处理 ~1 / ~0（顺序不可换）
    try:
        decoded = unquote_to_bytes(fragment).decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError(f"$ref {ref!r} 的片段不是合法 UTF-8（§4.1.1）") from None
    segments = decoded.split("/")
    if len(segments) != 2 or segments[0] != "$defs" or not segments[1]:
        raise ValueError(f'$ref {ref!r} 只允许 "#/$defs/<名字>" 一种形式（§4.1.1）')
    name = segments[1]
    if re.search(r"~(?![01])", name):
        raise ValueError(f"$ref {ref!r} 的名字含非法转义（~ 后只能是 0 或 1，§4.1.1）")
    return name.replace("~1", "/").replace("~0", "~")


def _check_closed_schema(schema: dict[str, Any]) -> None:
    """§4.1.1：封闭关键字子集、布尔 schema、`$defs` 位置、展开深度、`$ref` 形式/目标/无环规则。

    外部引用、未列出的关键字、非唯一形式的 ``$ref``、``$defs`` 位置错误、引用成环都在
    这里被拒（经字段校验转成 ``manifest_invalid``）；清单文件的 JSON 嵌套深度另由
    ``_check_json_depth`` 在读取阶段判定。因此读取清单不访问网络或文件，求值期也只见允许的
    关键字与子集内 pattern。
    """
    defs = schema.get("$defs")
    names = set(defs) if isinstance(defs, dict) else set()
    edges: dict[str, set[str]] = {}

    def check_node(node: Any, is_root: bool, position: str, owner: str | None) -> None:
        if isinstance(node, bool):
            return
        if not isinstance(node, dict):
            raise ValueError(f"{position}: schema 必须是对象或布尔值（§4.1.1 封闭 schema）")
        for keyword in node:
            if keyword not in _ALLOWED_KEYWORDS:
                raise ValueError(f"{position}: 不允许的关键字 {keyword!r}（§4.1.1 封闭关键字子集）")
            if keyword == "$defs" and not is_root:
                raise ValueError(f"{position}: $defs 只允许出现在根上（§4.1.1）")
            if keyword == "$schema" and not is_root:
                raise ValueError(f"{position}: $schema 只允许出现在根上（§4.1.1）")
        dialect = node.get("$schema")
        if dialect is not None and dialect not in (_DRAFT_2020_12, _DRAFT_2020_12 + "#"):
            raise ValueError(f"$schema {dialect!r} 必须缺省或为 {_DRAFT_2020_12}")
        ref = node.get("$ref")
        if ref is not None:
            if not isinstance(ref, str):
                raise ValueError(f"{position}: $ref 必须是字符串")
            name = _parse_defs_ref(ref)
            if name not in names:
                raise ValueError(f"{position}: $ref {ref!r} 指向的 $defs 条目不存在")
            if owner is not None:
                edges.setdefault(owner, set()).add(name)
        if isinstance(node.get("items"), list):
            raise ValueError(f"{position}: items 只允许单个 schema（§4.1.1）")
        pattern = node.get("pattern")
        if isinstance(pattern, str):
            try:
                check_pattern(pattern)
            except PatternSubsetError as exc:
                raise ValueError(
                    f"{position}: pattern {pattern!r} 超出 §4.1.1 子集：{exc}"
                ) from None
        props = node.get("patternProperties")
        if isinstance(props, dict):
            for key in props:
                if isinstance(key, str):
                    try:
                        check_pattern(key)
                    except PatternSubsetError as exc:
                        raise ValueError(
                            f"{position}: patternProperties 键 {key!r} 超出 §4.1.1 子集：{exc}"
                        ) from None
        for keyword in _SUBSCHEMA_VALUE:
            if keyword in node:
                check_node(node[keyword], False, f"{position}/{keyword}", owner)
        for keyword in _SUBSCHEMA_LIST:
            seq = node.get(keyword)
            if seq is None:
                continue
            if not isinstance(seq, list):
                raise ValueError(f"{position}/{keyword}: 必须是 schema 数组")
            for index, item in enumerate(seq):
                check_node(item, False, f"{position}/{keyword}/{index}", owner)
        for keyword in _SUBSCHEMA_MAP:
            mapping = node.get(keyword)
            if mapping is None:
                continue
            if not isinstance(mapping, dict):
                raise ValueError(f"{position}/{keyword}: 必须是「名字 → schema」映射")
            if any(not isinstance(key, str) for key in mapping):
                raise ValueError(f"{position}/{keyword}: 键必须是字符串（§4.1.1）")
            for key, sub in mapping.items():
                check_node(sub, False, f"{position}/{keyword}/{key}", owner)
        root_defs = node.get("$defs")
        if is_root and isinstance(root_defs, dict):
            if any(not isinstance(key, str) for key in root_defs):
                raise ValueError("/$defs: 键必须是字符串（§4.1.1）")
            for key, sub in root_defs.items():
                check_node(sub, False, f"/$defs/{key}", key)

    check_node(schema, True, "", None)
    _check_defs_acyclic(sorted(names), edges)
    _check_expanded_depth(schema, defs if isinstance(defs, dict) else {})


def _schema_children(node: dict[str, Any], defs: dict[str, Any]) -> Iterator[Any]:
    """求值后继：schema 位置上的子 schema，以及 ``$ref`` 的目标（§4.1.1 展开深度的边）。"""
    for keyword in _SUBSCHEMA_VALUE:
        if keyword in node:
            yield node[keyword]
    for keyword in _SUBSCHEMA_LIST:
        seq = node.get(keyword)
        if isinstance(seq, list):
            yield from seq
    for keyword in _SUBSCHEMA_MAP:
        mapping = node.get(keyword)
        if isinstance(mapping, dict):
            yield from mapping.values()
    ref = node.get("$ref")
    if isinstance(ref, str):
        try:
            target = defs.get(_parse_defs_ref(ref))
        except ValueError:
            target = None
        if target is not None:
            yield target


def _check_expanded_depth(schema: dict[str, Any], defs: dict[str, Any]) -> None:
    """§4.1.1：展开深度 ≤ 64（迭代后序 + 记忆化；`$defs` 已是 DAG，线性可算）。"""
    memo: dict[int, int] = {}
    stack: list[tuple[Any, bool]] = [(schema, False)]
    while stack:
        node, visited = stack.pop()
        if not isinstance(node, dict):
            memo[id(node)] = 1
            continue
        if visited:
            best = 0
            for child in _schema_children(node, defs):
                best = max(best, memo.get(id(child), 1))
            if best + 1 > _MAX_EXPANDED_DEPTH:
                raise ValueError(f"schema 的展开深度超过 {_MAX_EXPANDED_DEPTH}（§4.1.1）")
            memo[id(node)] = best + 1
            continue
        if id(node) in memo:
            continue
        stack.append((node, True))
        for child in _schema_children(node, defs):
            if id(child) not in memo:
                stack.append((child, False))


def _check_defs_acyclic(names: list[str], edges: dict[str, set[str]]) -> None:
    """`$defs` 条目之间的引用 MUST 无环（§4.1.1）：环会在求值时无限递归，行为随实现而异。"""
    state: dict[str, int] = {}  # 0 未访问 / 1 在栈上 / 2 已完成

    def visit(start: str) -> None:
        state[start] = 1
        stack: list[tuple[str, Iterator[str]]] = [(start, iter(sorted(edges.get(start, ()))))]
        while stack:
            current, targets = stack[-1]
            for target in targets:
                flag = state.get(target, 0)
                if flag == 1:
                    raise ValueError(f"$defs 引用成环（经 {target!r}，§4.1.1）：求值时会无限递归")
                if flag == 0:
                    state[target] = 1
                    stack.append((target, iter(sorted(edges.get(target, ())))))
                    break
            else:
                state[current] = 2
                stack.pop()

    for name in names:
        if state.get(name, 0) == 0:
            visit(name)


def _translate_schema(schema: dict[str, Any]) -> tuple[dict[str, Any], dict[str, list[str]]]:
    """深拷贝 ``config_schema``，把每个 ``pattern`` 与 ``patternProperties`` 的键换成 §4.1.1 的转译
    写法；返回（副本, 映射），映射把转译后的字符串指回原文列表（键碰撞时可能出现多个原文），供
    错误消息还原。

    替换必须发生在交给校验器**之前**：jsonschema 内部除了 ``pattern`` / ``patternProperties``
    关键字，``additionalProperties`` 与 ``unevaluatedProperties`` 也会拿 patternProperties 的键
    直接调 ``re.search``（jsonschema/_utils.py），只覆写关键字覆盖不到这些路径；被求值的内容都在
    本遍历的覆盖内——``$ref`` 目标已被 ``_check_closed_schema`` 限定为 schema 位置。键的转译结果
    可能碰撞（如 ``\\d`` 与 ``[0-9]`` 都转译成 ``[0-9]``），碰撞时把子 schema 并成 ``allOf``——
    一个属性名匹配多个模式时本就要同时满足各子 schema，语义等价。
    """
    copied = copy.deepcopy(schema)
    mapping: dict[str, list[str]] = {}
    seen: set[int] = set()

    def translate(text: str) -> str:
        translated = translate_pattern(text)
        originals = mapping.setdefault(translated, [])
        if text not in originals:
            originals.append(text)
        return translated

    def rewrite(resource: Resource[Any]) -> None:
        contents = resource.contents
        if isinstance(contents, dict) and id(contents) not in seen:
            seen.add(id(contents))
            pattern = contents.get("pattern")
            if isinstance(pattern, str):
                contents["pattern"] = translate(pattern)
            props = contents.get("patternProperties")
            if isinstance(props, dict):
                groups: dict[Any, list[Any]] = {}
                for key, subschema in props.items():
                    groups.setdefault(translate(key) if isinstance(key, str) else key, []).append(
                        subschema
                    )
                contents["patternProperties"] = {
                    key: subs[0] if len(subs) == 1 else {"allOf": subs}
                    for key, subs in groups.items()
                }
        for sub in resource.subresources():
            rewrite(sub)

    rewrite(DRAFT202012.create_resource(copied))
    return copied, mapping


def _restore_patterns(message: str, mapping: dict[str, list[str]]) -> str:
    """把错误消息里内嵌的转译写法还原为原 pattern（碰撞时列出全部原文）。

    jsonschema 的消息把 pattern 以 repr（含两侧引号、反斜杠已转义）内嵌；按「长转译串优先」做整段
    repr 替换——短串是长串子串时（如 ``\\x61`` 与 ``\\x61\\x62``）先替短串会把长串打碎，顺序即
    为此而设。
    """
    ordered = sorted(mapping.items(), key=lambda item: len(item[0]), reverse=True)
    for translated, originals in ordered:
        quoted = repr(translated)
        if quoted in message:
            message = message.replace(quoted, repr(" / ".join(originals)))
    return message


def _prune_absent(value: Any) -> Any:
    """去掉回填时占位的缺省 ``None``（``ClosedModel`` 只拒绝显式 null）。

    ``config_schema`` 原样保留：其中的 null 是内容（如 ``default`` 的值），不是缺省。
    """
    if isinstance(value, dict):
        return {
            key: (item if key == "config_schema" else _prune_absent(item))
            for key, item in value.items()
            if item is not None
        }
    if isinstance(value, (list, tuple)):
        return [_prune_absent(item) for item in value]
    return value


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
    时已被拒绝（``_check_closed_schema``），校验因此不会访问网络或文件；``pattern`` /
    ``patternProperties`` 的匹配用 §4.1.1 的子集语义：先在 schema 副本上做转译
    （``_translate_schema``），再交给 draft 2020-12 校验器，使 jsonschema 内部所有用正则的路径
    都只见到转译后的写法。
    """
    translated, mapping = manifest._translated
    try:
        validator = Draft202012Validator(translated, registry=Registry())
        error = best_match(validator.iter_errors(config))
    except RecursionError:
        # §4.1/§4.1.1 的上界（JSON 深度、展开深度）保证合法输入不会走到这里：触发即实现缺陷。
        # 按内部错误响亮失败，不得映射成校验结论——那会把合法配置静默拒绝。
        # 包裹覆盖语义检查这两处；更早阶段（解析/深度判定）若有同类溢出同样按内部错误裸抛。
        raise RuntimeError(
            "config_schema 求值超出解释器递归限额（内部错误；合法输入不应触达，见 §4.1.1 的上界）"
        ) from None
    if error is not None:
        where = error.json_path
        message = _restore_patterns(error.message, mapping)
        raise InstanceFailure(
            "config_schema_violation", f"实例配置未通过 config_schema（{where}）：{message}"
        )
