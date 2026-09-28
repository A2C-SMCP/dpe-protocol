//! 与 Python `datetime.isoformat()` 逐字节一致的时间类型。
//!
//! `DocMetadata` 的时间字段进入 `doc_hash`，内核用 `isoformat()` 序列化：
//!
//! - 保留原始墙钟时间与偏移，不换算时区；UTC 输出 `+00:00` 而不是 `Z`
//! - 微秒非零时输出 6 位小数，为零时省略小数部分
//! - naive 时间不带偏移
//!
//! 解析接受 pydantic 的常见输入：`T` 或空格分隔、可选小数秒（截断到微秒）、`Z` / `±HH:MM` / 无偏移。

use std::fmt;
use std::str::FromStr;

use chrono::{NaiveDate, NaiveDateTime, NaiveTime, Timelike};
use serde::{Deserialize, Deserializer, Serialize, Serializer};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct PyDateTime {
    /// 墙钟时间（已截断到微秒）
    pub naive: NaiveDateTime,
    /// UTC 偏移秒数，`None` 表示 naive
    pub offset_seconds: Option<i32>,
}

impl PyDateTime {
    pub fn naive(naive: NaiveDateTime) -> Self {
        Self { naive: truncate_to_micros(naive), offset_seconds: None }
    }

    pub fn with_offset(naive: NaiveDateTime, offset_seconds: i32) -> Self {
        Self { naive: truncate_to_micros(naive), offset_seconds: Some(offset_seconds) }
    }

    /// Python `datetime.isoformat()`。
    pub fn isoformat(&self) -> String {
        let mut out = self.naive.format("%Y-%m-%dT%H:%M:%S").to_string();
        let micros = self.naive.nanosecond() / 1_000;
        if micros != 0 {
            out.push_str(&format!(".{micros:06}"));
        }
        if let Some(offset) = self.offset_seconds {
            let sign = if offset < 0 { '-' } else { '+' };
            let abs = offset.unsigned_abs();
            out.push_str(&format!("{sign}{:02}:{:02}", abs / 3600, (abs % 3600) / 60));
            if abs % 60 != 0 {
                out.push_str(&format!(":{:02}", abs % 60));
            }
        }
        out
    }
}

fn truncate_to_micros(naive: NaiveDateTime) -> NaiveDateTime {
    let nanos = naive.nanosecond() / 1_000 * 1_000;
    naive.with_nanosecond(nanos).unwrap_or(naive)
}

impl fmt::Display for PyDateTime {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.isoformat())
    }
}

impl FromStr for PyDateTime {
    type Err = String;

    fn from_str(input: &str) -> Result<Self, Self::Err> {
        let err = || format!("invalid datetime: {input:?}");
        let s = input.trim();
        if s.len() == 10 {
            let date = NaiveDate::parse_from_str(s, "%Y-%m-%d").map_err(|_| err())?;
            return Ok(Self::naive(date.and_time(NaiveTime::MIN)));
        }
        if s.len() < 19 {
            return Err(err());
        }
        let (date_part, rest) = s.split_at(10);
        let sep = rest.chars().next().ok_or_else(err)?;
        if sep != 'T' && sep != 't' && sep != ' ' {
            return Err(err());
        }
        let rest = &rest[1..];
        // 分离偏移：Z / +HH:MM / -HH:MM（时间部分本身不含 '+'/'-'）
        let (time_part, offset) = if let Some(stripped) = rest.strip_suffix(['Z', 'z']) {
            (stripped, Some(0))
        } else if let Some(pos) = rest.find(['+', '-']) {
            (&rest[..pos], Some(parse_offset(&rest[pos..]).ok_or_else(err)?))
        } else {
            (rest, None)
        };
        let (hms, frac) = match time_part.split_once(['.', ',']) {
            Some((hms, frac)) => (hms, Some(frac)),
            None => (time_part, None),
        };
        let date = NaiveDate::parse_from_str(date_part, "%Y-%m-%d").map_err(|_| err())?;
        let mut time = NaiveTime::parse_from_str(hms, "%H:%M:%S").map_err(|_| err())?;
        if let Some(frac) = frac {
            if frac.is_empty() || !frac.bytes().all(|b| b.is_ascii_digit()) {
                return Err(err());
            }
            let digits: String = frac.chars().chain(std::iter::repeat('0')).take(6).collect();
            let micros: u32 = digits.parse().map_err(|_| err())?;
            time = time.with_nanosecond(micros * 1_000).ok_or_else(err)?;
        }
        let naive = date.and_time(time);
        Ok(match offset {
            Some(seconds) => Self::with_offset(naive, seconds),
            None => Self::naive(naive),
        })
    }
}

fn parse_offset(s: &str) -> Option<i32> {
    let sign = match s.as_bytes().first()? {
        b'+' => 1,
        b'-' => -1,
        _ => return None,
    };
    let body = &s[1..];
    let (h, m) = match body.split_once(':') {
        Some((h, m)) => (h, m),
        None if body.len() == 4 => body.split_at(2),
        None if body.len() == 2 => (body, "00"),
        None => return None,
    };
    let (h, m): (i32, i32) = (h.parse().ok()?, m.parse().ok()?);
    (h < 24 && m < 60).then_some(sign * (h * 3600 + m * 60))
}

impl Serialize for PyDateTime {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        serializer.serialize_str(&self.isoformat())
    }
}

impl<'de> Deserialize<'de> for PyDateTime {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        let s = String::deserialize(deserializer)?;
        s.parse().map_err(serde::de::Error::custom)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn isoformat_matches_python() {
        let cases = [
            ("2026-01-02T03:04:05Z", "2026-01-02T03:04:05+00:00"),
            ("2026-01-02T03:04:05.12+08:00", "2026-01-02T03:04:05.120000+08:00"),
            ("2026-01-02 03:04:05.0000019-05:30", "2026-01-02T03:04:05.000001-05:30"),
            ("2026-12-31T23:59:59.000000", "2026-12-31T23:59:59"),
            ("2026-01-02", "2026-01-02T00:00:00"),
        ];
        for (input, expected) in cases {
            assert_eq!(input.parse::<PyDateTime>().unwrap().isoformat(), expected, "{input}");
        }
        assert!("2026-13-01T00:00:00".parse::<PyDateTime>().is_err());
    }
}
