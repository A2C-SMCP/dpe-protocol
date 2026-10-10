"""connector 契约 §4.1.1 正则子集：实现行为与一致性向量（vectors/config_schema_patterns.json）。"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

import pytest
from dpe_sdk.run import InstanceFailure
from dpe_sdk.run._pattern import (
    MAX_EXPANSION,
    MAX_LENGTH,
    MAX_NESTING,
    PatternSubsetError,
    check_pattern,
    search,
    translate_pattern,
)
from dpe_sdk.run.manifest import parse_manifest, validate_config

EXAMPLE: dict[str, Any] = {
    "manifest_version": 1,
    "name": "git-connector",
    "version": "0.1.0",
    "protocol_versions": ["dpe-connector/1"],
    "config_schema": {"type": "object"},
}


def _strict_json(text: str) -> Any:
    """按向量消费约定用严格解析器读取（拒绝重复键）。"""

    def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        seen: set[str] = set()
        for key, _ in pairs:
            if key in seen:
                raise ValueError(f"重复键 {key!r}")
            seen.add(key)
        return dict(pairs)

    return json.loads(text, object_pairs_hook=_reject_duplicates)


def with_schema(schema: Any) -> dict[str, Any]:
    data = copy.deepcopy(EXAMPLE)
    data["config_schema"] = schema
    return data


@pytest.fixture(scope="module")
def patterns(vectors_dir: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(
        (vectors_dir / "config_schema_patterns.json").read_text(encoding="utf-8")
    )
    assert data["kind"] == "pattern"
    return data


# ---------------------------------------------------------------------------
# 一致性向量
# ---------------------------------------------------------------------------


def test_valid_patterns_are_accepted_and_compilable(patterns: dict[str, Any]) -> None:
    assert patterns["valid_patterns"], "向量不应为空"
    for entry in patterns["valid_patterns"]:
        check_pattern(entry["pattern"])  # 不抛即通过
        re.compile(translate_pattern(entry["pattern"]))


def test_invalid_patterns_are_rejected(patterns: dict[str, Any]) -> None:
    assert patterns["invalid_patterns"], "向量不应为空"
    for entry in patterns["invalid_patterns"]:
        pattern = entry.get("pattern")
        if pattern is None:
            # pattern_json：无法以普通字符串表达的输入（孤立代理项等），按原始 JSON 文本解析
            pattern = _strict_json(entry["pattern_json"])
        with pytest.raises(PatternSubsetError):
            check_pattern(pattern)


def test_match_cases_follow_spec_semantics(patterns: dict[str, Any]) -> None:
    assert patterns["match_cases"], "向量不应为空"
    for case in patterns["match_cases"]:
        value = case.get("value")
        if "value_json" in case:
            value = _strict_json(case["value_json"])
        got = search(case["pattern"], value)
        assert got == case["match"], f"{case['pattern']!r} 对 {case['value']!r}"


def test_schema_cases(patterns: dict[str, Any]) -> None:
    assert patterns["schema_cases"], "向量不应为空"
    for case in patterns["schema_cases"]:
        manifest_data = with_schema(case["schema"])
        if case["manifest_valid"]:
            manifest = parse_manifest(manifest_data)
            if "config" in case:
                if case["config_valid"]:
                    validate_config(manifest, case["config"])
                else:
                    with pytest.raises(InstanceFailure) as info:
                        validate_config(manifest, case["config"])
                    assert info.value.code == "config_schema_violation"
        else:
            with pytest.raises(InstanceFailure) as info:
                parse_manifest(manifest_data)
            assert info.value.code == "manifest_invalid"


# ---------------------------------------------------------------------------
# 实现单元：拒绝原因与结构上界
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pattern", "reason"),
    [
        ("(?=a)b", "lookaround"),
        ("(?<!a)b", "lookaround"),
        ("\\p{L}", "unicode_property_escape"),
        ("(a)\\1", "backreference"),
        ("(?i)a", "inline_flag"),
        ("(?<n>a)", "named_group"),
        ("a*?", "lazy_quantifier"),
        ("a{2}+", "possessive_quantifier"),
        ("a{2}{3}", "stacked_quantifier"),
        ("{,3}", "invalid_repetition"),
        ("a{2,1}", "invalid_repetition"),
        ("[z-a]", "class_range_order"),
        ("[\\d-z]", "class_range_endpoint"),
        ("[]", "empty_class"),
        ("[a&&b]", "class_set_operation"),
        ("[a~~b]", "class_set_operation"),
        ("a^b", "anchor_position"),
        ("(^a)", "anchor_position"),
        ("(a$)", "anchor_position"),
        ("a$b", "anchor_position"),
        ("\\q", "unknown_escape"),
        ("\\/", "unknown_escape"),
        ("\\b", "unknown_escape"),
        ("\\u0041", "unknown_escape"),
        ("[a-b-c]", "class_dash_position"),
        ("*a", "syntax"),
        ("[a", "syntax"),
        ("(a", "syntax"),
        ("a" * (MAX_LENGTH + 1), "length"),
        ("a{4097}", "expansion"),
        ("a{4097,}", "expansion"),
        ("a{1,4097}", "expansion"),
        ("[aaaa]{1025}", "expansion"),
    ],
)
def test_reasons(pattern: str, reason: str) -> None:
    with pytest.raises(PatternSubsetError) as info:
        check_pattern(pattern)
    assert info.value.reason == reason


@pytest.mark.parametrize(
    "pattern",
    [
        "^[a-z][a-z0-9_]*$",
        "^(?:ab|cd)+$",
        "(a|)",
        "()",
        "[-a]",
        "[a-]",
        "[\\-]",
        "[^\\]a]",
        "\\xE9",
        "[\\x00-\\x1f]",
        "[\\D]",
        "[^\\D]",
        "[^\\W]",
        "a{0}",
        "a{4096}",
        "a{4096,}",
        "a{1,4096}",
        "[aaaa]{1024}",
        "(?:a|bc){1365}",
        "\\W{255}",
        "." * MAX_LENGTH,
    ],
)
def test_valid_patterns(pattern: str) -> None:
    check_pattern(pattern)
    re.compile(translate_pattern(pattern))


def test_worst_case_shape_compiles_and_matches() -> None:
    # 规范最坏情况：16 个跨字节宽度的互不相邻码点的手写类 × 255（展开规模 4080，与探针同款）
    klass = "".join(chr(0x100 + 0x110 * k) for k in range(16))
    pattern = f"[{klass}]{{255}}"
    check_pattern(pattern)
    translated = translate_pattern(pattern)
    assert re.search(translated, klass * 255)
    assert not re.search(translated, "A" * 255)


def test_translation_output_is_bounded() -> None:
    """转译输出有确定上界（``_pattern._TRANSLATE_CACHE_MAX`` 驻留有界的前提）。

    最坏形状是 1024 个 ``.``（每个 1 个标量值 → 48 字符的显式码点类），实测
    48 × MAX_LENGTH = 49152 字符 ≈ 48 KiB；断言取 ``<=``：发射变小不失败，变大即失败，
    届时须同步缓存上限的注释。
    """
    assert len(translate_pattern("." * MAX_LENGTH)) <= 48 * MAX_LENGTH


def test_expansion_counting_is_not_deduplicated() -> None:
    check_pattern("[aaaa]{1024}")  # 恰 4096
    with pytest.raises(PatternSubsetError) as info:
        check_pattern("[aaaa]{1025}")
    assert info.value.reason == "expansion"


def test_expansion_factor_for_open_ranges() -> None:
    # {m,} 的因子记 m（Rust 会把 m 份展开）
    check_pattern("a{4096,}")
    with pytest.raises(PatternSubsetError):
        check_pattern("a{4097,}")


def test_nesting_depth_bound() -> None:
    check_pattern("(" * MAX_NESTING + "a" + ")" * MAX_NESTING)
    check_pattern("(" * MAX_NESTING + "a{4096}" + ")" * MAX_NESTING)
    # 最坏嵌套形状：每层分组都带量词（引擎的解析嵌套还计入量词与字符类）
    worst = "(?:" * MAX_NESTING + "[a]*" + ")*" * MAX_NESTING
    check_pattern(worst)
    re.compile(translate_pattern(worst))
    with pytest.raises(PatternSubsetError) as info:
        check_pattern("(" * (MAX_NESTING + 1) + "a" + ")" * (MAX_NESTING + 1))
    assert info.value.reason == "nesting_depth"
    # 极深嵌套不得退化为 RecursionError
    with pytest.raises(PatternSubsetError) as info:
        check_pattern("(" * 500 + "a" + ")" * 500)
    assert info.value.reason == "nesting_depth"


def test_lone_surrogate_is_not_a_scalar_value() -> None:
    lone = json.loads('"\\ud800"')
    with pytest.raises(PatternSubsetError) as info:
        check_pattern(lone)
    assert info.value.reason == "not_scalar_value"
    with pytest.raises(PatternSubsetError):
        check_pattern("[" + lone + "]")


# ---------------------------------------------------------------------------
# 子集语义经 validate_config 的端到端行为
# ---------------------------------------------------------------------------


def test_ascii_classes_and_anchors_end_to_end() -> None:
    schema = {"type": "object", "properties": {"repo": {"type": "string", "pattern": "^\\w+$"}}}
    manifest = parse_manifest(with_schema(schema))
    validate_config(manifest, {"repo": "abc_9"})
    with pytest.raises(InstanceFailure) as info:
        validate_config(manifest, {"repo": "héllo"})  # \w 为 ASCII
    assert info.value.code == "config_schema_violation"


def test_dollar_does_not_match_before_trailing_newline() -> None:
    schema = {"type": "object", "properties": {"ref": {"type": "string", "pattern": "^main$"}}}
    manifest = parse_manifest(with_schema(schema))
    validate_config(manifest, {"ref": "main"})
    with pytest.raises(InstanceFailure):
        validate_config(manifest, {"ref": "main\n"})


def test_dot_excludes_carriage_return() -> None:
    schema = {"type": "object", "properties": {"v": {"type": "string", "pattern": "^.$"}}}
    manifest = parse_manifest(with_schema(schema))
    validate_config(manifest, {"v": "😀"})
    with pytest.raises(InstanceFailure):
        validate_config(manifest, {"v": "\r"})


def test_pattern_is_unanchored() -> None:
    schema = {"type": "object", "properties": {"v": {"type": "string", "pattern": "acme"}}}
    manifest = parse_manifest(with_schema(schema))
    validate_config(manifest, {"v": "https://acme.example/x"})
    with pytest.raises(InstanceFailure):
        validate_config(manifest, {"v": "ACM"})


def test_data_keys_named_pattern_are_not_keywords() -> None:
    schema = {
        "type": "object",
        "const": {"pattern": "(?=x)"},
        "default": {"pattern": "("},
        "examples": [{"pattern": "[z-a]"}],
    }
    manifest = parse_manifest(with_schema(schema))
    validate_config(manifest, {"pattern": "(?=x)"})


def test_empty_pattern_properties_key_matches_every_property() -> None:
    """空 pattern 匹配任意属性名：转译不得产出会被「|」拼接吞掉的空串（jsonschema 的
    additionalProperties 判定会把空 join 当作「没有模式」）。"""
    schema = {
        "type": "object",
        "patternProperties": {"": {"type": "integer"}},
        "additionalProperties": False,
    }
    manifest = parse_manifest(with_schema(schema))
    validate_config(manifest, {"any": 1})
    with pytest.raises(InstanceFailure):
        validate_config(manifest, {"any": "s"})  # 子 schema 仍适用


def test_expanded_depth_bound() -> None:
    """§4.1.1：展开深度 ≤64（引用跳转与原地嵌套都计）；顶格形状必须能求值。"""

    def chained(n: int, layers: int = 0) -> dict[str, Any]:
        defs: dict[str, Any] = {}
        for i in range(n):
            node: Any = (
                {"$ref": f"#/$defs/d{i + 1}"} if i + 1 < n else {"type": "string", "pattern": "^a$"}
            )
            for _ in range(layers):
                node = {"allOf": [node]}
            defs[f"d{i}"] = node
        return defs

    def schema_with(n: int, layers: int = 0) -> dict[str, Any]:
        return {
            "type": "object",
            "$defs": chained(n, layers),
            "properties": {"x": {"$ref": "#/$defs/d0"}},
        }

    # 顶格：纯引用链 62 跳（展开深度 64）——必须能求值，不得触实现递归上限
    manifest = parse_manifest(with_schema(schema_with(62)))
    validate_config(manifest, {"x": "a"})
    with pytest.raises(InstanceFailure):
        validate_config(manifest, {"x": "b"})

    # 链 × 原地嵌套（31 跳 × 1 层 allOf）同样在界内且可求值
    mixed = parse_manifest(with_schema(schema_with(31, 1)))
    validate_config(mixed, {"x": "a"})

    # 越界：63 跳 / 32 跳 × 1 层
    for schema in (schema_with(63), schema_with(32, 1)):
        with pytest.raises(InstanceFailure) as info:
            parse_manifest(with_schema(schema))
        assert info.value.code == "manifest_invalid"


def test_manifest_model_copy_revalidates() -> None:
    """model_copy(update=…) 必须重跑校验并重建转译副本（派生态不得与字段脱钩）。"""
    manifest = parse_manifest(
        with_schema({"type": "object", "properties": {"x": {"type": "string", "pattern": "^a$"}}})
    )
    validate_config(manifest, {"x": "a"})
    copied = manifest.model_copy(
        update={
            "config_schema": {
                "type": "object",
                "properties": {"x": {"type": "string", "pattern": "^b$"}},
            }
        }
    )
    validate_config(copied, {"x": "b"})
    with pytest.raises(InstanceFailure):
        validate_config(copied, {"x": "a"})  # 新 pattern 生效，不得沿用旧转译
    with pytest.raises(InstanceFailure):
        manifest.model_copy(update={"config_schema": {"type": "object", "$id": "x"}})


def test_object_keywords_require_string_keys() -> None:
    """properties / patternProperties / $defs 的键必须是字符串。

    JSON 文件不可达（I-JSON 键必为 str），但 ``parse_manifest`` 是公开入口：非字符串键必须在
    装载期拒绝，而不是等到求值时抛裸 ``TypeError``。
    """
    for schema in (
        {"type": "object", "properties": {1: {"type": "integer"}}},
        {"type": "object", "patternProperties": {1: {}}},
        {"type": "object", "$defs": {1: {}}},
    ):
        with pytest.raises(InstanceFailure) as info:
            parse_manifest(with_schema(schema))
        assert info.value.code == "manifest_invalid"


def test_manifest_json_depth_bound() -> None:
    """§4.1：清单文件的 JSON 嵌套深度 ≤ 64，超出（含数据位置上的深度）判 manifest_invalid。"""

    def nested_properties(levels: int) -> dict[str, Any]:
        node: dict[str, Any] = {"type": "object"}
        for _ in range(levels):
            node = {"type": "object", "properties": {"a": node}}
        return node

    def nested_array(levels: int) -> Any:
        node: Any = "x"
        for _ in range(levels):
            node = [node]
        return node

    manifest = parse_manifest(with_schema(nested_properties(30)))  # 清单深度 63
    validate_config(manifest, {})
    with pytest.raises(InstanceFailure) as info:
        parse_manifest(with_schema(nested_properties(31)))  # 清单深度 65
    assert info.value.code == "manifest_invalid"

    parse_manifest(with_schema({"type": "object", "default": nested_array(61)}))  # 深度 64
    with pytest.raises(InstanceFailure) as info:
        parse_manifest(with_schema({"type": "object", "default": nested_array(62)}))  # 深度 65
    assert info.value.code == "manifest_invalid"


def test_definition_json_depth_bound() -> None:
    """§4.4：实例定义（含 config）的 JSON 嵌套深度 ≤ 64，超出判 definition_invalid。"""
    from dpe_sdk.run.definition import parse_definition

    def nested_object(levels: int) -> dict[str, Any]:
        node: dict[str, Any] = {}
        for _ in range(levels):
            node = {"k": node}
        return node

    base: dict[str, Any] = {
        "definition_version": 1,
        "id": "acme",
        "remote": {"url": "https://dpe.example.com/remotes/acme"},
        "uri_prefix": "git://acme/repo/",
        "plugin": {"command": ["/bin/true"], "manifest": "dpe-connector.json"},
        "config": {},
    }
    parse_definition({**base, "config": nested_object(62)}, Path("."))  # 深度 64
    with pytest.raises(InstanceFailure) as info:
        parse_definition({**base, "config": nested_object(63)}, Path("."))  # 深度 65
    assert info.value.code == "definition_invalid"


def test_class_sequence_rule_is_adjacency_based() -> None:
    """相邻的 -- 无论前一个是否转义都违例；\\xHH 写法合法。"""
    check_pattern("[\\x2D\\x2D]")
    check_pattern("[-a]")
    check_pattern("[a-]")
    check_pattern("[\\-\\-]")
    for pattern in ("[a\\--b]", "[a--b]", "[--]"):
        with pytest.raises(PatternSubsetError) as info:
            check_pattern(pattern)
        assert info.value.reason == "class_set_operation"


def test_ref_decoding_strictness() -> None:
    """前缀必须字面 #/；非法百分号序列与 ~ 后非 0/1 一律拒绝。"""
    for ref in ("#%2F$defs%2Fname", "#/$defs/a~2b", "#/$defs/a%ZZb"):
        with pytest.raises(InstanceFailure) as info:
            parse_manifest(
                with_schema(
                    {
                        "type": "object",
                        "$defs": {"name": True, "a~2b": True, "a%ZZb": True},
                        "properties": {"x": {"$ref": ref}},
                    }
                )
            )
        assert info.value.code == "manifest_invalid"


