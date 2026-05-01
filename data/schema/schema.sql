-- Trading Intelligence System — PostgreSQL schema (TimescaleDB)
-- Run order: extensions → tables → indexes → hypertable

-- ---------------------------------------------------------------------------
-- Extensions
-- ---------------------------------------------------------------------------
CREATE EXTENSION IF NOT EXISTS timescaledb;
CREATE EXTENSION IF NOT EXISTS "pgcrypto";  -- gen_random_uuid()

-- ---------------------------------------------------------------------------
-- tickers
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tickers (
    ticker              VARCHAR(10) PRIMARY KEY,
    cik                 VARCHAR(20),
    company_name        TEXT,
    float_shares        BIGINT,
    shares_outstanding  BIGINT,
    exchange            VARCHAR(20),
    sector              VARCHAR(100),
    first_seen          TIMESTAMPTZ DEFAULT NOW(),
    active              BOOLEAN DEFAULT TRUE,
    float_updated_at    TIMESTAMPTZ,
    notes               TEXT
);

CREATE INDEX IF NOT EXISTS idx_tickers_cik     ON tickers(cik);
CREATE INDEX IF NOT EXISTS idx_tickers_active  ON tickers(active);
CREATE INDEX IF NOT EXISTS idx_tickers_float_updated_at
    ON tickers(float_updated_at) WHERE active = TRUE;

-- ---------------------------------------------------------------------------
-- promoter_entities
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS promoter_entities (
    entity_id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name                    TEXT NOT NULL,
    type                    VARCHAR(50) NOT NULL,
    first_seen_edgar        TIMESTAMPTZ,
    sec_enforcement_case    BOOLEAN DEFAULT FALSE,
    enforcement_case_url    TEXT,
    current_status          VARCHAR(50) DEFAULT 'active',
    notes                   TEXT,
    created_at              TIMESTAMPTZ DEFAULT NOW(),
    updated_at              TIMESTAMPTZ DEFAULT NOW()
);

-- ---------------------------------------------------------------------------
-- promoter_campaigns
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS promoter_campaigns (
    campaign_id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id               UUID REFERENCES promoter_entities(entity_id),
    ticker                  VARCHAR(10) REFERENCES tickers(ticker),
    launch_date             DATE,
    end_date                DATE,
    compensation_amount     NUMERIC(12,2),
    compensation_type       VARCHAR(50),
    source_filing           TEXT,
    day1_move_pct           NUMERIC(8,4),
    peak_move_pct           NUMERIC(8,4),
    days_to_peak            INTEGER,
    decay_speed             VARCHAR(20),
    campaign_result         VARCHAR(30),
    notes                   TEXT,
    created_at              TIMESTAMPTZ DEFAULT NOW()
);

-- ---------------------------------------------------------------------------
-- promoter_network_edges
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS promoter_network_edges (
    edge_id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_a                UUID REFERENCES promoter_entities(entity_id),
    entity_b                UUID REFERENCES promoter_entities(entity_id),
    co_appearance_count     INTEGER DEFAULT 1,
    first_co_appearance     TIMESTAMPTZ,
    last_co_appearance      TIMESTAMPTZ,
    filing_references       JSONB DEFAULT '[]'
);

-- ---------------------------------------------------------------------------
-- underwriters — Nasdaq Capital Market equivalent of promoter_entities.
-- Tracks the placement-agent / underwriter network behind small-cap IPOs
-- and follow-on offerings. manipulation_flagged identifies entities named
-- in regulatory or investigative reporting.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS underwriters (
    underwriter_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name                    TEXT NOT NULL,
    type                    VARCHAR(50),
    first_seen_edgar        TIMESTAMPTZ,
    ncm_listing_count       INTEGER DEFAULT 0,
    manipulation_flagged    BOOLEAN DEFAULT FALSE,
    flag_source             TEXT,
    notes                   TEXT,
    created_at              TIMESTAMPTZ DEFAULT NOW()
);

