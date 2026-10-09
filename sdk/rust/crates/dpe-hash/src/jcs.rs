//! RFC 8785 JCS 序列化（spec/hash-contract-1.md §3.3）。
//!
//! 按规范文本独立实现，与 `scripts/gen_vectors.py`、Python dpe-hash 的一致由向量逐字节校验，
//! 而不是共享代码。
//!
//! 解析得到的越界浮点字面量（如 `1e400`，解析为无穷）与绝对值超过 2^53−1 的整数字面量
//! 一律拒绝，错误带出错位置的 JSON Pointer。注意 `serde_json::Value` 本身无法承载 NaN /
//! Infinity：在 Rust 里用 `json!` / `Value::from(f64)` 构造时，非有限 f64 已被 serde_json
//! 转成 `null`，本模块看到的只是 null（见 crate 文档「I-JSON 与数值」）。
//!
//! 数值按字面文本判定：含 `.` / `e` / `E` 的是浮点，否则是整数——与 serde_json 是否开启
//! `arbitrary_precision` 无关。

use std::cmp::Ordering;
use std::fmt::Write as _;

use serde_json::{Number, Value};

use crate::error::{Error, ErrorKind, Result};

const MAX_SAFE_INTEGER: u64 = (1 << 53) - 1;

/// 序列化中途的错误：逐层回溯时收集路径分量，最外层再转换为公开错误。
pub(crate) struct Invalid {
    kind: ErrorKind,
    message: String,
    segments: Vec<String>,
}

impl Invalid {
    fn new(kind: ErrorKind, message: String) -> Self {
        Invalid {
            kind,
            message,
            segments: Vec::new(),
        }
    }
}

/// 把 JSON 值序列化为 RFC 8785 规范化字符串（不做 null 键删除等任何 DPE 规范化）。
pub fn jcs(value: &Value) -> Result<String> {
    canonical(value, false, "")
}

/// JCS 规范化字符串；`strip_nulls` 为真时同时递归删除值为 null 的键（契约 1 §3.2）。
/// `at` 是 `value` 自身的 JSON Pointer，用作错误路径的前缀。
pub(crate) fn canonical(value: &Value, strip_nulls: bool, at: &str) -> Result<String> {
    let mut out = String::new();
    write_value(value, strip_nulls, &mut out).map_err(|mut err| {
        err.segments.reverse();
        let mut path = at.to_owned();
        for segment in &err.segments {
            push_segment(&mut path, segment);
        }
        Error::new(err.kind, err.message, path)
    })?;
    Ok(out)
}

/// 按 UTF-16 码元序比较（JCS 的键排序规则）。
pub(crate) fn utf16_cmp(a: &str, b: &str) -> Ordering {
    a.encode_utf16().cmp(b.encode_utf16())
}

/// 在 JSON Pointer 末尾追加一个路径分量（RFC 6901 转义 `~` 与 `/`）。
pub(crate) fn push_segment(path: &mut String, segment: &str) {
    path.push('/');
    for ch in segment.chars() {
        match ch {
            '~' => path.push_str("~0"),
            '/' => path.push_str("~1"),
            c => path.push(c),
        }
    }
}

/// `at` 之后追加若干分量得到的 JSON Pointer。
pub(crate) fn pointer(at: &str, segments: &[&str]) -> String {
    let mut path = at.to_owned();
    for segment in segments {
        push_segment(&mut path, segment);
    }
    path
}

/// 字符串的 JCS 形式（含两侧引号）：只转义 `"`、`\` 与控制字符，其余原样输出。
pub(crate) fn write_string(s: &str, out: &mut String) {
    out.push('"');
    for ch in s.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\u{08}' => out.push_str("\\b"),
            '\t' => out.push_str("\\t"),
            '\n' => out.push_str("\\n"),
            '\u{0c}' => out.push_str("\\f"),
            '\r' => out.push_str("\\r"),
            c if (c as u32) < 0x20 => {
                let _ = write!(out, "\\u{:04x}", c as u32);
            }
            c => out.push(c),
        }
    }
    out.push('"');
}

