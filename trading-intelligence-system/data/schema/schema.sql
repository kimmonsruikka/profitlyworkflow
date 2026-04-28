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
    ticker          VARCHAR(10) PRIMARY KEY,
    company_name    TEXT,
    float_shares    BIGINT,
    exchange        VARCHAR(20),
    sector          VARCHAR(100),
    first_seen      TIMESTAMPTZ DEFAULT NOW(),
    active          BOOLEAN DEFAULT TRUE,
    notes           TEXT
);

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
    timestamp       TIMESTAMPTZ NOT NULL,
    open            NUMERIC(10,4),
    high            NUMERIC(10,4),
    low             NUMERIC(10,4),
    close           NUMERIC(10,4),
    volume          BIGINT,
    vwap            NUMERIC(10,4),
    spread_pct      NUMERIC(8,6),
    liquidity_score NUMERIC(5,2),
    PRIMARY KEY (ticker, timestamp)
);

SELECT create_hypertable(
    'price_data', 'timestamp',
    chunk_time_interval => INTERVAL '1 day',
    if_not_exists => TRUE
);

-- ---------------------------------------------------------------------------
-- Indexes
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_promoter_campaigns_entity_id      ON promoter_campaigns(entity_id);
CREATE INDEX IF NOT EXISTS idx_promoter_campaigns_ticker         ON promoter_campaigns(ticker);
CREATE INDEX IF NOT EXISTS idx_promoter_network_edges_a          ON promoter_network_edges(entity_a);
CREATE INDEX IF NOT EXISTS idx_promoter_network_edges_b          ON promoter_network_edges(entity_b);

CREATE INDEX IF NOT EXISTS idx_sec_filings_filed_processed       ON sec_filings(filed_at, processed);
CREATE INDEX IF NOT EXISTS idx_sec_filings_ticker_form           ON sec_filings(ticker, form_type);

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
