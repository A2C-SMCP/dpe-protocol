//! `Retry-After` 的解析（HTTP 绑定 §5，RFC 9110 §10.2.3 / §5.6.7）：客户端与一致性跑分器
//! 共用（dpe-sdk 经 `dpe_sdk::protocol` 重新导出）。
//!
//! 只接受 RFC 9110 的文法：`delay-seconds`（`1*DIGIT`）或 HTTP-date 的三种形式，区分大小写、
//! 时区只认字面 `GMT`；其余输入视同没有提示。日历换算全用整数运算，任何输入都不 panic、不
//! 溢出。与生成器 `scripts/gen_vectors.py` 的参考实现行为一致，由 `vectors/retry_after.json`
//! 逐例固定。

/// `Retry-After` 头换算成秒（RFC 9110 §10.2.3）：`delay-seconds`（`1*DIGIT`）或 `HTTP-date`
/// （§5.6.7 的三种形式），相对当前 UTC 时间；已过去的时刻为 0，不合文法或无法解析时为
/// `None`（「没有给出提示」）。合法但大到无法表示的 delay-seconds 为 `f64::INFINITY`：它
/// 仍是「等待很久」的提示，不能退化成「没有提示」——调用方可据此区分「没有给」「有限值」
/// 「无穷」三种情况。
///
/// 只接受 RFC 9110 的文法（区分大小写、时区只认字面 `GMT`，见 `parse_http_date`）；行为由
/// `vectors/retry_after.json` 逐例固定，与 Python 的 `dpe_hash.retry_after_seconds` 对等。
pub fn retry_after_seconds(value: &str) -> Option<f64> {
    retry_after_seconds_at(value, unix_now_seconds())
}

/// [`retry_after_seconds`] 的显式时钟版本（测试与可控时钟用）；`now_unix_seconds` 只用于
/// rfc850-date 两位年份的 50 年规则（RFC 9110 §5.6.7）。
pub fn retry_after_seconds_at(value: &str, now_unix_seconds: i64) -> Option<f64> {
    let text = value.trim_matches([' ', '\t']); // OWS 只含 SP 与 HTAB
    if !text.is_empty() && text.bytes().all(|byte| byte.is_ascii_digit()) {
        // delay-seconds：超长数字串（无法表示的秒数）解析为 inf，不当作「没有给」
        return Some(text.parse::<f64>().unwrap_or(f64::INFINITY));
    }
    let when = parse_http_date(text, now_unix_seconds)?;
    Some((when as f64 - now_unix_seconds as f64).max(0.0))
}

/// 当前 Unix 秒（UTC）；系统时间早于纪元或不可用时为 0。
fn unix_now_seconds() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|elapsed| elapsed.as_secs() as i64)
        .unwrap_or(0)
}

/// HTTP-date（RFC 9110 §5.6.7）→ Unix 秒（UTC）；不合文法或日期不存在时为 `None`。
///
/// §5.6.7 的三种形式都必须接受：IMF-fixdate（`Sun, 06 Nov 1994 08:49:37 GMT`）、
/// rfc850-date（`Sunday, 06-Nov-94 08:49:37 GMT`）与 asctime-date
/// （`Sun Nov  6 08:49:37 1994`）。文法**区分大小写**，SP 的个数是文法的一部分（按 `' '`
/// 逐字切分，asctime 的 `date3` 只在 `SP SP 1DIGIT` 形式下允许 1 位日）；时区只认字面
/// `GMT`。星期名不与日期核对（§5.6.7：接收方允许不校验，日期本身合法即可）；日期必须真实
/// 存在（`31 Feb`、非闰年的 `29 Feb` 拒绝）；时刻按 `hour 00-23` / `minute 00-59` /
/// `second 00-60`（闰秒）。`now_unix_seconds` 只用于 rfc850 两位年份的 50 年规则。
fn parse_http_date(text: &str, now_unix_seconds: i64) -> Option<i64> {
    imf_fixdate(text)
        .or_else(|| rfc850_date(text, now_unix_seconds))
        .or_else(|| asctime_date(text))
}

