-- Polymarket Trader Bot — PostgreSQL schema
-- Auto-loaded by postgres:16-alpine on first container start.

CREATE TABLE IF NOT EXISTS markets (
    condition_id    TEXT PRIMARY KEY,
    question        TEXT NOT NULL,
    category        TEXT,
    end_date        TIMESTAMPTZ,
    resolved        BOOLEAN DEFAULT FALSE,
    outcome         TEXT,
    volume_usd      NUMERIC,
    liquidity_usd   NUMERIC,
    fetched_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS prices (
    id          BIGSERIAL PRIMARY KEY,
    token_id    TEXT NOT NULL,
    timestamp   TIMESTAMPTZ NOT NULL,
    price       NUMERIC NOT NULL CHECK (price >= 0 AND price <= 1),
    volume      NUMERIC,
    UNIQUE (token_id, timestamp)
);

CREATE TABLE IF NOT EXISTS trades (
    id              BIGSERIAL PRIMARY KEY,
    strategy        TEXT NOT NULL,
    condition_id    TEXT NOT NULL,
    token_id        TEXT NOT NULL,
    side            TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
    size_usd        NUMERIC NOT NULL,
    price           NUMERIC NOT NULL,
    fee_usd         NUMERIC DEFAULT 0,
    mode            TEXT NOT NULL CHECK (mode IN ('backtest', 'paper', 'live')),
    executed_at     TIMESTAMPTZ NOT NULL,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS news_articles (
    id              BIGSERIAL PRIMARY KEY,
    source          TEXT NOT NULL,
    title           TEXT NOT NULL,
    body            TEXT,
    url             TEXT UNIQUE,
    published_at    TIMESTAMPTZ NOT NULL,
    fetched_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS news_features (
    id                      BIGSERIAL PRIMARY KEY,
    condition_id            TEXT NOT NULL,
    timestamp               TIMESTAMPTZ NOT NULL,
    article_count_24h       INT,
    article_count_delta     NUMERIC,
    avg_sentiment_score     NUMERIC,
    sentiment_std           NUMERIC,
    sentiment_delta_24h     NUMERIC,
    price_vs_sentiment_gap  NUMERIC,
    created_at              TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS orderbook_snapshots (
    id              BIGSERIAL PRIMARY KEY,
    token_id        TEXT NOT NULL,
    timestamp       TIMESTAMPTZ NOT NULL,
    bids            JSONB,          -- top 5 bid levels [{price, size}, ...]
    asks            JSONB,          -- top 5 ask levels [{price, size}, ...]
    mid_price       NUMERIC,
    spread          NUMERIC,
    UNIQUE (token_id, timestamp)
);

CREATE TABLE IF NOT EXISTS llm_estimates (
    id              BIGSERIAL PRIMARY KEY,
    condition_id    TEXT NOT NULL,
    model           TEXT NOT NULL,
    prompt_hash     TEXT NOT NULL,
    probability     NUMERIC NOT NULL CHECK (probability >= 0 AND probability <= 1),
    confidence      NUMERIC,
    reasoning       TEXT,
    sources         JSONB,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- ── Phase 6 tables ────────────────────────────────────────────────────────────

-- Live orders: every real order submitted to the CLOB
CREATE TABLE IF NOT EXISTS live_orders (
    id              BIGSERIAL PRIMARY KEY,
    order_id        TEXT UNIQUE NOT NULL,       -- internal UUID
    clob_order_id   TEXT,                       -- ID returned by CLOB API
    tx_hash         TEXT,                       -- Polygon tx hash (populated at fill)
    strategy        TEXT NOT NULL,
    condition_id    TEXT NOT NULL,
    token_id        TEXT NOT NULL,
    side            TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
    order_type      TEXT NOT NULL DEFAULT 'MARKET', -- MARKET | LIMIT
    requested_size_usd  NUMERIC NOT NULL,
    filled_size_usd     NUMERIC,
    limit_price         NUMERIC,
    fill_price          NUMERIC,
    slippage_bps        NUMERIC DEFAULT 0,
    fee_usd             NUMERIC DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'PENDING'
                        CHECK (status IN ('PENDING','FILLED','PARTIAL','CANCELLED','FAILED')),
    submitted_at    TIMESTAMPTZ NOT NULL,
    filled_at       TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Portfolio snapshots: point-in-time equity curve
CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    id                  BIGSERIAL PRIMARY KEY,
    mode                TEXT NOT NULL CHECK (mode IN ('paper', 'live')),
    strategy            TEXT NOT NULL DEFAULT 'unknown',
    ticks               BIGINT NOT NULL DEFAULT 0,
    cash_usd            NUMERIC NOT NULL,
    positions_value_usd NUMERIC NOT NULL,
    total_value_usd     NUMERIC NOT NULL,
    unrealized_pnl      NUMERIC NOT NULL DEFAULT 0,
    realized_pnl        NUMERIC NOT NULL DEFAULT 0,
    open_positions      INT NOT NULL DEFAULT 0,
    snapshot_at         TIMESTAMPTZ NOT NULL,
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

-- On-chain reconciliation reports (Blockscout)
CREATE TABLE IF NOT EXISTS reconciliation_reports (
    id                      BIGSERIAL PRIMARY KEY,
    wallet_address          TEXT NOT NULL,
    chain_id                INT NOT NULL DEFAULT 137,
    onchain_usdc_balance    NUMERIC NOT NULL,
    internal_cash_balance   NUMERIC NOT NULL,
    balance_discrepancy     NUMERIC NOT NULL,
    unrecorded_transfers    JSONB DEFAULT '[]',
    unconfirmed_tx_hashes   JSONB DEFAULT '[]',
    ok                      BOOLEAN NOT NULL,
    checked_at              TIMESTAMPTZ NOT NULL,
    created_at              TIMESTAMPTZ DEFAULT NOW()
);

-- Indexes for query performance
CREATE INDEX IF NOT EXISTS idx_prices_token_ts ON prices (token_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_trades_strategy ON trades (strategy, executed_at DESC);
CREATE INDEX IF NOT EXISTS idx_news_published ON news_articles (published_at DESC);
CREATE INDEX IF NOT EXISTS idx_news_features_condition ON news_features (condition_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_orderbook_token_ts ON orderbook_snapshots (token_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_llm_condition ON llm_estimates (condition_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_live_orders_strategy ON live_orders (strategy, submitted_at DESC);
CREATE INDEX IF NOT EXISTS idx_live_orders_status ON live_orders (status, submitted_at DESC);
CREATE INDEX IF NOT EXISTS idx_portfolio_snapshots_at ON portfolio_snapshots (snapshot_at DESC);
CREATE INDEX IF NOT EXISTS idx_reconciliation_wallet ON reconciliation_reports (wallet_address, checked_at DESC);
