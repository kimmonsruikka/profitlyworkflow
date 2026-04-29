"""Risk gatekeeper. Every order submission goes through check_all() first.

Reads runtime state from Redis (today's loss, PDT count, consecutive losses)
and from the positions table (current exposure, S2 concurrent count).
Never approves an order that violates a rule. Every decision is logged.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, time as dtime
from decimal import Decimal
from typing import Optional
from zoneinfo import ZoneInfo

from loguru import logger
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from config import constants
from config.settings import settings
from data.models.gate_decision import GateDecision
from data.models.position import Position
from data.models.signal import Signal
from data.repositories.schemas import SignalSchema
from risk.pdt_tracker import PdtTracker


# ---------------------------------------------------------------------------
# Result type + rule names (constants so callers can branch on rule_violated)
# ---------------------------------------------------------------------------
class GatekeeperResult(BaseModel):
    approved: bool
    rule_violated: str | None = None
    message: str = ""


@dataclass(frozen=True)
class Rules:
    DAILY_LOSS_LIMIT = "daily_loss_limit"
    PDT_LIMIT = "pdt_limit"
    COMBINED_EXPOSURE = "combined_exposure"
    S2_CONCURRENT = "s2_concurrent_limit"
    S2_EXPOSURE = "s2_exposure_limit"
    LIQUIDITY_FLOOR = "liquidity_floor"
    MARKET_HOURS = "market_hours"
    SPREAD = "spread"
    POSITION_SIZE = "position_size_cap"


def _approved(message: str = "ok") -> GatekeeperResult:
    return GatekeeperResult(approved=True, message=message)


def _denied(rule: str, message: str) -> GatekeeperResult:
    return GatekeeperResult(approved=False, rule_violated=rule, message=message)


def _today_key() -> str:
    return f"daily_loss:{date.today().isoformat()}"


def _parse_hhmm(text: str) -> dtime:
    h, m = text.split(":")
    return dtime(int(h), int(m))


def _now_et() -> datetime:
    return datetime.now(ZoneInfo(settings.TIMEZONE))


def _entry_price(signal: SignalSchema) -> Decimal:
    low = signal.entry_price_low
    high = signal.entry_price_high
    if low is None and high is None:
        raise ValueError("signal needs at least one entry-price bound")
    if low is None:
        return Decimal(high)  # type: ignore[arg-type]
    if high is None:
        return Decimal(low)
    return (Decimal(low) + Decimal(high)) / Decimal(2)


def _position_value(signal: SignalSchema) -> Decimal:
    if signal.share_count is None or signal.share_count <= 0:
        raise ValueError("signal has no share_count")
    return _entry_price(signal) * Decimal(signal.share_count)


# ---------------------------------------------------------------------------
# Gatekeeper
# ---------------------------------------------------------------------------
class RiskGatekeeper:
    def __init__(
        self,
        redis: Redis,
        session: AsyncSession,
        pdt: Optional[PdtTracker] = None,
    ) -> None:
        self.redis = redis
        self.session = session
        self.pdt = pdt or PdtTracker(redis)

    # -------------------- orchestration --------------------
    async def check_all(
        self, signal: SignalSchema, account_balance: float
    ) -> GatekeeperResult:
        portfolio_value = account_balance
        position_value = float(_position_value(signal))
        liquidity = float(signal.liquidity_score) if signal.liquidity_score else 0.0
        is_intraday = signal.strategy == "S1"

        # Build callables (not coroutines) so an early-deny doesn't leak
        # un-awaited coroutines for later checks.
        checks: list = [
            lambda: self.check_daily_loss_limit(account_balance),
            lambda: self.check_pdt_limit(is_intraday, account_balance),
            lambda: self.check_liquidity_floor(liquidity),
            lambda: self.check_market_hours(signal.strategy, signal.generated_at),
            lambda: self.check_position_size_cap(
                position_value, portfolio_value, float(_entry_price(signal))
            ),
            lambda: self.check_combined_exposure(position_value, portfolio_value),
            lambda: self.check_s2_concurrent_limit(signal.strategy),
            lambda: self.check_s2_exposure_limit(
                signal.strategy, position_value, portfolio_value
            ),
        ]

        for make_check in checks:
            result = await make_check()
            await self.log_gate_decision(
                signal_id=signal.signal_id,
                rule=result.rule_violated or "approved",
                approved=result.approved,
                message=result.message,
            )
            if not result.approved:
                return result

        return _approved("all checks passed")

    # -------------------- individual checks --------------------
    async def check_daily_loss_limit(self, account_balance: float) -> GatekeeperResult:
        raw = await self.redis.get(_today_key())
        loss = float(raw or 0)
        threshold = constants.MAX_DAILY_LOSS_PCT * account_balance
        if loss >= threshold:
            return _denied(
                Rules.DAILY_LOSS_LIMIT,
                f"daily loss ${loss:.2f} >= threshold ${threshold:.2f}",
            )
        return _approved(f"daily loss ${loss:.2f} < threshold ${threshold:.2f}")

    async def check_pdt_limit(
        self, is_intraday: bool, account_balance: float
    ) -> GatekeeperResult:
        if account_balance >= constants.PDT_ACCOUNT_THRESHOLD:
            return _approved("above PDT threshold")
        if not is_intraday:
            return _approved("swing trade — PDT not applicable")
        used = await self.pdt.get_current_count()
        if used >= constants.PDT_MAX_DAY_TRADES:
            return _denied(
                Rules.PDT_LIMIT,
                f"PDT count {used} >= {constants.PDT_MAX_DAY_TRADES}",
            )
        return _approved(f"PDT count {used} < {constants.PDT_MAX_DAY_TRADES}")

    async def check_combined_exposure(
        self, new_position_value: float, portfolio_value: float
    ) -> GatekeeperResult:
        if portfolio_value <= 0:
            return _denied(Rules.COMBINED_EXPOSURE, "portfolio_value must be positive")
        current = await self._current_exposure(strategy=None)
        ratio = (current + new_position_value) / portfolio_value
        if ratio > constants.COMBINED_EXPOSURE_MAX:
            return _denied(
                Rules.COMBINED_EXPOSURE,
                f"combined exposure {ratio:.1%} > cap {constants.COMBINED_EXPOSURE_MAX:.0%}",
            )
        return _approved(f"combined exposure {ratio:.1%}")

    async def check_s2_concurrent_limit(self, strategy: str) -> GatekeeperResult:
        if strategy != "S2":
            return _approved("not S2")
        stmt = (
            select(func.count())
            .select_from(Position)
            .where(Position.strategy == "S2", Position.status == "open")
        )
        count = (await self.session.execute(stmt)).scalar_one() or 0
        if count >= constants.S2_MAX_CONCURRENT:
            return _denied(
                Rules.S2_CONCURRENT,
                f"S2 open positions {count} >= {constants.S2_MAX_CONCURRENT}",
            )
        return _approved(f"S2 open positions {count}")

    async def check_s2_exposure_limit(
        self, strategy: str, new_position_value: float, portfolio_value: float
    ) -> GatekeeperResult:
        if strategy != "S2":
            return _approved("not S2")
        if portfolio_value <= 0:
            return _denied(Rules.S2_EXPOSURE, "portfolio_value must be positive")
        current_s2 = await self._current_exposure(strategy="S2")
        ratio = (current_s2 + new_position_value) / portfolio_value
        if ratio > constants.S2_MAX_EXPOSURE_PCT:
            return _denied(
                Rules.S2_EXPOSURE,
                f"S2 exposure {ratio:.1%} > cap {constants.S2_MAX_EXPOSURE_PCT:.0%}",
            )
        return _approved(f"S2 exposure {ratio:.1%}")

    async def check_liquidity_floor(self, liquidity_score: float) -> GatekeeperResult:
        if liquidity_score < constants.LIQUIDITY_SCORE_MINIMUM:
            return _denied(
                Rules.LIQUIDITY_FLOOR,
                f"liquidity {liquidity_score} < floor {constants.LIQUIDITY_SCORE_MINIMUM}",
            )
        return _approved(f"liquidity {liquidity_score}")

    async def check_market_hours(
        self,
        strategy: str,
        catalyst_time: datetime | None = None,
        now: datetime | None = None,
    ) -> GatekeeperResult:
        if strategy != "S1":
            return _approved("not S1 — market-hours rule N/A")

        current = now or _now_et()
        cutoff = _parse_hhmm(constants.S1_ENTRY_CUTOFF)
        if current.time() >= cutoff:
            return _denied(
                Rules.MARKET_HOURS,
                f"current {current.time().strftime('%H:%M')} >= S1 cutoff {constants.S1_ENTRY_CUTOFF}",
            )

        afternoon = _parse_hhmm(constants.AFTERNOON_ENTRY_CUTOFF)
        if current.time() >= afternoon:
            if catalyst_time is None:
                return _denied(
                    Rules.MARKET_HOURS,
                    "afternoon entry without catalyst timestamp",
                )
            catalyst_local = catalyst_time
            if catalyst_local.tzinfo is None:
                catalyst_local = catalyst_local.replace(tzinfo=current.tzinfo)
            age_seconds = (current - catalyst_local).total_seconds()
            if age_seconds > 30 * 60:
                return _denied(
                    Rules.MARKET_HOURS,
                    f"afternoon entry but catalyst is {age_seconds/60:.0f} mins old",
                )
        return _approved(f"market-hours ok at {current.time().strftime('%H:%M')}")

    async def check_spread(self, spread_pct: float) -> GatekeeperResult:
        if spread_pct > constants.SPREAD_LIMIT_ABSOLUTE:
            return _denied(
                Rules.SPREAD,
                f"spread {spread_pct:.2%} > limit {constants.SPREAD_LIMIT_ABSOLUTE:.2%}",
            )
        return _approved(f"spread {spread_pct:.2%}")

    async def check_position_size_cap(
        self,
        position_value: float,
        portfolio_value: float,
        price: float,
    ) -> GatekeeperResult:
        if portfolio_value <= 0:
            return _denied(Rules.POSITION_SIZE, "portfolio_value must be positive")
        ratio = position_value / portfolio_value
        if ratio > constants.MAX_POSITION_PCT:
            return _denied(
                Rules.POSITION_SIZE,
                f"position {ratio:.1%} > cap {constants.MAX_POSITION_PCT:.0%}",
            )
        if price < 2.00 and ratio > constants.MAX_POSITION_PCT_SUB2:
            return _denied(
                Rules.POSITION_SIZE,
                f"sub-$2 position {ratio:.1%} > sub-$2 cap "
                f"{constants.MAX_POSITION_PCT_SUB2:.0%}",
            )
        return _approved(f"position {ratio:.1%}")

    async def check_consecutive_losses(self) -> tuple[bool, float]:
        raw = await self.redis.get("consecutive_losses")
        count = int(raw or 0)
        if count >= constants.CONSECUTIVE_LOSS_THRESHOLD:
            return True, constants.CONSECUTIVE_LOSS_SIZE_REDUCTION
        return False, 1.0

    # -------------------- audit log --------------------
    async def log_gate_decision(
        self,
        signal_id: uuid.UUID | None,
        rule: str,
        approved: bool,
        message: str,
    ) -> None:
        try:
            row = GateDecision(
                signal_id=signal_id,
                rule_triggered=rule,
                approved=approved,
                message=message[:500],
            )
            self.session.add(row)
            await self.session.flush()
        except Exception:
            # Audit logging must never block a real decision
            logger.exception("gate decision audit insert failed (rule={})", rule)

    # -------------------- helpers --------------------
    async def _current_exposure(self, strategy: str | None) -> float:
        """Sum (current_price OR entry_price) * shares across open positions."""
        price_expr = func.coalesce(Position.current_price, Position.entry_price)
        stmt = select(func.coalesce(func.sum(price_expr * Position.shares), 0)).where(
            Position.status == "open"
        )
        if strategy is not None:
            stmt = stmt.where(Position.strategy == strategy)
        value = (await self.session.execute(stmt)).scalar_one() or 0
        return float(value)
