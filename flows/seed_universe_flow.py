"""Prefect flow that seeds the CIK universe from SEC.

Manual trigger from Prefect Cloud. Wraps the same seed_universe() the
operator script uses so the underlying logic stays in one place.

Deploy with:
    prefect deploy flows/seed_universe_flow.py:seed_cik_universe_flow
"""

from __future__ import annotations

from prefect import flow, get_run_logger, task

from data.db import get_session
from ingestion.edgar.cik_universe import seed_universe


@task(name="run-seed-universe", retries=2, retry_delay_seconds=30)
async def _run_seed() -> int:
    async with get_session() as session:
        return await seed_universe(session)


@flow(name="seed-cik-universe")
async def seed_cik_universe_flow() -> int:
    """Pull SEC small-exchange listings into the tickers table.

    Returns the number of rows upserted. Logs via Prefect's run logger so
    the count surfaces in the Prefect Cloud UI.
    """
    logger = get_run_logger()
    logger.info("starting seed_cik_universe_flow")
    count = await _run_seed()
    logger.info("seed_cik_universe_flow upserted %d tickers", count)
    return count


if __name__ == "__main__":
    import asyncio

    asyncio.run(seed_cik_universe_flow())
