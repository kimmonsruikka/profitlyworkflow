"""Pydantic schemas returned by repository methods.

Repositories return these instead of ORM objects so business logic never
touches a detached SQLAlchemy instance.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict


class _Schema(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class TickerSchema(_Schema):
    ticker: str
    cik: str | None = None
    company_name: str | None = None
    float_shares: int | None = None
    exchange: str | None = None
    sector: str | None = None
    first_seen: datetime | None = None
    active: bool = True
    notes: str | None = None


class PromoterEntitySchema(_Schema):
    entity_id: uuid.UUID
    name: str
    type: str
    first_seen_edgar: datetime | None = None
    sec_enforcement_case: bool = False
    enforcement_case_url: str | None = None
    current_status: str | None = None
    notes: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class PromoterCampaignSchema(_Schema):
    campaign_id: uuid.UUID
    entity_id: uuid.UUID | None = None
    ticker: str | None = None
    launch_date: date | None = None
    end_date: date | None = None
    compensation_amount: Decimal | None = None
    compensation_type: str | None = None
    source_filing: str | None = None
    day1_move_pct: Decimal | None = None
    peak_move_pct: Decimal | None = None
    days_to_peak: int | None = None
    decay_speed: str | None = None
    campaign_result: str | None = None
    notes: str | None = None
    created_at: datetime | None = None


class PromoterNetworkEdgeSchema(_Schema):
    edge_id: uuid.UUID
    entity_a: uuid.UUID | None = None
    entity_b: uuid.UUID | None = None
    co_appearance_count: int = 1
    first_co_appearance: datetime | None = None
    last_co_appearance: datetime | None = None
    filing_references: list[Any] = []


class PromoterFingerprint(BaseModel):
    """Aggregate stats describing how a promoter's campaigns typically behave."""

    entity_id: uuid.UUID
    campaign_count: int
    avg_day1_move_pct: float | None
    avg_days_to_peak: float | None
    decay_speed_distribution: dict[str, int]


class SignalSchema(_Schema):
    signal_id: uuid.UUID
    strategy: str
    s2_category: str | None = None
    ticker: str
    generated_at: datetime
    catalyst_type: str | None = None
    confidence_score: Decimal | None = None
    liquidity_score: Decimal | None = None
    promoter_entity_id: uuid.UUID | None = None
    entry_price_low: Decimal | None = None
    entry_price_high: Decimal | None = None
    stop_price: Decimal | None = None
    target1_price: Decimal | None = None
    target2_price: Decimal | None = None
    risk_dollars: Decimal | None = None
    share_count: int | None = None
    outcome: str | None = None
    decline_reason: str | None = None
    paper_entry_price: Decimal | None = None
    alert_sent_at: datetime | None = None
    response_at: datetime | None = None
    response_time_seconds: int | None = None
    created_at: datetime | None = None


class TradeSchema(_Schema):
    trade_id: uuid.UUID
    signal_id: uuid.UUID | None = None
    strategy: str
    ticker: str
    entry_price: Decimal
    entry_time: datetime
    exit_price: Decimal | None = None
    exit_time: datetime | None = None
    shares: int
    pnl_dollars: Decimal | None = None
    pnl_r: Decimal | None = None
    hold_minutes: int | None = None
    exit_reason: str | None = None
    mae_dollars: Decimal | None = None
    mfe_dollars: Decimal | None = None
    liquidity_score_entry: Decimal | None = None
    liquidity_score_exit: Decimal | None = None
    slippage_cents_entry: Decimal | None = None
    slippage_cents_exit: Decimal | None = None
    overnight_hold: bool = False
    broker: str | None = None
    broker_order_id: str | None = None
    created_at: datetime | None = None


class PositionSchema(_Schema):
    position_id: uuid.UUID
    strategy: str
    ticker: str
    entry_price: Decimal
    entry_time: datetime
    shares: int
    current_price: Decimal | None = None
    stop_price: Decimal | None = None
    target1_price: Decimal | None = None
    target2_price: Decimal | None = None
    unrealized_pnl: Decimal | None = None
    unrealized_pnl_r: Decimal | None = None
    days_held: int = 0
    thesis_category: str | None = None
    thesis_intact: bool = True
    status: str = "open"
    signal_id: uuid.UUID | None = None
    updated_at: datetime | None = None
    created_at: datetime | None = None


class PriceBar(_Schema):
    ticker: str
    timestamp: datetime
    open: Decimal | None = None
    high: Decimal | None = None
    low: Decimal | None = None
    close: Decimal | None = None
    volume: int | None = None
    vwap: Decimal | None = None
    spread_pct: Decimal | None = None
    liquidity_score: Decimal | None = None


class DailyPnL(BaseModel):
    date: date
    realized: Decimal
    trade_count: int


class Expectancy(BaseModel):
    strategy: str
    sample_size: int
    win_rate: float
    avg_win_r: float
    avg_loss_r: float
    expectancy_r: float


class CatalystWinRate(BaseModel):
    catalyst_type: str
    sample_size: int
    win_rate: float
