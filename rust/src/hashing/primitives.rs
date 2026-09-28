//! hash-contract-v1 §2 / §6 通用原语。
//!
//! [`json_stable`] 必须与 Python `json.dumps(x, sort_keys=True, ensure_ascii=False)` 逐字节一致，
//! 而 `serde_json` 的默认输出在以下几处都不同，所以这里手写序列化：
//!
//! | 项 | Python（基准） | serde_json 默认 |
//! | --- | --- | --- |
//! | 分隔符 | `", "` / `": "` | `","` / `":"` |
//! | 浮点 | `1e+16`、`1.5e-05`、`1.0` | `1e16`、`1.5e-5`、`1.0` |
//! | 控制字符 | `\u001f`（小写十六进制），DEL / U+2028 原样 | 不同实现不一 |
//!
//! 键排序：Rust `String` 的 `Ord` 按 UTF-8 字节比较，等价于 Unicode 码点序，与 Python 一致。

use std::fmt::Write as _;

use serde_json::{Number, Value};
use sha2::{Digest, Sha256};

/// 长度前缀拼接：每个 part 前置 4 字节大端无符号长度。
pub fn concat_parts(parts: &[&[u8]]) -> Vec<u8> {
    let mut out = Vec::with_capacity(parts.iter().map(|p| p.len() + 4).sum());
    for part in parts {
        let len = u32::try_from(part.len()).expect("hash part exceeds 4 GiB");
        out.extend_from_slice(&len.to_be_bytes());
        out.extend_from_slice(part);
    }
    out
}

/// `sha256` 十六进制摘要的前 32 个字符（128 位）。
pub fn digest(data: &[u8]) -> String {
    let mut hex = hex::encode(Sha256::digest(data));
    hex.truncate(32);
    hex
}

/// 稳定 JSON（见模块文档）。
pub fn json_stable(value: &Value) -> String {
    let mut out = String::new();
    write_value(value, &mut out);
    out
}

fn write_value(value: &Value, out: &mut String) {
    match value {
        Value::Null => out.push_str("null"),
        Value::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        Value::Number(n) => write_number(n, out),
        Value::String(s) => write_string(s, out),
        Value::Array(items) => {
            out.push('[');
            for (i, item) in items.iter().enumerate() {
                if i > 0 {
                    out.push_str(", ");
                }
                write_value(item, out);
            }
            out.push(']');
        }
        Value::Object(map) => {
            let mut keys: Vec<&String> = map.keys().collect();
            keys.sort();
            out.push('{');
            for (i, key) in keys.into_iter().enumerate() {
                if i > 0 {
                    out.push_str(", ");
                }
                write_string(key, out);
                out.push_str(": ");
                write_value(&map[key], out);
            }
            out.push('}');
        }
    }
}

fn write_string(s: &str, out: &mut String) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{08}' => out.push_str("\\b"),
            '\u{0c}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 => {
                let _ = write!(out, "\\u{:04x}", c as u32);
            }
            c => out.push(c),
        }
    }
    out.push('"');
}

fn write_number(n: &Number, out: &mut String) {
    if n.is_f64() {
        out.push_str(&python_float_repr(n.as_f64().expect("f64 number")));
    } else {
        out.push_str(&n.to_string());
    }
}

/// Python `float.__repr__`：最短往返数字；指数在 `[-4, 16)` 内用定点记法（整数补 `.0`），
/// 否则用科学计数法，指数带符号且至少两位（`1e+16`、`1.5e-05`）。
pub fn python_float_repr(f: f64) -> String {
    if f.is_nan() {
        return "NaN".into();
    }
    if f.is_infinite() {
        return if f > 0.0 { "Infinity".into() } else { "-Infinity".into() };
    }
    if f == 0.0 {
        return if f.is_sign_negative() { "-0.0".into() } else { "0.0".into() };
    }
    // Rust 的 `{:e}` 同样输出最短往返数字，形如 `-1.2345e-7`
    let sci = format!("{f:e}");
    let (mantissa, exp) = sci.split_once('e').expect("scientific format");
    let exp: i32 = exp.parse().expect("exponent");
    let (sign, mantissa) = mantissa.strip_prefix('-').map_or(("", mantissa), |m| ("-", m));
    let digits: String = mantissa.chars().filter(|c| *c != '.').collect();

    if (-4..16).contains(&exp) {
        let point = exp + 1; // 小数点位于第 point 个数字之后
        let body = if point <= 0 {
            format!("0.{}{}", "0".repeat((-point) as usize), digits)
        } else if point as usize >= digits.len() {
            format!("{}{}.0", digits, "0".repeat(point as usize - digits.len()))
        } else {
            let (int, frac) = digits.split_at(point as usize);
            format!("{int}.{frac}")
        };
        format!("{sign}{body}")
    } else {
        let mantissa = if digits.len() > 1 { format!("{}.{}", &digits[..1], &digits[1..]) } else { digits };
        let exp_sign = if exp < 0 { '-' } else { '+' };
        format!("{sign}{mantissa}e{exp_sign}{:02}", exp.abs())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn concat_parts_is_unambiguous() {
        assert_ne!(concat_parts(&[b"ab", b"c"]), concat_parts(&[b"a", b"bc"]));
        assert_eq!(concat_parts(&[b"ab"]), b"\x00\x00\x00\x02ab");
    }

    #[test]
    fn float_repr_matches_python() {
        let cases = [
            (1.0, "1.0"),
            (0.5, "0.5"),
            (1e16, "1e+16"),
            (1234567890123456.0, "1234567890123456.0"),
            (1.5e-5, "1.5e-05"),
            (0.0001, "0.0001"),
            (-2.5, "-2.5"),
            (-0.0, "-0.0"),
            (std::f64::consts::PI, "3.141592653589793"),
            (1e22, "1e+22"),
            (123.456, "123.456"),
            (1.7976931348623157e308, "1.7976931348623157e+308"),
            (5e-324, "5e-324"),
        ];
        for (f, expected) in cases {
            assert_eq!(python_float_repr(f), expected, "{f}");
        }
    }

    #[test]
    fn json_stable_matches_python_format() {
        assert_eq!(json_stable(&json!({"b": 1, "a": "中文", "c": [1, 2]})), r#"{"a": "中文", "b": 1, "c": [1, 2]}"#);
        // 码点序：U+FFFF 在 U+2000B 之前（UTF-16 码元序下顺序相反）
        assert_eq!(json_stable(&json!({"𠀋": 1, "\u{ffff}": 0})), "{\"\u{ffff}\": 0, \"𠀋\": 1}");
        assert_eq!(json_stable(&json!("a\u{1}\u{7f}\u{2028}")), "\"a\\u0001\u{7f}\u{2028}\"");
    }
}
