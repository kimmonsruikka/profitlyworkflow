"""EDGAR RSS watcher.

Polls SEC EDGAR for new filings every EDGAR_POLL_INTERVAL_MINUTES, weekdays
between EDGAR_POLL_HOUR_START_ET and EDGAR_POLL_HOUR_END_ET. For each priority
form type (8-K, S-1, S-3, Form 4) we fetch the latest-filings RSS, filter
client-side to our CIK universe, dedupe against sec_filings.accession_number,
insert a stub row, and enqueue a Celery task for downstream parsing.

Run the scheduler with:
    python -m ingestion.edgar.rss_watcher
"""

from __future__ import annotations

import asyncio
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Iterable
from zoneinfo import ZoneInfo

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from loguru import logger
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from config import constants
from config.settings import settings
from data.db import get_session
from data.models.sec_filing import SecFiling
from data.models.ticker import Ticker
from flows.celery_app import celery_app
from ingestion.edgar.cik_universe import _normalize_cik, load_universe_ciks


# ---------------------------------------------------------------------------
# RSS parsing
# ---------------------------------------------------------------------------
_ATOM_NS = {"a": "http://www.w3.org/2005/Atom"}

# Title format: "8-K - APPLE INC (0000320193) (Filer)"
_TITLE_RE = re.compile(r"^([\w\-/]+)\s*-\s*(.+?)\s*\((\d{6,10})\)")
# Accession in updated/id link: e.g. /Archives/edgar/data/320193/000032019324000123/0000320193-24-000123-index.htm
_ACCESSION_RE = re.compile(r"(\d{10}-\d{2}-\d{6})")


def _sec_headers() -> dict[str, str]:
    user_agent = settings.SEC_USER_AGENT or "trading-intelligence-system contact@example.com"
    return {"User-Agent": user_agent, "Accept": "application/atom+xml"}


def _build_rss_url(form_type: str) -> str:
    """EDGAR's getcurrent endpoint returns an Atom feed of recent filings."""
    return (
        f"{constants.EDGAR_RSS_URL}"
        f"?action=getcurrent&type={form_type}"
        f"&company=&dateb=&owner=include"
        f"&count={constants.EDGAR_RSS_FETCH_COUNT}&output=atom"
    )


