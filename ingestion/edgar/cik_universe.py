"""CIK universe management.

Two responsibilities:
  - Resolve our active ticker set to the CIKs we want EDGAR to alert us on.
  - Seed the initial universe by pulling SEC's company_tickers_exchange.json
    and filtering to small-exchange listings (the catalyst-trading hunting
    ground). Float filtering happens later — this seed leaves float_shares
    NULL pending Polygon ingestion.
"""

from __future__ import annotations

from typing import Iterable

import httpx
from loguru import logger
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from config import constants
from config.settings import settings
from data.models.ticker import Ticker


def _sec_headers() -> dict[str, str]:
    """SEC requires identifying contact info on every direct request."""
    user_agent = settings.SEC_USER_AGENT or "trading-intelligence-system contact@example.com"
    return {"User-Agent": user_agent, "Accept": "application/json"}


async def fetch_company_tickers_exchange() -> list[dict]:
    """Pull SEC's master ticker→CIK→exchange JSON.

    The file is structured as:
      {"fields": ["cik", "name", "ticker", "exchange"],
       "data": [[320193, "Apple Inc.", "AAPL", "Nasdaq"], ...]}

    Returned as a list of dicts so callers don't depend on the column order.
    """
    async with httpx.AsyncClient(
        timeout=constants.EDGAR_HTTP_TIMEOUT_SECONDS,
        headers=_sec_headers(),
    ) as client:
        resp = await client.get(constants.EDGAR_COMPANY_TICKERS_EXCHANGE_URL)
        resp.raise_for_status()
        payload = resp.json()

    fields = payload.get("fields") or []
    rows = payload.get("data") or []
    return [dict(zip(fields, row)) for row in rows]


def _normalize_cik(value: int | str | None) -> str | None:
    """SEC CIKs are 10-digit zero-padded strings. JSON gives an int."""
    if value is None or value == "":
        return None
    return str(value).zfill(10)


def _is_small_exchange(exchange: str | None) -> bool:
    if not exchange:
        return False
    target = {e.lower() for e in constants.EDGAR_SMALL_EXCHANGES}
    return exchange.lower() in target


async def seed_universe(
    session: AsyncSession,
    *,
    exchanges: Iterable[str] | None = None,
    limit: int | None = None,
) -> int:
    """Populate the tickers table with small-exchange listings from SEC.

    `exchanges` overrides the default `EDGAR_SMALL_EXCHANGES` set. `limit`
    caps how many rows to upsert (target ~500-1000 for the initial universe).
    Existing rows keep their float/sector/notes; only ticker/cik/company_name
    /exchange are touched.

    Returns the number of rows upserted.
    """
    target_exchanges = {e.lower() for e in (exchanges or constants.EDGAR_SMALL_EXCHANGES)}
    cap = limit or constants.EDGAR_UNIVERSE_TARGET_SIZE

    rows = await fetch_company_tickers_exchange()
    candidates = [
        r for r in rows
        if (r.get("exchange") or "").lower() in target_exchanges and r.get("ticker")
    ][:cap]

    if not candidates:
        logger.warning("seed_universe: zero candidates from SEC matching {}", target_exchanges)
        return 0

    upserted = 0
    for row in candidates:
        ticker = str(row["ticker"]).upper()
        cik = _normalize_cik(row.get("cik"))
        company_name = row.get("name")
        exchange = row.get("exchange")

        stmt = (
            pg_insert(Ticker)
            .values(
                ticker=ticker,
                cik=cik,
                company_name=company_name,
                exchange=exchange,
                active=True,
            )
            .on_conflict_do_update(
                index_elements=[Ticker.ticker],
                set_={
                    "cik": cik,
                    "company_name": company_name,
                    "exchange": exchange,
                },
            )
        )
        await session.execute(stmt)
        upserted += 1

    await session.flush()
    logger.info(
        "seed_universe: upserted {n} tickers from SEC for exchanges={ex}",
        n=upserted, ex=sorted(target_exchanges),
    )
    return upserted


async def load_universe_ciks(session: AsyncSession) -> set[str]:
    """Return the set of CIKs we monitor — active tickers with a CIK assigned."""
    stmt = select(Ticker.cik).where(
        Ticker.active.is_(True),
        Ticker.cik.isnot(None),
    )
    rows = (await session.execute(stmt)).scalars().all()
    ciks = {_normalize_cik(c) for c in rows if c}
    ciks.discard(None)
    return ciks  # type: ignore[return-value]


async def load_active_universe(session: AsyncSession) -> list[Ticker]:
    """Return the active ticker rows (full ORM objects, for diagnostics)."""
    stmt = select(Ticker).where(Ticker.active.is_(True)).order_by(Ticker.ticker)
    return list((await session.execute(stmt)).scalars().all())
