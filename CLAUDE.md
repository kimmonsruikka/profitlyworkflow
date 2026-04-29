# Trading Intelligence System — Claude Code Guide

## Architecture Overview

This repository is a signal intelligence platform for catalyst-driven momentum trading on low-float equities. It runs **two strategies concurrently on shared infrastructure**: Strategy 1 reacts to confirmed catalysts intraday on a 15-minute to 1-day hold; Strategy 2 positions ahead of catalysts by reading the promotion infrastructure (IR firms, attorneys, transfer agents named in SEC filings) days before retail sees the move. The system finds and scores opportunities, sends a structured alert to Telegram with confidence / liquidity / promoter-network context, and the operator decides EXECUTE or PASS. Either way the outcome is captured (live trade, paper trade, or expired alert) and fed back into the model.

The codebase is organized in thin layers: ingestion (EDGAR, Polygon, Benzinga, social) → intelligence (promoter graph, catalyst classifier, liquidity scorer, regime detector) → signals (S1 evaluator, S2 category detectors, unified confidence scorer) → risk gatekeeper → execution (broker abstraction, order manager, paper engine) → alerts (Telegram bot, briefs) → feedback (trade logger, learning loop, reports). All project files live at the repo root; CI/CD workflows are in `.github/workflows/`.

## Critical Rules for Code Changes

- **`BROKER_MODE` and `is_live_trading` must be checked before any order submission.** `Settings.is_live_trading` only returns `True` when `BROKER_MODE=live` AND `ENVIRONMENT=production` — both gates required.
- **The risk gatekeeper must be called before every order — no exceptions.** Use `RiskGatekeeper.check_all(signal, account_balance)` and respect `GatekeeperResult.approved`.
- **All order submissions go through `OrderManager`, never directly to broker clients.** The manager wires the gatekeeper, builds the staged order, and submits via the injected broker (real or paper).
- **Paper trade outcomes must be tagged with one of:** `USER_DECLINED`, `SIGNAL_EXPIRED`, or `SYSTEM_PAPER`. Use `PaperTradeEngine.record_outcome()`.
- **Never commit `.env` files.** Only `.env.example` is tracked. The `.gitignore` excludes `.env` and `.env.*` (but allows `.env.example`).
- **All magic numbers belong in `config/constants.py`.** No inline thresholds, percentages, or time windows anywhere else.

## Key Files to Understand First

- `config/constants.py` — every threshold, percentage, time window, and parameter
- `config/settings.py` — environment configuration, the `is_live_trading` safety gate, loguru setup
- `risk/gatekeeper.py` — every order passes through `check_all()`; rule names live in `Rules`
- `execution/broker/base.py` — the `BrokerClient` abstract interface contract
- `data/schema/schema.sql` — source of truth for the data model (TimescaleDB hypertable on `price_data`)

## Database

- SQLAlchemy 2.0 async sessions throughout (`AsyncSession` + `async_sessionmaker`)
- All queries go through repository classes in `data/repositories/`
- Repositories return Pydantic schemas (`data/repositories/schemas.py`), never raw ORM instances
- Never write raw SQL outside of `schema.sql` and Alembic migrations
- TimescaleDB hypertable on `price_data` — use `time_bucket` for aggregations, query by `(ticker, timestamp)` range

## Testing

- Unit tests mock all external APIs (alpaca SDK, Redis, Postgres) — see `tests/unit/test_broker.py` for the `sys.modules`-stub pattern and `tests/unit/test_risk_gatekeeper.py` for the `FakeRedis`/mock-session pattern
- Tests must pass with `BROKER_MODE=paper` and empty API keys (this is what CI runs)
- Run from the project directory: `PYTHONPATH=. pytest tests/unit/ -v`
- Verify the full environment with: `python scripts/verify_setup.py` (add `--strict` to make connection failures critical)

## Current Phase

**Phase 0 — Foundation build.** No live trading. No real API connections required yet. Focus is on infrastructure soundness: schema, repositories, gatekeeper, broker abstraction, paper engine, CI/CD, health endpoint. Phase 1 begins the promoter knowledge base build and the signal engine; Phase 2 adds reduced-size live trading via Alpaca; Phase 3 expands to both strategies on IBKR. See the phased checklist in `README.md`.
