"""Operator alerting watchdog — runs every 60s via cron, fires Telegram
alerts on state transitions from clear to breached.

Five conditions checked each invocation (condition 5 is daily-throttled):

  1. celery-worker not running          (systemctl is-active)
  2. edgar-watcher not running          (systemctl is-active)
  3. backlog threshold breached         (sec_filings WHERE processed=false)
  4. recent celery task failure rate    (journalctl, last 15 min)
  5. float refresh schedule missed      (flow_run_log, daily-throttled)

Plus an implicit sixth condition for meta-alerting:

  6. alerter cannot reach the database  (raised when 3 or 5 fail)

State lives in /var/log/trading/alert_state.json. State transitions:

  - clear → breached:  fire alert, mark breached, record timestamp
  - breached → breached: silent (alert was already fired)
  - breached → clear:  silently clear state. v1 does NOT send a
                       'recovered' message — that's queued for v2.

First run with no state file: initialize all-clear, fire NOTHING.
This avoids alert spam on initial deploy / fresh state.

Operational expectations
------------------------
  - Steady state: zero alerts, zero stdout output, exit 0.
  - State change: one alert per condition that flipped.
  - Cron runs minute-level; condition 5 internally throttles to once
    per UTC day so the minute-cadence doesn't produce daily-condition
    spam.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import traceback
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

env_file = ROOT / ".env.production"
if env_file.exists():
    load_dotenv(env_file, override=True)


STATE_FILE = Path("/var/log/trading/alert_state.json")
BACKLOG_THRESHOLD = 10
CELERY_FAILURE_THRESHOLD = 3
CELERY_FAILURE_LOOKBACK_MINUTES = 15
FLOAT_REFRESH_STALE_DAYS = 8
FLOAT_REFRESH_FLOW_NAME = "weekly-float-update"  # actual value in flow_run_log

# Condition keys. Order is the firing order.
CONDITIONS = (
    "celery_worker_alive",
    "edgar_watcher_alive",
    "backlog_threshold",
    "celery_failure_rate",
    "float_refresh_schedule",
    # alerter_db_unreachable is implicit; not in CONDITIONS so the daily
    # throttling logic doesn't accidentally treat it as a condition-5 case.
)


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------
def default_state() -> dict[str, Any]:
    """Initial all-clear state shape. New keys added in future PRs need a
    migration path; current shape is the v1 baseline."""
    base = {
        c: {"breached": False, "last_alerted_at": None}
        for c in CONDITIONS
    }
    base["float_refresh_schedule"]["last_checked_date"] = None
    base["alerter_db_unreachable"] = {"breached": False, "last_alerted_at": None}
    return base


def read_state(path: Path) -> tuple[dict[str, Any], bool]:
    """Return (state, is_first_run). is_first_run=True when the file
    didn't exist or was unreadable — caller suppresses alerts in that case
    so the first invocation just establishes the baseline."""
    if not path.exists():
        return default_state(), True
    try:
        with open(path) as f:
            loaded = json.load(f)
    except (json.JSONDecodeError, OSError):
        # Corrupt / unreadable → treat as first run (rebuild fresh).
        return default_state(), True

    # Backfill any missing keys from defaults so a v2 state-file shape
    # doesn't trip up a v1-baseline reader (and vice versa).
    state = default_state()
    for key, val in loaded.items():
        if key in state and isinstance(val, dict):
            state[key].update(val)
    return state, False


def write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(state, f, indent=2)


# ---------------------------------------------------------------------------
# Condition checks — IO wrappers (mockable in tests)
# ---------------------------------------------------------------------------
def systemctl_is_active(service: str) -> str:
    """Return systemctl's is-active output (e.g. 'active', 'inactive',
    'failed'). Trailing newline stripped. Errors return the error text
    so the alert still surfaces something useful."""
    try:
        result = subprocess.run(
            ["systemctl", "is-active", service],
            capture_output=True, text=True, timeout=10, check=False,
        )
        return (result.stdout or result.stderr or "").strip() or "unknown"
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        return f"error: {type(exc).__name__}"


def last_active_timestamp(service: str) -> str:
    """Last 'Started <service>' timestamp from journalctl, or 'unknown'.

    Spec calls for the last 'active (running)' marker. Systemd emits
    'Started <unit>' when a unit starts, which is the same event;
    journalctl --grep on that string is the most reliable approximation.
    """
    try:
        result = subprocess.run(
            [
                "journalctl",
                "-u", service,
                "--grep", "Started",
                "-n", "1",
                "--output", "short-iso",
                "--no-pager",
            ],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return "unknown"

    for line in (result.stdout or "").splitlines():
        # Skip the journalctl header line ("-- Logs begin at ...")
        if line.startswith("--"):
            continue
        # journalctl short-iso format: "2026-05-06T11:14:23+0000 host systemd[1]: Started ..."
        parts = line.split(" ", 1)
        if parts:
            return parts[0]
    return "unknown"


# Match anything between brackets — celery task IDs are UUIDs in
# production but tests use shorter ids. Anchor on `raised unexpected:`
# so we only count actual task crashes, not arbitrary `[bracket]` text.
_CELERY_TASK_ID_RE = re.compile(r"\[([^\]]+)\]\s+raised unexpected:\s*(.*)$")


def parse_celery_failures(journal_output: str) -> tuple[int, str | None]:
    """Walk journalctl output for celery-worker, count distinct task IDs
    with 'raised unexpected:' lines. Return (count, most_recent_error_text).

    Pure function over the journal text — easy to unit test.
    """
    errors_by_task: dict[str, str] = {}  # task_id → message text
    most_recent_msg: str | None = None
    for line in journal_output.splitlines():
        match = _CELERY_TASK_ID_RE.search(line)
        if not match:
            continue
        task_id, msg = match.group(1), match.group(2).strip()
        # Each task_id counted once even if it appears multiple times.
        errors_by_task[task_id] = msg
        most_recent_msg = msg  # last match wins → "most recent"
    return len(errors_by_task), most_recent_msg


def fetch_celery_journal(since_minutes: int = CELERY_FAILURE_LOOKBACK_MINUTES) -> str:
    try:
        result = subprocess.run(
            [
                "journalctl",
                "-u", "celery-worker",
                "--since", f"{since_minutes} minutes ago",
                "--no-pager",
            ],
            capture_output=True, text=True, timeout=10, check=False,
        )
        return result.stdout or ""
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""


async def count_unprocessed_filings(session) -> int:
    from sqlalchemy import func, select
    from data.models.sec_filing import SecFiling

    return (
        await session.execute(
            select(func.count()).select_from(SecFiling).where(
                SecFiling.processed.is_(False)
            )
        )
    ).scalar_one() or 0


async def last_completed_float_refresh(session) -> datetime | None:
    """Most recent flow_run_log row with status='completed' for the
    float-refresh flow. Returns its completed_at (or started_at if
    completed_at is null)."""
    from sqlalchemy import select
    from data.models.flow_run_log import FlowRunLog

    row = (
        await session.execute(
            select(FlowRunLog)
            .where(FlowRunLog.flow_name == FLOAT_REFRESH_FLOW_NAME)
            .where(FlowRunLog.status == "completed")
            .order_by(FlowRunLog.started_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    return row.completed_at or row.started_at


# ---------------------------------------------------------------------------
# Pure helpers for date math + alert formatting
# ---------------------------------------------------------------------------
def next_sunday_6am_et(now_utc: datetime) -> datetime:
    """Next Sunday 06:00 America/New_York. If today is Sunday past 06:00
    ET, returns the Sunday a week out."""
    et = ZoneInfo("America/New_York")
    now_et = now_utc.astimezone(et)
    # Monday=0 ... Sunday=6
    days_until_sunday = (6 - now_et.weekday()) % 7
    target = now_et.replace(hour=6, minute=0, second=0, microsecond=0)
    if days_until_sunday == 0 and now_et.hour >= 6:
        days_until_sunday = 7
    return (target + timedelta(days=days_until_sunday)).astimezone(timezone.utc)


def today_utc_iso(now_utc: datetime | None = None) -> str:
    return (now_utc or datetime.now(timezone.utc)).date().isoformat()


def format_celery_alert(status: str, last_seen: str) -> str:
    return (
        "⚠ celery-worker not running\n"
        f"Status: {status}\n"
        f"Last seen active: {last_seen}"
    )


def format_edgar_alert(status: str, last_seen: str) -> str:
    return (
        "⚠ edgar-watcher not running\n"
        f"Status: {status}\n"
        f"Last seen active: {last_seen}"
    )


def format_backlog_alert(count: int, threshold: int = BACKLOG_THRESHOLD) -> str:
    return (
        "⚠ Backlog threshold breached\n"
        f"{count} unprocessed filings in sec_filings\n"
        f"Threshold: {threshold}"
    )


def format_failures_alert(count: int, recent_error: str | None) -> str:
    err = (recent_error or "(no error text captured)")[:80]
    return (
        "⚠ Celery task failures detected\n"
        f"{count} task crashes in last 15 minutes\n"
        f"Most recent error: {err}"
    )


def format_float_refresh_alert(
    last_refresh: datetime | None, now_utc: datetime
) -> str:
    if last_refresh is None:
        last_str = "never"
        days_ago_str = "≥8 days ago"
    else:
        last_str = last_refresh.date().isoformat()
        days_ago = (now_utc.date() - last_refresh.date()).days
        days_ago_str = f"{days_ago} days ago"
    next_run = next_sunday_6am_et(now_utc)
    next_str = next_run.astimezone(ZoneInfo("America/New_York")).strftime(
        "%Y-%m-%d %H:%M ET"
    )
    return (
        "⚠ Float refresh schedule missed\n"
        f"Last successful refresh: {last_str} ({days_ago_str})\n"
        f"Next expected: {next_str}"
    )


def format_db_unreachable_alert(error: str) -> str:
    return (
        "⚠ Alerter cannot reach database\n"
        f"{error[:200]}"
    )


# ---------------------------------------------------------------------------
# Telegram delivery
# ---------------------------------------------------------------------------
def send_telegram(token: str, chat_id: str, text: str) -> None:
    import httpx

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    response = httpx.post(
        url,
        json={
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        },
        timeout=15.0,
    )
    response.raise_for_status()


# ---------------------------------------------------------------------------
# State-transition kernel — pure logic, easy to test
# ---------------------------------------------------------------------------
@dataclass
class CheckResult:
    name: str
    breached: bool
    alert_text: str | None = None  # formatted alert when breached
    extras: dict[str, Any] = field(default_factory=dict)  # state metadata


def apply_transition(
    state: dict[str, Any],
    result: CheckResult,
    now_utc: datetime,
) -> str | None:
    """Apply clear/breached transition. Returns alert text to fire, or
    None when nothing should be fired (steady-state or recovery).

    Mutates state in place — caller writes it back to disk after the loop.
    """
    cond = state[result.name]
    was_breached = bool(cond.get("breached"))

    if result.breached and not was_breached:
        cond["breached"] = True
        cond["last_alerted_at"] = now_utc.isoformat()
        cond.update(result.extras)
        return result.alert_text

    if result.breached and was_breached:
        # Already alerted; carry any extras forward (e.g. last_checked_date).
        cond.update(result.extras)
        return None

    if not result.breached and was_breached:
        # Recovery — silent in v1.
        cond["breached"] = False
        cond["last_alerted_at"] = None
        cond.update(result.extras)
        return None

    # not result.breached and not was_breached — keep extras up to date.
    cond.update(result.extras)
    return None


# ---------------------------------------------------------------------------
# The condition-evaluation pipeline
# ---------------------------------------------------------------------------
async def evaluate_conditions(
    state: dict[str, Any],
    now_utc: datetime,
    *,
    systemctl_runner: Callable[[str], str] = systemctl_is_active,
    last_active_runner: Callable[[str], str] = last_active_timestamp,
    journal_runner: Callable[[int], str] = fetch_celery_journal,
    db_session_factory: Callable[[], Awaitable[Any]] | None = None,
) -> tuple[list[CheckResult], str | None]:
    """Run all five condition checks. Returns (results, db_error_text).

    db_error_text is non-None when condition 3 or 5 hit a DB failure;
    caller emits the meta-alert.
    """
    results: list[CheckResult] = []

    # --- 1. celery-worker
    status = systemctl_runner("celery-worker")
    breached = status != "active"
    last_seen = last_active_runner("celery-worker") if breached else ""
    results.append(CheckResult(
        name="celery_worker_alive",
        breached=breached,
        alert_text=format_celery_alert(status, last_seen) if breached else None,
    ))

    # --- 2. edgar-watcher
    status = systemctl_runner("edgar-watcher")
    breached = status != "active"
    last_seen = last_active_runner("edgar-watcher") if breached else ""
    results.append(CheckResult(
        name="edgar_watcher_alive",
        breached=breached,
        alert_text=format_edgar_alert(status, last_seen) if breached else None,
    ))

    # --- 4. celery failure rate (parsed from journal — no DB needed)
    journal_text = journal_runner(CELERY_FAILURE_LOOKBACK_MINUTES)
    fail_count, recent_err = parse_celery_failures(journal_text)
    breached = fail_count >= CELERY_FAILURE_THRESHOLD
    results.append(CheckResult(
        name="celery_failure_rate",
        breached=breached,
        alert_text=format_failures_alert(fail_count, recent_err) if breached else None,
    ))

    # --- 3 & 5: DB-backed
    db_error_text: str | None = None
    backlog_breached = False
    backlog_count = 0
    float_breached = False
    last_refresh: datetime | None = None
    today_iso = today_utc_iso(now_utc)
    last_checked = state["float_refresh_schedule"].get("last_checked_date")
    run_condition_5 = last_checked != today_iso

    if db_session_factory is None:
        from data.db import get_session
        cm_factory = get_session
    else:
        cm_factory = db_session_factory

    try:
        async with cm_factory() as session:
            backlog_count = await count_unprocessed_filings(session)
            backlog_breached = backlog_count >= BACKLOG_THRESHOLD
            if run_condition_5:
                last_refresh = await last_completed_float_refresh(session)
                if last_refresh is None:
                    float_breached = True
                else:
                    age_days = (now_utc.date() - last_refresh.date()).days
                    float_breached = age_days >= FLOAT_REFRESH_STALE_DAYS
    except Exception as exc:  # noqa: BLE001 — meta-alert path captures all
        db_error_text = repr(exc)

    # condition 3 — backlog
    results.append(CheckResult(
        name="backlog_threshold",
        breached=backlog_breached,
        alert_text=format_backlog_alert(backlog_count) if backlog_breached else None,
    ))

    # condition 5 — float refresh schedule (daily-throttled)
    extras = {"last_checked_date": today_iso} if run_condition_5 else {}
    results.append(CheckResult(
        name="float_refresh_schedule",
        breached=float_breached if run_condition_5 else state[
            "float_refresh_schedule"]["breached"],
        alert_text=(
            format_float_refresh_alert(last_refresh, now_utc)
            if (run_condition_5 and float_breached)
            else None
        ),
        extras=extras,
    ))

    return results, db_error_text


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------
async def run_alerter(
    *,
    state_path: Path = STATE_FILE,
    now_utc: datetime | None = None,
    telegram_sender: Callable[[str, str, str], None] = send_telegram,
    systemctl_runner: Callable[[str], str] = systemctl_is_active,
    last_active_runner: Callable[[str], str] = last_active_timestamp,
    journal_runner: Callable[[int], str] = fetch_celery_journal,
    db_session_factory: Callable[[], Awaitable[Any]] | None = None,
    token: str | None = None,
    chat_id: str | None = None,
) -> int:
    """Single end-to-end invocation. Returns process exit code."""
    now = now_utc or datetime.now(timezone.utc)
    token = token or (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat_id = chat_id or (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    if not token or not chat_id:
        missing = [
            n for n, v in (
                ("TELEGRAM_BOT_TOKEN", token), ("TELEGRAM_CHAT_ID", chat_id)
            ) if not v
        ]
        print(
            f"operator_alerter: missing env var(s): {', '.join(missing)}",
            file=sys.stderr,
        )
        return 1

    state, is_first_run = read_state(state_path)

    if is_first_run:
        # Establish baseline — no alerts on the very first invocation.
        write_state(state_path, state)
        return 0

    results, db_error = await evaluate_conditions(
        state, now,
        systemctl_runner=systemctl_runner,
        last_active_runner=last_active_runner,
        journal_runner=journal_runner,
        db_session_factory=db_session_factory,
    )

    alerts_to_send: list[tuple[str, str]] = []
    cleared: list[str] = []

    # Apply state transitions for the five primary conditions.
    for result in results:
        before_breached = bool(state[result.name].get("breached"))
        text = apply_transition(state, result, now)
        if text:
            alerts_to_send.append((result.name, text))
        elif before_breached and not result.breached:
            cleared.append(result.name)

    # Meta-alert for the implicit DB-unreachable condition.
    db_result = CheckResult(
        name="alerter_db_unreachable",
        breached=db_error is not None,
        alert_text=format_db_unreachable_alert(db_error or "") if db_error else None,
    )
    db_text = apply_transition(state, db_result, now)
    if db_text:
        alerts_to_send.append((db_result.name, db_text))
    elif state["alerter_db_unreachable"].get("breached") is False and \
         (db_error is None) and bool(
             state.get("alerter_db_unreachable", {}).get("last_alerted_at")
         ):
        cleared.append(db_result.name)

    # Send alerts. Any send failure → exit 1 with traceback.
    for name, text in alerts_to_send:
        telegram_sender(token, chat_id, text)
        print(f"Alert fired: {name} at {now.isoformat()}")

    for name in cleared:
        print(f"Alert cleared: {name} at {now.isoformat()}")

    write_state(state_path, state)
    return 0


async def main() -> int:
    try:
        return await run_alerter()
    except Exception:
        traceback.print_exc(file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
