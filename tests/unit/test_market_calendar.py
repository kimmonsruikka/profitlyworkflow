from __future__ import annotations

from datetime import date, datetime, timezone

from utils.market_calendar import (
    ET,
    expected_bar_count,
    is_market_open,
    is_trading_day,
    trading_days_between,
)


# ---------------------------------------------------------------------------
# is_trading_day
# ---------------------------------------------------------------------------
def test_weekday_non_holiday_is_trading_day():
    # Tuesday, Apr 7 2026 — confirmed not a holiday
    assert is_trading_day(date(2026, 4, 7)) is True


def test_saturday_is_not_trading_day():
    assert is_trading_day(date(2026, 4, 4)) is False  # Saturday


def test_christmas_is_not_trading_day():
    assert is_trading_day(date(2026, 12, 25)) is False


def test_thanksgiving_is_not_trading_day():
    assert is_trading_day(date(2026, 11, 26)) is False


# ---------------------------------------------------------------------------
# is_market_open
# ---------------------------------------------------------------------------
def test_market_open_at_10am_et_on_trading_day():
    ts = datetime(2026, 4, 7, 14, 0, tzinfo=timezone.utc)  # 10:00 ET
    assert is_market_open(ts) is True


def test_market_closed_overnight():
    ts = datetime(2026, 4, 7, 4, 0, tzinfo=timezone.utc)  # midnight ET
    assert is_market_open(ts) is False


def test_market_closed_on_weekend():
    ts = datetime(2026, 4, 4, 15, 0, tzinfo=timezone.utc)  # Sat 11am ET
    assert is_market_open(ts) is False


def test_market_closed_on_holiday():
    ts = datetime(2026, 12, 25, 15, 0, tzinfo=timezone.utc)
    assert is_market_open(ts) is False


def test_market_closed_at_4pm_et_exactly():
    """16:00 ET is the close — boundary should be exclusive."""
    ts = datetime(2026, 4, 7, 20, 0, tzinfo=timezone.utc)  # 16:00 ET
    assert is_market_open(ts) is False


# ---------------------------------------------------------------------------
# trading_days_between + expected_bar_count
# ---------------------------------------------------------------------------
def test_trading_days_between_full_week():
    # Mon Apr 6 2026 — Fri Apr 10 2026: 5 trading days
    assert trading_days_between(date(2026, 4, 6), date(2026, 4, 10)) == 5


def test_trading_days_between_skips_weekend():
    # Fri Apr 10 — Mon Apr 13: 2 trading days
    assert trading_days_between(date(2026, 4, 10), date(2026, 4, 13)) == 2


def test_expected_bar_count_full_trading_day_at_1m():
    """A whole trading session = 390 minute bars."""
    start = datetime(2026, 4, 7, 13, 30, tzinfo=timezone.utc)  # 09:30 ET
    end = datetime(2026, 4, 7, 20, 0, tzinfo=timezone.utc)     # 16:00 ET
    assert expected_bar_count(start, end, granularity_minutes=1) == 390


def test_expected_bar_count_full_trading_day_at_5m():
    start = datetime(2026, 4, 7, 13, 30, tzinfo=timezone.utc)
    end = datetime(2026, 4, 7, 20, 0, tzinfo=timezone.utc)
    assert expected_bar_count(start, end, granularity_minutes=5) == 78


def test_expected_bar_count_zero_for_overnight_window():
    """4pm to next-day 9am is entirely outside market hours."""
    start = datetime(2026, 4, 7, 20, 0, tzinfo=timezone.utc)
    end = datetime(2026, 4, 8, 13, 0, tzinfo=timezone.utc)
    assert expected_bar_count(start, end, granularity_minutes=1) == 0


def test_expected_bar_count_zero_for_weekend():
    start = datetime(2026, 4, 4, 13, 30, tzinfo=timezone.utc)  # Saturday
    end = datetime(2026, 4, 5, 20, 0, tzinfo=timezone.utc)     # Sunday
    assert expected_bar_count(start, end, granularity_minutes=1) == 0
