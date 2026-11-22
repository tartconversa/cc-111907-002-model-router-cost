"""时间与账期工具。

契约规定：时间均为带时区的 UTC ISO 字符串，区间左闭右开；``received`` 是事实
进入系统的时点，不可在更早的分析中使用；账期为 ``YYYY-MM``。
"""

from __future__ import annotations

from datetime import datetime, timezone

MIN_TS = "0001-01-01T00:00:00+00:00"
MAX_TS = "9999-12-31T23:59:59+00:00"


def parse_ts(value: str) -> datetime:
    """解析带时区的 ISO 时间；结尾 Z 归一到 +00:00。"""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError(f"时间必须带时区: {value}")
    return dt.astimezone(timezone.utc)


def now_ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def period_of(value: str) -> str:
    """从 ISO 时刻取账期月份 YYYY-MM（按 UTC）。"""
    return parse_ts(value).strftime("%Y-%m")


def period_bounds(period: str) -> tuple[str, str]:
    """账期左闭右开边界，UTC。"""
    year, month = (int(x) for x in period.split("-"))
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    if month == 12:
        end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        end = datetime(year, month + 1, 1, tzinfo=timezone.utc)
    return start.isoformat(), end.isoformat()


def is_within(ts: str, period: str) -> bool:
    start, end = period_bounds(period)
    return start <= ts < end


def as_of_filter(known_at: str | None) -> str:
    """把 --as-of 参数归一化，缺省为“看见一切”的最大时刻。"""
    return known_at if known_at is not None else MAX_TS
