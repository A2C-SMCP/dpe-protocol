//! `parse_ijson`（core §2.8 第 0 步的解析部分）：拒绝重复键与孤立代理项；数值不在解析阶段
//! 拒绝，保留字面量交给校验第 4 步。
//!
//! Python 侧的「非 UTF-8」「非 JSON 值」用例在 Rust 中部分无法构造（`&str` 保证 UTF-8、
//! `Value` 只能承载合法 JSON），由类型系统与解析阶段保证，不在此重复。

mod common;

use dpe_hash::{content_hash, parse_ijson, ErrorKind, CONTRACT};
use serde_json::Value;

use common::load;

/// 向量里的 `input_json` 用例：`ijson_*` 在解析阶段被拒（`DPE_VALIDATION`、位置为对象自身）；
/// `number_overflow_*` 解析成功且数值字面量保留（越界由校验第 4 步在出错的值上拒绝）。
#[test]
fn invalid_vector_input_json() {
    let mut rejected = 0;
    let mut overflow = 0;
    for vec in load("invalid") {
        for case in vec["cases"].as_array().unwrap() {
            let Some(text) = case.get("input_json").and_then(Value::as_str) else {
                continue;
            };
            let name = case["name"].as_str().unwrap();
            match parse_ijson(text) {
                Ok(value) => {
                    assert!(!name.starts_with("ijson_"), "{name}: 应被拒绝");
                    // 字面量保留为数值（未被解析阶段拒绝；精确表示的完整链路见
                    // out_of_range_numbers_reach_validation_step）
                    assert!(value["metadata"]["a"].is_number(), "{name}: 数值应原样保留");
                    overflow += 1;
                }
                Err(e) => {
                    assert!(name.starts_with("ijson_"), "{name}: 不应被拒绝：{e}");
                    assert_eq!(e.kind(), ErrorKind::Validation, "{name}");
                    assert_eq!(e.code(), "DPE_VALIDATION", "{name}");
                    assert_eq!(e.path(), "", "{name}");
                    rejected += 1;
                }
            }
        }
    }
    assert!(rejected >= 6, "向量应有 ijson 拒绝用例");
    assert!(overflow >= 2, "向量应有数值越界用例");
}

/// 重复键在任意深度都被拒绝（对象嵌套、对象数组内）。
#[test]
fn duplicate_keys_rejected_at_any_depth() {
    for text in [
        r#"{"category": "Title", "category": "Video"}"#,
        r#"{"a": {"b": 1, "b": 2}}"#,
        r#"[{"a": 1, "a": 1}]"#,
    ] {
        let e = parse_ijson(text).expect_err(text);
        assert_eq!(e.kind(), ErrorKind::Validation, "{text}");
        assert_eq!(e.code(), "DPE_VALIDATION", "{text}");
        assert_eq!(e.path(), "", "{text}");
    }
}

/// 孤立代理项与 `NaN` / `Infinity` 等非 JSON 数值、坏 JSON 都在解析阶段被拒。
#[test]
fn non_ijson_text_rejected() {
    for text in [
        r#"{"category": "Title", "text": "\ud800"}"#,
        r#"{"category": "Title", "metadata": {"\udc00": 1}}"#,
        r#"{"x": NaN}"#,
        r#"{"x": Infinity}"#,
        r#"{"category": "Title""#,
    ] {
        let e = parse_ijson(text).expect_err(text);
        assert_eq!(e.kind(), ErrorKind::Validation, "{text}");
        assert_eq!(e.path(), "", "{text}");
    }
}

/// 合法 JSON 原样通过（含边界整数与浮点），与常规解析结果相同。
#[test]
fn valid_json_round_trips() {
    let text = r#"{"a": [1, 2.5, "x", null, true], "b": {"c": []}, "d": 9007199254740991}"#;
    assert_eq!(
        parse_ijson(text).unwrap(),
        serde_json::from_str::<Value>(text).unwrap()
    );
}

/// 越界数值不在解析阶段拒绝：`1e400` 与超长整数解析成功，由校验第 4 步在出错的值上拒绝
/// （与 Python 对超位数整数的行为对等）。
#[test]
fn out_of_range_numbers_reach_validation_step() {
    let value = parse_ijson(r#"{"category": "Title", "metadata": {"a": 1e400}}"#).unwrap();
    let e = content_hash(&value, CONTRACT).expect_err("1e400 应被拒绝");
    assert_eq!(e.code(), "DPE_VALIDATION");
    assert_eq!(e.path(), "/metadata/a");

    let text = format!(
        r#"{{"category": "Title", "metadata": {{"a": {}}}}}"#,
        "9".repeat(400)
    );
    let value = parse_ijson(&text).unwrap();
    let e = content_hash(&value, CONTRACT).expect_err("超界整数应被拒绝");
    assert_eq!(e.code(), "DPE_VALIDATION");
    assert_eq!(e.path(), "/metadata/a");
}
