"""周的定义与换算。统一用 ISO 周（周一到周日），写作 2011-W48。

- 环比（wow）：和上一周比
- 同比（yoy）：和去年同一周比。有的年份有第 53 周、有的没有，对不上时直接报错，不悄悄换成别的周。
"""

import re
from datetime import date, timedelta

WEEK_PATTERN = re.compile(r"(\d{4})-W(\d{2})")
LOW_TRADING_DAYS = 4  # 一周交易天数少于这个值，视为节假日周，和正常周比较会失真
COMPARE_LABELS = {"wow": "环比（上一周）", "yoy": "同比（去年同一周）"}


def _parse(week: str) -> tuple[int, int]:
    match = WEEK_PATTERN.fullmatch(week)
    if not match:
        raise ValueError(f"周的格式应为 YYYY-Www，例如 2011-W48；收到：{week!r}")
    return int(match.group(1)), int(match.group(2))


def week_monday(week: str) -> date:
    """周 → 这一周的周一。"""
    year, number = _parse(week)
    try:
        return date.fromisocalendar(year, number, 1)
    except ValueError as exc:
        raise ValueError(f"{year} 年没有第 {number} 周") from exc


def week_of(day: date) -> str:
    """日期 → 所在的 ISO 周。"""
    year, number, _ = day.isocalendar()
    return f"{year}-W{number:02d}"


def previous_week(week: str) -> str:
    """上一周。"""
    return week_of(week_monday(week) - timedelta(days=7))


def same_week_last_year(week: str) -> str:
    """去年同一周；去年没有这一周（如第 53 周）时报错。"""
    year, number = _parse(week)
    week_monday(week)
    try:
        date.fromisocalendar(year - 1, number, 1)
    except ValueError as exc:
        raise ValueError(f"{year - 1} 年没有第 {number} 周，{week} 无法做同比") from exc
    return f"{year - 1}-W{number:02d}"


def base_week(week: str, compare: str) -> str:
    """按对比方式找到基期。"""
    if compare == "wow":
        return previous_week(week)
    if compare == "yoy":
        return same_week_last_year(week)
    raise ValueError("对比方式只能是 wow（环比）或 yoy（同比）")
