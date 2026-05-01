# Trading Intelligence System

A signal intelligence platform for catalyst-driven momentum trading on low-float equities. The system monitors the promotion infrastructure, detects pre-catalyst setups, scores signals with confidence intervals, and delivers actionable trade alerts with suggested entry, sizing, and exit parameters. You decide to execute or pass. The system trades either way — live or paper — and learns from both.

---

## Core Philosophy

This is not a fully automated trading bot. It is a **signal intelligence platform with human-confirmed execution**.

- The system finds and scores the opportunity
- You receive a structured alert with full context
- You execute or pass
- Either way the outcome is captured and fed back into the model
- The promoter knowledge base compounds over time as a proprietary edge

Two strategies run concurrently on the same intelligence infrastructure:

**Strategy 1 — Catalyst Momentum:** Reacts to confirmed catalysts on low-float equities. Hold time 15 minutes to 1 day. Edge is speed of detection and promoter pattern recognition.

**Strategy 2 — Pre-Catalyst Positioning:** Positions ahead of catalysts by reading the promotion infrastructure before it launches. Hold time 1 day to 2 weeks. Edge is seeing the setup before retail does.

---

## Why Low Float

Float under 10 million shares. This is where asymmetric returns live on catalyst plays. A promotion campaign on a 3 million share float moves price violently because there are not enough shares to absorb demand. The same catalyst on a 50 million float barely registers.

The tradeoff: liquidity is thin, spreads are wide, halts are frequent, and dilution can destroy a position overnight. The system is designed specifically around these characteristics — not despite them.

---

## The Promoter Infrastructure Edge

Most low-float catalyst moves are driven by paid promotion campaigns. The cycle is predictable and publicly visible if you know where to look:

1. Company registers shares via S-1 or S-3 (SEC filing, public)
2. Company hires IR firm (disclosed in 8-K filing, public)
3. Promoter launches campaign — newsletters, social, press (disclosed compensation, public)
4. Retail FOMO drives price up on thin float
5. Insiders and promoters sell into strength
6. Price collapses

The system reads steps 1 and 2 before step 3 happens. That is the edge.

The promoter knowledge base maps every IR firm, attorney, transfer agent, and auditor that repeatedly appears in low-float promotion campaigns — sourced from SEC enforcement cases, EDGAR filings, and historical campaign data. When a new filing contains a known entity from the network, confidence on the incoming setup increases before any price movement has occurred.

---

## System Architecture

```
DATA LAYER
├── EDGAR RSS Watcher          Real-time 8-K / S-1 / S-3 / Form 4 / DEF 14A
├── [SEC-API.io](http://SEC-API.io) Parser          Structured extraction from raw filings
├── [Polygon.io](http://Polygon.io) Starter         Historical OHLCV, cached in TimescaleDB hypertable
├── Benzinga Pro API           Real-time news and catalyst feed
├── Ortex                      Short interest, days to cover, borrow rate
├── Telegram (Telethon)        Primary social signal — pump-group monitoring
├── Reddit (PRAW)              Async sentiment batch — penny / WSB velocity
└── FinBERT (local) +          Hybrid sentiment classification —
    Claude Haiku API           cheap first-pass + rich second-pass

INTELLIGENCE LAYER
├── Promoter Knowledge Base    Entity network graph, campaign history,
│                              reliability scores, liquidity fingerprints
├── Catalyst Classifier        Type detection, scoring, decay modeling
├── Liquidity Scorer           Real-time spread + depth + market maker analysis
└── Market Regime Detector     HOT / NEUTRAL / COLD environment classification

LEARNING ARCHITECTURE
├── predictions table          One row per signal evaluation, immutable,
│                              calibrated probability + feature_vector
├── outcomes table             Closed-out maturities — MFE / MAE / realized
│                              return / WIN/LOSS/NEUTRAL/INVALID label
├── model_versions table       Catalog of every scorer; only one in_production
│                              at a time, others run in_shadow
└── Outcome Resolution Flow    Hourly + 17:00 ET sweep — closes matured
                               predictions and writes outcome rows

SIGNAL ENGINE
├── Strategy 1 Evaluator       Entry conditions, exit parameters, position sizing
├── Strategy 2 Evaluator       Category A/B/C/D setup detection and scoring
├── Risk Gatekeeper            Pre-order validation, PDT tracking, exposure limits
└── Catalyst Scorer            Phase 1: rules-v1 hand-coded; Phase 2+: GBDT
                               (XGBoost / LightGBM) shadow → production with
                               SHAP feature attributions on every alert

DECISION INTERFACE
├── Pre-Market Brief           6:15am daily — watchlist, overnight positions,
│                              regime, EDGAR overnight activity
├── Live Signal Alert          10-second decision format — confidence, entry,
│                              stop, targets, size, promoter context, countdown
├── Overnight Carry Decision   In after-market brief — hold or close analysis
└── After-Market Debrief       4:30pm daily — outcomes, paper trade results,
                               tomorrow seeds, account status

EXECUTION ROUTER
├── EXECUTE                    Live order via broker API (Alpaca → IBKR Phase 3)
├── PASS                       Paper trade logged as USER_DECLINED + reason capture
├── EXPIRE                     Paper trade logged as SIGNAL_EXPIRED
└── SYSTEM_PAPER               Phase 1 paper mode

FEEDBACK ENGINE
├── Trade Logger               Full trade record — R achieved, MAE, MFE, slippage
├── Paper Trade Logger         Three-bucket tracking with separate analytics
├── Decline Reason Capture     Why you passed — calibrates instinct over time
├── Promoter Score Updater     Reliability scores update after each campaign closes
├── Learning Loop              Signal templates update with exponential weighting
└── Report Generator           Daily, weekly, and promoter-specific reports
```