def parse_atom_feed(xml_text: str) -> list[dict]:
    """Extract filings from an EDGAR Atom feed.

    Returns dicts with: accession_number, form_type, company_name, cik,
    filed_at, link.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        logger.warning("EDGAR feed parse failed: {}", exc)
        return []

    out: list[dict] = []
    for entry in root.findall("a:entry", _ATOM_NS):
        title_el = entry.find("a:title", _ATOM_NS)
        updated_el = entry.find("a:updated", _ATOM_NS)
        link_el = entry.find("a:link", _ATOM_NS)
        if title_el is None or title_el.text is None:
            continue

        match = _TITLE_RE.match(title_el.text.strip())
        if not match:
            continue
        form_type = match.group(1).strip()
        company = match.group(2).strip()
        cik = _normalize_cik(match.group(3))

        link = link_el.get("href") if link_el is not None else None
        accession = None
        if link:
            acc_match = _ACCESSION_RE.search(link)
            if acc_match:
                accession = acc_match.group(1)

        filed_at: datetime | None = None
        if updated_el is not None and updated_el.text:
            try:
                filed_at = datetime.fromisoformat(updated_el.text.replace("Z", "+00:00"))
            except ValueError:
                filed_at = None

        out.append({
            "accession_number": accession,
            "form_type": form_type,
            "company_name": company,
            "cik": cik,
            "filed_at": filed_at,
            "link": link,
        })
    return out


# ---------------------------------------------------------------------------
# Fetch + filter + persist
# ---------------------------------------------------------------------------
async def fetch_form_feed(client: httpx.AsyncClient, form_type: str) -> list[dict]:
    url = _build_rss_url(form_type)
    last_error: Exception | None = None
    for attempt in range(constants.EDGAR_HTTP_RETRY):
        try:
            resp = await client.get(url)
            resp.raise_for_status()
            return parse_atom_feed(resp.text)
        except httpx.HTTPError as exc:
            last_error = exc
            await asyncio.sleep(2 ** attempt)
    logger.error("EDGAR fetch {} failed after retries: {}", form_type, last_error)
    return []


async def _existing_accessions(
    session: AsyncSession, accessions: Iterable[str]
) -> set[str]:
    accs = [a for a in accessions if a]
    if not accs:
        return set()
    stmt = select(SecFiling.accession_number).where(SecFiling.accession_number.in_(accs))
    rows = (await session.execute(stmt)).scalars().all()
    return {a for a in rows if a}


async def _ticker_for_ciks(
    session: AsyncSession, ciks: Iterable[str]
) -> dict[str, str]:
    """Batch resolve CIKs to ticker symbols via the tickers table.

    Returns a mapping {cik: ticker}. CIKs without a tickers-table row are
    omitted from the result; callers should fall back to None.
    """
    cik_list = [c for c in {c for c in ciks if c}]
    if not cik_list:
        return {}
    stmt = select(Ticker.cik, Ticker.ticker).where(Ticker.cik.in_(cik_list))
    rows = (await session.execute(stmt)).all()
    return {cik: ticker for cik, ticker in rows if cik and ticker}


async def persist_and_queue(session: AsyncSession, filings: list[dict]) -> int:
    """Insert new filings and enqueue Celery tasks. Returns count enqueued."""
    if not filings:
        return 0
    seen = await _existing_accessions(
        session, (f["accession_number"] for f in filings if f.get("accession_number"))
    )
    cik_to_ticker = await _ticker_for_ciks(
        session, (f["cik"] for f in filings if f.get("cik"))
    )

    queued = 0
    for f in filings:
        acc = f.get("accession_number")
        if not acc or acc in seen:
            continue
        if f.get("filed_at") is None or f.get("form_type") is None:
            continue

        cik = f.get("cik")
        ticker = cik_to_ticker.get(cik) if cik else None
        if cik and ticker is None:
            logger.debug(
                "no ticker for cik={cik} on filing {acc} ({form}) — storing NULL",
                cik=cik, acc=acc, form=f.get("form_type"),
            )

        stmt = (
            pg_insert(SecFiling)
            .values(
                ticker=ticker,
                cik=cik,
                filed_at=f["filed_at"],
                form_type=f["form_type"],
                accession_number=acc,
                processed=False,
            )
            .on_conflict_do_nothing(index_elements=[SecFiling.accession_number])
        )
        await session.execute(stmt)

        process_filing.delay({
            "accession_number": acc,
            "cik": f["cik"],
            "form_type": f["form_type"],
            "company_name": f.get("company_name"),
            "link": f.get("link"),
            "filed_at": f["filed_at"].isoformat(),
        })
        queued += 1

    await session.flush()
    return queued


async def poll_once() -> dict[str, int]:
    """One polling cycle: fetch every priority form, filter, persist, queue.

    Returns a per-form count dict for logging/monitoring.
    """
    async with get_session() as session:
        universe = await load_universe_ciks(session)
        if not universe:
            logger.warning("poll_once: empty CIK universe — seed_universe never run?")
            return {}

        stats: dict[str, int] = {}
        async with httpx.AsyncClient(
            timeout=constants.EDGAR_HTTP_TIMEOUT_SECONDS,
            headers=_sec_headers(),
        ) as client:
            for form in constants.EDGAR_PRIORITY_FORMS:
                feed = await fetch_form_feed(client, form)
                in_universe = [f for f in feed if f.get("cik") and f["cik"] in universe]
                queued = await persist_and_queue(session, in_universe)
                stats[form] = queued
                logger.info(
                    "poll {form}: feed={n} in_universe={u} queued={q}",
                    form=form, n=len(feed), u=len(in_universe), q=queued,
                )
        return stats


# ---------------------------------------------------------------------------
# Celery task — stub for downstream parsing
# ---------------------------------------------------------------------------
@celery_app.task(name="ingestion.edgar.process_filing", bind=True, max_retries=3)
def process_filing(self, payload: dict) -> dict:
    """Phase 0: just acknowledge. Real 8-K item / IR-firm parsing comes next."""
    logger.info(
        "process_filing accepted form={form} cik={cik} accession={acc}",
        form=payload.get("form_type"),
        cik=payload.get("cik"),
        acc=payload.get("accession_number"),
    )
    return {"acknowledged": True, "accession": payload.get("accession_number")}


# ---------------------------------------------------------------------------
# APScheduler entry
# ---------------------------------------------------------------------------
def build_scheduler() -> AsyncIOScheduler:
    """Configure APScheduler with the polling cadence and gating."""
    tz = ZoneInfo(settings.TIMEZONE)
    scheduler = AsyncIOScheduler(timezone=tz)

    trigger = CronTrigger(
        day_of_week="mon-fri",
        hour=f"{constants.EDGAR_POLL_HOUR_START_ET}-{constants.EDGAR_POLL_HOUR_END_ET}",
        minute=f"*/{constants.EDGAR_POLL_INTERVAL_MINUTES}",
        timezone=tz,
    )

    async def _job() -> None:
        try:
            stats = await poll_once()
            logger.info("EDGAR poll done: {}", stats)
        except Exception:
            logger.exception("EDGAR poll cycle failed")

    scheduler.add_job(_job, trigger, id="edgar_rss_poll", replace_existing=True)
    return scheduler


async def _run_forever() -> None:
    scheduler = build_scheduler()
    scheduler.start()
    logger.info(
        "EDGAR RSS watcher started (every {m}m, weekdays {s}-{e} ET, forms={f})",
        m=constants.EDGAR_POLL_INTERVAL_MINUTES,
        s=constants.EDGAR_POLL_HOUR_START_ET,
        e=constants.EDGAR_POLL_HOUR_END_ET,
        f=constants.EDGAR_PRIORITY_FORMS,
    )
    try:
        await asyncio.Event().wait()
    finally:
        scheduler.shutdown(wait=False)


if __name__ == "__main__":
    try:
        asyncio.run(_run_forever())
    except (KeyboardInterrupt, SystemExit):
        pass