fn write_value(value: &Value, strip_nulls: bool, out: &mut String) -> Result<(), Invalid> {
    match value {
        Value::Null => out.push_str("null"),
        Value::Bool(true) => out.push_str("true"),
        Value::Bool(false) => out.push_str("false"),
        Value::Number(n) => out.push_str(&number(n)?),
        Value::String(s) => write_string(s, out),
        Value::Array(items) => {
            out.push('[');
            for (i, item) in items.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                // 数组元素不受 null 删除影响
                write_value(item, strip_nulls, out).map_err(|mut err| {
                    err.segments.push(i.to_string());
                    err
                })?;
            }
            out.push(']');
        }
        Value::Object(map) => {
            let mut entries: Vec<(&String, &Value)> = map
                .iter()
                .filter(|(_, v)| !(strip_nulls && v.is_null()))
                .collect();
            entries.sort_by(|(a, _), (b, _)| utf16_cmp(a, b));
            out.push('{');
            for (i, (key, item)) in entries.into_iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                write_string(key, out);
                out.push(':');
                write_value(item, strip_nulls, out).map_err(|mut err| {
                    err.segments.push(key.clone());
                    err
                })?;
            }
            out.push('}');
        }
    }
    Ok(())
}

/// ECMAScript `Number::toString`（base 10），按字面文本区分整数与浮点。
fn number(n: &Number) -> Result<String, Invalid> {
    let text = n.to_string();
    if text.bytes().any(|b| matches!(b, b'.' | b'e' | b'E')) {
        let x: f64 = text.parse().map_err(|_| {
            Invalid::new(
                ErrorKind::Validation,
                format!("不是合法的 JSON 数值：{text}"),
            )
        })?;
        if !x.is_finite() {
            return Err(Invalid::new(
                ErrorKind::Validation,
                "JCS 不允许 NaN / Infinity".into(),
            ));
        }
        return Ok(float(x));
    }
    let (negative, digits) = match text.strip_prefix('-') {
        Some(rest) => (true, rest),
        None => (false, text.as_str()),
    };
    match digits.parse::<u64>() {
        Ok(0) => Ok("0".into()), // 含 -0
        Ok(u) if u <= MAX_SAFE_INTEGER => Ok(if negative {
            format!("-{u}")
        } else {
            u.to_string()
        }),
        _ => Err(Invalid::new(
            ErrorKind::IntegerOutOfRange,
            format!("整数超出 ±(2^53−1)：{text}"),
        )),
    }
}

/// 有限 f64 的 ES 格式：先取最短往返十进制的有效数字与指数，再按 ES 规则重排。
/// 不依赖任何全局状态（区域设置、精度上下文）。
///
/// 有效数字取自 Ryu，不用标准库的 `{:e}`：契约 1 §3.3 要求最短位数中取最接近真值者、
/// 等距时取偶数（Python `repr` 与生成器同此），标准库在等距时不取偶数
/// （如 1059438285926254.25 输出 `…54.3`，规范值为 `…54.2`，向量 `jcs_numbers`）。
fn float(x: f64) -> String {
    if x == 0.0 {
        return "0".into(); // 含 -0.0
    }
    let mut buffer = ryu::Buffer::new();
    // Ryu 的输出形如 `1.5e22`、`1e-7`、`0.001`、`123.0`：统一拆成 value = 0.digits × 10^n
    let shortest = buffer.format_finite(x.abs());
    let (mantissa, exponent) = shortest.split_once('e').unwrap_or((shortest, "0"));
    let exponent: i64 = exponent.parse().expect("Ryu 的指数是整数");
    let (int_part, frac_part) = mantissa.split_once('.').unwrap_or((mantissa, ""));
    let all_digits = format!("{int_part}{frac_part}");
    let leading = all_digits.len() - all_digits.trim_start_matches('0').len();
    let digits = all_digits.trim_matches('0').to_owned();
    let k = digits.len() as i64;
    let n = int_part.len() as i64 + exponent - leading as i64;
    let body = if k <= n && n <= 21 {
        format!("{digits}{}", "0".repeat((n - k) as usize))
    } else if 0 < n && n <= 21 {
        let split = n as usize;
        format!("{}.{}", &digits[..split], &digits[split..])
    } else if -6 < n && n <= 0 {
        format!("0.{}{digits}", "0".repeat((-n) as usize))
    } else {
        let mantissa = if k > 1 {
            format!("{}.{}", &digits[..1], &digits[1..])
        } else {
            digits.clone()
        };
        let e = n - 1;
        format!("{mantissa}e{}{}", if e >= 0 { "+" } else { "-" }, e.abs())
    };
    if x < 0.0 {
        format!("-{body}")
    } else {
        body
    }
}
