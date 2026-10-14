//! HTTP 绑定 §5：`Retry-After` 的解析。消费 `vectors/retry_after.json`，逐例经
//! [`retry_after_seconds_at`] 断言（与 Python dpe-hash 的 `tests/test_retry_after.py` 对等）。
//!
//! vector-kind: retry_after

mod common;

use dpe_hash::retry_after_seconds_at;

/// 向量里的时刻（`YYYY-MM-DDTHH:MM:SSZ`）→ Unix 秒（只为读向量，不是被测实现）。
fn unix_seconds(text: &str) -> i64 {
    let field = |range: std::ops::Range<usize>| text[range].parse::<i64>().unwrap();
    assert!(text.len() == 20 && text.ends_with('Z'), "{text}");
    let (year, month, day) = (field(0..4), field(5..7), field(8..10));
    let (hour, minute, second) = (field(11..13), field(14..16), field(17..19));
    // Howard Hinnant 的 days_from_civil
    let y = if month <= 2 { year - 1 } else { year };
    let era = y.div_euclid(400);
    let yoe = y - era * 400;
    let doy = (153 * (if month > 2 { month - 3 } else { month + 9 }) + 2) / 5 + day - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    let days = era * 146_097 + doe - 719_468;
    days * 86400 + hour * 3600 + minute * 60 + second
}

#[test]
fn vector_cases() {
    let vector = common::read("retry_after.json");
    assert_eq!(vector["kind"], "retry_after");
    let cases = vector["cases"].as_array().unwrap();
    assert!(!cases.is_empty(), "向量不应为空");
    for case in cases {
        let input = case["input"].as_str().unwrap();
        let now = case["now"].as_str().unwrap();
        let got = retry_after_seconds_at(input, unix_seconds(now));
        let label = format!("{input:?} @ {now}");
        match &case["seconds"] {
            serde_json::Value::Null => assert_eq!(got, None, "{label}"),
            serde_json::Value::String(s) if s == "infinity" => {
                assert_eq!(got, Some(f64::INFINITY), "{label}")
            }
            number => assert_eq!(got, Some(number.as_f64().unwrap()), "{label}"),
        }
    }
}

/// 公开 API 接受任意 i64 的 now：接近边界时 rfc850 的年份落到 i64 秒表示不了的范围，按不可用
/// 处理，debug 与 release 都不 panic。
#[test]
fn extreme_now_does_not_overflow() {
    for now in [i64::MAX, i64::MIN, i64::MAX - 86400, i64::MIN + 86400] {
        for value in [
            "Monday, 01-Jan-00 00:00:00 GMT",
            "Thursday, 31-Dec-99 23:59:60 GMT",
            "Thu, 08 Oct 2026 12:00:30 GMT",
            "Thu Oct  8 12:00:30 2026",
        ] {
            let _ = retry_after_seconds_at(value, now);
        }
    }
}
