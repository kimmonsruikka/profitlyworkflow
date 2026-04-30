"""Seed the CIK universe from SEC's company_tickers_exchange.json.

One-shot operator script. Run on the droplet:

    sudo -u trading /app/profitlyworkflow/venv/bin/python \\
        scripts/seed_cik_universe.py

The script fetches SEC's master JSON, filters to small-exchange listings
(EDGAR_SMALL_EXCHANGES — OTC, Pink, OTCBB, NYSE MKT, NYSE American),
upserts up to EDGAR_UNIVERSE_TARGET_SIZE rows into the tickers table,
and prints a summary so the EDGAR watcher has CIKs to monitor.

Idempotent — re-running picks up newly listed companies and refreshes
exchange/company_name on existing rows. float_shares is left alone for
the Polygon ingestor to fill.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from dotenv import load_dotenv

# Load .env.production BEFORE any imports that touch config.settings.
# Operator scripts run via `sudo -u trading python scripts/foo.py` don't
# inherit shell env vars, so we have to load the file explicitly here.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

env_file = ROOT / ".env.production"
if env_file.exists():
    load_dotenv(env_file, override=True)

from loguru import logger  # noqa: E402

from data.db import get_session  # noqa: E402
from ingestion.edgar.cik_universe import (  # noqa: E402
    load_active_universe,
    seed_universe,
)


async def main() -> int:
    logger.info("seeding CIK universe from SEC...")
    try:
        async with get_session() as session:
            count = await seed_universe(session)
            sample = (await load_active_universe(session))[:10]
    except Exception:
        logger.exception("seed failed")
        return 1

    print()
    print(f"Seeded {count} tickers into the universe.")
    print()
    if sample:
        print("Sample (first 10 active tickers):")
        print(f"  {'TICKER':<8} {'CIK':<12} {'EXCHANGE':<14} COMPANY")
        print(f"  {'-' * 6:<8} {'-' * 10:<12} {'-' * 12:<14} {'-' * 30}")
        for t in sample:
            print(
                f"  {t.ticker:<8} {(t.cik or '-'):<12} "
                f"{(t.exchange or '-'):<14} {t.company_name or ''}"
            )
    else:
        print("(no tickers in universe yet — check SEC fetch logs above)")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