def test_translate_pattern_validates_itself() -> None:
    """转译入口自带完整校验，不依赖调用序（assert 在 -O 下会消失）。"""
    for pattern in ("a" * (MAX_LENGTH + 1), "[--]", "(?=a)"):
        with pytest.raises(PatternSubsetError):
            translate_pattern(pattern)


def test_matching_universe_is_scalar_values() -> None:
    """匹配宇宙是 Unicode 标量值：正类区间与取反类对孤立代理项都不匹配。"""
    lone = "\ud800"
    klass = "^[" + chr(0xD7FF) + "-" + chr(0xE000) + "]$"
    assert search(klass, lone) is False
    assert search("^[^a]$", lone) is False
    assert search(klass, chr(0xE000)) is True


def test_zero_size_atom_repetition_is_bounded() -> None:
    """空组至少计 1：大计数不得绕过展开规模上界，也不得让引擎在编译/匹配时崩溃或长挂。"""
    for pattern in ("(?:){4096}", "(){4096}"):
        check_pattern(pattern)
        re.compile(translate_pattern(pattern))
    manifest = parse_manifest(
        with_schema(
            {"type": "object", "properties": {"x": {"type": "string", "pattern": "(?:){4096}"}}}
        )
    )
    validate_config(manifest, {"x": "s"})
    for pattern in ("(?:){4097}", "(){4294967296}", "(?:(?:){4096}){4096}"):
        with pytest.raises(PatternSubsetError) as info:
            check_pattern(pattern)
        assert info.value.reason == "expansion"


