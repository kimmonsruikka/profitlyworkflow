"""PriceSource implementation: TimescaleDB cache → Polygon on miss.

Implements the `PriceSource` Protocol declared in
`flows.outcome_resolution_flow`. Caching is invisible to callers — they
call `get_ohlcv()` and receive an `OHLCVResult` with bars + metadata
(was it a cache hit, was the data complete, what ranges are still
missing).

Cache merge logic (the load-bearing part):

  1. Query price_data for (ticker, granularity, ts BETWEEN start AND end).
  2. Walk expected timestamps from the market calendar; identify gap
     ranges where the cache is missing data.
  3. If no cache rows: one Polygon call for the full range.
     If gaps: one Polygon call per gap (typically just one — gaps tend
     to be contiguous when a ticker has been seen recently).
     If no gaps: cache hit, no Polygon call.
  4. Write everything fetched from Polygon back into price_data with
     ON CONFLICT DO NOTHING (idempotent on retry).
  5. Return the merged set sorted by timestamp.

`is_complete` is computed from `expected_bar_count` in
`utils.market_calendar` against the merged result. The threshold lives
in `PRICE_DATA_COMPLETENESS_THRESHOLD`.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Awaitable, Callable, Iterable

from loguru import logger
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from config import constants
from data.models.price_data import PriceData
from flows.outcome_resolution_flow import OHLCVResult, PriceBar
from ingestion.market_data.polygon_client import (
    PolygonClient,
    PolygonNoDataError,
    PolygonNotFoundError,
)
from utils.market_calendar import expected_bar_count


# PriceBar (from flows.outcome_resolution_flow) is the single source of
# truth for the bar shape. Re-exported here so callers that import
# polygon_price_source don't need a cross-package import.
__all__ = [
    "OHLCVResult",
    "PriceBar",
    "PolygonCachedPriceSource",
    "identify_gaps",
]


_GRANULARITY_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "1d": 390}


def _to_float(value) -> float | None:
    if value is None:
        return None
    return float(value)


def _granularity_minutes(granularity: str) -> int:
    if granularity not in _GRANULARITY_MINUTES:
        raise ValueError(
            f"unsupported granularity {granularity!r} — pick from "
            f"{sorted(_GRANULARITY_MINUTES)}"
        )
    return _GRANULARITY_MINUTES[granularity]


def identify_gaps(
    cached_timestamps: Iterable[datetime],
    start: datetime,
    end: datetime,
    granularity: str,
    *,
    max_gap_skip_minutes: int = 60,
) -> list[tuple[datetime, datetime]]:
    """Find ranges in [start, end] not covered by `cached_timestamps`.

    Returns a list of (gap_start, gap_end) ranges. Each range is the
    smallest contiguous window the fetcher needs to ask Polygon for.

    `max_gap_skip_minutes` collapses tiny gaps within a fetched range —
    Polygon legitimately returns no bar for minutes the ticker didn't
    trade, and we don't want each missing minute spawning its own API
    call. Anything wider than this gets a fresh fetch.
    """
    cached_sorted = sorted(set(cached_timestamps))
    if not cached_sorted:
        return [(start, end)]

    gaps: list[tuple[datetime, datetime]] = []
    skip = timedelta(minutes=max_gap_skip_minutes)

    # Gap before the earliest cached bar.
    if cached_sorted[0] - start > skip:
        gaps.append((start, cached_sorted[0]))

    # Gaps between consecutive cached bars wider than the skip threshold.
    for prev, curr in zip(cached_sorted, cached_sorted[1:]):
        if curr - prev > skip:
            gaps.append((prev, curr))

    # Gap after the latest cached bar.
    if end - cached_sorted[-1] > skip:
        gaps.append((cached_sorted[-1], end))

    return gaps


class PolygonCachedPriceSource:
    """PriceSource implementation backed by a TimescaleDB cache.

    Constructor takes the polygon client (lazy — never creates one
    itself), a session-factory for cache reads/writes, and the granularity
    each call should use.

    The session factory pattern lets the resolution flow inject the same
    `get_session()` it uses elsewhere; tests inject a fake.
    """

    name: str = "polygon"

    def __init__(
        self,
        polygon_client: PolygonClient,
        session_factory: Callable[[], "Awaitable"],
        *,
        completeness_threshold: float = constants.PRICE_DATA_COMPLETENESS_THRESHOLD,
    ) -> None:
        self.polygon = polygon_client
        self._session_factory = session_factory
        self.completeness_threshold = completeness_threshold

    async def get_ohlcv(
        self,
        ticker: str,
        start: datetime,
        end: datetime,
        granularity: str = constants.PRICE_GRANULARITY_RULES["short_granularity"],
    ) -> OHLCVResult:
        gran_minutes = _granularity_minutes(granularity)
        async with self._session_factory() as session:
            cached = await self._read_cache(session, ticker, start, end, granularity)
            cached_ts = [b.timestamp for b in cached]
            gaps = identify_gaps(cached_ts, start, end, granularity)

            new_bars: list[PriceBar] = []
            still_missing: list[tuple[datetime, datetime]] = []
            for gap_start, gap_end in gaps:
                try:
                    fetched = await self.polygon.get_aggregates(  # type: ignore[attr-defined]
                        ticker, gap_start, gap_end, granularity
                    )
                except (PolygonNoDataError, PolygonNotFoundError):
                    still_missing.append((gap_start, gap_end))
                    continue
                except Exception:
                    # Transient network/5xx — caller should retry the
                    # whole resolution on the next flow run. Surface as
                    # a missing range and let the caller decide.
                    logger.exception(
                        "polygon.get_aggregates({}, {}, {}) failed",
                        ticker, gap_start, gap_end,
                    )
                    still_missing.append((gap_start, gap_end))
                    continue
                for raw in fetched:
                    new_bars.append(
                        PriceBar(
                            timestamp=raw["timestamp"],
                            open=_to_float(raw["open"]) or 0.0,
                            high=_to_float(raw["high"]) or 0.0,
                            low=_to_float(raw["low"]) or 0.0,
                            close=_to_float(raw["close"]) or 0.0,
                            volume=raw.get("volume"),
                        )
                    )

            if new_bars:
                await self._write_cache(session, ticker, granularity, new_bars)

        merged = sorted(cached + new_bars, key=lambda b: b.timestamp)
        # Dedupe in case a Polygon refetch overlapped with cached bars.
        seen: set[datetime] = set()
        deduped: list[PriceBar] = []
        for b in merged:
            if b.timestamp in seen:
                continue
            seen.add(b.timestamp)
            deduped.append(b)

        if not cached and not new_bars:
            source = "polygon"
        elif not cached:
            source = "polygon"
        elif not new_bars:
            source = "cache"
        else:
            source = "mixed"

        expected = expected_bar_count(start, end, gran_minutes)
        is_complete = (
            expected == 0
            or len(deduped) >= expected * self.completeness_threshold
        )

        return OHLCVResult(
            bars=deduped,
            source=source,
            missing_ranges=still_missing,
            is_complete=is_complete,
        )

    # ---------------- cache I/O ----------------
    async def _read_cache(
        self,
        session: AsyncSession,
        ticker: str,
        start: datetime,
        end: datetime,
        granularity: str,
    ) -> list[PriceBar]:
        stmt = (
            select(PriceData)
            .where(PriceData.ticker == ticker)
            .where(PriceData.timestamp >= start)
            .where(PriceData.timestamp <= end)
            .order_by(PriceData.timestamp)
        )
        # `granularity` filter — applied here rather than in the where()
        # chain because the column was added in migration 0007 and may not
        # exist in legacy ORM mappings. Use the model attribute when the
        # mapping is updated; fall back gracefully meanwhile.
        if hasattr(PriceData, "granularity"):
            stmt = stmt.where(PriceData.granularity == granularity)

        rows = (await session.execute(stmt)).scalars().all()
        return [
            PriceBar(
                timestamp=r.timestamp,
                open=float(r.open) if r.open is not None else 0.0,
                high=float(r.high) if r.high is not None else 0.0,
                low=float(r.low) if r.low is not None else 0.0,
                close=float(r.close) if r.close is not None else 0.0,
                volume=int(r.volume) if r.volume is not None else None,
            )
            for r in rows
        ]

    async def _write_cache(
        self,
        session: AsyncSession,
        ticker: str,
        granularity: str,
        bars: list[PriceBar],
    ) -> None:
        if not bars:
            return
        rows = [
            {
                "ticker": ticker,
                "granularity": granularity,
                "timestamp": b.timestamp,
                "open": Decimal(str(b.open)),
                "high": Decimal(str(b.high)),
                "low": Decimal(str(b.low)),
                "close": Decimal(str(b.close)),
                "volume": b.volume,
            }
            for b in bars
        ]
        # ON CONFLICT DO NOTHING — caller may have written some of these
        # before; preserves idempotency on retry.
        stmt = pg_insert(PriceData).values(rows).on_conflict_do_nothing(
            index_elements=["ticker", "granularity", "timestamp"]
        )
        await session.execute(stmt)
        await session.flush()
