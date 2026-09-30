//! RFC 8785 JCS 序列化（spec/hash-contract-1.md §3.3）。
//!
//! 与 Python 实现、向量生成器互相独立：对齐对象是 JCS 规范文本，
//! 一致性由向量逐字节校验。

use serde_json::Value;

/// ECMAScript `Number::toString`（base 10）。
fn es_number(n: &serde_json::Number) -> Result<String, String> {
    if let Some(i) = n.as_i64() {
        if i.unsigned_abs() > (1u64 << 53) - 1 {
            return Err(format!("integer out of IEEE-754 safe range: {i}"));
        }
        return Ok(i.to_string());
    }
    if let Some(u) = n.as_u64() {
        if u > (1u64 << 53) - 1 {
            return Err(format!("integer out of IEEE-754 safe range: {u}"));
        }
        return Ok(u.to_string());
    }
    let f = n.as_f64().ok_or("not a JSON number")?;
    if !f.is_finite() {
        return Err("NaN / Infinity not allowed in JCS".into());
    }
    if f == 0.0 {
        return Ok("0".into()); // 含 -0.0
    }
    // `{:e}` 给出最短往返十进制（`d[.ddd]e±x`），再按 ES 规则重排。
    let sci = format!("{f:e}");
    let (sign, rest) = match sci.strip_prefix('-') {
        Some(r) => ("-", r),
        None => ("", sci.as_str()),
    };
    let (mantissa, exp_str) = rest.split_once('e').ok_or("malformed {:e} output")?;
    let exp: i64 = exp_str.parse().map_err(|_| "malformed exponent")?;
    let digits: String = mantissa.chars().filter(|c| *c != '.').collect();
    let k = digits.len() as i64;
    let n_pos = exp + 1; // value = 0.digits × 10^n_pos
    let body = if k <= n_pos && n_pos <= 21 {
        format!("{digits}{}", "0".repeat((n_pos - k) as usize))
    } else if 0 < n_pos && n_pos <= 21 {
        let split = n_pos as usize;
        format!("{}.{}", &digits[..split], &digits[split..])
    } else if -6 < n_pos && n_pos <= 0 {
        format!("0.{}{digits}", "0".repeat((-n_pos) as usize))
    } else {
        let mantissa_out = if k > 1 {
            format!("{}.{}", &digits[..1], &digits[1..])
        } else {
            digits.clone()
        };
        let e = n_pos - 1;
        format!(
            "{mantissa_out}e{}{}",
            if e >= 0 { "+" } else { "-" },
            e.abs()
        )
    };
    Ok(format!("{sign}{body}"))
}

fn escape_string(s: &str, out: &mut String) {
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
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push('"');
}

/// 把 JSON 值序列化为 RFC 8785 规范化字符串。
pub fn jcs(value: &Value) -> Result<String, String> {
    let mut out = String::new();
    write_jcs(value, &mut out)?;
    Ok(out)
}

fn write_jcs(value: &Value, out: &mut String) -> Result<(), String> {
    match value {
        Value::Null => out.push_str("null"),
        Value::Bool(true) => out.push_str("true"),
        Value::Bool(false) => out.push_str("false"),
        Value::Number(n) => out.push_str(&es_number(n)?),
        Value::String(s) => escape_string(s, out),
        Value::Array(items) => {
            out.push('[');
            for (i, item) in items.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                write_jcs(item, out)?;
            }
            out.push(']');
        }
        Value::Object(map) => {
            // 键按 UTF-16 码元序排序
            let mut keys: Vec<&String> = map.keys().collect();
            keys.sort_by_key(|k| k.encode_utf16().collect::<Vec<u16>>());
            out.push('{');
            for (i, key) in keys.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                escape_string(key, out);
                out.push(':');
                write_jcs(&map[key.as_str()], out)?;
            }
            out.push('}');
        }
    }
    Ok(())
}
