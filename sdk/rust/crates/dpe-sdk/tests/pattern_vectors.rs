//! 消费向量 kind：connector 契约 §4.1.1（`config_schema` 的正则子集）的
//! `vectors/config_schema_patterns.json` 四组用例（valid_patterns / invalid_patterns /
//! match_cases / schema_cases）全部逐条消费。
//!
//! 跨 crate 登记标记（`sdk/rust/vector-consumers.json` → `dpe-hash/tests/vectors.rs` 的
//! `all_vector_kinds_have_consumers` 核验本文件存在且带下行标记，登记与真实消费方不脱钩）：
//!
//! vector-kind: pattern

mod common;

use serde_json::{json, Value};

use dpe_sdk::run::manifest::{parse_manifest, validate_config};
use dpe_sdk::run::pattern::{check_pattern, search, Pattern};

/// §4.1 的示例清单：向量的 `schema` 被包进清单对象（清单 JSON 深度计数含包装层，vectors/README.md）。
fn with_schema(schema: Value) -> Value {
    json!({
        "manifest_version": 1,
        "name": "git-connector",
        "version": "0.1.0",
        "protocol_versions": ["dpe-connector/1"],
        "config_schema": schema,
    })
}

fn patterns_vector() -> Value {
    let vector = common::read("config_schema_patterns.json");
    assert_eq!(vector["kind"], "pattern");
    vector
}

/// valid_patterns：MUST 被接受且可转译（转译即编译为显式码点区间的匹配器）。
#[test]
fn valid_patterns_are_accepted_and_compilable() {
    let vector = patterns_vector();
    let cases = vector["valid_patterns"].as_array().unwrap();
    assert!(!cases.is_empty(), "向量不应为空");
    for entry in cases {
        let pattern = entry["pattern"].as_str().unwrap();
        check_pattern(pattern).unwrap_or_else(|e| panic!("{pattern:?}: {e}"));
        Pattern::new(pattern).unwrap_or_else(|e| panic!("{pattern:?} 不可转译/编译：{e}"));
    }
}

/// invalid_patterns：MUST 被拒绝。`pattern_json` 的输入（孤立代理项）无法以 Rust 字符串表达，
/// 严格解析在解析阶段即拒绝——与「不是 Unicode 标量值序列」同一裁定（同 hash 向量的
/// `input_json` 口径）。
#[test]
fn invalid_patterns_are_rejected() {
    let vector = patterns_vector();
    let cases = vector["invalid_patterns"].as_array().unwrap();
    assert!(!cases.is_empty(), "向量不应为空");
    for entry in cases {
        match entry.get("pattern") {
            Some(pattern) => {
                let pattern = pattern.as_str().unwrap();
                assert!(check_pattern(pattern).is_err(), "应被拒绝：{pattern:?}");
            }
            None => match common::parse_strict(entry["pattern_json"].as_str().unwrap()) {
                Err(common::Rejected) => {}
                Ok(value) => {
                    let pattern = value.as_str().expect("pattern_json 是字符串");
                    assert!(check_pattern(pattern).is_err(), "应被拒绝：{pattern:?}");
                }
            },
        }
    }
}

/// match_cases：逐条钉住 §4.1.1 的匹配语义；`value_json` 同 `pattern_json` 的约定。
#[test]
fn match_cases_follow_spec_semantics() {
    let vector = patterns_vector();
    let cases = vector["match_cases"].as_array().unwrap();
    assert!(!cases.is_empty(), "向量不应为空");
    for case in cases {
        let pattern = case["pattern"].as_str().unwrap();
        let expected = case["match"].as_bool().unwrap();
        let value = match case.get("value") {
            Some(value) => value.clone(),
            None => match common::parse_strict(case["value_json"].as_str().unwrap()) {
                Ok(value) => value,
                Err(common::Rejected) => {
                    // 含孤立代理项的值不是 Unicode 标量值序列，Rust 侧不可表达；
                    // 匹配宇宙是标量值，本向量对此类输入的期望即「不匹配」——核对该期望后跳过。
                    assert!(!expected, "不可表达的值只允许期望不匹配：{case}");
                    continue;
                }
            },
        };
        let value = value.as_str().expect("向量的 value 是字符串");
        let got = search(pattern, value).unwrap_or_else(|e| panic!("{pattern:?}: {e}"));
        assert_eq!(got, expected, "pattern {pattern:?} 对 {value:?}");
    }
}

/// schema_cases：清单合法性（manifest_valid）与实例配置求值（config / config_valid）逐条消费，
/// 与 Python SDK 的 `test_schema_cases` 同口径（两者必须成对出现，向量侧已有断言）。
#[test]
fn schema_cases() {
    let vector = patterns_vector();
    let cases = vector["schema_cases"].as_array().unwrap();
    assert!(!cases.is_empty(), "向量不应为空");
    for case in cases {
        let name = case["name"].as_str().unwrap();
        let manifest_data = with_schema(case["schema"].clone());
        if case["manifest_valid"].as_bool().unwrap() {
            let manifest = parse_manifest(&manifest_data)
                .unwrap_or_else(|e| panic!("{name}：清单应有效却失败：{e}"));
            if let Some(config) = case.get("config") {
                let expected = case["config_valid"].as_bool().unwrap();
                match validate_config(&manifest, config) {
                    Ok(()) => assert!(expected, "{name}：config_valid 期望 false，实得通过"),
                    Err(error) => {
                        assert!(!expected, "{name}：config_valid 期望 true，实得：{error}");
                        assert_eq!(error.code(), "config_schema_violation", "{name}");
                    }
                }
            }
        } else {
            let error =
                parse_manifest(&manifest_data).expect_err(&format!("{name}：清单应不合法却通过"));
            assert_eq!(error.code(), "manifest_invalid", "{name}");
        }
    }
}
