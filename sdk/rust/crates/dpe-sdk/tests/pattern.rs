//! §4.1.1 子集引擎的行为测试（实现单元与端到端语义；向量消费见 pattern_vectors.rs）。

use serde_json::{json, Value};

use dpe_sdk::run::manifest::{parse_manifest, validate_config};
use dpe_sdk::run::pattern::{
    check_pattern, search, Pattern, MAX_EXPANSION, MAX_LENGTH, MAX_NESTING,
};

fn reason(pattern: &str) -> &'static str {
    check_pattern(pattern).expect_err(pattern).reason()
}

fn with_schema(schema: Value) -> Value {
    json!({
        "manifest_version": 1,
        "name": "git-connector",
        "version": "0.1.0",
        "protocol_versions": ["dpe-connector/1"],
        "config_schema": schema,
    })
}

#[test]
fn constants_match_spec() {
    assert_eq!((MAX_LENGTH, MAX_EXPANSION, MAX_NESTING), (1024, 4096, 64));
}

/// 拒绝原因与规范参考实现/ Python SDK 同名（reason 不跨实现断言，此处钉住实现自己的口径）。
#[test]
fn rejection_reasons() {
    let cases: &[(&str, &str)] = &[
        ("(?=a)b", "lookaround"),
        ("(?<!a)b", "lookaround"),
        (r"\p{L}", "unicode_property_escape"),
        (r"(a)\1", "backreference"),
        ("(?i)a", "inline_flag"),
        ("(?<n>a)", "named_group"),
        ("a*?", "lazy_quantifier"),
        ("a{2}+", "possessive_quantifier"),
        ("a{2}{3}", "stacked_quantifier"),
        ("{,3}", "invalid_repetition"),
        ("a{2,1}", "invalid_repetition"),
        ("[z-a]", "class_range_order"),
        (r"[\d-z]", "class_range_endpoint"),
        ("[]", "empty_class"),
        ("[^]", "empty_class"),
        ("[a&&b]", "class_set_operation"),
        ("[a~~b]", "class_set_operation"),
        ("a^b", "anchor_position"),
        ("(^a)", "anchor_position"),
        ("(a$)", "anchor_position"),
        ("a$b", "anchor_position"),
        (r"\q", "unknown_escape"),
        (r"\/", "unknown_escape"),
        (r"\b", "unknown_escape"),
        (r"\u0041", "unknown_escape"),
        ("[a-b-c]", "class_dash_position"),
        ("*a", "syntax"),
        ("[a", "syntax"),
        ("(a", "syntax"),
        ("a{4097}", "expansion"),
        ("a{4097,}", "expansion"),
        ("a{1,4097}", "expansion"),
        ("[aaaa]{1025}", "expansion"),
    ];
    for (pattern, expected) in cases {
        assert_eq!(reason(pattern), *expected, "{pattern:?}");
    }
    assert_eq!(reason(&"a".repeat(MAX_LENGTH + 1)), "length");
}

#[test]
fn valid_patterns_pass_and_compile() {
    let patterns = [
        r"^[a-z][a-z0-9_]*$",
        r"^(?:ab|cd)+$",
        "(a|)",
        "()",
        "[-a]",
        "[a-]",
        r"[\-]",
        r"[^\]a]",
        r"\xE9",
        r"[\x00-\x1f]",
        r"[\D]",
        r"[^\D]",
        r"[^\W]",
        "a{0}",
        "a{4096}",
        "a{4096,}",
        "a{1,4096}",
        "[aaaa]{1024}",
        "(?:a|bc){1365}",
        r"\W{255}",
        "[&]",
        r"[a\x26\x26b]",
        "",
    ];
    for pattern in patterns {
        check_pattern(pattern).unwrap_or_else(|e| panic!("{pattern:?}: {e}"));
        Pattern::new(pattern).unwrap_or_else(|e| panic!("{pattern:?}: {e}"));
    }
    check_pattern(&".".repeat(MAX_LENGTH)).unwrap();
    Pattern::new(&".".repeat(MAX_LENGTH)).unwrap();
}

