"""Minimal NYSE market calendar — weekdays + a hardcoded holiday list.

Production-grade trading calendars (early closes, half-days, futures
overnight sessions) are out of scope. When that's needed, swap in
`pandas-market-calendars` and replace the helpers here. For now this is
just enough to compute "expected bar count" for the cached PriceSource
to decide if a window's price data is complete.

NYSE regular session: 09:30–16:00 ET, Monday–Friday, excluding the
HOLIDAYS_NYSE set below. Early closes (1:00 pm) are treated as full days
here — the impact on bar-count expectations is small and the cached
PriceSource accepts ≥50% completeness anyway.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo


ET = ZoneInfo("America/New_York")

MARKET_OPEN_TIME = time(9, 30)
MARKET_CLOSE_TIME = time(16, 0)
MARKET_MINUTES_PER_DAY = 390  # 09:30 → 16:00 = 6.5h × 60

# NYSE observed holidays for 2026 and 2027. Source: NYSE schedule.
# When 2028 nears, extend this set or swap in pandas-market-calendars.
HOLIDAYS_NYSE: frozenset[date] = frozenset({
    # 2026
    date(2026, 1, 1),    # New Year's Day
    date(2026, 1, 19),   # MLK Day
    date(2026, 2, 16),   # Presidents Day
    date(2026, 4, 3),    # Good Friday
    date(2026, 5, 25),   # Memorial Day
    date(2026, 6, 19),   # Juneteenth
    date(2026, 7, 3),    # Independence Day (observed)
    date(2026, 9, 7),    # Labor Day
    date(2026, 11, 26),  # Thanksgiving
    date(2026, 12, 25),  # Christmas
    # 2027
    date(2027, 1, 1),    # New Year's Day
    date(2027, 1, 18),   # MLK Day
    date(2027, 2, 15),   # Presidents Day
    date(2027, 3, 26),   # Good Friday
    date(2027, 5, 31),   # Memorial Day
    date(2027, 6, 18),   # Juneteenth (observed)
    date(2027, 7, 5),    # Independence Day (observed)
    date(2027, 9, 6),    # Labor Day
    date(2027, 11, 25),  # Thanksgiving
    date(2027, 12, 24),  # Christmas (observed)
})


def is_trading_day(d: date) -> bool:
    """Weekday and not a holiday."""
    if d.weekday() >= 5:
        return False
    if d in HOLIDAYS_NYSE:
        return False
    return True


def is_market_open(ts: datetime) -> bool:
    """Is `ts` inside an NYSE regular trading session?"""
    et = ts.astimezone(ET) if ts.tzinfo else ts.replace(tzinfo=ET)
    if not is_trading_day(et.date()):
        return False
    return MARKET_OPEN_TIME <= et.time() < MARKET_CLOSE_TIME


def trading_days_between(start: date, end: date) -> int:
    """Inclusive count of trading days in [start, end]."""
    if end < start:
        return 0
    days = 0
    d = start
    while d <= end:
        if is_trading_day(d):
            days += 1
        d += timedelta(days=1)
    return days


def expected_bar_count(
    start: datetime, end: datetime, granularity_minutes: int
) -> int:
    """How many bars should the cache contain for [start, end] at this granularity?

    Coarse: counts whole-day market minutes for trading days in the
    range, plus a partial-day adjustment when start/end fall inside a
    trading session. The PriceSource layer accepts ≥50% completeness so
    coarse-but-conservative is fine.
    """
    if end <= start or granularity_minutes <= 0:
        return 0

    start_et = start.astimezone(ET) if start.tzinfo else start.replace(tzinfo=ET)
    end_et = end.astimezone(ET) if end.tzinfo else end.replace(tzinfo=ET)

    total_minutes = 0
    for offset in range((end_et.date() - start_et.date()).days + 1):
        d = start_et.date() + timedelta(days=offset)
        if not is_trading_day(d):
            continue
        # Day's market window in ET, clipped to the requested range.
        day_open = datetime.combine(d, MARKET_OPEN_TIME, tzinfo=ET)
        day_close = datetime.combine(d, MARKET_CLOSE_TIME, tzinfo=ET)
        window_start = max(start_et, day_open)
        window_end = min(end_et, day_close)
        if window_end > window_start:
            total_minutes += int((window_end - window_start).total_seconds() // 60)

    return total_minutes // granularity_minutes