def test_error_message_survives_substring_translations() -> None:
    """还原消息按「长转译串优先」替换：短串是长串子串时不得把长串打碎。"""
    schema = {"type": "object", "properties": {"p1": {"pattern": "a"}, "p2": {"pattern": "ab"}}}
    manifest = parse_manifest(with_schema(schema))
    with pytest.raises(InstanceFailure) as info:
        validate_config(manifest, {"p2": "zz"})
    assert repr("ab") in str(info.value)


def test_closed_keyword_subset() -> None:
    """§4.1.1 封闭关键字子集：未列出的关键字一律 manifest_invalid，允许集内正常。"""
    banned: list[dict[str, Any]] = [
        {"properties": {"x": {"$id": "https://self/x"}}},
        {"properties": {"x": {"$anchor": "n"}}},
        {"properties": {"x": {"$dynamicRef": "#m"}}},
        {"definitions": {"d": {}}},
        {"dependencies": {"a": ["b"]}},
        {"properties": {"x": {"contentSchema": {"type": "string"}}}},
        {"properties": {"x": {"multipleOf": 2}}},
        {"properties": {"x": {"customKeyword": 1}}},
        {"unevaluatedProperties": False},
        {"prefixItems": [{"type": "string"}]},
        {"properties": {"x": {"$defs": {"d": {}}}}},  # $defs 非根
        {"properties": {"x": {"$schema": "https://json-schema.org/draft/2020-12/schema"}}},
        {"$schema": "http://json-schema.org/draft-07/schema#"},
        {"type": "array", "items": [{"type": "string"}]},
    ]
    for banned_extra in banned:
        with pytest.raises(InstanceFailure) as info:
            parse_manifest(with_schema({"type": "object", **banned_extra}))
        assert info.value.code == "manifest_invalid"

    allowed = {
        "type": "object",
        "$schema": "https://json-schema.org/draft/2020-12/schema#",
        "$comment": "说明",
        "properties": {"x": {"type": "string", "minLength": 1, "maxLength": 5, "pattern": "^a$"}},
        "if": {"required": ["x"]},
        "then": {"required": ["x"]},
        "propertyNames": {"pattern": "^[a-z]+$"},
        "additionalProperties": False,
    }
    parse_manifest(with_schema(allowed))


