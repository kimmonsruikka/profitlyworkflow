"""Pull float / shares outstanding from Polygon for every active ticker.

Phase 1 batch job. Walks the active universe, fetches ticker reference
data, writes float_shares + shares_outstanding back to the tickers table,
and deactivates rows whose float exceeds FLOAT_MAX or that Polygon doesn't
recognize. Throttling is handled inside PolygonClient.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Awaitable, Callable

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


async def _load_active_tickers(session: AsyncSession) -> list[Ticker]:
    stmt = select(Ticker).where(Ticker.active.is_(True)).order_by(Ticker.ticker)
    return list((await session.execute(stmt)).scalars().all())


async def update_floats_for_universe(
    session: AsyncSession,
    polygon: PolygonClient,
    *,
    progress_callback: ProgressCb | None = None,
) -> FloatUpdateReport:
    """Walk the active universe; update each row from Polygon.

    progress_callback receives (visited_count, total_count, last_ticker)
    so callers can stream progress. None disables progress output.
    """
    active = await _load_active_tickers(session)
    report = FloatUpdateReport(total=len(active))

    for idx, row in enumerate(active, start=1):
        try:
            details = await polygon.get_ticker_details(row.ticker)
        except PolygonNotFoundError:
            row.active = False
            report.deactivated_not_found += 1
            report.not_found_tickers.append(row.ticker)
        except Exception:
            # Network / 5xx — leave the row alone so the next run retries
            logger.exception("float update failed for {}", row.ticker)
            report.errors += 1
        else:
            float_shares = details.get("float_shares")
            shares_outstanding = details.get("shares_outstanding")
            row.float_shares = float_shares
            row.shares_outstanding = shares_outstanding

            if float_shares is not None and float_shares > constants.FLOAT_MAX:
                row.active = False
                report.deactivated_oversized += 1
                report.oversized_tickers.append(row.ticker)
            else:
                report.updated += 1

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