-- ---------------------------------------------------------------------------
-- sec_filings
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sec_filings (
    filing_id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ticker                  VARCHAR(10),
    cik                     VARCHAR(20),
    filed_at                TIMESTAMPTZ NOT NULL,
    form_type               VARCHAR(20) NOT NULL,
    accession_number        VARCHAR(50) UNIQUE,
    item_numbers            JSONB DEFAULT '[]',
    ir_firm_mentioned       TEXT,
    compensation_disclosed  BOOLEAN DEFAULT FALSE,
    compensation_amount     NUMERIC(12,2),
    s3_effective            BOOLEAN DEFAULT FALSE,
    form4_insider_buy       BOOLEAN DEFAULT FALSE,
    full_text               JSONB DEFAULT '{}',
    processed               BOOLEAN DEFAULT FALSE,
    underwriter_id          UUID REFERENCES underwriters(underwriter_id),
    created_at              TIMESTAMPTZ DEFAULT NOW()
);

-- ---------------------------------------------------------------------------
-- signals
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS signals (
    signal_id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    strategy                VARCHAR(10) NOT NULL,
    s2_category             VARCHAR(5),
    ticker                  VARCHAR(10) NOT NULL,
    generated_at            TIMESTAMPTZ NOT NULL,
    catalyst_type           VARCHAR(50),
    confidence_score        NUMERIC(5,2),
    liquidity_score         NUMERIC(5,2),
    promoter_entity_id      UUID REFERENCES promoter_entities(entity_id),
    entry_price_low         NUMERIC(10,4),
    entry_price_high        NUMERIC(10,4),
    stop_price              NUMERIC(10,4),
    target1_price           NUMERIC(10,4),
    target2_price           NUMERIC(10,4),
    risk_dollars            NUMERIC(10,2),
    share_count             INTEGER,
    outcome                 VARCHAR(30),
    decline_reason          VARCHAR(100),
    paper_entry_price       NUMERIC(10,4),
    alert_sent_at           TIMESTAMPTZ,
    response_at             TIMESTAMPTZ,
    response_time_seconds   INTEGER,
    created_at              TIMESTAMPTZ DEFAULT NOW()
);

-- ---------------------------------------------------------------------------
-- trades
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS trades (
    trade_id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    signal_id               UUID REFERENCES signals(signal_id),
    strategy                VARCHAR(10) NOT NULL,
    ticker                  VARCHAR(10) NOT NULL,
    entry_price             NUMERIC(10,4) NOT NULL,
    entry_time              TIMESTAMPTZ NOT NULL,
    exit_price              NUMERIC(10,4),
    exit_time               TIMESTAMPTZ,
    shares                  INTEGER NOT NULL,
    pnl_dollars             NUMERIC(10,2),
    pnl_r                   NUMERIC(8,4),
    hold_minutes            INTEGER,
    exit_reason             VARCHAR(50),
    mae_dollars             NUMERIC(10,2),
    mfe_dollars             NUMERIC(10,2),
    liquidity_score_entry   NUMERIC(5,2),
    liquidity_score_exit    NUMERIC(5,2),
    slippage_cents_entry    NUMERIC(8,4),
    slippage_cents_exit     NUMERIC(8,4),
    overnight_hold          BOOLEAN DEFAULT FALSE,
    broker                  VARCHAR(20),
    broker_order_id         TEXT,
    created_at              TIMESTAMPTZ DEFAULT NOW()
);

-- ---------------------------------------------------------------------------
-- positions
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS positions (
    position_id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    strategy                VARCHAR(10) NOT NULL,
    ticker                  VARCHAR(10) NOT NULL,
    entry_price             NUMERIC(10,4) NOT NULL,
    entry_time              TIMESTAMPTZ NOT NULL,
    shares                  INTEGER NOT NULL,
    current_price           NUMERIC(10,4),
    stop_price              NUMERIC(10,4),
    target1_price           NUMERIC(10,4),
    target2_price           NUMERIC(10,4),
    unrealized_pnl          NUMERIC(10,2),
    unrealized_pnl_r        NUMERIC(8,4),
    days_held               INTEGER DEFAULT 0,
    thesis_category         VARCHAR(5),
    thesis_intact           BOOLEAN DEFAULT TRUE,
    status                  VARCHAR(20) DEFAULT 'open',
    signal_id               UUID REFERENCES signals(signal_id),
    updated_at              TIMESTAMPTZ DEFAULT NOW(),
    created_at              TIMESTAMPTZ DEFAULT NOW()
);

