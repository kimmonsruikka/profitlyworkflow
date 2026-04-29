"""Verify the runtime environment is wired correctly.

Run from the project root:

    python scripts/verify_setup.py

Exits 0 if every critical check passes, 1 otherwise. Connection checks fail
gracefully if the local services aren't running yet — they are flagged but
the script only exits 1 on imports/env-var failures by default. Pass
`--strict` to make connection failures critical too.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import sys
from typing import Awaitable, Callable

from loguru import logger


PASS = "PASS"
FAIL = "FAIL"
WARN = "WARN"


REQUIRED_MODULES = [
    "config",
    "config.constants",
    "config.settings",
    "data",
    "data.db",
    "data.models",
    "data.repositories",
    "risk",
    "risk.gatekeeper",
    "risk.pdt_tracker",
    "execution",
    "execution.broker",
    "execution.order_manager",
    "execution.paper_trade",
    "api.health",
    "api.main",
]

# From .env.example. Variables that must have non-empty values for the system
# to actually function — enforced strictly. Some keys (like X / Reddit /
# Telegram monitor) are optional in Phase 0 and only emit warnings.
REQUIRED_ENV_VARS = [
    "ENVIRONMENT",
    "BROKER_MODE",
    "ALPACA_API_KEY",
    "ALPACA_SECRET_KEY",
    "ALPACA_BASE_URL",
    "DATABASE_URL",
    "REDIS_URL",
    "POLYGON_API_KEY",
    "BENZINGA_API_KEY",
    "ORTEX_API_KEY",
    "SEC_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
]

OPTIONAL_ENV_VARS = [
    "TELEGRAM_MONITOR_API_ID",
    "TELEGRAM_MONITOR_API_HASH",
    "REDDIT_CLIENT_ID",
    "REDDIT_CLIENT_SECRET",
    "REDDIT_USER_AGENT",
    "X_API_BEARER_TOKEN",
    "STOCKTWITS_ACCESS_TOKEN",
    "PREFECT_API_KEY",
    "PREFECT_API_URL",
    "IBKR_HOST",
    "IBKR_PORT",
    "IBKR_CLIENT_ID",
    "LOG_LEVEL",
    "TIMEZONE",
]


# ---------------------------------------------------------------------------
# Reporter
# ---------------------------------------------------------------------------
class Reporter:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.warnings: list[str] = []

    def report(self, status: str, label: str, detail: str = "") -> None:
        line = f"  [{status}] {label}"
        if detail:
            line += f" — {detail}"
        print(line)
        if status == FAIL:
            self.failures.append(label)
        elif status == WARN:
            self.warnings.append(label)

    def section(self, title: str) -> None:
        print(f"\n== {title} ==")


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------
def check_imports(r: Reporter) -> None:
    r.section("Module imports")
    for name in REQUIRED_MODULES:
        try:
            importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001
            r.report(FAIL, name, str(exc))
        else:
            r.report(PASS, name)


def check_env_vars(r: Reporter) -> None:
    r.section("Environment variables")
    from config.settings import settings

    for name in REQUIRED_ENV_VARS:
        value = getattr(settings, name, None)
        if value in (None, "", 0):
            r.report(FAIL, name, "missing or empty")
        else:
            r.report(PASS, name)

    for name in OPTIONAL_ENV_VARS:
        value = getattr(settings, name, None)
        if value in (None, "", 0):
            r.report(WARN, name, "optional, not set")
        else:
            r.report(PASS, name)


async def _check_postgres() -> tuple[bool, str]:
    from sqlalchemy import text

    from data.db import engine

    try:
        async with engine.connect() as conn:
            result = await conn.execute(text("SELECT 1"))
            value = result.scalar_one()
            return True, f"SELECT 1 = {value}"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


async def _check_redis() -> tuple[bool, str]:
    from redis.asyncio import Redis

    from config.settings import settings

    if not settings.REDIS_URL:
        return False, "REDIS_URL not set"
    client = Redis.from_url(settings.REDIS_URL)
    try:
        pong = await client.ping()
        return bool(pong), "PONG" if pong else "no PONG"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)
    finally:
        try:
            await client.aclose()
        except Exception:  # noqa: BLE001
            pass


async def _run_async_check(
    name: str, fn: Callable[[], Awaitable[tuple[bool, str]]], r: Reporter, strict: bool
) -> None:
    ok, detail = await fn()
    if ok:
        r.report(PASS, name, detail)
    else:
        r.report(FAIL if strict else WARN, name, detail)


async def check_connections(r: Reporter, strict: bool) -> None:
    r.section("External services")
    await _run_async_check("PostgreSQL", _check_postgres, r, strict)
    await _run_async_check("Redis", _check_redis, r, strict)


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="Verify environment setup")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Treat connection failures as critical (default: warn-only).",
    )
    args = parser.parse_args()

    # Quiet down loguru — the reporter handles output
    logger.remove()
    logger.add(sys.stderr, level="WARNING")

    r = Reporter()
    check_imports(r)
    check_env_vars(r)
    asyncio.run(check_connections(r, strict=args.strict))

    print("\n== Summary ==")
    print(f"  failures: {len(r.failures)}")
    print(f"  warnings: {len(r.warnings)}")
    return 1 if r.failures else 0


if __name__ == "__main__":
    sys.exit(main())
