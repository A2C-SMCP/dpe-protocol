"""``Retry-After`` 的解析（HTTP 绑定 §5，RFC 9110 §10.2.3 / §5.6.7）：客户端与跑分器共用。

只接受 RFC 9110 的文法：``delay-seconds``（``1*DIGIT``）或 HTTP-date 的三种形式（IMF-fixdate、
rfc850-date、asctime-date），区分大小写、时区只认字面 ``GMT``；其余输入视同没有提示。不借用
``email.utils`` 这类邮件日期（RFC 5322）解析器——它的时区缩写表、宽松数字与两位年份规则都不是
HTTP-date。日历换算全用整数运算，任何输入都不会抛异常或溢出。与生成器
``scripts/gen_vectors.py`` 的参考实现行为一致，由 ``vectors/retry_after.json`` 逐例固定（同
Rust 的 ``dpe_hash::retry_after_seconds_at``）。
"""

from __future__ import annotations

import math
from datetime import UTC, datetime

__all__ = ["retry_after_seconds"]

_DIGITS = frozenset("0123456789")
#: §5.6.7 的 ``day-name`` 与 ``day-name-l``（区分大小写）
_DAY_NAMES = frozenset(("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"))
_DAY_NAMES_LONG = frozenset(
    ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
)
_MONTHS = {
    name: number
    for number, name in enumerate(
        ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"),
        start=1,
    )
}

_Time = tuple[int, int, int]


def _days_from_civil(year: int, month: int, day: int) -> int:
    """公历日期 → 距 1970-01-01 的天数（Howard Hinnant 的 days_from_civil）。"""
    year -= month <= 2
    era = year // 400
    yoe = year - era * 400
    doy = (153 * (month + (-3 if month > 2 else 9)) + 2) // 5 + day - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return era * 146097 + doe - 719468