-- ---------------------------------------------------------------------------
-- account_state
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS account_state (
    date                    DATE PRIMARY KEY,
    opening_balance         NUMERIC(12,2),
    closing_balance         NUMERIC(12,2),
    daily_pnl               NUMERIC(10,2),
    daily_pnl_pct           NUMERIC(8,6),
    pdt_count_rolling       INTEGER DEFAULT 0,
    trades_today            INTEGER DEFAULT 0,
    signals_generated       INTEGER DEFAULT 0,
    signals_executed        INTEGER DEFAULT 0,
    signals_declined        INTEGER DEFAULT 0,
    signals_expired         INTEGER DEFAULT 0,
    s2_positions_open       INTEGER DEFAULT 0,
    total_exposure_pct      NUMERIC(8,6)
);

-- ---------------------------------------------------------------------------
-- price_data (TimescaleDB hypertable)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS price_data (
    ticker          VARCHAR(10) NOT NULL,
    granularity     VARCHAR(10) NOT NULL,
    timestamp       TIMESTAMPTZ NOT NULL,
    open            NUMERIC(10,4),
    high            NUMERIC(10,4),
    low             NUMERIC(10,4),
    close           NUMERIC(10,4),
    volume          BIGINT,
    vwap            NUMERIC(10,4),
    spread_pct      NUMERIC(8,6),
    liquidity_score NUMERIC(5,2),
    PRIMARY KEY (ticker, granularity, timestamp)
);

SELECT create_hypertable(
    'price_data', 'timestamp',
    chunk_time_interval => INTERVAL '1 day',
    if_not_exists => TRUE
);

-- "Most-recent first" lookup index for cache range queries.
CREATE INDEX IF NOT EXISTS idx_price_data_lookup
    ON price_data (ticker, granularity, "timestamp" DESC);

-- ---------------------------------------------------------------------------
-- Indexes
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_promoter_campaigns_entity_id      ON promoter_campaigns(entity_id);
CREATE INDEX IF NOT EXISTS idx_promoter_campaigns_ticker         ON promoter_campaigns(ticker);
CREATE INDEX IF NOT EXISTS idx_promoter_network_edges_a          ON promoter_network_edges(entity_a);
CREATE INDEX IF NOT EXISTS idx_promoter_network_edges_b          ON promoter_network_edges(entity_b);

CREATE INDEX IF NOT EXISTS idx_sec_filings_filed_processed       ON sec_filings(filed_at, processed);
CREATE INDEX IF NOT EXISTS idx_sec_filings_ticker_form           ON sec_filings(ticker, form_type);
CREATE INDEX IF NOT EXISTS idx_sec_filings_underwriter           ON sec_filings(underwriter_id);

CREATE INDEX IF NOT EXISTS idx_underwriters_name                 ON underwriters(name);
CREATE INDEX IF NOT EXISTS idx_underwriters_flagged              ON underwriters(manipulation_flagged);

CREATE INDEX IF NOT EXISTS idx_signals_ticker_generated          ON signals(ticker, generated_at);
CREATE INDEX IF NOT EXISTS idx_signals_outcome                   ON signals(outcome);
CREATE INDEX IF NOT EXISTS idx_signals_strategy_generated        ON signals(strategy, generated_at);
CREATE INDEX IF NOT EXISTS idx_signals_promoter_entity           ON signals(promoter_entity_id);

CREATE INDEX IF NOT EXISTS idx_trades_ticker_entry               ON trades(ticker, entry_time);
CREATE INDEX IF NOT EXISTS idx_trades_strategy_entry             ON trades(strategy, entry_time);
CREATE INDEX IF NOT EXISTS idx_trades_signal_id                  ON trades(signal_id);