/// 规范最坏情况：16 个跨字节宽度的互不相邻码点的手写类 × 255（展开规模 4080，与
/// scripts/pattern_budget 探针同款）——各实现 MUST 能编译并匹配。
#[test]
fn worst_case_shape_compiles_and_matches() {
    let klass: String = (0..16)
        .map(|k| char::from_u32(0x100 + 0x110 * k).unwrap())
        .collect();
    let pattern = format!("[{klass}]{{255}}");
    check_pattern(&pattern).unwrap();
    let compiled = Pattern::new(&pattern).unwrap();
    assert!(compiled.is_match(&klass.repeat(255)));
    assert!(!compiled.is_match(&"A".repeat(255)));
}

#[test]
fn expansion_counting_is_not_deduplicated() {
    check_pattern("[aaaa]{1024}").unwrap(); // 恰 4096
    assert_eq!(reason("[aaaa]{1025}"), "expansion");
}

#[test]
fn expansion_factor_for_open_ranges() {
    check_pattern("a{4096,}").unwrap();
    assert_eq!(reason("a{4097,}"), "expansion");
}

#[test]
fn nesting_depth_bound() {
    check_pattern(&format!(
        "{}a{}",
        "(".repeat(MAX_NESTING),
        ")".repeat(MAX_NESTING)
    ))
    .unwrap();
    check_pattern(&format!(
        "{}a{{4096}}{}",
        "(".repeat(MAX_NESTING),
        ")".repeat(MAX_NESTING)
    ))
    .unwrap();
    // 最坏嵌套形状：每层分组都带量词（引擎的解析嵌套还计入量词与字符类）
    let worst = format!(
        "{}[a]*{}",
        "(?:".repeat(MAX_NESTING),
        ")*".repeat(MAX_NESTING)
    );
    check_pattern(&worst).unwrap();
    Pattern::new(&worst).unwrap();
    assert_eq!(
        reason(&format!(
            "{}a{}",
            "(".repeat(MAX_NESTING + 1),
            ")".repeat(MAX_NESTING + 1)
        )),
        "nesting_depth"
    );
    // 极深嵌套不得退化为栈溢出
    assert_eq!(
        reason(&format!("{}a{}", "(".repeat(500), ")".repeat(500))),
        "nesting_depth"
    );
}

#[test]
fn class_sequence_rule_is_adjacency_based() {
    for pattern in [
        r"[\x2D\x2D]",
        "[-a]",
        "[a-]",
        r"[\-\-]",
        "[&]",
        r"[a\x26\x26b]",
        r"[a\x7E\x7Eb]",
    ] {
        check_pattern(pattern).unwrap_or_else(|e| panic!("{pattern:?}: {e}"));
    }
    for pattern in [r"[a\--b]", "[a--b]", "[--]", "[a&&b]", "[a~~b]"] {
        assert_eq!(reason(pattern), "class_set_operation", "{pattern:?}");
    }
}

#[test]
fn zero_size_atom_repetition_is_bounded() {
    for pattern in ["(?:){4096}", "(){4096}"] {
        check_pattern(pattern).unwrap_or_else(|e| panic!("{pattern:?}: {e}"));
        Pattern::new(pattern).unwrap();
    }
    for pattern in ["(?:){4097}", "(){4294967296}", "(?:(?:){4096}){4096}"] {
        assert_eq!(reason(pattern), "expansion", "{pattern:?}");
    }
}

/// 取反后为空的类（`[^\d\D]` 覆盖全体标量值）不匹配任何输入——转译不得产出编译失败或被
/// 引擎按「无模式」处理的形态。
#[test]
fn empty_negated_class_matches_nothing() {
    Pattern::new(r"[^\d\D]").unwrap();
    assert_eq!(search(r"[^\d\D]", "a"), Ok(false));
    assert_eq!(search(r"[^\d\D]", ""), Ok(false));
    assert_eq!(search(r"(?:a|[^\d\D])", "a"), Ok(true));
}

