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

-- Indexes for query performance
CREATE INDEX IF NOT EXISTS idx_prices_token_ts ON prices (token_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_trades_strategy ON trades (strategy, executed_at DESC);
CREATE INDEX IF NOT EXISTS idx_news_published ON news_articles (published_at DESC);
CREATE INDEX IF NOT EXISTS idx_news_features_condition ON news_features (condition_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_orderbook_token_ts ON orderbook_snapshots (token_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_llm_condition ON llm_estimates (condition_id, created_at DESC);