/// IMF-fixdate：`day-name "," SP date1 SP time-of-day SP GMT`；`date1 = day SP month SP year`
/// （RFC 9110 §5.6.7）。
fn imf_fixdate(text: &str) -> Option<i64> {
    let parts: Vec<&str> = text.split(' ').collect();
    let [weekday, day, month, year, time, "GMT"] = parts.as_slice() else {
        return None;
    };
    if !DAY_NAMES.contains(&weekday.strip_suffix(',')?) {
        return None;
    }
    timestamp(
        four_digits(year)?,
        month_number(month)?,
        two_digits(day)?,
        time_of_day(time)?,
    )
}

/// rfc850-date：`day-name-l "," SP date2 SP time-of-day SP GMT`；`date2 = day "-" month "-"`
/// 加两位年份（RFC 9110 §5.6.7，年份按 50 年规则解释，见 [`two_digit_year`]）。
fn rfc850_date(text: &str, now_unix_seconds: i64) -> Option<i64> {
    let parts: Vec<&str> = text.split(' ').collect();
    let [weekday, date2, time, "GMT"] = parts.as_slice() else {
        return None;
    };
    if !DAY_NAMES_LONG.contains(&weekday.strip_suffix(',')?) {
        return None;
    }
    let mut date = date2.split('-');
    let day = two_digits(date.next()?)?;
    let month = month_number(date.next()?)?;
    let year2 = two_digits(date.next()?)?;
    if date.next().is_some() {
        return None;
    }
    let time = time_of_day(time)?;
    let year = two_digit_year(year2, now_unix_seconds, month, day, time);
    timestamp(year, month, day, time)
}

/// asctime-date：`day-name SP date3 SP time-of-day SP year`，
/// `date3 = month SP ( 2DIGIT / ( SP 1DIGIT ) )`（RFC 9110 §5.6.7）。
fn asctime_date(text: &str) -> Option<i64> {
    let parts: Vec<&str> = text.split(' ').collect();
    let (weekday, month, day, time, year) = match parts.as_slice() {
        [weekday, month, day, time, year] => (*weekday, *month, two_digits(day)?, *time, *year),
        // `SP SP 1DIGIT`：日只有 1 位，与月份名之间有两个 SP
        [weekday, month, "", day, time, year] => (*weekday, *month, one_digit(day)?, *time, *year),
        _ => return None,
    };
    if !DAY_NAMES.contains(&weekday) {
        return None;
    }
    timestamp(
        four_digits(year)?,
        month_number(month)?,
        day,
        time_of_day(time)?,
    )
}

/// §5.6.7 的 `day-name`（区分大小写）。
const DAY_NAMES: [&str; 7] = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
/// §5.6.7 的 `day-name-l`（rfc850-date 的完整星期名）。
const DAY_NAMES_LONG: [&str; 7] = [
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
];

/// 月份字面 `%s"Jan"` 等（区分大小写）→ 月份号。
fn month_number(text: &str) -> Option<u32> {
    Some(match text {
        "Jan" => 1,
        "Feb" => 2,
        "Mar" => 3,
        "Apr" => 4,
        "May" => 5,
        "Jun" => 6,
        "Jul" => 7,
        "Aug" => 8,
        "Sep" => 9,
        "Oct" => 10,
        "Nov" => 11,
        "Dec" => 12,
        _ => return None,
    })
}

/// `2DIGIT`：恰好两位十进制。
fn two_digits(text: &str) -> Option<u32> {
    if text.len() != 2 || !text.bytes().all(|byte| byte.is_ascii_digit()) {
        return None;
    }
    text.parse().ok()
}

/// `1DIGIT`。
fn one_digit(text: &str) -> Option<u32> {
    if text.len() != 1 || !text.bytes().all(|byte| byte.is_ascii_digit()) {
        return None;
    }
    text.parse().ok()
}

/// `4DIGIT` 年份。
fn four_digits(text: &str) -> Option<i64> {
    if text.len() != 4 || !text.bytes().all(|byte| byte.is_ascii_digit()) {
        return None;
    }
    text.parse().ok()
}