CREATE INDEX IF NOT EXISTS idx_positions_status_strategy         ON positions(status, strategy);
CREATE INDEX IF NOT EXISTS idx_positions_ticker                  ON positions(ticker);
CREATE INDEX IF NOT EXISTS idx_positions_signal_id               ON positions(signal_id);

-- ---------------------------------------------------------------------------
-- gate_decisions — every risk gatekeeper decision, append-only audit log
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS gate_decisions (
    decision_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    signal_id       UUID REFERENCES signals(signal_id),
    rule_triggered  VARCHAR(64) NOT NULL,
    approved        BOOLEAN NOT NULL,
    message         VARCHAR(500),
    timestamp       TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_gate_decisions_signal       ON gate_decisions(signal_id);
CREATE INDEX IF NOT EXISTS idx_gate_decisions_timestamp    ON gate_decisions(timestamp);

-- ---------------------------------------------------------------------------
-- LEARNING ARCHITECTURE
-- model_versions / predictions / outcomes
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS model_versions (
    version_id              VARCHAR(40) PRIMARY KEY,
    model_class             VARCHAR(40) NOT NULL,
    feature_schema_version  VARCHAR(20) NOT NULL,
    trained_at              TIMESTAMPTZ,
    training_set_size       INTEGER,
    calibration_metrics     JSONB DEFAULT '{}'::jsonb,
    in_production           BOOLEAN NOT NULL DEFAULT FALSE,
    in_shadow               BOOLEAN NOT NULL DEFAULT FALSE,
    artifact_path           TEXT,
    notes                   TEXT
);

CREATE TABLE IF NOT EXISTS predictions (
    prediction_id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at                 TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ticker                     VARCHAR(10) NOT NULL,
    signal_type                VARCHAR(40) NOT NULL,
    feature_vector             JSONB NOT NULL,
    feature_schema_version     VARCHAR(20) NOT NULL,
    scorer_version             VARCHAR(40) NOT NULL,
    confidence                 NUMERIC(5,4) NOT NULL,
    predicted_window_minutes   INTEGER NOT NULL,
    predicted_target_pct       NUMERIC(6,3),
    alert_sent                 BOOLEAN NOT NULL DEFAULT FALSE,
    user_decision              VARCHAR(20),
    decision_reason            TEXT,
    trade_id                   UUID,
    outcome_id                 UUID
);

CREATE INDEX IF NOT EXISTS idx_predictions_ticker_time
    ON predictions(ticker, created_at);
CREATE INDEX IF NOT EXISTS idx_predictions_unresolved
    ON predictions(created_at) WHERE outcome_id IS NULL;

CREATE TABLE IF NOT EXISTS outcomes (
    outcome_id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    prediction_id                 UUID NOT NULL REFERENCES predictions(prediction_id),
    resolved_at                   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    window_close_at               TIMESTAMPTZ NOT NULL,
    max_favorable_excursion_pct   NUMERIC(6,3),
    max_adverse_excursion_pct     NUMERIC(6,3),
    realized_return_pct           NUMERIC(6,3),
    paper_return_pct              NUMERIC(6,3),
    hit_target                    BOOLEAN,
    hit_stop                      BOOLEAN,
    outcome_label                 VARCHAR(20) NOT NULL,
    price_data_source             VARCHAR(40) NOT NULL,
    invalid_reason                VARCHAR(50),
    UNIQUE (prediction_id)
);

CREATE INDEX IF NOT EXISTS idx_outcomes_label ON outcomes(outcome_label);

-- ---------------------------------------------------------------------------
-- flow_run_log — operational history for Prefect flows
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS flow_run_log (
    flow_run_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    flow_name     VARCHAR(80) NOT NULL,
    started_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at  TIMESTAMPTZ,
    status        VARCHAR(20) NOT NULL,
    summary       JSONB,
    error_message TEXT
);

CREATE INDEX IF NOT EXISTS idx_flow_run_log_name_started
    ON flow_run_log(flow_name, started_at DESC);