def _civil_from_days(days: int) -> tuple[int, int, int]:
    """距 1970-01-01 的天数 → 公历日期（Howard Hinnant 的 civil_from_days）。"""
    shifted = days + 719468
    era = shifted // 146097
    doe = shifted - era * 146097
    yoe = (doe - doe // 1460 + doe // 36524 - doe // 146096) // 365
    doy = doe - (365 * yoe + yoe // 4 - yoe // 100)
    mp = (5 * doy + 2) // 153
    day = doy - (153 * mp + 2) // 5 + 1
    month = mp + 3 if mp < 10 else mp - 9
    return yoe + era * 400 + (month <= 2), month, day


def _days_in_month(year: int, month: int) -> int:
    if month == 2:
        return 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28
    return 30 if month in (4, 6, 9, 11) else 31


def _fixed_digits(text: str, width: int) -> int | None:
    """恰好 ``width`` 位 ASCII 十进制（``2DIGIT`` / ``4DIGIT`` / ``1DIGIT``）。"""
    if len(text) != width or not all(ch in _DIGITS for ch in text):
        return None
    return int(text)


def _time_of_day(text: str) -> _Time | None:
    """``hour ":" minute ":" second``：hour ≤ 23、minute ≤ 59、second ≤ 60（闰秒）。"""
    parts = text.split(":")
    if len(parts) != 3:
        return None
    hour, minute, second = (_fixed_digits(part, 2) for part in parts)
    if hour is None or minute is None or second is None:
        return None
    if hour > 23 or minute > 59 or second > 60:
        return None
    return hour, minute, second


def _instant(year: int, month: int, day: int, time: _Time) -> int | None:
    """Unix 秒；日期不存在（如 11 月 31 日、非闰年的 2 月 29 日）为 ``None``。"""
    if not 1 <= day <= _days_in_month(year, month):
        return None
    hour, minute, second = time
    return _days_from_civil(year, month, day) * 86400 + hour * 3600 + minute * 60 + second


def _imf_fixdate(text: str) -> int | None:
    """``day-name "," SP day SP month SP year SP time-of-day SP GMT``。"""
    parts = text.split(" ")
    if len(parts) != 6 or parts[5] != "GMT" or not parts[0].endswith(","):
        return None
    if parts[0][:-1] not in _DAY_NAMES or parts[2] not in _MONTHS:
        return None
    day, year, time = _fixed_digits(parts[1], 2), _fixed_digits(parts[3], 4), _time_of_day(parts[4])
    if day is None or year is None or time is None:
        return None
    return _instant(year, _MONTHS[parts[2]], day, time)


def _rfc850_year(year2: int, now: int, month: int, day: int, time: _Time) -> int:
    """50 年规则（§5.6.7）：末两位为 ``year2``、且时刻不晚于「now 加 50 年」的最晚年份。"""
    days, seconds = divmod(now, 86400)
    now_year, now_month, now_day = _civil_from_days(days)
    limit = (
        now_year + 50,
        now_month,
        now_day,
        (seconds // 3600, seconds % 3600 // 60, seconds % 60),
    )
    year = (now_year + 50) // 100 * 100 + year2
    if (year, month, day, time) > limit:
        year -= 100
    return year


def _rfc850_date(text: str, now: int) -> int | None:
    """``day-name-l "," SP day "-" month "-" 2DIGIT SP time-of-day SP GMT``。"""
    parts = text.split(" ")
    if len(parts) != 4 or parts[3] != "GMT" or not parts[0].endswith(","):
        return None
    date = parts[1].split("-")
    if parts[0][:-1] not in _DAY_NAMES_LONG or len(date) != 3 or date[1] not in _MONTHS:
        return None
    day, year2, time = _fixed_digits(date[0], 2), _fixed_digits(date[2], 2), _time_of_day(parts[2])
    if day is None or year2 is None or time is None:
        return None
    month = _MONTHS[date[1]]
    return _instant(_rfc850_year(year2, now, month, day, time), month, day, time)


def _asctime_date(text: str) -> int | None:
    """``day-name SP month SP ( 2DIGIT / ( SP 1DIGIT ) ) SP time-of-day SP year``。"""
    parts = text.split(" ")
    if len(parts) == 5:
        weekday, month, day_text, time_text, year_text = parts
        day = _fixed_digits(day_text, 2)
    elif len(parts) == 6 and parts[2] == "":
        weekday, month, _, day_text, time_text, year_text = parts
        day = _fixed_digits(day_text, 1)
    else:
        return None
    if weekday not in _DAY_NAMES or month not in _MONTHS:
        return None
    year, time = _fixed_digits(year_text, 4), _time_of_day(time_text)
    if day is None or year is None or time is None:
        return None
    return _instant(year, _MONTHS[month], day, time)


def retry_after_seconds(value: str | None, *, now: datetime | None = None) -> float | None:
    """``Retry-After`` 字段值换算成等待秒数（HTTP 绑定 §5）。

    ``delay-seconds`` 即其值；HTTP-date 相对 ``now``（缺省为当前 UTC 时间，不带时区的按 UTC
    理解），已过去的时刻为 0。缺省或不合文法时为 ``None``（视同没有提示）。合法但大到无法表示
    的 delay-seconds 为 ``math.inf``：它仍是「等待很久」的提示，不能退化成「没有提示」。
    """
    if value is None:
        return None
    text = value.strip(" \t")  # OWS 只含 SP 与 HTAB
    if text and all(ch in _DIGITS for ch in text):
        try:
            return float(int(text))
        except (ValueError, OverflowError):  # 超过 int 的位数上限 / float 溢出
            return math.inf
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    stamp = current.timestamp()
    whole = math.floor(stamp)  # 50 年规则按整秒比较
    when = _imf_fixdate(text)
    if when is None:
        when = _rfc850_date(text, whole)
    if when is None:
        when = _asctime_date(text)
    if when is None:
        return None
    return max(0.0, when - stamp)
