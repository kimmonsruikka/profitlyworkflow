"""Pull float / shares outstanding from Polygon for every active ticker.

Phase 1 batch job. Walks the active universe, fetches ticker reference
data, writes float_shares + shares_outstanding back to the tickers table,
and deactivates rows whose float exceeds FLOAT_MAX or that Polygon doesn't
recognize. Throttling is handled inside PolygonClient.

Two layers:
  - update_one_ticker(session, polygon, row) — operates on a single Ticker
    row, returns a structured TickerUpdateResult. The script and the
    Prefect flow both call this through update_floats_for_universe.
  - update_floats_for_universe(session, polygon) — walks all active
    rows oldest-stale-first (float_updated_at ASC NULLS FIRST) and
    aggregates results into a FloatUpdateReport.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Awaitable, Callable, Literal

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import constants
from data.models.ticker import Ticker
from ingestion.market_data.polygon_client import (
    PolygonClient,
    PolygonNotFoundError,
)


ProgressCb = Callable[[int, int, str], None]


TickerUpdateStatus = Literal[
    "updated",
    "deactivated_oversized",
    "deactivated_not_on_polygon",
    "error",
]


@dataclass(frozen=True)
class TickerUpdateResult:
    """Outcome of a single update_one_ticker call."""

    status: TickerUpdateStatus
    ticker_symbol: str
    old_float: int | None = None
    new_float: int | None = None
    error: str | None = None


@dataclass
class FloatUpdateReport:
    total: int = 0
    updated: int = 0
    deactivated_oversized: int = 0
    deactivated_not_found: int = 0
    errors: int = 0
    not_found_tickers: list[str] = field(default_factory=list)
    oversized_tickers: list[str] = field(default_factory=list)

    def summary_lines(self) -> list[str]:
        return [
            f"total active tickers visited: {self.total}",
            f"  updated (float ≤ {constants.FLOAT_MAX:,}): {self.updated}",
            f"  deactivated (float > {constants.FLOAT_MAX:,}): {self.deactivated_oversized}",
            f"  deactivated (not on Polygon):                  {self.deactivated_not_found}",
            f"  errors (kept active for retry):                {self.errors}",
        ]

    def as_dict(self) -> dict:
        """Flat dict shape — used by the Prefect flow to write to flow_run_log."""
        return {
            "total": self.total,
            "updated": self.updated,
            "deactivated_oversized": self.deactivated_oversized,
            "deactivated_not_found": self.deactivated_not_found,
            "errors": self.errors,
        }


async def _load_active_tickers(session: AsyncSession) -> list[Ticker]:
    """Active tickers ordered oldest-stale-first.

    NULLS FIRST so brand-new and never-refreshed rows are touched
    before slightly-stale ones. If a flow run is interrupted, the
    next run picks up where it left off naturally — every row that
    didn't get refreshed last time still has the older
    float_updated_at value (or NULL).
    """
    stmt = (
        select(Ticker)
        .where(Ticker.active.is_(True))
        .order_by(Ticker.float_updated_at.asc().nulls_first(), Ticker.ticker)
    )
    return list((await session.execute(stmt)).scalars().all())


async def update_one_ticker(
    session: AsyncSession,
    polygon: PolygonClient,
    row: Ticker,
) -> TickerUpdateResult:
    """Refresh one ticker's float from Polygon and persist.

    Mutates `row` in place; the caller's session must be open and is
    responsible for the eventual flush/commit. Always sets
    float_updated_at on success paths (updated, deactivated_oversized,
    deactivated_not_on_polygon) so staleness tracking is honest.
    Errors leave the row untouched so the next sweep retries.
    """
    old_float = row.float_shares
    symbol = row.ticker
    now = datetime.now(timezone.utc)

    try:
        details = await polygon.get_ticker_details(symbol)
    except PolygonNotFoundError:
        row.active = False
        row.float_updated_at = now
        return TickerUpdateResult(
            status="deactivated_not_on_polygon",
            ticker_symbol=symbol,
            old_float=old_float,
            new_float=None,
        )
    except Exception as exc:
        # Network / 5xx — leave the row alone so the next run retries.
        # We deliberately do NOT update float_updated_at here.
        logger.exception("float update failed for {}", symbol)
        return TickerUpdateResult(
            status="error",
            ticker_symbol=symbol,
            old_float=old_float,
            error=repr(exc),
        )

    new_float = details.get("float_shares")
    row.float_shares = new_float
    row.shares_outstanding = details.get("shares_outstanding")
    row.float_updated_at = now

    if new_float is not None and new_float > constants.FLOAT_MAX:
        row.active = False
        return TickerUpdateResult(
            status="deactivated_oversized",
            ticker_symbol=symbol,
            old_float=old_float,
            new_float=new_float,
        )

    return TickerUpdateResult(
        status="updated",
        ticker_symbol=symbol,
        old_float=old_float,
        new_float=new_float,
    )


async def update_floats_for_universe(
    session: AsyncSession,
    polygon: PolygonClient,
    *,
    progress_callback: ProgressCb | None = None,
) -> FloatUpdateReport:
    """Walk the active universe oldest-stale-first; refresh each row.

    progress_callback receives (visited_count, total_count, last_ticker)
    so callers can stream progress. None disables progress output.
    """
    active = await _load_active_tickers(session)
    report = FloatUpdateReport(total=len(active))

    for idx, row in enumerate(active, start=1):
        result = await update_one_ticker(session, polygon, row)
        if result.status == "updated":
            report.updated += 1
        elif result.status == "deactivated_oversized":
            report.deactivated_oversized += 1
            report.oversized_tickers.append(result.ticker_symbol)
        elif result.status == "deactivated_not_on_polygon":
            report.deactivated_not_found += 1
            report.not_found_tickers.append(result.ticker_symbol)
        else:
            report.errors += 1

        if progress_callback:
            progress_callback(idx, report.total, row.ticker)

    await session.flush()
    logger.info("float update complete: {}", " | ".join(report.summary_lines()))
    return report


async def run_float_update(
    session_factory: Callable[[], Awaitable["AsyncSession"]] | None = None,
    polygon: PolygonClient | None = None,
    progress_callback: ProgressCb | None = None,
) -> FloatUpdateReport:
    """Convenience entrypoint shared by the operator script and the flow.

    Caller manages the session lifecycle by passing a context-manager
    factory; tests can inject a fake one. Polygon client is created on
    demand so production callers don't need to construct one.
    """
    from data.db import get_session  # local import to keep test isolation simple

    polygon = polygon or PolygonClient()
    cm = session_factory() if session_factory else get_session()
    async with cm as session:
        return await update_floats_for_universe(
            session, polygon, progress_callback=progress_callback
        )