/// 空 pattern 语义为「匹配任意串」。
#[test]
fn empty_pattern_matches_any_string() {
    assert_eq!(search("", ""), Ok(true));
    assert_eq!(search("", "anything"), Ok(true));
}

/// `search` 未锚定；`$` 不匹配末尾换行之前；`.` 不含 \r、\n。
#[test]
fn search_semantics_follow_spec() {
    assert_eq!(search("acme", "https://acme.example/x"), Ok(true));
    assert_eq!(search("acme", "ACM"), Ok(false));
    assert_eq!(search("^main$", "main"), Ok(true));
    assert_eq!(search("^main$", "main\n"), Ok(false));
    assert_eq!(search("^.$", "😀"), Ok(true));
    assert_eq!(search("^.$", "\r"), Ok(false));
    assert_eq!(search("^.$", "\n"), Ok(false));
    assert!(
        search("(?=a)", "a").is_err(),
        "不合法 pattern 经 search 返回错误"
    );
}

// ---------------------------------------------------------------------------
// 子集语义经 validate_config 的端到端行为
// ---------------------------------------------------------------------------

fn manifest_with(schema: Value) -> dpe_sdk::run::manifest::Manifest {
    parse_manifest(&with_schema(schema)).expect("清单应有效")
}

#[test]
fn ascii_classes_and_anchors_end_to_end() {
    let manifest = manifest_with(json!({
        "type": "object",
        "properties": {"repo": {"type": "string", "pattern": "^\\w+$"}},
    }));
    validate_config(&manifest, &json!({"repo": "abc_9"})).unwrap();
    assert!(
        validate_config(&manifest, &json!({"repo": "héllo"})).is_err(),
        "\\w 为 ASCII"
    );
}

#[test]
fn data_keys_named_pattern_are_not_keywords() {
    let manifest = manifest_with(json!({
        "type": "object",
        "const": {"pattern": "(?=x)"},
        "default": {"pattern": "("},
        "examples": [{"pattern": "[z-a]"}],
    }));
    validate_config(&manifest, &json!({"pattern": "(?=x)"})).unwrap();
}

#[test]
fn empty_pattern_properties_key_matches_every_property() {
    let manifest = manifest_with(json!({
        "type": "object",
        "patternProperties": {"": {"type": "integer"}},
        "additionalProperties": false,
    }));
    validate_config(&manifest, &json!({"any": 1})).unwrap();
    assert!(validate_config(&manifest, &json!({"any": "s"})).is_err());
}

#[test]
fn additional_properties_coverage_uses_subset_semantics() {
    let manifest = manifest_with(json!({
        "type": "object",
        "patternProperties": {"^\\w+$": {}},
        "additionalProperties": false,
    }));
    validate_config(&manifest, &json!({"abc": 1})).unwrap();
    for bad in [json!({"é": 1}), json!({"a\n": 1})] {
        // \w 为 ASCII；$ 不匹配末尾换行之前
        assert!(validate_config(&manifest, &bad).is_err(), "{bad}");
    }
}

#[test]
fn pattern_properties_translation_collision_keeps_all_subschemas() {
    let manifest = manifest_with(json!({
        "type": "object",
        "patternProperties": {"\\d": {"type": "string"}, "[0-9]": {"maxLength": 1}},
    }));
    validate_config(&manifest, &json!({"1": "a"})).unwrap();
    assert!(
        validate_config(&manifest, &json!({"1": "ab"})).is_err(),
        "转译后同形的两个键的子 schema 都必须适用"
    );
}

#[test]
fn error_message_shows_original_pattern() {
    let manifest = manifest_with(json!({
        "type": "object",
        "properties": {"r": {"type": "string", "pattern": "^\\w+$"}},
    }));
    let failure = validate_config(&manifest, &json!({"r": "héllo"})).unwrap_err();
    let message = failure.message();
    assert!(message.contains(&format!("{:?}", "^\\w+$")), "{message}");
    assert!(message.contains("/r"), "{message}");
    assert!(
        !message.contains("\\x{30}"),
        "不得泄漏转译后的写法：{message}"
    );
}
