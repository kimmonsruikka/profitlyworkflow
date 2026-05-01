"""Re-dispatch sec_filings rows stuck at processed=False.

When the celery worker is unable to process filings (outage, queue
full, deployment race), the watcher still inserts rows into
sec_filings with processed=False but the corresponding Celery task
either never runs or fails before flipping the flag. New filings
keep arriving fine after the underlying bug is fixed, but the
backlog from the outage window sits there forever unless re-queued.

This script scans sec_filings for processed=False rows and
re-dispatches each via process_filing.delay() with the same payload
shape the watcher would have queued.

LIMITATION — link reconstruction
--------------------------------
The watcher passes the EDGAR Atom feed's `link` to the Celery task
transiently; it isn't persisted in sec_filings. For backlog rows we
don't have the original link, so by default the worker hits the
empty-text branch and marks processed=True without re-extracting.
That recovers the flag (the primary goal) but loses the text.

If you want the worker to attempt re-extraction, pass
--reconstruct-links: we synthesize the deterministic EDGAR archive
URL from CIK + accession (same shape scripts/reprocess_filings.py
uses for the stale-stub path). The reconstructed URL is the filing
index page; the parser walks the index and finds the primary
document, so this typically works for 8-K / S-1 / S-3 / Form 4 in
the same way the live path does.

Usage
-----
Dry run (no dispatch):
    python scripts/reprocess_unprocessed_filings.py --dry-run

Drain the entire backlog at the default rate:
    python scripts/reprocess_unprocessed_filings.py

Drain only 8-Ks created during the outage window:
    python scripts/reprocess_unprocessed_filings.py \\
        --form-type 8-K \\
        --created-before 2026-05-01T17:13:01

Drain incrementally (50 at a time):
    python scripts/reprocess_unprocessed_filings.py --limit 50

Run on the droplet:
    sudo -u trading bash -c '
        set -a; source /app/profitlyworkflow/.env.production; set +a
        cd /app/profitlyworkflow
        ./venv/bin/python scripts/reprocess_unprocessed_filings.py [flags]
    '
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

env_file = ROOT / ".env.production"
if env_file.exists():
    load_dotenv(env_file, override=True)

from sqlalchemy import select  # noqa: E402

from data.db import get_session  # noqa: E402
from data.models.sec_filing import SecFiling  # noqa: E402
from ingestion.edgar.rss_watcher import process_filing  # noqa: E402


# Conservative default — celery prefork concurrency is 2, so we don't
# need to push hard. 10/sec lets a 280-row backlog clear in ~30 seconds
# while leaving headroom for the worker to actually process them.
DEFAULT_DISPATCH_RATE = 10  # tasks per second
PROGRESS_EVERY = 50


@dataclass(frozen=True)
class Filters:
    form_type: str | None
    created_before: datetime | None
    limit: int | None


def _reconstruct_archive_link(cik: str | None, accession: str | None) -> str | None:
    """Deterministic EDGAR filing-index URL from CIK + accession.

    Mirrors scripts/reprocess_filings.py — same shape, same caveats.
    Returns None when CIK or accession are missing/unparseable.
    """
    if not cik or not accession:
        return None
    try:
        cik_int = int(cik)
    except (TypeError, ValueError):
        return None
    acc_no_dashes = accession.replace("-", "")
    return (
        f"https://www.sec.gov/Archives/edgar/data/{cik_int}/"
        f"{acc_no_dashes}/{accession}-index.htm"
    )


async def load_unprocessed(filters: Filters) -> list[SecFiling]:
    """Pull rows where processed=False, oldest-first, optionally filtered."""
    async with get_session() as session:
        stmt = (
            select(SecFiling)
            .where(SecFiling.processed.is_(False))
            .order_by(SecFiling.created_at.asc())
        )
        if filters.form_type:
            stmt = stmt.where(SecFiling.form_type == filters.form_type)
        if filters.created_before is not None:
            stmt = stmt.where(SecFiling.created_at < filters.created_before)
        if filters.limit is not None:
            stmt = stmt.limit(filters.limit)
        return list((await session.execute(stmt)).scalars().all())


def build_payload(row: SecFiling, *, reconstruct_link: bool) -> dict:
    """Match the watcher's process_filing.delay() payload shape."""
    link = (
        _reconstruct_archive_link(row.cik, row.accession_number)
        if reconstruct_link
        else None
    )
    return {
        "accession_number": row.accession_number,
        "cik": row.cik,
        "form_type": row.form_type,
        "company_name": None,  # not persisted in sec_filings
        "link": link,
        "filed_at": row.filed_at.isoformat() if row.filed_at else None,
    }


def dispatch_all(
    rows: Iterable[SecFiling],
    *,
    total: int,
    dry_run: bool,
    reconstruct_link: bool,
    rate_per_second: int,
) -> int:
    """Dispatch each row's payload via process_filing.delay() at the
    requested rate. Returns count actually dispatched."""
    if total == 0:
        print("0 to reprocess — nothing to do.")
        return 0

    sleep_per = 1.0 / max(rate_per_second, 1)
    dispatched = 0
    start = time.monotonic()

    for idx, row in enumerate(rows, start=1):
        payload = build_payload(row, reconstruct_link=reconstruct_link)
        if dry_run:
            print(f"[dry-run] would dispatch {payload['accession_number']} ({payload['form_type']})")
        else:
            process_filing.delay(payload)
            time.sleep(sleep_per)
        dispatched += 1

        if idx % PROGRESS_EVERY == 0 or idx == total:
            remaining = total - idx
            print(f"  {idx}/{total} reprocessed, {remaining} remain")

    elapsed = time.monotonic() - start
    print()
    print(f"Done. dispatched={dispatched} elapsed={elapsed:.1f}s "
          f"dry_run={dry_run} reconstruct_link={reconstruct_link}")
    return dispatched


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be queued; don't actually dispatch.",
    )
    p.add_argument(
        "--form-type",
        type=str,
        default=None,
        help="Limit to one form type (e.g. 8-K, S-3, 4).",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap the number of rows reprocessed (oldest-first).",
    )
    p.add_argument(
        "--created-before",
        type=datetime.fromisoformat,
        default=None,
        help=(
            "Only reprocess rows whose created_at is before this timestamp "
            "(ISO 8601, e.g. 2026-05-01T17:13:01). Useful for scoping to "
            "a specific outage window."
        ),
    )
    p.add_argument(
        "--reconstruct-links",
        action="store_true",
        help=(
            "Synthesize EDGAR archive URLs from CIK + accession so the "
            "worker attempts text re-extraction. Without this flag, the "
            "worker hits the empty-text branch and only flips the flag."
        ),
    )
    p.add_argument(
        "--rate",
        type=int,
        default=DEFAULT_DISPATCH_RATE,
        help=f"Dispatches per second (default {DEFAULT_DISPATCH_RATE}).",
    )
    return p.parse_args(argv)


async def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    filters = Filters(
        form_type=args.form_type,
        created_before=args.created_before,
        limit=args.limit,
    )

    rows = await load_unprocessed(filters)
    total = len(rows)
    print(
        f"Found {total} unprocessed filings"
        + (f" form_type={filters.form_type}" if filters.form_type else "")
        + (f" created_before={filters.created_before.isoformat()}" if filters.created_before else "")
        + (f" limit={filters.limit}" if filters.limit else "")
    )

    dispatch_all(
        rows,
        total=total,
        dry_run=args.dry_run,
        reconstruct_link=args.reconstruct_links,
        rate_per_second=args.rate,
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