def test_ref_forms_and_defs_rules() -> None:
    """$ref 只有 "#/$defs/<名字>" 一种形式；目标必须存在；$defs 引用必须无环。"""

    def schema_with(ref: str, defs: dict[str, Any] | None = None) -> dict[str, Any]:
        data: dict[str, Any] = {"type": "object", "properties": {"x": {"$ref": ref}}}
        if defs is not None:
            data["$defs"] = defs
        return data

    manifest = parse_manifest(
        with_schema(schema_with("#/$defs/name", {"name": {"type": "string", "pattern": "^\\w+$"}}))
    )
    validate_config(manifest, {"x": "abc_9"})
    with pytest.raises(InstanceFailure):
        validate_config(manifest, {"x": "héllo"})  # 引用目标处的 pattern 仍按子集语义

    for bad_ref in ("#", "#name", "#/$defs/a/b", "#/properties/x", "other.json#/$defs/x"):
        with pytest.raises(InstanceFailure) as info:
            parse_manifest(with_schema(schema_with(bad_ref)))
        assert info.value.code == "manifest_invalid"

    with pytest.raises(InstanceFailure):
        parse_manifest(with_schema(schema_with("#/$defs/nope", {"a": {}})))
    with pytest.raises(InstanceFailure):  # 自引用成环
        parse_manifest(
            with_schema(
                {
                    "type": "object",
                    "$defs": {"a": {"$ref": "#/$defs/a"}},
                    "properties": {"x": {"$ref": "#/$defs/a"}},
                }
            )
        )
    with pytest.raises(InstanceFailure):  # 互引成环
        parse_manifest(
            with_schema(
                {
                    "type": "object",
                    "$defs": {"a": {"$ref": "#/$defs/b"}, "b": {"$ref": "#/$defs/a"}},
                }
            )
        )

    # 名字按 RFC 6901 §6 解码：先百分号解码、再 ~1/~0
    spaced = parse_manifest(
        with_schema(
            {
                "type": "object",
                "$defs": {"a b": {"type": "string", "pattern": "^a$"}},
                "properties": {"x": {"$ref": "#/$defs/a%20b"}},
            }
        )
    )
    validate_config(spaced, {"x": "a"})
    slashed = parse_manifest(
        with_schema(
            {
                "type": "object",
                "$defs": {"a/b": True},
                "properties": {"x": {"$ref": "#/$defs/a%7E1b"}},
            }
        )
    )
    validate_config(slashed, {"x": 1})


