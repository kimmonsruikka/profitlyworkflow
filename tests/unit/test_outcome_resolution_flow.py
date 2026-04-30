from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from flows.outcome_resolution_flow import (
    PriceBar,
    classify_outcome,
    compute_outcome_metrics,
)


# ---------------------------------------------------------------------------
# classify_outcome — pure rule application
# ---------------------------------------------------------------------------
def test_classify_outcome_target_hit_is_win():
    assert classify_outcome(realized_return_pct=1.0, hit_target=True, hit_stop=False) == "WIN"


def test_classify_outcome_stop_hit_is_loss():
    assert classify_outcome(realized_return_pct=-0.5, hit_target=False, hit_stop=True) == "LOSS"


def test_classify_outcome_realized_above_threshold_is_win():
    assert classify_outcome(realized_return_pct=2.5, hit_target=False, hit_stop=False) == "WIN"


def test_classify_outcome_realized_below_threshold_is_loss():
    assert classify_outcome(realized_return_pct=-2.0, hit_target=False, hit_stop=False) == "LOSS"


def test_classify_outcome_in_between_is_neutral():
    assert classify_outcome(realized_return_pct=0.5, hit_target=False, hit_stop=False) == "NEUTRAL"


def test_classify_outcome_no_data_is_invalid():
    assert classify_outcome(realized_return_pct=None, hit_target=None, hit_stop=None) == "INVALID"


# ---------------------------------------------------------------------------
# compute_outcome_metrics — fixture price series
# ---------------------------------------------------------------------------
def _bar(ts: datetime, o: float, h: float, l: float, c: float) -> PriceBar:
    return PriceBar(timestamp=ts, open=o, high=h, low=l, close=c)


def test_compute_metrics_winner_path():
    """Entry $4.00 → high $5.00 (+25% MFE) → close $4.80 (+20% realized)."""
    t0 = datetime(2026, 5, 1, 14, 30, tzinfo=timezone.utc)
    bars = [
        _bar(t0,                   4.00, 4.10, 3.95, 4.05),
        _bar(t0 + timedelta(minutes=5), 4.05, 4.50, 4.00, 4.45),
        _bar(t0 + timedelta(minutes=10), 4.45, 5.00, 4.40, 4.80),
    ]
    metrics = compute_outcome_metrics(bars, target_pct=10.0)
    assert metrics["max_favorable_excursion_pct"] == pytest.approx(25.0, abs=0.01)
    assert metrics["realized_return_pct"] == pytest.approx(20.0, abs=0.01)
    assert metrics["hit_target"] is True


def test_compute_metrics_loser_path():
    """Entry $4.00 → low $3.50 (-12.5% MAE) → close $3.80 (-5%)."""
    t0 = datetime(2026, 5, 1, 14, 30, tzinfo=timezone.utc)
    bars = [
        _bar(t0,                   4.00, 4.05, 3.95, 3.95),
        _bar(t0 + timedelta(minutes=5), 3.95, 3.95, 3.50, 3.60),
        _bar(t0 + timedelta(minutes=10), 3.60, 3.85, 3.55, 3.80),
    ]
    metrics = compute_outcome_metrics(bars, target_pct=10.0)
    assert metrics["max_adverse_excursion_pct"] == pytest.approx(-12.5, abs=0.01)
    assert metrics["realized_return_pct"] == pytest.approx(-5.0, abs=0.01)
    assert metrics["hit_target"] is False


def test_compute_metrics_empty_bars_returns_none_fields():
    metrics = compute_outcome_metrics([], target_pct=5.0)
    for key in (
        "max_favorable_excursion_pct",
        "max_adverse_excursion_pct",
        "realized_return_pct",
        "hit_target",
        "hit_stop",
    ):
        assert metrics[key] is None


def test_compute_metrics_zero_entry_returns_none_fields():
    """Bad data — entry of 0 would div-by-zero. Must return None safely."""
    t0 = datetime(2026, 5, 1, 14, 30, tzinfo=timezone.utc)
    metrics = compute_outcome_metrics(
        [_bar(t0, 0.0, 0.0, 0.0, 0.0)], target_pct=5.0,
    )
    assert metrics["realized_return_pct"] is None
    assert metrics["max_favorable_excursion_pct"] is None


# ---------------------------------------------------------------------------
# PriceSource Protocol — fake implementation for end-to-end tests
# ---------------------------------------------------------------------------
from flows.outcome_resolution_flow import OHLCVResult


class FakePriceSource:
    name = "test-fake"

    def __init__(self, bars: list[PriceBar], *, is_complete: bool = True) -> None:
        self._bars = bars
        self._is_complete = is_complete
        self.calls: list[tuple[str, datetime, datetime, str]] = []

    def get_ohlcv(
        self,
        ticker: str,
        start: datetime,
        end: datetime,
        granularity: str = "1m",
    ) -> OHLCVResult:
        self.calls.append((ticker, start, end, granularity))
        return OHLCVResult(
            bars=list(self._bars),
            source="cache" if self._bars else "polygon",
            is_complete=self._is_complete,
        )


def test_fake_price_source_satisfies_protocol():
    """Lightweight contract check that the test fake matches the Protocol shape."""
    from flows.outcome_resolution_flow import PriceSource

    fake = FakePriceSource([])
    assert isinstance(fake, PriceSource)


def test_metrics_with_realistic_winner_and_target_signed_stop():
    t0 = datetime(2026, 5, 1, 14, 30, tzinfo=timezone.utc)
    bars = [
        _bar(t0, 10.00, 10.50, 9.80, 10.40),
        _bar(t0 + timedelta(minutes=5), 10.40, 11.00, 10.30, 10.95),
    ]
    # target +5%, stop -3%  → MFE 10%, realized 9.5%, MAE -2%
    metrics = compute_outcome_metrics(bars, target_pct=5.0, stop_pct=-3.0)
    assert metrics["hit_target"] is True
    assert metrics["hit_stop"] is False
    assert metrics["max_adverse_excursion_pct"] == pytest.approx(-2.0, abs=0.01)
