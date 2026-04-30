# Trading Intelligence System — Claude Code Guide

## Current State

**Infrastructure** (live on DigitalOcean NYC `159.203.190.10`):
- `trading-app.service` — FastAPI on `127.0.0.1:8000`
- `edgar-watcher.service` — 5,000 tickers, 8 form types
- `celery-worker.service` — concurrency=2
- Redis (Celery broker)
- PostgreSQL 18 + TimescaleDB — 13 tables
- App path: `/app/profitlyworkflow`

**Phase 1 status**

Complete:
- EDGAR pipeline, 8-K extractor, `filing_parser`
- Celery worker, S-3 effective detection
- Form 4 insider parsing, underwriters table
- 5,000 ticker universe (OTC + Nasdaq CM)
- 110+ filings processed

Next:
- Telegram pump-group monitoring (Telethon, primary social signal)
- Reddit (PRAW) async sentiment batch
- SEC enforcement case mining (promoter DB)
- FinBERT + Claude Haiku hybrid sentiment classifier implementations
- Daily brief generators

StockTwits is **deferred** (their public API is closed to new
registrations as of 2026); revisit only after Telegram + Reddit prove
insufficient.

**Key decisions**
- OTC edge → social APIs + promoter network
- Nasdaq CM edge → EDGAR filing patterns
- Float filter <10M unifies both
- Massive/Polygon = Starter (upgrade before Phase 2)
- `BROKER_MODE=paper` (Alpaca)

**End of day** — start the float updater before closing the laptop:

```bash
tmux new-session -s floats2
sudo -u trading bash -c '
    set -a
    source /app/profitlyworkflow/.env.production
    set +a
    cd /app/profitlyworkflow
    ./venv/bin/python scripts/update_floats.py
'
# detach: Ctrl+B then D    (~13 hours on Starter)
```

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
- **Every signal evaluation MUST write a `predictions` row before any alert fires.** Use `SignalEngine.evaluate()` — never call the scorer directly from alert code paths. The prediction row is written whether or not an alert fires; that's the point.
- **Predictions are immutable once written.** To "correct" a prediction, write a new prediction with a reference to the prior in `feature_vector.supersedes`. The only writable columns post-creation are `user_decision`, `decision_reason`, `trade_id`, and `outcome_id`.
- **`FEATURE_SCHEMA_VERSION` (in `config/constants.py`) MUST be bumped whenever the feature vector definition changes.** Old predictions remain valid under their original schema; new predictions use the new version. Cross-version comparisons during scorer graduation respect this pinning.
- **Probability-shaped scores only — `[0.0, 1.0]` floats.** This is a *format* invariant: every scorer's `ScoreResult.probability` is a float in `[0.0, 1.0]`. Do not introduce 0–100 scoring anywhere in the internal pipeline. Convert to integer percentages only in alert formatters at presentation time. *Calibration* (the empirical property that confidence 0.7 means a ~70% realized hit rate) is a separate concern and a graduation milestone, **not** an invariant of every scorer. Rules-v1 is uncalibrated by design — its `ScoreResult.uncalibrated_warning` is `True`. Future scorers flip that flag to `False` only after passing Brier / ECE validation. Alert formatters may use the flag to suppress "78% confidence" framing on uncalibrated scores. (The legacy `signals.confidence_score` / `CONFIDENCE_THRESHOLD_S1=65` constants are part of the existing gatekeeper layer and predate the predictions-table loop; do not migrate them in this PR.)
- **"Outcome resolution" is the term for closing matured predictions.** Do not use "attribution" — that term is reserved for SHAP-style feature attribution which the eventual GBDT will produce separately.

## Key Files to Understand First

- `config/constants.py` — every threshold, percentage, time window, and parameter (incl. `FEATURE_SCHEMA_VERSION`, `OUTCOME_LABEL_RULES`)
- `config/settings.py` — environment configuration, the `is_live_trading` safety gate, loguru setup
- `risk/gatekeeper.py` — every order passes through `check_all()`; rule names live in `Rules`
- `execution/broker/base.py` — the `BrokerClient` abstract interface contract
- `data/schema/schema.sql` — source of truth for the data model (TimescaleDB hypertable on `price_data`)
- `signals/scoring/catalyst_scorer.py` — `CatalystScorer` abstract + `RulesV1Scorer` (Phase-1 in-production scorer)
- `signals/engine.py` — `SignalEngine.evaluate()`; the chokepoint that writes the prediction row before any alert
- `flows/outcome_resolution_flow.py` — outcome closer, hourly + 17:00 ET sweep; `PriceSource` Protocol injection point
- `data/repositories/predictions_repo.py` — prediction CRUD + `get_unresolved_matured()` query the resolver walks

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

See **Current State** at the top for the live snapshot. Phase 0 (foundation) is complete; Phase 1 (intelligence building — promoter database, signal engine, catalyst scorer, briefs) is in progress; Phase 2 (live execution validation at reduced size via Alpaca) is pending the data-tier upgrade; Phase 3 (full two-strategy system on IBKR) is pending Phase 2 results. See the phased checklist in `README.md` for the per-phase exit criteria.

## Data Tier Upgrade Before Phase 2

Data tier upgrade required before Phase 2: [Polygon.io](http://Polygon.io) real-time ($199/mo) + Benzinga News ($99/mo). Current Starter plan ($29/mo) is 15-min delayed — unusable for live Strategy 1 signal execution. Note: [Polygon.io](http://Polygon.io) rebranded to [Massive.com](http://Massive.com) in October 2025; the API, polygon-api-client package, and POLYGON_API_KEY env var are unchanged. See "Data Stack Upgrades by Phase" in README.md for the full checklist.