def test_content_annotations_and_equality() -> None:
    """content 词只作注解；enum/const 的等值语义（1 == 1.0，1 != true）钉住。"""
    schema = {
        "type": "object",
        "properties": {
            "x": {"type": "string", "contentEncoding": "base64", "contentMediaType": "image/png"},
            "n": {"enum": [1]},
            "c": {"const": 0},
        },
    }
    manifest = parse_manifest(with_schema(schema))
    validate_config(manifest, {"x": "这不是 base64", "n": 1.0, "c": 0.0})
    with pytest.raises(InstanceFailure):
        validate_config(manifest, {"n": True})  # 1 != true
    with pytest.raises(InstanceFailure):
        validate_config(manifest, {"c": False})  # 0 != false


def test_property_named_pattern_is_not_a_keyword() -> None:
    schema = {
        "type": "object",
        "properties": {"pattern": {"type": "string", "pattern": "^\\d+$"}},
    }
    manifest = parse_manifest(with_schema(schema))
    validate_config(manifest, {"pattern": "123"})
    with pytest.raises(InstanceFailure):
        validate_config(manifest, {"pattern": "abc"})


def test_internal_pattern_paths_use_subset_semantics() -> None:
    """jsonschema 内部用正则的路径也必须看到转译后的写法。

    ``additionalProperties`` 与 ``unevaluatedProperties`` 会拿 patternProperties 的键（拼接后）
    直接 ``re.search``（jsonschema/_utils.py），只覆写 pattern 关键字覆盖不到这些路径。
    """
    schema = {"type": "object", "patternProperties": {"^\\w+$": {}}, "additionalProperties": False}
    manifest = parse_manifest(with_schema(schema))
    validate_config(manifest, {"abc": 1})
    for bad in ({"é": 1}, {"a\n": 1}):  # \w 为 ASCII；$ 不匹配末尾换行之前
        with pytest.raises(InstanceFailure):
            validate_config(manifest, bad)

    additional = {
        "type": "object",
        "patternProperties": {"^a$": {}},
        "additionalProperties": False,
    }
    manifest = parse_manifest(with_schema(additional))
    validate_config(manifest, {"a": 1})
    with pytest.raises(InstanceFailure):
        validate_config(manifest, {"a\n": 1})


