from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from config import constants
from data.repositories.schemas import SignalSchema
from risk.gatekeeper import RiskGatekeeper, Rules


# ---------------------------------------------------------------------------
# Fixtures: minimally-functional mocks of Redis and AsyncSession
# ---------------------------------------------------------------------------
class FakeRedis:
    def __init__(self, store: dict[str, str] | None = None) -> None:
        self._kv: dict[str, str] = dict(store or {})
        self._zset: dict[str, dict[str, float]] = {}

    async def get(self, key: str):
        return self._kv.get(key)

    async def set(self, key: str, value: str) -> None:
        self._kv[key] = value

    async def zadd(self, key: str, mapping: dict[str, float]) -> None:
        self._zset.setdefault(key, {}).update(mapping)

    async def zcard(self, key: str) -> int:
        return len(self._zset.get(key, {}))

    async def zremrangebyscore(self, key: str, min_score: float, max_score: float) -> int:
        zs = self._zset.get(key, {})
        to_remove = [m for m, s in zs.items() if min_score <= s <= max_score]
        for m in to_remove:
            zs.pop(m, None)
        return len(to_remove)


def make_session(scalar_results: list) -> MagicMock:
    """Mock an AsyncSession.execute() returning the given scalar values in order."""
    session = MagicMock()
    iterator = iter(scalar_results)

    async def execute(_):
        result = MagicMock()
        result.scalar_one = MagicMock(return_value=next(iterator))
        return result

    session.execute = AsyncMock(side_effect=execute)
    session.add = MagicMock()
    session.flush = AsyncMock()
    return session


def make_signal(
    *,
    strategy: str = "S1",
    entry_low: str = "4.14",
    entry_high: str = "4.28",
    stop: str = "3.95",
    target: str = "4.83",
    shares: int = 1820,
    risk: str = "350",
    liquidity: str = "74",
) -> SignalSchema:
    return SignalSchema(
        signal_id=uuid.uuid4(),
        strategy=strategy,
        ticker="ABCD",
        generated_at=datetime.now(timezone.utc),
        confidence_score=Decimal("82"),
        liquidity_score=Decimal(liquidity),
        entry_price_low=Decimal(entry_low),
        entry_price_high=Decimal(entry_high),
        stop_price=Decimal(stop),
        target1_price=Decimal(target),
        risk_dollars=Decimal(risk),
        share_count=shares,
    )


# ---------------------------------------------------------------------------
# check_daily_loss_limit
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_daily_loss_limit_blocks_at_threshold():
    redis = FakeRedis({f"daily_loss:{__import__('datetime').date.today().isoformat()}": "700"})
    gate = RiskGatekeeper(redis=redis, session=make_session([]))
    result = await gate.check_daily_loss_limit(account_balance=35_000)
    assert result.approved is False
    assert result.rule_violated == Rules.DAILY_LOSS_LIMIT


@pytest.mark.asyncio
async def test_daily_loss_limit_passes_below_threshold():
    redis = FakeRedis({f"daily_loss:{__import__('datetime').date.today().isoformat()}": "100"})
    gate = RiskGatekeeper(redis=redis, session=make_session([]))
    result = await gate.check_daily_loss_limit(account_balance=35_000)
    assert result.approved is True


# ---------------------------------------------------------------------------
# check_pdt_limit
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_pdt_blocks_intraday_at_limit_with_small_account():
    import time
    redis = FakeRedis()
    # seed 3 day-trades inside the rolling window
    for i in range(constants.PDT_MAX_DAY_TRADES):
        await redis.zadd("pdt_trades", {f"t{i}": time.time()})
    gate = RiskGatekeeper(redis=redis, session=make_session([]))
    result = await gate.check_pdt_limit(is_intraday=True, account_balance=20_000)
    assert result.approved is False
    assert result.rule_violated == Rules.PDT_LIMIT


@pytest.mark.asyncio
async def test_pdt_allows_swing_trade_when_at_limit():
    import time
    redis = FakeRedis()
    for i in range(constants.PDT_MAX_DAY_TRADES):
        await redis.zadd("pdt_trades", {f"t{i}": time.time()})
    gate = RiskGatekeeper(redis=redis, session=make_session([]))
    result = await gate.check_pdt_limit(is_intraday=False, account_balance=20_000)
    assert result.approved is True


@pytest.mark.asyncio
async def test_pdt_not_applied_above_threshold():
    import time
    redis = FakeRedis()
    for i in range(10):  # way over PDT max
        await redis.zadd("pdt_trades", {f"t{i}": time.time()})
    gate = RiskGatekeeper(redis=redis, session=make_session([]))
    result = await gate.check_pdt_limit(
        is_intraday=True, account_balance=constants.PDT_ACCOUNT_THRESHOLD + 1
    )
    assert result.approved is True


# ---------------------------------------------------------------------------
# check_combined_exposure
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_combined_exposure_blocks_above_55_pct():
    portfolio = 35_000.0
    # Existing exposure of $15,000 → adding $5,000 puts us at 20k/35k = 57%.
    session = make_session([15_000])
    gate = RiskGatekeeper(redis=FakeRedis(), session=session)
    result = await gate.check_combined_exposure(
        new_position_value=5_000, portfolio_value=portfolio
    )
    assert result.approved is False
    assert result.rule_violated == Rules.COMBINED_EXPOSURE


@pytest.mark.asyncio
async def test_combined_exposure_passes_under_cap():
    session = make_session([5_000])
    gate = RiskGatekeeper(redis=FakeRedis(), session=session)
    result = await gate.check_combined_exposure(
        new_position_value=3_000, portfolio_value=35_000
    )
    assert result.approved is True


# ---------------------------------------------------------------------------
# check_consecutive_losses
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_consecutive_loss_reduction_returns_correct_factor():
    redis = FakeRedis({"consecutive_losses": str(constants.CONSECUTIVE_LOSS_THRESHOLD)})
    gate = RiskGatekeeper(redis=redis, session=make_session([]))
    active, factor = await gate.check_consecutive_losses()
    assert active is True
    assert factor == constants.CONSECUTIVE_LOSS_SIZE_REDUCTION


@pytest.mark.asyncio
async def test_consecutive_loss_inactive_below_threshold():
    redis = FakeRedis({"consecutive_losses": "1"})
    gate = RiskGatekeeper(redis=redis, session=make_session([]))
    active, factor = await gate.check_consecutive_losses()
    assert active is False
    assert factor == 1.0


# ---------------------------------------------------------------------------
# happy path through check_all
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_check_all_clean_state_passes(monkeypatch):
    """All gates pass when state is clean.

    Forces a morning-time evaluation so the S1 market-hours check passes
    regardless of when the test suite runs.
    """
    from risk import gatekeeper as gk_mod

    fixed_now = datetime(2026, 4, 29, 10, 30, tzinfo=timezone.utc)
    monkeypatch.setattr(gk_mod, "_now_et", lambda: fixed_now)

    # Two select(...).scalar_one() calls happen in check_all when strategy=S1:
    # 1) _current_exposure(None) → combined exposure
    # No S2 checks fire on S1 strategy (early return without DB call).
    session = make_session([0, 0])
    redis = FakeRedis()
    gate = RiskGatekeeper(redis=redis, session=session)
    # ~10% position so the size cap doesn't fire
    signal = make_signal(strategy="S1", shares=800)
    result = await gate.check_all(signal=signal, account_balance=35_000)
    assert result.approved is True, result.message