/// `time-of-day = hour ":" minute ":" second`（各 `2DIGIT`；hour ≤ 23、minute ≤ 59、
/// second ≤ 60——RFC 9110 §5.6.7 允许闰秒）。
fn time_of_day(text: &str) -> Option<(i64, i64, i64)> {
    let mut parts = text.split(':');
    let hour = two_digits(parts.next()?)?;
    let minute = two_digits(parts.next()?)?;
    let second = two_digits(parts.next()?)?;
    if parts.next().is_some() || hour > 23 || minute > 59 || second > 60 {
        return None;
    }
    Some((hour.into(), minute.into(), second.into()))
}

/// 组装 Unix 秒；日期不存在（如 11 月 31 日、非闰年的 2 月 29 日）为 `None`。
fn timestamp(
    year: i64,
    month: u32,
    day: u32,
    (hour, minute, second): (i64, i64, i64),
) -> Option<i64> {
    if day < 1 || i64::from(day) > days_in_month(year, month) {
        return None;
    }
    // 极端的 now（接近 i64 边界）会让 rfc850 的年份落到 i64 秒表示不了的范围：按不可用处理
    days_from_civil(year, month, i64::from(day))
        .checked_mul(86400)?
        .checked_add(hour * 3600 + minute * 60 + second)
}

/// rfc850 的两位年份 → 四位年份（RFC 9110 §5.6.7 的 50 年规则）：末两位为 `year2`、且时刻
/// 不晚于「现在加 50 年」（逐字段比较，含时刻）的最晚年份——晚于 50 年的读作上一个世纪，
/// 世纪交替前后同样适用（现在为 1999 年末时，`00` 读作 2000）。
fn two_digit_year(
    year2: u32,
    now_unix_seconds: i64,
    month: u32,
    day: u32,
    time: (i64, i64, i64),
) -> i64 {
    let (now_year, now_month, now_day) = civil_from_days(now_unix_seconds.div_euclid(86400));
    let seconds_of_day = now_unix_seconds.rem_euclid(86400);
    let now_time = (
        seconds_of_day / 3600,
        seconds_of_day % 3600 / 60,
        seconds_of_day % 60,
    );
    let limit = (now_year + 50, now_month, now_day, now_time);
    // 候选取「现在加 50 年」所在世纪：它加 100 必然越界，至多回退一个世纪
    let candidate = (now_year + 50).div_euclid(100) * 100 + i64::from(year2);
    if (candidate, month, day, time) > limit {
        candidate - 100
    } else {
        candidate
    }
}

/// 距 1970-01-01 的天数 → 公历日期（Howard Hinnant 的 civil_from_days 算法）。
fn civil_from_days(days: i64) -> (i64, u32, u32) {
    let shifted = days + 719_468;
    let era = if shifted >= 0 {
        shifted
    } else {
        shifted - 146_096
    } / 146_097;
    let day_of_era = shifted - era * 146_097;
    let year_of_era =
        (day_of_era - day_of_era / 1460 + day_of_era / 36_524 - day_of_era / 146_096) / 365;
    let year = year_of_era + era * 400;
    let day_of_year = day_of_era - (365 * year_of_era + year_of_era / 4 - year_of_era / 100);
    let month_prime = (5 * day_of_year + 2) / 153;
    let day = day_of_year - (153 * month_prime + 2) / 5 + 1;
    let month = if month_prime < 10 {
        month_prime + 3
    } else {
        month_prime - 9
    };
    (year + i64::from(month <= 2), month as u32, day as u32)
}

fn days_in_month(year: i64, month: u32) -> i64 {
    match month {
        1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
        4 | 6 | 9 | 11 => 30,
        2 if is_leap_year(year) => 29,
        2 => 28,
        _ => 0,
    }
}

fn is_leap_year(year: i64) -> bool {
    year % 4 == 0 && (year % 100 != 0 || year % 400 == 0)
}

/// 公历日期 → 距 1970-01-01 的天数（Howard Hinnant 的 days_from_civil 算法）。
fn days_from_civil(year: i64, month: u32, day: i64) -> i64 {
    let year = if month <= 2 { year - 1 } else { year };
    let era = if year >= 0 { year } else { year - 399 } / 400;
    let year_of_era = year - era * 400;
    let day_of_year =
        (153 * i64::from(if month > 2 { month - 3 } else { month + 9 }) + 2) / 5 + day - 1;
    let day_of_era = year_of_era * 365 + year_of_era / 4 - year_of_era / 100 + day_of_year;
    era * 146097 + day_of_era - 719468
}