---

## Learning Architecture

The system is probability-shaped end to end. Every signal evaluation runs a **catalyst scorer** that maps a feature dict to a float in `[0.0, 1.0]`, and writes a row to `predictions` *before any alert fires*. Conversion to "82%" only happens at presentation time in the alert formatter — internal code never traffics in 0–100 integers.

The `[0.0, 1.0]` range is the *format contract* every scorer promises. **Calibration** — the empirical property that confidence 0.7 means a ~70% realized hit rate — is a *graduation milestone*, not an invariant of every scorer. Rules-v1 is uncalibrated by design; its `ScoreResult.uncalibrated_warning` is `True`. Future scorers flip the flag to `False` only after Brier / ECE validation.

Three tables anchor the loop:

- **`predictions`** — immutable. Every evaluation, whether an alert fires or not. Captures `feature_vector`, `feature_schema_version`, `scorer_version`, `confidence`, the predicted window, and (later) the operator's EXECUTE / PASS / EXPIRED decision. To "correct" a prediction, write a new one with `feature_vector.supersedes` referencing the prior — never mutate.
- **`outcomes`** — one per resolved prediction (UNIQUE constraint). MFE, MAE, realized return (and counterfactual paper return), `hit_target` / `hit_stop` flags, and a `WIN` / `LOSS` / `NEUTRAL` / `INVALID` label derived from `OUTCOME_LABEL_RULES` in `config/constants.py`.
- **`model_versions`** — catalog of every scorer that has ever written predictions. `in_production = TRUE` for the writer of new predictions; `in_shadow = TRUE` for candidate models running alongside but not driving alerts.

The **outcome resolution flow** runs hourly during market hours plus a 17:00 ET sweep. It walks `predictions WHERE outcome_id IS NULL AND created_at + window <= now()`, pulls OHLCV for each window, computes the metrics, and writes the outcome row. Price data is fetched through a Polygon-backed `PriceSource` with TimescaleDB caching — the cache is read first, gaps are filled from Polygon, and Polygon responses are written back so the same window costs one call ever. Granularity is chosen per prediction window length: **1-minute bars for windows ≤ 1 day, 5-minute bars for longer**.

Predictions on tickers without Polygon coverage are resolved as **`INVALID`** with a structured `invalid_reason` (`no_price_data`, `insufficient_bars`, `polygon_error`) — they're not silently dropped. Transient network errors leave the prediction unresolved so the next sweep retries it.

**EDGAR filings flow through `SignalEngine.evaluate_edgar_filing()`** in the Celery worker. Prediction-worthy filings — material 8-K items (1.01, 2.01, 2.02, 5.02, 5.03, 8.01, 3.02), S-3 marked effective, Form 4 buys ≥ $50K, or any filing on a ticker that matches the promoter network — fire a prediction via rules-v1 and write a row to `predictions`. Non-worthy filings are still parsed and stored on `sec_filings` but skip the prediction path; the skip reason is logged so operators can audit the filter without scanning every filing.

**All Phase-1 predictions are uncalibrated** (`ScoreResult.uncalibrated_warning=True`) until the rules-v1 → GBDT graduation. Confidence values should not be presented to users as probabilities yet — alert formatters that display percentages on uncalibrated scores have to mark them as such.

**Scorer graduation path:**

1. **Phase 1 — rules-v1.** Hand-coded threshold sums, placeholder weights. Already in production. `model_versions.version_id = 'rules-v1'` is seeded by migration 0006.
2. **Graduation trigger — 500–1000 labeled prediction-outcome pairs.** When the resolved-outcome count crosses this band and the WIN/LOSS distribution is non-degenerate, training a GBDT (XGBoost or LightGBM) becomes worthwhile. Earlier than that and you're fitting noise.
3. **Phase 2+ — GBDT in shadow mode.** New scorer logs predictions side-by-side with rules-v1 for at least 90 days. Promoted when AUC, Brier, and ECE materially better than rules-v1 *and* well-calibrated (not just sharper).
4. **Post-promotion — SHAP feature attributions.** Once GBDT is in production every alert ships the top-3 SHAP attributions inline so the operator sees *why* the score is what it is. (Distinct from "outcome resolution" — SHAP is per-prediction explainability; resolution is closing matured predictions with their actual outcome.)

`feature_schema_version` (in `config/constants.py` as `FEATURE_SCHEMA_VERSION`) gets bumped whenever the feature vector definition changes. Old predictions stay valid under their original schema; new predictions use the new version. The scorer comparison work that drives graduation respects this version pinning.