def test_pattern_properties_translation_collision_merges_subschemas() -> None:
    """`\\d` 与 `[0-9]` 转译后同形：匹配多个模式时各子 schema 都必须满足（不得静默覆盖）。"""
    schema = {
        "type": "object",
        "patternProperties": {"\\d": {"type": "string"}, "[0-9]": {"maxLength": 1}},
    }
    manifest = parse_manifest(with_schema(schema))
    validate_config(manifest, {"1": "a"})
    with pytest.raises(InstanceFailure) as info:
        validate_config(manifest, {"1": "ab"})
    assert "too long" in str(info.value)  # 后一个键的子 schema 生效


def test_error_message_shows_original_pattern() -> None:
    schema = {"type": "object", "properties": {"r": {"type": "string", "pattern": "^\\w+$"}}}
    manifest = parse_manifest(with_schema(schema))
    with pytest.raises(InstanceFailure) as info:
        validate_config(manifest, {"r": "héllo"})
    message = str(info.value)
    assert repr("^\\w+$") in message  # 显示原 pattern
    assert "\\x30" not in message  # 不泄漏转译后的写法


def test_collision_error_message_lists_all_originals() -> None:
    schema = {
        "type": "object",
        "patternProperties": {"\\d": {}, "[0-9]": {}},
        "additionalProperties": False,
    }
    manifest = parse_manifest(with_schema(schema))
    with pytest.raises(InstanceFailure) as info:
        validate_config(manifest, {"x": 1})
    message = str(info.value)
    assert "\\d" in message and "[0-9]" in message  # 碰撞键的全部原文都在


