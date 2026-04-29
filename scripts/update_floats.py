"""Pull float data from Polygon for every active ticker.

Operator one-shot. Long-running on the Starter plan:
  1000 tickers * 12s/req = ~3.5 hours.

Run on the droplet:

    sudo -u trading /app/profitlyworkflow/venv/bin/python \\
        /app/profitlyworkflow/scripts/update_floats.py

Idempotent — safe to re-run; rows that errored stay active and are
re-attempted on the next pass.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from loguru import logger  # noqa: E402

from config import constants  # noqa: E402
from data.db import get_session  # noqa: E402
from ingestion.market_data.float_updater import (  # noqa: E402
    update_floats_for_universe,
)
from ingestion.market_data.polygon_client import PolygonClient  # noqa: E402


def _make_progress_printer():
    started = time.monotonic()
    interval = constants.POLYGON_FLOAT_BATCH_PROGRESS_INTERVAL

    def progress(visited: int, total: int, last_ticker: str) -> None:
        if visited % interval == 0 or visited == total:
            elapsed = time.monotonic() - started
            rate = visited / elapsed if elapsed else 0.0
            remaining = (total - visited) / rate if rate else 0
            print(
                f"  [{visited:>4}/{total}] last={last_ticker:<6} "
                f"elapsed={elapsed/60:.1f}m  est_remaining={remaining/60:.1f}m",
                flush=True,
            )

    return progress


async def main() -> int:
    logger.info("starting float update for active universe...")
    polygon = PolygonClient()

    try:
        async with get_session() as session:
            report = await update_floats_for_universe(
                session,
                polygon,
                progress_callback=_make_progress_printer(),
            )
    except Exception:
        logger.exception("float update failed")
        return 1

    print()
    print("Float update complete.")
    for line in report.summary_lines():
        print(f"  {line}")
    print()
    if report.not_found_tickers:
        print(f"  Sample not-found (first 10): {', '.join(report.not_found_tickers[:10])}")
    if report.oversized_tickers:
        print(f"  Sample oversized (first 10): {', '.join(report.oversized_tickers[:10])}")
    print()
    print(
        f"Active universe is now: "
        f"{report.updated} tickers (float ≤ {constants.FLOAT_MAX:,})."
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
