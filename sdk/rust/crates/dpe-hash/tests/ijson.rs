//! `parse_ijson`（core §2.8 第 0 步的解析部分）：拒绝重复键与孤立代理项；数值不在解析阶段
//! 拒绝，保留字面量交给校验第 4 步。
//!
//! Python 侧的「非 UTF-8」「非 JSON 值」用例在 Rust 中部分无法构造（`&str` 保证 UTF-8、
//! `Value` 只能承载合法 JSON），由类型系统与解析阶段保证，不在此重复。

mod common;

use dpe_hash::{content_hash, parse_ijson, ErrorKind, CONTRACT};
use serde_json::{json, Value};

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

// ---------------------------------------------------------------------------
// 自建解析器专项
// ---------------------------------------------------------------------------

/// `arbitrary_precision` 下 serde_json 用 `$serde_json::private::Number` 的单键对象表示数字；
/// 源数据里恰好同形的对象是**内容**（北极星 P2：不设保留键、不做过滤），不得被读成数字、
/// 也不得被误拒——`serde_json` 的 `Value` 解析两种错误都会犯，本实现保留对象，
/// 与 Python 标准库解析器的结果一致（hash 一致）。
#[test]
fn private_number_token_is_source_content() {
    let text = r#"{"category":"Title","metadata":{"$serde_json::private::Number":"1"}}"#;
    let value = parse_ijson(text).expect("同形对象是合法的 I-JSON");
    assert!(value["metadata"]["$serde_json::private::Number"].is_string());
    assert_eq!(
        content_hash(&value, CONTRACT).unwrap(),
        content_hash(
            &json!({"category": "Title", "metadata": {"$serde_json::private::Number": "1"}}),
            CONTRACT
        )
        .unwrap()
    );

    // 多键、非数值串值、顶层形态同样保留为对象
    for text in [
        r#"{"a":{"$serde_json::private::Number":"1","x":2}}"#,
        r#"{"a":{"$serde_json::private::Number":"abc"}}"#,
        r#"{"$serde_json::private::Number":"1"}"#,
    ] {
        let value = parse_ijson(text).unwrap_or_else(|e| panic!("{text}: {e}"));
        assert!(value.is_object(), "{text}");
    }
}

/// 常规文本（无保留 token 场景）与 serde_json 的解析逐值一致：转义、代理对、数字、嵌套。
#[test]
fn agrees_with_serde_json_on_ordinary_text() {
    for text in [
        r#"{"a": [1, 2.5, "x", null, true], "b": {"c": []}, "d": 9007199254740991}"#,
        r#"{"s": "中文 emoji 😀 A 𝄞 \" \\ \/ \b \f \n \r \t"}"#,
        r#"{"n": [-0, 0.5, 1e2, 1E+2, 1e-2, 1.0, 0.1]}"#,
        r#"[]"#,
        r#"{}"#,
        r#"[[[[[1]]]]]"#,
        "{\"k\": \"\u{7f}\u{10ffff}\"}",
    ] {
        assert_eq!(
            parse_ijson(text).unwrap_or_else(|e| panic!("{text}: {e}")),
            serde_json::from_str::<Value>(text).unwrap_or_else(|e| panic!("{text}: {e}")),
            "{text}"
        );
    }
}

/// 字符串转义按 RFC 8259 逐个校验；`\u0000` 是合法字符，代理对必须成对。
#[test]
fn string_escapes_validated() {
    for text in [
        r#"{"a": "\x"}"#,        // 非法转义字符
        r#"{"a": "\u12G4"}"#,    // 非十六进制
        r#"{"a": "\u123"}"#,     // 位数不足
        r#"{"a": "\uD834"}"#,    // 孤立高代理项
        r#"{"a": "\uDD1E"}"#,    // 孤立低代理项
        r#"{"a": "\uD834A"}"#,   // 高代理项后不是低代理项
        "{\"a\": \"\t\"}",       // 未转义制表符（控制字符）
        "{\"a\": \"\u{0000}\"}", // 未转义 NUL
    ] {
        let e = parse_ijson(text).expect_err(text);
        assert_eq!(e.code(), "DPE_VALIDATION", "{text}");
        assert_eq!(e.path(), "", "{text}");
    }
    let value = parse_ijson(r#"{"a": "\u0000"}"#).unwrap();
    assert_eq!(value["a"].as_str().unwrap(), "\0");
    // 合法代理对转义（𝄞 → 𝄞，U+1D11E）在键位置与值位置都解码成功
    let value = parse_ijson(r#"{"\uD834\uDD1E": "\uD834\uDD1E"}"#).unwrap();
    let key = value.as_object().unwrap().keys().next().unwrap();
    assert_eq!(key, "𝄞");
    assert_eq!(value["𝄞"], json!("𝄞"));
    // 转义与多字节字面字符、其他转义混用（走慢路径的混合场景）
    let value = parse_ijson(r#"{"a": "\uD834\uDD1E x\ty", "b": "\u0041"}"#).unwrap();
    assert_eq!(value["a"], json!("𝄞 x\ty"));
    assert_eq!(value["b"], json!("A"));
}

/// 数字按 RFC 8259 语法校验；越界与否不在此判定（第 4 步）。
#[test]
fn number_syntax_validated() {
    for text in [
        "01", "-", "+1", ".5", "1.", "1e", "1e+", "- 1", "0x1", "1_000",
    ] {
        let e = parse_ijson(text).expect_err(text);
        assert_eq!(e.code(), "DPE_VALIDATION", "{text}");
    }
    for text in [
        "0",
        "-0",
        "0.5",
        "1e2",
        "1E+2",
        "1e-2",
        "9007199254740993",
        "1e400",
    ] {
        parse_ijson(text).unwrap_or_else(|e| panic!("{text}: {e}"));
    }
}

/// 结构语法：尾逗号、缺冒号/逗号、非字符串键、顶层多值、未闭合等都拒绝。
#[test]
fn structure_syntax_validated() {
    for text in [
        r#"{"a": 1,}"#,
        r#"[1,]"#,
        r#"{"a" 1}"#,
        r#"{a: 1}"#,
        r#"[1 2]"#,
        r#"{,}"#,
        "",
        "   ",
        "1 2",
        "truex",
        r#"{"a":}"#,
        "{",
    ] {
        let e = parse_ijson(text).expect_err(text);
        assert_eq!(e.code(), "DPE_VALIDATION", "{text}");
        assert_eq!(e.path(), "", "{text}");
    }
}

/// 解析防护上限（core §2.8 的 64 加展开视图的 4 层信封 = 68 层容器）：68 层通过、69 层拒绝。
///
/// 它只防递归耗尽栈，不是对象深度上界：对象界由校验按对象自身判定（见
/// `tests/nesting_depth.rs`），只要每个 DPE 对象都在上界内，解析不得拒收。
#[test]
fn parse_depth_guard() {
    let ok = format!("{}1{}", "[".repeat(68), "]".repeat(68));
    parse_ijson(&ok).expect("68 层容器应通过");
    let deep = format!("{}1{}", "[".repeat(69), "]".repeat(69));
    let e = parse_ijson(&deep).expect_err("69 层容器应被拒绝");
    assert_eq!(e.code(), "DPE_VALIDATION");
    assert_eq!(e.path(), "");
}