def test_pattern_properties_keys_are_checked_and_matched() -> None:
    bad = {"type": "object", "patternProperties": {"^(?=a)": {}}}
    with pytest.raises(InstanceFailure) as info:
        parse_manifest(with_schema(bad))
    assert info.value.code == "manifest_invalid"

    good = {
        "type": "object",
        "patternProperties": {"^x_[a-z]+$": {"type": "integer"}},
    }
    manifest = parse_manifest(with_schema(good))
    validate_config(manifest, {"x_ab": 1})
    with pytest.raises(InstanceFailure):
        validate_config(manifest, {"x_ab": "s"})


def test_subschema_patterns_are_checked_wherever_they_appear() -> None:
    for placement in (
        {"additionalProperties": {"pattern": "(?=x)"}},
        {"allOf": [{"pattern": "(?=x)"}]},
        {"$defs": {"d": {"pattern": "(?=x)"}}, "properties": {"a": {"$ref": "#/$defs/d"}}},
        {"properties": {"a": {"not": {"pattern": "(?=x)"}}}},
    ):
        schema = {"type": "object", **placement}
        with pytest.raises(InstanceFailure) as info:
            parse_manifest(with_schema(schema))
        assert info.value.code == "manifest_invalid"


def test_max_constants_match_spec() -> None:
    assert (MAX_LENGTH, MAX_EXPANSION, MAX_NESTING) == (1024, 4096, 64)