---

## Technology Stack

| Layer | Technology | Purpose |
|---|---|---|
| Language | Python 3.12+ | Core runtime |
| Orchestration | Prefect Cloud | Workflow scheduling and monitoring |
| Task Queue | Celery + Redis | Async parallel processing |
| Database | PostgreSQL + TimescaleDB | Persistent storage + time-series |
| State | Redis | Real-time PDT counter, P&L, positions |
| Alerts | python-telegram-bot | Signal delivery + EXECUTE/PASS buttons |
| Broker (Phase 1-2) | Alpaca | Paper + live trading API |
| Broker (Phase 3+) | IBKR Pro | Direct market maker routing |
| Price Data | [Polygon.io](http://Polygon.io) | Real-time + historical OHLCV |
| News | Benzinga Pro API | Catalyst feed |
| Short Interest | Ortex | SI%, days to cover, borrow rate |
| SEC Filings | EDGAR RSS + [SEC-API.io](http://SEC-API.io) | Filing detection + parsing |
| Social | Telegram (primary) / Reddit / FinBERT + Claude Haiku | Velocity + sentiment |
| CI/CD | GitHub Actions | Test on push, deploy on merge to main |
| Hosting | DigitalOcean NYC | Application droplet |
| Database Host | DigitalOcean Managed | PostgreSQL with auto-backup |
| API Bridge | FastAPI | REST bridge to COS / TypeScript systems |
| Monitoring | Uptime Robot | Process and uptime alerting |

---

## Risk Architecture

**Account:** $35,000  
**PDT Status:** Cleared (above $25,000 threshold)

| Parameter | Value |
|---|---|
| Strategy 1 risk per trade | 1% ($350) |
| Strategy 2 risk per position | 1–1.5% ($350–525) |
| Combined daily max loss | 2% ($700) |
| Max concurrent S1 positions | 1 |
| Max concurrent S2 positions | 4 |
| Max single S2 position size | 15% of portfolio |
| Max total swing exposure | 40% of portfolio |
| Minimum cash buffer | 45% always |
| Minimum signal R/R | 3:1 |
| Minimum confidence threshold | 65% (S1) / 70% (S2) |
| Liquidity score floor | 40/100 (below = paper only) |

**Hard rules enforced by system:**
- Daily loss limit hit → new entries blocked, gate resets at market open
- S3 shelf detected on held position → immediate exit, no override
- 3 consecutive losses → position size reduced 50% for next 5 trades
- Combined exposure >55% → manual override required for new entry
- S1 confidence threshold raises to 75% when 3+ S2 positions open

---

## Alert Format Reference

### Live Signal Alert (Strategy 1)
```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🔔 LIVE SIGNAL — $TICKER
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Confidence:  82%  |  Liquidity: 74/100 (GOOD)
Spread:      0.7% ↓ tightening
Promoter:    [Name] — 5 campaigns, 78% reliability
             avg Day 1: +38%, window: 9:40–10:50am

CATALYST: [One sentence. What happened and when.]

Float: 4.1M  |  Short: 22%  |  Turnover: 0.4x

ENTRY:    $4.14 – $4.28 limit
STOP:     $3.95  |  RISK: $350 → 1,820 shares
TARGET 1: $4.83 (+2R) → sell 50%
TARGET 2: $5.52 (+3R) → trail remainder

⏱ Entry valid: 11:40 remaining

[ EXECUTE ]              [ PASS ]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

### Strategy 2 Signal Alert
```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📡 STRATEGY 2 SIGNAL — $TICKER
Category: Pre-Promotion (Category A)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Confidence:  74%  |  Timeline: 4–12 days
Network:     IR firm + attorney + transfer agent
             fingerprint matches 3 prior campaigns

THESIS: [Two sentences. Setup and expected catalyst.]

ENTRY:    $0.96–$1.02 limit (place before open)
STOP:     $0.81 (52-week low — thesis broken)
TARGET 1: $1.60 (campaign launch → sell 40%)
TARGET 2: $2.40 (campaign peak → sell remainder)
TIME STOP: 14 days from entry

[ EXECUTE — PLACE LIMIT ]     [ PASS ]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

---

## Strategy 2 Setup Categories

| Category | Setup Type | Expected Hold | Target Return |
|---|---|---|---|
| A | Pre-Promotion Positioning | 3–14 days | 50–200% |
| B | Short Squeeze Buildup | 5–21 days | 100–400%+ |
| C | Sector Sympathy | 1–5 days | 30–80% |
| D | Scheduled Catalyst Pre-Position | Until event | 40–150% |

---

## Monthly Operating Costs

| Item | Cost |
|---|---|
| DigitalOcean Droplet (4GB/2vCPU NYC) | $24 |
| DigitalOcean Managed PostgreSQL | $15 |
| [Polygon.io](http://Polygon.io) Starter | $29 |
| Benzinga Pro API | $150 |
| Ortex | $100 |
| [SEC-API.io](http://SEC-API.io) | $50 |
| Anthropic API (Claude Haiku, sentiment) | ~$30 (estimate) |
| Prefect Cloud | $0 |
| Alpaca | $0 |
| Uptime Robot | $0 |
| **Total Phase 1–2** | **~$398/month** |

Notes:
- **X (Twitter) API:** removed. X moved to pay-per-use in Feb 2026; pricing reassessed when usage volume is known. Phase 1 social ingestion is Telegram + Reddit (both free).
- **StockTwits:** removed. Public API closed to new registrations as of 2026. See "Social Ingestion" in Phase 1 for the deferral reasoning.
- **Anthropic API estimate** is pending velocity-filter precision — Claude Haiku is the second-pass classifier on posts FinBERT flags as bullish or ambiguous. Cost moves with how aggressive the filter is.

Break-even on costs: ~14% annual return on $35,000 account. One good Strategy 2 pre-promotion position covers approximately 3 months of data costs.

---

## Data Stack Upgrades by Phase

Note: [Polygon.io](http://Polygon.io) rebranded to [Massive.com](http://Massive.com) in October 2025. The API, Python client (polygon-api-client), and POLYGON_API_KEY env var are unchanged.

### Current (Phase 1 — Building)
| Item | Cost |
|---|---|
| [Polygon.io](http://Polygon.io) (now [Massive.com](http://Massive.com)) Stocks Starter — 15-min delayed | $29 |
| Benzinga News | $0 (not yet active) |

### Required Before Phase 2 (Live Signal Firing)
Upgrade [Polygon.io](http://Polygon.io) to real-time tier and add Benzinga News BEFORE the first live Strategy 1 signal fires. 15-minute delayed data makes Strategy 1 unusable — entry timing depends on real-time VWAP and spread.

| Item | Current | Upgrade To | Cost |
|---|---|---|---|
| [Polygon.io](http://Polygon.io) data tier | Starter $29/mo (15-min delay) | Real-time $199/mo | +$170/mo |
| Benzinga News | Not active | Add-on via [Polygon.io](http://Polygon.io) | +$99/mo |
| **Phase 2 total data cost** | **$29** | **$298/mo** | |

Annual pricing available: [Polygon.io](http://Polygon.io) real-time at $160/month ($1,920/year) if paid annually. Only commit to annual after the system is proven profitable — not before.

### Upgrade Checklist
- [ ] Upgrade [Polygon.io](http://Polygon.io) to real-time tier before Phase 2
- [ ] Add Benzinga News add-on via [Polygon.io](http://Polygon.io) dashboard
- [ ] Update POLYGON_API_KEY in .env.production if key changes on plan upgrade
- [ ] Add BENZINGA_API_KEY to .env.production
- [ ] Add BENZINGA_API_KEY to GitHub repository secrets

---

## Phase Rollout

### Phase 0 — Foundation *(Weeks 1–2)*
> **Question to answer:** Is the infrastructure solid before any real data flows through it?
>
> **Status:** infrastructure scaffold complete. Repo lives at `/app/profitlyworkflow` on the droplet (root-flattened, not `/app/trading-intelligence-system`). FastAPI health endpoint live behind systemd as the `trading` user, bound to `127.0.0.1:8000`. Postgres + TimescaleDB schema migrated. Redis up. Pending in this phase: Prefect Cloud + Celery wiring, EDGAR/Polygon/Benzinga pipelines, Telegram bot, and the operational items (Uptime Robot, backup-restore drill).

#### Infrastructure Setup
- [x] Provision DigitalOcean Droplet (NYC, 4GB/2vCPU)
- [x] Provision DigitalOcean Managed PostgreSQL
- [x] Install and configure TimescaleDB extension
- [x] Install and configure Redis
- [x] Configure SSH keys and firewall rules
- [x] Install Python 3.12, virtual environment
- [ ] Configure Uptime Robot monitoring (alert if unreachable >5 mins)
- [ ] Set up automated daily PostgreSQL backup
- [ ] Verify backup restore works

#### Prefect + Celery
- [ ] Create Prefect Cloud account (free tier)
- [ ] Install Prefect agent on Droplet
- [ ] Configure Celery with Redis as broker
- [ ] Test flow: "hello world" Prefect flow runs on schedule and logs in dashboard
- [ ] Test task: Celery worker processes async task, result stored in Redis

#### Repository Scaffold
- [x] Create GitHub repository (private)
- [x] Build full directory structure per spec
- [x] Create `.env.example` with all required environment variables
- [x] Create `requirements.txt` with all initial packages
- [x] Create `docker-compose.yml` for local PostgreSQL + Redis
- [x] Configure `.gitignore` (Python, .env, __pycache__, secrets)
- [x] Set up GitHub Actions: `test.yml` (run on push)
- [x] Set up GitHub Actions: `deploy.yml` (deploy to DO on merge to main)
- [x] Add DO_HOST, DO_USER, DO_SSH_KEY to GitHub Secrets

#### Database Schema
- [x] Write complete `schema.sql` with all tables and indexes
- [x] Create SQLAlchemy models for all tables
- [x] Configure Alembic for migrations
- [x] Run initial migration against Managed PostgreSQL
- [x] Create TimescaleDB hypertable for `price_data`
- [x] Write `[constants.py](http://constants.py)` with all thresholds and parameters (no magic numbers)
- [x] Write `[settings.py](http://settings.py)` loading all config from environment variables

#### Data Pipeline Foundations
- [ ] EDGAR RSS polling (5-minute interval, weekdays 4am–8pm ET)
- [ ] Build initial CIK universe (float <15M, ~800–1200 companies)
- [ ] [SEC-API.io](http://SEC-API.io) integration for 8-K parsing
- [ ] 8-K item extraction (Item 8.01 priority)
- [ ] IR firm name extraction from filing text
- [ ] [Polygon.io](http://Polygon.io) WebSocket connection (real-time trades + quotes)
- [ ] [Polygon.io](http://Polygon.io) historical OHLCV pull for ticker universe
- [ ] Benzinga Pro API news feed connection
- [ ] Test: pipe 10 historical 8-Ks through parser, verify accuracy
- [ ] Test: verify price data flowing into TimescaleDB hypertable
- [ ] Test: verify data continuity, check for feed gaps

#### Telegram Bot
- [ ] Create bot via BotFather, save token
- [ ] Configure python-telegram-bot
- [ ] EXECUTE / PASS inline buttons working
- [ ] Decline reason prompt after PASS
- [ ] Message editing for live countdown timer
- [ ] Test: send mock alert, press EXECUTE, verify callback received
- [ ] Test: send mock alert, press PASS, verify reason capture works
- [ ] Test: let alert expire, verify SIGNAL_EXPIRED routes to paper trade

#### Phase 0 Verification Gate
- [x] All tables created and indexed in production database
- [ ] EDGAR pipeline running and storing data
- [ ] Polygon feed connected and logging to TimescaleDB
- [ ] Benzinga feed connected and logging
- [ ] Telegram bot sends and receives in production
- [ ] Prefect dashboard shows all flows with correct schedule
- [ ] Redis state reads and writes verified
- [ ] Uptime Robot alert tested (manually stopped droplet, alert fired)
- [ ] Daily backup confirmed and restore tested
- [x] All GitHub Actions passing on push

---

### Phase 1 — Intelligence Building *(Weeks 3–8)*
> **Question to answer:** Is the promoter knowledge base and signal scoring producing valid, actionable signals?
> **Trading:** Paper only. No live orders.

#### Promoter Database — Historical Build (Weeks 3–4)
- [ ] Pull all SEC enforcement actions tagged "penny stock" / "pump and dump" 2015–present
- [ ] Extract and log all named entities per case (promoter, IR firm, attorney, transfer agent, auditor)
- [ ] Extract compensation amounts and structures per case
- [ ] Extract tickers, campaign dates, and network relationships per case
- [ ] Flag current status of each entity (active / dissolved / barred)
- [ ] Pull [StockPromoters.com](http://StockPromoters.com) historical campaigns 2019–present
- [ ] Cross-reference campaigns against [Polygon.io](http://Polygon.io) historical price data
- [ ] Calculate per campaign: day1_move, peak_move, days_to_peak, decay_speed
- [ ] Link StockPromoters campaigns to entity database records
- [ ] Run EDGAR full-text search: "investor relations" + "compensation" in 8-Ks 2022–present
- [ ] Filter to float <15M, extract IR firm names and compensation
- [ ] Match against StockPromoters campaigns for validation
- [ ] Build `promoter_network_edges` from all co-appearances
- [ ] Identify clusters (recurring entity combinations)
- [ ] Score clusters by co-appearance count, avg campaign performance, enforcement history
- [ ] Output: top 20 active clusters ranked by reliability and recency

#### Social Ingestion *(Weeks 4–5, ordered by priority)*

Reordered for Phase 1: Telegram first, then Reddit, then revisit StockTwits only if needed. Telegram is closer to where coordinated pumps actually originate, which directly serves the promoter-network thesis. StockTwits is deferred — see notes below.

1. [ ] **Telegram pump-group monitoring** (Telethon, free) — primary social signal
2. [ ] **Reddit** (PRAW, free) — `r/pennystocks`, `r/wallstreetbets` velocity, async batch
3. [ ] **FinBERT (local) + Claude Haiku API** hybrid sentiment classification — interface defined; concrete classifiers land alongside the ingestion PRs
4. [ ] **StockTwits** — *DEFERRED.* Their public API is closed to new registrations as of 2026. Revisit only after Telegram + Reddit prove insufficient. If revisited, scope to the trending-symbols endpoint via Apify scraper, not 5K-ticker polling.

#### Signal Engine Build (Weeks 5–6)
- [ ] Build catalyst type classifier (all types per spec)
- [x] Build catalyst scorer — `rules-v1` producing calibrated probabilities in `[0.0, 1.0]` (placeholder weights pending calibration)
- [ ] Phase-1 calibration: collect 500–1000 prediction-outcome pairs to calibrate `rules-v1` weights empirically
- [ ] Phase 2+ graduation: train GBDT (XGBoost / LightGBM) in shadow mode against `rules-v1`; promote when AUC / Brier / ECE materially better and well-calibrated
- [ ] Phase 2+ explainability: attach SHAP feature attributions to every alert once GBDT promoted
- [ ] Build real-time liquidity scorer (spread + depth + MM count + volume)
- [ ] Establish baseline spreads by time of day and day of week from 90-day history
- [ ] Calibrate liquidity score bins against historical fill quality
- [ ] Build market regime detector (HOT / NEUTRAL / COLD)
- [ ] Build Strategy 1 entry condition evaluator (all 7 conditions per spec)
- [ ] Build Strategy 1 position sizer (risk dollars / stop distance = shares)
- [ ] Build Strategy 1 exit parameter calculator (2R/3R targets + trailing logic)
- [ ] Build Strategy 2 Category A detector (pre-promotion signals)
- [ ] Build Strategy 2 Category B detector (squeeze buildup)
- [ ] Build Strategy 2 Category C detector (sector sympathy)
- [ ] Build Strategy 2 Category D detector (scheduled catalyst)
- [ ] Build unified confidence scorer
- [ ] Build risk gatekeeper (all 7 rules per spec)
- [ ] Build PDT tracker (rolling 5-day window)
- [ ] Backtest each S2 category against historical data, validate setup count and win rates

#### Pre-Market and After-Market Briefs (Week 5)
- [ ] Pre-market brief Prefect flow (6:15am weekdays)
- [ ] After-market debrief Prefect flow (4:30pm weekdays)
- [ ] Weekly review Prefect flow (Sundays)
- [ ] Pre-market brief format per spec (regime, account, EDGAR overnight, watchlist, liquidity)
- [ ] After-market debrief format per spec (results, paper outcomes, overnight seeds)
- [ ] Overnight carry section in both briefs
- [ ] Swing portfolio status section in both briefs

#### Paper Signal Generation (Weeks 7–8)
- [ ] Full signal pipeline running (EDGAR detected → score → evaluate → alert)
- [ ] Three-bucket paper trade logger (USER_DECLINED / SIGNAL_EXPIRED / SYSTEM_PAPER)
- [ ] Decline reason capture prompt working
- [ ] Expired alert routes to paper trade at last valid entry price (not current price)
- [ ] Learning loop Prefect flow (post-trade feedback, template updates)
- [ ] Promoter score updater (reliability scores update after campaign closes)
- [ ] Run system for 2 full weeks, respond EXECUTE or PASS on every signal
- [ ] Review briefs daily, log qualitative notes on accuracy

#### Phase 1 Exit Criteria (all required before advancing)
- [ ] Promoter database has >50 entities mapped with at least 3 campaigns each
- [ ] At least 15 active clusters identified in network graph
- [ ] Signal engine generating 3–8 signals per week (not zero, not twenty)
- [ ] Paper trade win rate >50% over minimum 30 signals evaluated
- [ ] Pre-market brief qualitatively useful (not noise)
- [ ] Zero infrastructure outages >15 minutes
- [ ] All Prefect flows running on schedule with <5% failure rate

---

### Phase 2 — Live Execution Validation *(Weeks 9–14)*
> **Question to answer:** Does the full execution loop work correctly and profitably at reduced size?
> **Trading:** Live via Alpaca, 0.5% risk ($175), Strategy 1 only, manual confirmation required

#### Pre-Phase 2 Setup
- [ ] Alpaca live account funded ($35,000)
- [ ] Alpaca paper account kept active (parallel paper trades continue)
- [ ] Broker abstraction layer complete ([base.py](http://base.py) + [alpaca.py](http://alpaca.py))
- [ ] Test: bracket order stages correctly in paper mode
- [ ] Test: EXECUTE callback submits order correctly
- [ ] Test: stop and target levels set at correct prices
- [ ] Position sizing formula verified (0.5% mode)
- [ ] Daily loss limit hard gate tested (set to $350, verify new signals blocked)
- [ ] Gate reset verified (resets at market open following day)
- [ ] BROKER_MODE environment variable gate tested

#### Weeks 9–10: First 10 Live Trades
- [ ] 10 live trades completed
- [ ] Order submitted correctly on every trade (verified in broker dashboard)
- [ ] Fill price within acceptable range on every trade
- [ ] Stop order placed at correct level on every trade
- [ ] Target order placed at correct level on every trade
- [ ] Slippage within acceptable range (<0.5% of entry)
- [ ] Position size matched formula output on every trade
- [ ] PDT counter updated correctly after every round-trip
- [ ] Daily P&L updated correctly after every trade
- [ ] No red flags triggered (wrong price, failed stop, size error, PDT error)

#### Weeks 11–12: Execution Confidence (Cumulative 20 Trades)
- [ ] Average R achieved tracked vs targeted
- [ ] Average slippage at entry and exit tracked
- [ ] Average time from alert to response tracked
- [ ] Average time from EXECUTE to fill confirmation tracked
- [ ] Liquidity score accuracy validated (score 70+ correlates to clean fills)

#### Weeks 13–14: Full Rate Operation (Cumulative 30 Trades)
- [ ] Full signal cadence running
- [ ] Overnight carry logic active (S1 single stock only)
- [ ] Both S1 alert types active

#### Phase 2 Exit Criteria (all required before advancing)
- [ ] 30 live trades completed
- [ ] Win rate >50% (15+ winners)
- [ ] Expectancy >0.3R per trade
- [ ] No execution errors in last 10 trades
- [ ] Average slippage <0.5% of entry price
- [ ] Daily loss limit triggered correctly at least once
- [ ] Zero days where loss exceeded 2% of portfolio
- [ ] Prefect flows 99%+ uptime over 6 weeks
- [ ] Every loss has a specific identified reason (not "random market")

---

### Phase 3 — Full System Live *(Weeks 15–26)*
> **Question to answer:** Does the complete two-strategy system produce consistent positive expectancy at full size?
> **Trading:** Live via IBKR, 1% S1 risk, 1–1.5% S2 risk, both strategies active

#### Pre-Phase 3 Setup
- [ ] IBKR Pro account opened and funded
- [ ] IBKR broker implementation built ([ibkr.py](http://ibkr.py))
- [ ] Direct routing configured for low-float names
- [ ] Parallel fill comparison: 5 trades compared Alpaca vs IBKR before full migration
- [ ] Risk parameters updated to Phase 3 levels in [constants.py](http://constants.py)
- [ ] S2 paper trade results from Phase 1–2 reviewed — best category identified
- [ ] S2 live trading begins with best-performing category only

#### S2 Concurrent Position Ramp
- [ ] Week 15–16: max 1 S2 concurrent position
- [ ] Week 17–18: Category A live, results reviewed
- [ ] Week 19–20: max 2 S2 concurrent, Category B added
- [ ] Week 21–22: max 3 S2 concurrent, Category C added
- [ ] Week 23+: max 4 S2 concurrent, Category D added
- [ ] Each tier expanded only after positive expectancy confirmed at prior tier

#### Phase 3 Ongoing Tracking
- [ ] Weekly combined expectancy (S1 + S2 blended) tracked
- [ ] Weekly max drawdown from peak balance tracked
- [ ] S2 thesis intact rate tracked (exit on thesis vs on stop)
- [ ] S1 catalyst confirmation rate on S2 holds tracked
- [ ] Promoter database: >100 entities, >200 campaigns resolved
- [ ] At least one S2 category with >20 trades and >55% win rate
- [ ] IBKR fill quality documented vs Alpaca baseline

#### Phase 3 Exit Criteria (system validated)
- [ ] 90 days of two-strategy operation completed
- [ ] Combined expectancy >0.5R per signal
- [ ] No week with >3% drawdown from start-of-week balance
- [ ] Account balance net positive over 12-week period
- [ ] Promoter database >100 entities, >200 campaigns resolved
- [ ] You trust the system enough to respond without detailed review every time

---

### Phase 4 — Scale and Optimize *(Week 27+)*
> Ongoing operating mode. No end date.

- [ ] Upgrade Droplet to 8GB/4vCPU when resource monitoring triggers
- [ ] Add PostgreSQL read replica when reporting queries impact primary
- [ ] Retire any S2 category with <50% win rate over >30 trades
- [ ] Retire any promoter from active watchlist with <40% reliability over >10 campaigns
- [ ] Raise entry confidence thresholds on catalyst types where avg R <1.5R over >20 trades
- [ ] Quarterly infrastructure review: data sources, fees, stack currency
- [ ] Quarterly promotion landscape review: new channels, dying channels, new operators
- [ ] IBKR direct routing rules built per market maker per float tier
- [ ] Consider options for defined-risk Category D plays
- [ ] Evaluate account size impact on low-float liquidity at $75k+ balance

---

## Repository Structure

```
profitlyworkflow/
├── .github/workflows/
│   ├── test.yml
│   └── deploy.yml
├── config/
│   ├── [settings.py](http://settings.py)
│   └── [constants.py](http://constants.py)
├── data/
│   ├── schema/
│   │   ├── schema.sql
│   │   └── migrations/
│   ├── models/
│   └── repositories/
├── ingestion/
│   ├── edgar/
│   ├── market_data/
│   ├── social/
│   └── short_interest/
├── intelligence/
│   ├── promoter/
│   ├── catalyst/
│   ├── liquidity/
│   └── regime/
├── signals/
│   ├── strategy1/
│   └── strategy2/
│       └── categories/
├── risk/
├── execution/
│   └── broker/
├── alerts/
│   └── formatters/
├── flows/
├── backtesting/
├── reporting/
├── tests/
│   ├── unit/
│   └── integration/
├── scripts/
├── .env.example
├── requirements.txt
├── docker-compose.yml
├── Dockerfile
├── alembic.ini
└── [README.md](http://README.md)
```

---

## Branch Strategy

| Branch | Purpose |
|---|---|
| `main` | Production. Protected. Deploy on merge. |
| `develop` | Integration. Merges to main when stable. |
| `feature/[name]` | Individual feature development. |
| `hotfix/[name]` | Critical production fixes only. |

---

## Operational Scripts

Manual recovery / maintenance tools live under `scripts/`. Run them on the droplet via `sudo -u trading` after sourcing `.env.production`:

```bash
sudo -u trading bash -c '
    set -a; source /app/profitlyworkflow/.env.production; set +a
    cd /app/profitlyworkflow
    ./venv/bin/python scripts/<script>.py [flags]
'
```

| Script | Purpose |
|---|---|
| `reprocess_unprocessed_filings.py` | Drains backlogs of `sec_filings` rows stuck at `processed=False` by re-dispatching `process_filing.delay()` for each. Use after a celery-side outage. Flags: `--dry-run`, `--form-type`, `--limit`, `--created-before`, `--reconstruct-links`, `--rate`. See `--help`. |
| `reprocess_filings.py` | Re-runs the 8-K extractor against `processed=True` rows whose extraction columns are still empty (legacy / pre-extractor stubs). |
| `update_floats.py` | Refreshes the share-float column on the ticker universe (~13h on Starter tier). Same logic the Prefect flow uses; this is the manual trigger for ad-hoc backfills outside the Sunday 06:00 ET schedule. |
| `seed_cik_universe.py` | Seeds / refreshes the CIK universe table from SEC company tickers JSON. |
| `verify_setup.py` | Smoke-checks env vars, DB connectivity, broker config; `--strict` makes connection failures critical. |

---

## Operational Flows (Prefect)

| Flow | Schedule | Purpose |
|---|---|---|
| `weekly-float-update` (`flows/float_update_flow.py`) | `0 6 * * 0` America/New_York (Sunday 06:00 ET) | Walks the active ticker universe, refreshes `float_shares` from Polygon, deactivates tickers >10M float or no longer listed. Sunday morning means the sweep finishes Sunday evening so data is fresh for Monday open. Each run writes a row to `flow_run_log` with status, summary, and (on failure) `error_message`. Manual ad-hoc invocation: `scripts/update_floats.py`. |
| `outcome-resolution-flow` (`flows/outcome_resolution_flow.py`) | Hourly + 17:00 ET sweep | Closes matured predictions by fetching realized prices from Polygon and writing the outcome row. |

`flow_run_log` is the source of truth for "did the flow run?" — query it directly:

```sql
SELECT flow_name, status, started_at, completed_at, summary
FROM flow_run_log
WHERE flow_name = 'weekly-float-update'
ORDER BY started_at DESC
LIMIT 10;
```

---

## Environment Variables

See `.env.example` for complete list. Three environment files, never committed:

- `.env.development` — local development
- `.env.staging` — paper trading on DigitalOcean  
- `.env.production` — live trading on DigitalOcean

`BROKER_MODE=paper|live` is the critical safety gate. All order submission functions check this before execution.

---

## Key Design Decisions

**Why human-confirmed entry:** Low-float momentum has enough variance and liquidity risk that a fully autonomous entry has non-trivial probability of buying into a halt, getting stuck in a spread, or chasing a ghost signal. The most successful version keeps automated detection and alerting with human-confirmed entry and automated exit management.

**Why the "no" dataset matters:** When you pass on a signal, logging the reason is as important as the trade result. After 90 days you know which "no" reasons correctly avoided losers (good instinct, validate it) and which missed winners (bias or fear — fix it).

**Why paper trade on expiry:** An alert that expires while you are unavailable is a data point. It tells you whether the system fires at inconvenient times, whether the move waited or didn't, and whether your response time is a variable in your results.

**Why the promoter network maps attorneys and transfer agents:** These entities appear in SEC enforcement cases alongside promoters. When a new 8-K names an attorney or transfer agent that appears in 4 prior promotion enforcement cases, that is a structural signal about the campaign before the promoter makes any public move. The relationship exists weeks before any newsletter blast.

---

## Performance Benchmarks

| Metric | Minimum to Advance | Target at Scale |
|---|---|---|
| Signal win rate | >50% | >60% |
| Expectancy per signal | >0.3R (Phase 2), >0.5R (Phase 3) | >0.8R |
| Average slippage | <0.5% of entry | <0.3% of entry |
| Max daily drawdown | Never exceeds 2% | Typically <1% |
| Prefect flow uptime | >99% | >99.5% |
| Alert response time | <60 seconds average | <30 seconds average |
| Promoter reliability score accuracy | Directionally correct | Statistically validated at >20 campaigns per entity |

---

## Legal and Compliance Notes

All data sources used are public. All promoter monitoring is based on publicly disclosed filings and publicly disclosed compensation. Monitoring Telegram channels to detect when a stock has attention building is not illegal. Trading ahead of a publicly disclosed promotion campaign based on public filing data is not insider trading.

What this system does not do and will never do: act on material non-public information, coordinate trading with promoters or IR firms, participate in pump-and-dump schemes.

Short-term trading gains are taxed as ordinary income. At typical marginal rates, a 3R winner after tax is closer to a 1.9R winner. Factor into performance reporting. Consider entity structure at scale.

---

*Last updated: Phase 1 — learning loop active end-to-end on EDGAR signals.*
