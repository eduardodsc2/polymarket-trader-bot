# Polymarket Trader Bot — Master Development Plan

> **Audience**: This plan is written for Claude Code. Each phase is self-contained with clear goals, deliverables, and acceptance criteria. Start with Phase 0 and do not advance to the next phase until all acceptance criteria are met.

> **MCP Tools available**: Three MCPs are connected and must be used at the phases indicated.
> - **Context7** (`resolve-library-id` + `query-docs`): Use in ALL phases before writing code with any external library.
> - **LunarCrush** (`Topic`, `Topic_Posts`, `Topic_Time_Series`, `Cryptocurrencies`): Use from Phase 4 onwards for crypto market social sentiment.
> - **Blockscout** (`get_address_info`, `get_transactions_by_address`, `get_token_transfers_by_address`, etc.): Use from Phase 5 onwards for on-chain verification on Polygon (chain_id="137").

---

## Executive Summary

We are building a modular, research-grade automated trading system for Polymarket. The approach is empirical: hypotheses are expressed as backtestable strategies, evaluated against well-known quantitative metrics, and only deployed live after passing rigorous tests.

The system will eventually support multiple concurrent strategies. We start simple, prove the infrastructure works, then layer complexity progressively.

---

## Phase Overview

| Phase | Name | Goal | Duration (est.) |
|---|---|---|---|
| 0 | Docker & Infrastructure | Containerized environment running on WSL2 | 2–3 days |
| 1 | Foundation & Data | APIs, historical data pipeline | 1–2 weeks |
| 2 | Backtest Engine | Event-driven engine, fill model, metrics | 2–3 weeks |
| 3 | Strategy Research | Implement & backtest 3 strategies | 3–4 weeks |
| 4 | LLM Integration | Value betting with Claude + news pipeline | 2–3 weeks |
| 5 | Paper Trading | Live environment, no real capital | 1–2 weeks |
| 6 | Live Deployment | Real capital, full risk controls | Ongoing |

---

## Phase 0 — Docker & Infrastructure

### Goal
Stand up the full containerized environment locally (WSL2 on Windows). After this phase, every subsequent task runs inside Docker — no Python or dependencies installed directly on the host.

### WSL2 Prerequisites (one-time, done on Windows host)
```
1. Enable WSL2:         wsl --install
2. Install Ubuntu 22:   wsl --install -d Ubuntu-22.04
3. Install Docker Desktop for Windows with WSL2 backend enabled
4. Open Ubuntu terminal and verify: docker --version && docker compose version
```
Docker Desktop handles the bridge between Windows and WSL2. All `docker compose` commands below are run from inside the Ubuntu WSL2 terminal.

---

### Tasks

**0.1 — Dockerfile (multi-stage)**

File: `Dockerfile`

```dockerfile
# ── Stage 1: base ─────────────────────────────────────────────────────────────
FROM python:3.11-slim AS base

WORKDIR /app

# System dependencies for scientific Python + Polygon crypto libs
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ libpq-dev curl git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# ── Stage 2: jupyter (adds JupyterLab) ────────────────────────────────────────
FROM base AS jupyter
RUN pip install --no-cache-dir jupyterlab
EXPOSE 8888
CMD ["jupyter", "lab", "--ip=0.0.0.0", "--no-browser", "--allow-root", \
     "--NotebookApp.token=${JUPYTER_TOKEN}"]

# ── Stage 3: scheduler (APScheduler process) ──────────────────────────────────
FROM base AS scheduler
CMD ["python", "-m", "live.scheduler"]

# ── Default target: bot ───────────────────────────────────────────────────────
FROM base AS bot
CMD ["python", "-m", "live.executor", "--mode", "paper"]
```

**0.2 — docker-compose.yml**

File: `docker-compose.yml`

```yaml
version: "3.9"

services:

  bot:
    build:
      context: .
      target: bot
    env_file: .env
    volumes:
      - ./data:/app/data          # Raw + processed data persists on host
      - ./results:/app/results    # Backtest outputs persist on host
      - .:/app                    # Source code live-mounted for development
    depends_on:
      db:
        condition: service_healthy
    networks:
      - polymarket

  db:
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: polymarket
      POSTGRES_PASSWORD: ${DB_PASSWORD}
      POSTGRES_DB: polymarket_bot
    volumes:
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U polymarket"]
      interval: 5s
      timeout: 5s
      retries: 5
    ports:
      - "5432:5432"               # Expose to WSL host for DB inspection tools
    networks:
      - polymarket

  jupyter:
    build:
      context: .
      target: jupyter
    env_file: .env
    volumes:
      - .:/app                    # Full repo mounted so notebooks see all code
    ports:
      - "8888:8888"
    depends_on:
      - db
    networks:
      - polymarket
    profiles:
      - research                  # Only starts with: docker compose --profile research up

  scheduler:
    build:
      context: .
      target: scheduler
    env_file: .env
    volumes:
      - .:/app
    depends_on:
      db:
        condition: service_healthy
    networks:
      - polymarket
    profiles:
      - live                      # Only starts with: docker compose --profile live up

volumes:
  pgdata:

networks:
  polymarket:
    driver: bridge
```

**0.3 — docker-compose.prod.yml**

File: `docker-compose.prod.yml` (overrides for VPS deployment)

```yaml
version: "3.9"

services:
  bot:
    volumes:
      - ./data:/app/data
      - ./results:/app/results
      # No source code live-mount in prod — use built image
    restart: unless-stopped

  db:
    ports: []                    # Don't expose DB port externally in prod
    restart: unless-stopped

  scheduler:
    restart: unless-stopped
```

**0.4 — .env.example**

File: `.env.example`

```bash
# ── Polymarket ────────────────────────────────────────────────────────────────
POLYMARKET_API_KEY=
POLYMARKET_API_SECRET=
POLYMARKET_API_PASSPHRASE=
POLYMARKET_PRIVATE_KEY=          # Polygon wallet private key

# ── Blockchain ────────────────────────────────────────────────────────────────
POLYGON_RPC_URL=                  # Alchemy or Infura endpoint

# ── LLM ──────────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY=
OPENAI_API_KEY=                   # Optional fallback

# ── News ─────────────────────────────────────────────────────────────────────
NEWSAPI_KEY=                      # newsapi.org free tier
REDDIT_CLIENT_ID=
REDDIT_CLIENT_SECRET=
REDDIT_USER_AGENT=polymarket-bot/1.0

# ── Database ─────────────────────────────────────────────────────────────────
DB_PASSWORD=changeme
DATABASE_URL=postgresql://polymarket:${DB_PASSWORD}@db:5432/polymarket_bot

# ── Jupyter ──────────────────────────────────────────────────────────────────
JUPYTER_TOKEN=changeme

# ── Bot config ────────────────────────────────────────────────────────────────
BOT_MODE=paper                    # paper | live
INITIAL_CAPITAL_USD=500
LLM_DAILY_SPEND_LIMIT_USD=1.00
```

**0.5 — .dockerignore**

File: `.dockerignore`

```
.env
.venv/
__pycache__/
*.pyc
*.pyo
data/raw/
data/processed/
results/
.git/
*.ipynb_checkpoints/
```

**0.6 — Database initialization**

File: `db/init.sql` — SQL schema auto-loaded by Postgres on first container start.

```sql
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

-- Indexes for query performance
CREATE INDEX IF NOT EXISTS idx_prices_token_ts ON prices (token_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_trades_strategy ON trades (strategy, executed_at DESC);
CREATE INDEX IF NOT EXISTS idx_news_published ON news_articles (published_at DESC);
```

**0.7 — Smoke test**

After `docker compose up -d`, run this to confirm everything is healthy:

```bash
# All containers running?
docker compose ps

# Bot can import its own modules?
docker compose run --rm bot python -c "from config.settings import settings; print('OK')"

# DB connection works?
docker compose run --rm bot python -c \
  "import sqlalchemy; e = sqlalchemy.create_engine('${DATABASE_URL}'); e.connect(); print('DB OK')"

# Jupyter reachable?
curl -s http://localhost:8888/api/status | python -m json.tool
```

### MCP Usage in Phase 0
- **Context7**: Before writing the `Dockerfile`, resolve `python:3.11-slim` base image best practices. Before writing `docker-compose.yml`, query docs for `docker compose` healthcheck syntax and volume mount patterns. Before writing `requirements.txt`, look up pinned versions for `sqlalchemy`, `py-clob-client`, `feedparser`.

### Acceptance Criteria for Phase 0
- [ ] `docker compose build` completes without errors (including `dashboard` image)
- [ ] `docker compose up -d` starts `bot`, `db`, and `dashboard` with status `healthy`
- [ ] Dashboard reachable at `http://localhost:8080` (even with empty data)
- [ ] Smoke tests all pass
- [ ] `config/schemas.py` created with all Pydantic `BaseModel` schemas stubbed
- [ ] `config/settings.py` created with `RANDOM_SEED` and `CIRCUIT_BREAKER_*` settings
- [ ] `data/` and `results/` directories on WSL host persist when containers restart
- [ ] `.env` is gitignored; `.env.example` is committed
- [ ] Jupyter reachable at `http://localhost:8888` with profile `research`
- [ ] `docker compose run --rm bot pytest tests/` runs (even with 0 tests initially)

---

## Phase 1 — Foundation & Data Layer

### Goal
Build a reliable data pipeline that fetches, stores, and serves clean historical Polymarket data for backtesting.

### Tasks

**1.1 — Project Setup**
- [ ] Initialize Python project with `pyproject.toml` or `setup.cfg`
- [ ] Create `.env.example` with all required environment variables
- [ ] Set up `requirements.txt` with pinned versions:
  - `py-clob-client`
  - `polymarket-apis`
  - `pandas`, `numpy`, `scipy`
  - `sqlalchemy`, `aiosqlite`
  - `httpx`, `aiohttp`
  - `python-dotenv`
  - `loguru`
  - `pytest`
- [ ] Configure `loguru` for structured logging
- [ ] Set up SQLite database schema (markets, prices, trades, orderbook_snapshots)

**1.2 — Gamma API Fetcher**

File: `data/fetchers/gamma_fetcher.py`

Fetches market metadata from `https://gamma-api.polymarket.com/markets`. For each market, stores:
- `condition_id` (unique identifier)
- `question` (text of the prediction question)
- `category` (crypto, politics, sports, etc.)
- `end_date`
- `resolved` (bool)
- `outcome` (YES/NO/null if unresolved)
- `volume` (total USDC traded)
- `liquidity` (current USDC in AMM)

```python
# Expected interface
fetcher = GammaFetcher()
markets = fetcher.get_active_markets(min_volume=10_000)
fetcher.get_resolved_markets(start_date="2024-01-01", end_date="2024-12-31")
```

**1.3 — CLOB Price History Fetcher**

File: `data/fetchers/clob_fetcher.py`

Uses `GET /prices-history` on `https://clob.polymarket.com` to pull YES/NO token price series for each market. Stores in `prices` table:
- `token_id`
- `timestamp`
- `price` (0–1)
- `volume` (optional)

Also fetch current orderbook snapshots (`GET /book`) and store top 5 bid/ask levels.

```python
fetcher = CLOBFetcher()
prices = fetcher.get_price_history(token_id="...", start_ts=..., end_ts=...)
book = fetcher.get_orderbook(token_id="...")
```

**1.4 — pmxt Archive Downloader**

File: `data/fetchers/pmxt_fetcher.py`

Downloads free hourly Parquet snapshots from `https://archive.pmxt.dev`. These contain historical orderbook depth data essential for realistic backtest fill modeling.

```python
downloader = PmxtDownloader()
downloader.download_range(start="2024-01-01", end="2024-06-01", output_dir="data/raw/pmxt/")
downloader.load_to_db(parquet_dir="data/raw/pmxt/")
```

**1.5 — Data Validation & Quality Report**

File: `data/validate.py`

Run after any data fetch to check:
- No missing timestamps (gaps > 1 hour flagged)
- Price always between 0 and 1
- YES + NO prices approximately sum to 1 (±5%) for resolved markets
- No duplicate rows
- Volume is non-negative

Output a quality report to `data/quality_report.json`.

**1.6 — Exploratory Data Notebook**

File: `notebooks/01_data_exploration.ipynb`

Cover:
- Distribution of market volumes
- Distribution of market durations
- Price volatility by category
- Historical YES+NO sum distribution (to identify arbitrage opportunities historically)
- Top 20 markets by volume

### MCP Usage in Phase 1
- **Context7**: Before writing `gamma_fetcher.py`, query `py-clob-client` and `polymarket-apis` docs for correct auth and endpoint usage. Before writing `clob_fetcher.py`, query the CLOB `/prices-history` and `/book` endpoint signatures. Before writing DB schema migrations, query `sqlalchemy` async patterns for PostgreSQL. Before defining schemas, query `pydantic` v2 docs for `BaseModel`, `field_validator`, and `model_validator` patterns.

### Acceptance Criteria for Phase 1
- [ ] All fetcher return types use Pydantic models from `config/schemas.py` — no raw dicts returned from any public function
- [ ] `RANDOM_SEED` applied in `backtest/engine.py` init — confirmed by running engine twice with same seed and getting identical trade sequences
- [ ] Can fetch and store metadata for 500+ resolved markets
- [ ] Can reconstruct price series for any market from 2024
- [ ] Data validation passes with <1% anomaly rate
- [ ] Notebook renders without errors and shows at least 3 interesting findings

---

## Phase 2 — Backtest Engine

### Goal
Build a correct, fast, event-driven backtesting engine that realistically simulates trading on Polymarket historical data.

### Design Principles
- **Event-driven**: replays events in chronological order (price updates, order fills, market resolutions)
- **Realistic fills**: uses historical orderbook depth to simulate slippage
- **No lookahead bias**: strategies can only see data up to the current event timestamp
- **Reproducible**: same seed = same results

### Tasks

**2.1 — Event System**

File: `backtest/events.py`

Define event types:
```python
@dataclass
class PriceUpdateEvent:
    timestamp: datetime
    token_id: str
    price: float
    bid: float
    ask: float

@dataclass
class MarketResolutionEvent:
    timestamp: datetime
    condition_id: str
    outcome: str  # "YES" or "NO"

@dataclass
class OrderFillEvent:
    timestamp: datetime
    order_id: str
    token_id: str
    side: str  # "BUY" or "SELL"
    price: float
    size: float
```

**2.2 — Portfolio Tracker**

File: `backtest/portfolio.py`

Tracks state during backtest:
- Cash balance (USDC)
- Open positions: `{token_id: {size, avg_cost, current_price}}`
- Trade history
- Daily PnL

Methods:
```python
portfolio.open_position(token_id, size, price, timestamp)
portfolio.close_position(token_id, size, price, timestamp)
portfolio.mark_to_market(price_updates)   # Update unrealized PnL
portfolio.resolve_position(token_id, outcome)  # Settlement at $1 or $0
portfolio.get_snapshot(timestamp) -> PortfolioSnapshot
```

**2.3 — Fill Model**

File: `backtest/fill_model.py`

Simulates realistic order fills:
- Market orders fill at best ask (buy) or best bid (sell) + slippage
- Limit orders fill when price crosses the limit
- Polymarket fee model: 2% on winnings (not on trade size)
- Partial fills when order size exceeds available liquidity at a price level

```python
fill_model = FillModel(slippage_bps=10, fee_pct=0.02)
fill = fill_model.simulate_market_buy(token_id, size, orderbook)
fill = fill_model.simulate_limit_buy(token_id, size, limit_price, orderbook)
```

**2.4 — Backtest Engine**

File: `backtest/engine.py`

Main orchestrator:
```python
engine = BacktestEngine(
    strategy=SumToOneArbitrageStrategy(),
    start_date="2024-01-01",
    end_date="2024-06-01",
    initial_capital=10_000,
    fill_model=FillModel(),
)
results = engine.run()
```

The engine:
1. Loads all relevant price/orderbook data into memory (chunked if large)
2. Creates a sorted event queue
3. Feeds events one-by-one to the strategy's `on_event()` method
4. Strategy returns list of `OrderRequest` objects
5. Engine applies fill model and updates portfolio
6. At end, computes all metrics and returns `BacktestResults`

**2.5 — Metrics Calculator**

File: `backtest/metrics.py`

```python
@dataclass
class BacktestMetrics:
    # Returns
    total_return: float          # e.g., 0.34 = 34%
    cagr: float                  # Compound annual growth rate

    # Risk-adjusted
    sharpe_ratio: float          # (return - rf) / std
    sortino_ratio: float         # (return - rf) / downside_std
    calmar_ratio: float          # CAGR / max_drawdown

    # Risk
    max_drawdown: float          # Worst peak-to-trough (%)
    max_drawdown_duration: int   # Days in worst drawdown
    volatility: float            # Annualized std of daily returns

    # Prediction quality
    brier_score: float           # Mean squared error of probability estimates

    # Trading stats
    total_trades: int
    win_rate: float
    avg_win: float
    avg_loss: float
    profit_factor: float         # gross_profit / gross_loss
    kelly_fraction: float        # Theoretical optimal bet fraction

    # Execution
    total_fees_paid: float
    avg_slippage_bps: float

def compute_metrics(portfolio_history, trades, probability_estimates) -> BacktestMetrics:
    ...
```

**2.6 — Results Report**

File: `backtest/report.py`

Given `BacktestMetrics`, generate:
- JSON summary (`results/backtest_{strategy}_{date}.json`)
- Equity curve plot (matplotlib)
- Drawdown chart
- Monthly returns heatmap
- Trade distribution histogram

**2.7 — Strategy Base Class**

File: `strategies/base_strategy.py`

```python
from abc import ABC, abstractmethod

class BaseStrategy(ABC):
    def __init__(self, config: dict):
        self.config = config
        self.portfolio = None  # Injected by engine

    @abstractmethod
    def on_price_update(self, event: PriceUpdateEvent) -> list[OrderRequest]:
        """Called on every price tick. Return list of orders to place."""
        pass

    @abstractmethod
    def on_market_resolution(self, event: MarketResolutionEvent) -> None:
        """Called when a market resolves."""
        pass

    def on_start(self) -> None:
        """Called once at backtest start. Override for initialization."""
        pass

    def on_end(self) -> None:
        """Called once at backtest end. Override for cleanup."""
        pass
```

### MCP Usage in Phase 2
- **Context7**: Before writing `metrics.py`, query `numpy`/`scipy` docs for Sharpe, Sortino, and drawdown computation patterns. Before writing `engine.py` event queue, query `heapq` or `sortedcontainers` for efficient priority queue usage in Python. Before writing `backtest/portfolio.py`, query `pydantic` v2 docs for mutable model patterns (`model_config = ConfigDict(frozen=False)`).

### Acceptance Criteria for Phase 2
- [ ] **Seed reproducibility**: running `engine.run()` twice with same `RANDOM_SEED` and same data produces byte-identical `BacktestMetrics` output
- [ ] **Pydantic**: `BacktestMetrics`, `PortfolioSnapshot`, `OrderFill` are all Pydantic models; engine raises `ValidationError` on malformed data rather than silently continuing
- [ ] Engine runs 6 months of data for 100 markets in under 60 seconds
- [ ] Fill model produces fills within 5% of realistic slippage vs. historical data
- [ ] All metrics are unit-tested against known inputs
- [ ] A dummy "always buy YES" strategy produces correct (terrible) metrics
- [ ] Equity curve plot renders correctly

---

## Phase 3 — Strategy Research & Evaluation

### Goal
Implement and rigorously backtest three distinct strategies. Identify which has the best risk-adjusted return profile and is worth taking to paper trading.

### Strategy 3A — Sum-to-One Arbitrage

**Concept**: When YES_price + NO_price < 1.00, buy both sides. You're guaranteed to win $1 per share pair, so any entry below $1 is risk-free profit.

**File**: `strategies/sum_to_one_arb.py`

**Logic**:
```
edge = 1.0 - (yes_ask + no_ask)
if edge > min_edge_threshold (e.g., 0.02):
    size = kelly_size(edge, certainty=1.0)
    place_buy(YES, size)
    place_buy(NO, size)
```

**Key parameters**:
- `min_edge`: minimum spread required (default 0.02 = 2%)
- `max_position_usdc`: max USDC per pair (default 500)

**Research questions**:
- How often does this opportunity appear historically?
- What is the average edge when it appears?
- Does it cluster around specific market types or times?
- What is the average time to resolution after entry?

---

### Strategy 3B — Passive Market Making

**Concept**: Post limit orders on both sides of the orderbook. Earn the bid-ask spread from traders who need immediate execution.

**File**: `strategies/market_maker.py`

**Logic**:
```
fair_value = (best_bid + best_ask) / 2
bid_price = fair_value - (spread / 2)
ask_price = fair_value + (spread / 2)

if inventory_skew > max_skew:
    tighten spread on side that reduces inventory
    widen spread on side that increases inventory

post_limit_order(BUY, bid_price, size)
post_limit_order(SELL, ask_price, size)
```

**Key parameters**:
- `base_spread`: minimum spread (default 0.02)
- `max_inventory_skew`: max allowed inventory imbalance (default 0.3)
- `inventory_target`: target YES fraction of inventory (default 0.5)
- `order_size_usdc`: size of each posted order

**Research questions**:
- Which markets have the highest fee rebate for liquidity providers?
- Does market age (days to resolution) correlate with spreads?
- What is the PnL decomposition: spread income vs. adverse selection loss?

---

### Strategy 3C — Calibration Betting (Pre-LLM baseline)

**Concept**: Use historical base rates to find systematically mispriced markets. For example, if "Will X happen in 30 days" markets historically resolve YES 15% of the time but currently price at 30%, bet NO.

**File**: `strategies/calibration_betting.py`

**Logic**:
```
base_rate = lookup_historical_base_rate(market_category, days_to_resolution)
current_price = get_market_price(market)
edge = abs(base_rate - current_price)

if edge > min_edge:
    direction = "NO" if base_rate < current_price else "YES"
    size = fractional_kelly(edge, kelly_fraction=0.25)
    place_order(direction, size)
```

This strategy does NOT use an LLM — it's the statistical baseline we'll improve with LLM in Phase 4.

**Key parameters**:
- `min_edge`: minimum mispricing threshold (default 0.05)
- `kelly_fraction`: fraction of Kelly to use (default 0.25)
- `max_days_to_resolution`: only enter if market resolves within N days

---

### Phase 3 Deliverables

For each strategy:
- Backtest results for full year 2024
- Full `BacktestMetrics` report (JSON + plots)
- Analysis notebook: `notebooks/02_strategy_analysis.ipynb`
- Strategy comparison table (all metrics side by side)

### Acceptance Criteria for Phase 3
- [ ] All 3 strategies backtested on identical dataset (same date range, same markets)
- [ ] At least 1 strategy achieves Sharpe > 1.0 and positive EV
- [ ] Brier Score computed for strategies that make probability estimates
- [ ] No strategy has Max Drawdown > 40%
- [ ] Strategy comparison notebook is clear and reproducible

---

## Phase 4 — LLM Integration + News Pipeline (Value Betting)

### Goal
Replace the statistical base rate lookup in Strategy 3C with an LLM probability estimator that is enriched with structured news context. The LLM reads the market question, ingests relevant recent news, reasons about it, and outputs a calibrated probability estimate.

### Full Architecture

```
News Sources (RSS, NewsAPI, Reddit)
    → news/fetcher.py        (collect + normalize articles)
    → news/relevance.py      (keyword match to market question)
    → news/store.py          (SQLite cache with TTL)
            ↓
    context: list[Article]
            ↓
Market Question + Context
    → llm/prompt_builder.py  (format structured prompt)
    → LLM API (Claude claude-sonnet-4-6 with web_search tool)
    → llm/response_parser.py (extract probability + confidence + sources)
    → llm/calibration.py     (Platt scaling correction)
    → llm/decision_engine.py (compare to market price → trade signal)
            ↓
    OrderRequest → strategies/value_betting.py
```

---

### 4.1 — News Pipeline

**New directory**: `news/`

```
news/
├── fetcher.py        # Collect articles from all sources
├── relevance.py      # Match articles to a given market question
├── store.py          # SQLite cache with TTL per article
├── sentiment.py      # Optional: numeric sentiment scoring
└── sources/
    ├── rss.py        # Generic RSS parser (feedparser)
    ├── newsapi.py    # NewsAPI wrapper (free tier: 100 req/day)
    └── reddit.py     # Reddit API wrapper (PRAW)
```

**Sources to implement (all free):**

| Source | Type | Coverage | Rate limit |
|---|---|---|---|
| Reuters RSS | RSS | General / Finance / World | Unlimited |
| AP News RSS | RSS | General / Politics / World | Unlimited |
| CoinDesk RSS | RSS | Crypto | Unlimited |
| Politico RSS | RSS | US Politics | Unlimited |
| BBC News RSS | RSS | World events | Unlimited |
| NewsAPI | REST API | 70+ sources aggregated | 100 req/day (free) |
| Reddit (r/worldnews, r/CryptoCurrency, r/politics) | API (PRAW) | Sentiment / community | 60 req/min |

**File**: `news/fetcher.py`

```python
class NewsFetcher:
    def fetch_recent(self, lookback_hours: int = 48) -> list[Article]:
        """Pull fresh articles from all configured sources."""

    def fetch_for_market(self, question: str, lookback_hours: int = 48) -> list[Article]:
        """Extract keywords from question, fetch + filter relevant articles."""
```

**Article schema:**
```python
@dataclass
class Article:
    source: str           # e.g., "reuters", "coindesk"
    title: str
    body: str             # truncated to 500 chars to control token cost
    url: str
    published_at: datetime
    relevance_score: float  # 0–1, computed by relevance.py
```

**CRITICAL — Lookahead bias prevention:**

Every article MUST have a `published_at` timestamp. The backtest engine enforces:
```python
# In engine.py — when building context for a decision at time T:
context = news_store.get_articles(
    keywords=keywords,
    before=current_event.timestamp,   # STRICT: only articles before T
    lookback_hours=48
)
```
This rule is non-negotiable. Any article without a verified timestamp is discarded.

---

### 4.2 — Relevance Matching

**File**: `news/relevance.py`

Two-stage filtering:

**Stage 1 — Keyword extraction** (fast, cheap):
```python
# LLM extracts 3–5 keywords from the market question once, cached permanently
keywords = llm.extract_keywords("Will Bitcoin exceed $120k by end of Q2 2025?")
# → ["bitcoin", "BTC", "crypto", "price", "$120k"]

articles = [a for a in articles if any(kw.lower() in a.title.lower() for kw in keywords)]
```

**Stage 2 — Semantic scoring** (optional, more accurate):
```python
# Uses sentence-transformers (local, free) to compute cosine similarity
# between market question embedding and article title embedding
from sentence_transformers import SentenceTransformer
model = SentenceTransformer("all-MiniLM-L6-v2")  # ~80MB, runs locally

question_emb = model.encode(question)
for article in articles:
    article.relevance_score = cosine_similarity(question_emb, model.encode(article.title))

# Keep only articles with relevance_score > 0.4
```

Stage 2 is optional for Phase 4 and can be enabled via config flag. Stage 1 alone is sufficient to start.

---

### 4.3 — LLM Estimator (updated)

**File**: `llm/estimator.py`

```python
class LLMEstimator:
    def estimate_probability(
        self,
        question: str,
        category: str,
        resolution_date: str,
        current_price: float,
        news_context: list[Article] = None,   # NEW: structured news
    ) -> ProbabilityEstimate:
        """
        Returns:
            probability: float [0, 1]
            confidence: float [0, 1]
            reasoning: str
            sources_used: list[str]   # NEW: which articles influenced estimate
        """
```

**Prompt design** (`llm/prompts/base.txt`):
```
You are a quantitative prediction market analyst. Estimate the probability
that the following event resolves YES.

MARKET: {question}
CATEGORY: {category}
RESOLVES: {resolution_date}
CURRENT MARKET PRICE (implied probability): {current_price:.2f}

RECENT NEWS CONTEXT:
{news_context}   ← formatted as numbered list of "Source | Title | Date | Summary"

INSTRUCTIONS:
Think step by step:
1. What is the base rate for this type of event historically?
2. What do the news items above suggest about the current probability?
3. Is the current market price consistent with the evidence, or does it appear mispriced?

Output ONLY in this exact format:
PROBABILITY: [0.00–1.00]
CONFIDENCE: [LOW / MEDIUM / HIGH]
EDGE: [estimated difference from market price, e.g. +0.12 or -0.08]
REASONING: [3–4 sentences]
SOURCES: [comma-separated list of source names used]
```

**Per-category prompt variants** (`llm/prompts/`):
```
prompts/
├── base.txt         # Default template (above)
├── crypto.txt       # Emphasizes on-chain data, exchange flows, technical levels
├── politics.txt     # Emphasizes polling, electoral history, base rates
├── sports.txt       # Emphasizes team stats, injuries, head-to-head records
└── ai_tech.txt      # Emphasizes product releases, regulatory signals
```

---

### 4.4 — News-Derived Numeric Features (for future model training)

**File**: `news/sentiment.py`

Even while using the LLM as the main estimator, compute and store numeric features for every market at decision time. These will be the training features if we later build a supervised model.

```python
@dataclass
class NewsFeatures:
    market_condition_id: str
    timestamp: datetime

    # Volume signals
    article_count_24h: int        # How much coverage?
    article_count_delta: float    # Change vs. prior 24h (surge detection)

    # Sentiment signals (computed with VADER — free, local, no API)
    avg_sentiment_score: float    # -1 (negative) to +1 (positive)
    sentiment_std: float          # Disagreement between sources
    sentiment_delta_24h: float    # Sentiment trend direction

    # Source signals
    top_source: str               # Most cited source in context window
    has_primary_source: bool      # Reuters/AP present (high credibility)

    # Market divergence signal
    price_vs_sentiment_gap: float # Market price minus sentiment-implied probability
```

Store all `NewsFeatures` to the database alongside trades. Even if not used in Phase 4 decisions, this builds the historical dataset for Phase 6 model improvements.

---

### 4.5 — Calibration (updated)

**File**: `llm/calibration.py`

Same Platt scaling process as before, now with an additional breakdown:

1. Calibrate LLM-only vs. LLM+news: does adding news context improve Brier Score?
2. Calibrate by category: is news more useful for crypto than politics?
3. Calibrate by `article_count_24h`: is the LLM more accurate when there is more coverage?

This tells us when to trust the news context and when to ignore it (e.g., very quiet news day → low confidence → skip trade).

Target Brier Scores:
- LLM + news, crypto markets: < 0.12
- LLM + news, political markets: < 0.18
- LLM only (no news): < 0.20 (baseline)

---

### 4.6 — Cost Management

| Item | Cost | Notes |
|---|---|---|
| RSS feeds | $0 | Unlimited, no auth |
| NewsAPI (free tier) | $0 | 100 req/day; enough for ~50 markets/day |
| Reddit PRAW | $0 | Free with OAuth |
| sentence-transformers | $0 | Runs locally, ~80MB model |
| VADER sentiment | $0 | Runs locally, no API |
| Claude Sonnet (LLM call) | ~$0.003–0.006/call | Higher with news context (more tokens) |
| LLM keyword extraction | ~$0.001/market | Cached permanently per market |

**Daily cost estimate (50 active markets monitored, 10 trades/day):**
- LLM estimator calls: 10 × $0.005 = **$0.05/day**
- Keyword extraction (one-time per market): ~$0.05 total across all markets
- **Total API cost: ~$1.50/month**

**Budget controls in `config/settings.py`:**
```python
NEWS_LOOKBACK_HOURS = 48          # How far back to fetch articles
NEWS_MIN_RELEVANCE_SCORE = 0.35   # Discard articles below this threshold
NEWS_MAX_ARTICLES_PER_PROMPT = 5  # Cap context size to control token cost
LLM_CACHE_TTL_HOURS = 6           # Don't re-query same market within 6h
LLM_MIN_VOLUME_USD = 50_000       # Only use LLM on markets with >$50k volume
LLM_DAILY_SPEND_LIMIT_USD = 1.00  # Hard stop if daily API cost exceeds $1
USE_SEMANTIC_RELEVANCE = False     # Stage 2 relevance (enable when ready)
```

---

### MCP Usage in Phase 4

**Context7:**
- Before writing `news/sources/rss.py`: query `feedparser` docs for entry timestamp parsing and encoding edge cases
- Before writing `news/sources/reddit.py`: query `praw` docs for OAuth setup and subreddit streaming
- Before writing `llm/estimator.py`: query `anthropic` Python SDK docs for latest `messages.create()` API with `tool_use` and streaming
- Before writing `llm/calibration.py`: query `scikit-learn` docs for `CalibratedClassifierCV` and Platt scaling
- Before writing `news/relevance.py` (semantic stage): query `sentence-transformers` docs for `SentenceTransformer.encode()` batch usage

**LunarCrush** (first use in this project):
- Create `news/sources/lunarcrush.py` as a new source adapter
- For every crypto market identified by the strategy scanner, call `Topic` tool with the underlying asset (e.g., "bitcoin", "ethereum", "polymarket")
- Extract: `sentiment`, `posts_active`, `interactions`, `galaxy_score`, `alt_rank`
- Store extracted values into `news_features` table alongside RSS/NewsAPI features
- Call `Topic_Time_Series` during the calibration phase to retrieve historical social data for resolved crypto markets — use this to build the LunarCrush feature dataset for Brier Score comparison
- Call `Cryptocurrencies` with `sector="prediction"` to monitor Polymarket-related tokens and community sentiment

**New file**: `news/sources/lunarcrush.py`
```python
class LunarCrushSource:
    """
    Fetches real-time and historical social sentiment from LunarCrush MCP.
    Used for crypto-category Polymarket markets only.
    Falls back gracefully (returns None) if topic not found.
    """
    CRYPTO_CATEGORIES = ["crypto", "bitcoin", "ethereum", "defi", "nft"]

    def is_applicable(self, market_category: str, question: str) -> bool:
        """Returns True if this market has a LunarCrush-trackable asset."""

    def get_current_sentiment(self, topic: str) -> SentimentReading | None:
        """Calls LunarCrush Topic MCP tool. Returns normalized SentimentReading."""

    def get_historical_sentiment(self, topic: str, hours: int = 48) -> list[SentimentReading]:
        """Calls LunarCrush Topic_Time_Series MCP tool for backtest feature building."""
```

### Acceptance Criteria for Phase 4
- [ ] News fetcher collects articles from at least 3 sources without errors
- [ ] **LunarCrush source implemented** and returns valid sentiment for BTC/ETH topics
- [ ] **LunarCrush features stored** in `news_features` table for all crypto markets
- [ ] Lookahead bias test passes: no article timestamp after decision timestamp in any backtest run
- [ ] LLM estimator (with news) returns valid output for >95% of queries
- [ ] Calibration comparison shows LLM+news Brier Score lower than LLM-only baseline
- [ ] `NewsFeatures` stored to DB for every decision (even if not used in signal)
- [ ] Per-category prompt variants implemented for crypto and politics
- [ ] Value betting strategy backtested end-to-end; compared to Strategy 3C (statistical baseline)
- [ ] Daily LLM cost confirmed below $0.10 in paper trading mode
- [ ] Cost per trade computed; target < $0.01 total including API cost

---

## Phase 5 — Paper Trading

### Goal
Run the best-performing strategy in a live environment with real API connections, real market data, but zero real capital. Validate all infrastructure before committing money.

### Tasks

**5.1 — Live Data Pipeline**

File: `live/data_stream.py`

Connect to Polymarket CLOB WebSocket:
```
wss://ws-subscriptions-clob.polymarket.com/ws/market
```

Subscribe to price updates for target markets. Feed into the same `on_price_update()` strategy interface used in backtesting.

**5.2 — Paper Executor + Circuit Breaker**

File: `live/executor.py` (paper mode) + `live/circuit_breaker.py`

In paper mode:
- Receives `OrderRequest` (Pydantic model) from strategy — validated on receipt
- Simulates fill using current live orderbook (same fill model as backtest)
- Logs trades but does NOT send to blockchain
- Tracks paper portfolio in DB

**Circuit Breaker** (`live/circuit_breaker.py`) — implemented here, used in Phase 6:

The executor wraps all order submission calls with a `CircuitBreaker` instance (injected via constructor). States:

- `CLOSED` — normal operation, all orders go through
- `OPEN` — 3 consecutive failures detected; submissions blocked; `CRITICAL` log emitted; dashboard alert pushed; cooldown timer starts (default 300s from `config/settings.py`)
- `HALF_OPEN` — cooldown expired; one probe order allowed; if it succeeds → `CLOSED`; if it fails → back to `OPEN`

In paper mode the circuit breaker tracks simulated fill failures. In live mode (Phase 6) it tracks real CLOB API failures. The state machine logic is pure and fully unit-tested — the executor is the only class that reads `is_open()` and calls `record_success/failure()`.

```
config/settings.py keys:
  CIRCUIT_BREAKER_FAILURE_THRESHOLD = 3    # consecutive failures to open
  CIRCUIT_BREAKER_COOLDOWN_SECONDS  = 300  # wait before half-open probe
```

**5.3 — Real-time Monitor**

File: `live/monitor.py`

Terminal dashboard (using `rich`) showing:
- Current paper portfolio value
- Open positions with unrealized PnL
- Today's realized PnL
- Recent trades
- Current Sharpe (rolling 30-day)
- Risk alerts (if any risk rule is close to being violated)

**5.4 — Alerting**

- Email or Telegram notification if:
  - PnL drops > 2% in a day
  - Any risk rule is triggered
  - A new high-edge opportunity is detected

### Paper Trading Target
- Run for minimum 4 weeks
- Track all metrics daily
- Confirm strategy behavior matches backtest expectations (slippage, win rate, etc.)

### 5.3 — Live Dashboard (new)

File: `dashboard/app.py` + `dashboard/static/`

A browser-based live monitoring dashboard accessible at `http://localhost:8080`. Built with **FastAPI** (backend) and **Chart.js + vanilla JS** (frontend). Communicates via **WebSocket** for real-time push — no polling.

**Dashboard panels:**

| Panel | Data source | Update frequency |
|---|---|---|
| Portfolio value (equity curve) | `trades` table | On every fill |
| Open positions table | `trades` (open only) | Every 5s |
| Today's PnL (realized + unrealized) | `portfolio` state | Every 5s |
| Sharpe ratio (rolling 30-day) | computed from `trades` | Every 60s |
| Win rate (all-time + last 7 days) | computed from `trades` | Every 60s |
| Active strategy + mode (paper/live) | `config` | On change |
| Risk alerts feed | `risk_manager` events | Immediate push |
| Recent trades log (last 20) | `trades` table | On every fill |
| News sentiment heatmap (by category) | `news_features` table | Every 5 min |
| LunarCrush social gauge (crypto markets) | live LunarCrush MCP | Every 10 min |

**Architecture:**

```
bot container
  └── live/monitor.py
        └── publishes events → PostgreSQL (notify) or internal queue
              ↓
dashboard container
  └── dashboard/app.py (FastAPI)
        ├── GET  /api/snapshot    → full portfolio state (initial load)
        ├── GET  /api/trades      → recent trades list
        ├── GET  /api/metrics     → Sharpe, drawdown, win rate
        └── WS   /ws              → push stream (fills, alerts, PnL updates)
              ↓
        dashboard/static/dashboard.js
              └── WebSocket client → Chart.js live charts + DOM updates
```

**Key design rules for dashboard:**
- Dashboard is **read-only** — it never writes to the DB or calls the CLOB API
- Bot writes data → dashboard reads and displays; no shared state
- Dashboard container can be restarted at any time without affecting the bot
- All dashboard logic is in `dashboard/` — nothing in `live/` depends on dashboard code

**dashboard/Dockerfile:**
```dockerfile
FROM python:3.11-slim
WORKDIR /app
RUN pip install fastapi uvicorn asyncpg websockets
COPY dashboard/ .
EXPOSE 8080
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
```

**docker-compose.yml addition:**
```yaml
  dashboard:
    build:
      context: .
      dockerfile: dashboard/Dockerfile
    env_file: .env
    ports:
      - "8080:8080"
    depends_on:
      db:
        condition: service_healthy
    networks:
      - polymarket
```

### MCP Usage in Phase 5

**Context7:**
- Before writing `live/data_stream.py`: query `websockets` or `aiohttp` docs for reconnect-on-disconnect patterns
- Before writing `dashboard/app.py`: query `fastapi` docs for WebSocket endpoint patterns and `asyncpg` for PostgreSQL LISTEN/NOTIFY

**Blockscout** (first use in this project):
- After each paper trading session (even though no real money moves), use Blockscout to verify the wallet address is valid and the Polygon network is responsive
- Implement `live/monitor.py` balance reconciliation stub using `get_address_info` (chain_id="137") so the function is ready before Phase 6
- Use `lookup_token_by_symbol` with "USDC" to confirm the USDC.e contract address on Polygon mainnet

**LunarCrush:**
- In paper trading mode, the LunarCrush source runs live — verify it returns fresh data (< 1 hour old) on each strategy scan cycle
- Log LunarCrush API latency to confirm it fits within the bot's decision loop timing budget

### Acceptance Criteria for Phase 5
- [ ] Live data stream runs for 72 hours without crashing
- [ ] Paper trades execute and record correctly
- [ ] **Dashboard**: accessible at `http://localhost:8080` with all panels rendering
- [ ] **Dashboard**: equity curve updates in real time within 2s of a paper fill
- [ ] **Dashboard**: risk alert appears immediately when a risk rule fires in test
- [ ] **Circuit Breaker**: forcing 3 consecutive fill failures triggers `OPEN` state, blocks further submissions, and pushes alert to dashboard within 2s
- [ ] **Circuit Breaker**: after cooldown, state transitions to `HALF_OPEN` and a single probe is allowed
- [ ] **Circuit Breaker**: `CircuitBreaker` unit tests pass with 100% branch coverage
- [ ] **Blockscout**: `get_address_info` on the paper wallet returns valid Polygon data
- [ ] **LunarCrush**: live sentiment updates flowing into decisions for crypto markets
- [ ] Paper portfolio metrics (Sharpe, win rate) within 20% of backtest projections
- [ ] All risk rules fire correctly in test scenarios

---

## Phase 6 — Live Deployment

### Goal
Deploy with real capital. Start conservatively and scale position sizes only after proven live performance.

### Capital Allocation Plan

| Stage | Capital | Max Position | Duration |
|---|---|---|---|
| Stage 1 (bootstrap) | $500 | $25 (5%) | 4 weeks |
| Stage 2 (validate) | $2,000 | $100 (5%) | 4 weeks |
| Stage 3 (scale) | $5,000+ | $250 (5%) | Ongoing |

Never advance to next stage without:
- Positive Sharpe in current stage
- Max Drawdown < 15%
- No risk rules violated

### Live Executor

File: `live/executor.py` (live mode)

Uses `py-clob-client` to submit real orders:
```python
from py_clob_client import ClobClient

client = ClobClient(
    host="https://clob.polymarket.com",
    key=os.getenv("POLYMARKET_PRIVATE_KEY"),
    chain_id=137,  # Polygon
)
client.post_order(signed_order)
```

Pre-trade checks before every order:
1. Verify USDC balance is sufficient
2. Check position size vs. risk limits
3. Check daily loss limit not exceeded
4. Verify market is still active
5. Check slippage estimate is within tolerance

### Monitoring & Ops

- All live trades logged to database with timestamps and full fill details
- Daily performance email at 8am
- Telegram alerts for any anomalies
- Weekly strategy review: compare live Sharpe vs. backtest Sharpe
- Monthly: re-run backtest with new data to update strategy parameters

### MCP Usage in Phase 6

**Blockscout** (critical in live trading):
```python
# live/monitor.py — daily reconciliation job (runs via scheduler container)
def daily_reconciliation():
    """
    Runs every day at 00:05 UTC via APScheduler.
    Uses Blockscout MCP to verify on-chain state matches internal DB.
    """
    # 1. get_token_transfers_by_address(wallet, chain_id="137")
    #    → compare with trades table: flag any unrecorded transfers
    # 2. get_address_info(wallet, chain_id="137")
    #    → compare USDC.e balance with portfolio.cash_balance
    #    → alert if discrepancy > $0.50
    # 3. For each open position tx_hash in trades table:
    #    → get_transaction_info(tx_hash, chain_id="137")
    #    → confirm status == "ok" (not pending or failed)
```

**LunarCrush** (continuous signal in live):
- `Cryptocurrencies(sector="prediction", sort="sentiment")` — run every 4 hours to discover new Polymarket-adjacent sentiment shifts
- `Topic_Posts` — surface the most viral posts about a market's topic to optionally include as LLM context

**Context7:**
- Before any `py-clob-client` order submission code: re-query docs to confirm the signing flow hasn't changed (API evolves frequently)

### Acceptance Criteria for Phase 6 (Stage 1)
- [ ] Successfully deposit USDC to Polygon wallet
- [ ] First real trade executes and settles correctly
- [ ] **Blockscout**: daily reconciliation job runs and confirms on-chain balance matches DB
- [ ] **Blockscout**: all live trade tx hashes verified as confirmed (status "ok") within 5 minutes of submission
- [ ] **Circuit Breaker**: triggers correctly on real CLOB API timeout; live order submission resumes after cooldown without manual intervention
- [ ] **Pydantic**: `ValidationError` logged and trade skipped (never a bare crash) when CLOB returns unexpected response shape
- [ ] All risk controls work in live environment
- [ ] 4 weeks Stage 1 performance: Sharpe > 0.5, Max DD < 15%

---

## Technology Decisions Log

This section documents key technology choices and why they were made.

| Decision | Choice | Reason |
|---|---|---|
| **Runtime** | **Docker + Docker Compose** | Reproducible across WSL2 (dev) and Linux VPS (prod); no "works on my machine" problems; trivial to move to cloud |
| **Dev environment** | **WSL2 (Ubuntu 22 on Windows)** | Full Linux environment inside Windows; Docker Desktop integrates natively; no dual-boot needed |
| **Live dashboard** | **FastAPI + WebSocket + Chart.js** | Single container, zero extra infra, browser-accessible at localhost:8080; pushes live updates via WebSocket instead of polling |
| **Code style** | **Pure functions + DI** | Decoupled business logic is trivially testable and reusable across backtest and live modes without modification |
| **Data contracts** | **Pydantic v2 BaseModel** | Validation at layer boundaries catches malformed API responses before they corrupt strategy logic; free serialization for FastAPI dashboard |
| **Fault tolerance** | **Circuit Breaker (CLOSED/OPEN/HALF_OPEN)** | Prevents retry storms and duplicate orders during CLOB connectivity issues; auto-recovers after cooldown without manual restart |
| **Reproducibility** | **Global RANDOM_SEED via config** | Deterministic backtests enable debugging of unexpected results by re-running with identical seed; mandatory for any function using random or numpy.random |
| **Docs reference** | **Context7 MCP** | Always fetch live library docs before coding; prevents hallucinated APIs and outdated usage patterns |
| **Social sentiment** | **LunarCrush MCP** | Real-time social metrics for crypto topics unavailable via free RSS/NewsAPI; galaxy_score and alt_rank are proprietary signals |
| **On-chain audit** | **Blockscout MCP** | Direct Polygon blockchain access for balance reconciliation and trade verification; free, no API key needed |
| Backtest engine | Custom event-driven | NautilusTrader is powerful but complex for our initial needs; custom engine is easier to debug and extend |
| Data storage | PostgreSQL (via Docker) | Single DB from day 1; no SQLite→PG migration needed; runs in `db` container |
| LLM | Claude Sonnet | Best cost/performance for structured output; fallback to GPT-4o |
| CLOB client | py-clob-client (official) | Official support, actively maintained |
| Historical data | pmxt archive (free) | No cost for historical research; upgrade to PolymarketData if 1-min orderbook needed |
| Risk sizing | Fractional Kelly (25%) | Full Kelly is theoretically optimal but causes catastrophic drawdowns in practice; 25% provides safety margin |

---

## Open Research Questions

These are questions to answer through data analysis, not assumptions:

1. **Which market categories are most inefficient?** (crypto, politics, sports, AI)
2. **Does liquidity correlate with calibration quality?** (high-volume markets more efficient?)
3. **What is the average Brier Score of "market price as probability"?** (establishes the baseline we need to beat)
4. **How often does sum-to-one arbitrage appear, and what drives it?** (timing, category, market age)
5. **How long does market-making work before adverse selection kills the edge?** (market lifecycle analysis)
6. **Can the LLM's Brier Score beat the market's Brier Score on any category?**

---

## Immediate Next Steps (Start Here)

The first thing Claude Code should do is execute **Phase 0**. All subsequent phases run inside Docker.

```bash
# Step 1 — Create full project directory structure
mkdir -p data/{raw,processed,fetchers} \
         strategies \
         backtest \
         live \
         news/sources \
         llm/prompts \
         config \
         notebooks \
         tests \
         results \
         db

# Step 2 — Create Docker files:
#   Dockerfile                    (multi-stage: base, jupyter, scheduler, bot)
#   dashboard/Dockerfile          (FastAPI dashboard)
#   docker-compose.yml            (bot, db, dashboard, jupyter, scheduler)
#   docker-compose.prod.yml       (production overrides)
#   .dockerignore

# Step 3 — Create .env.example with all required variables

# Step 4 — Create db/init.sql with full schema

# Step 5 — Create requirements.txt with pinned versions

# Step 6 — Create config/settings.py (reads from .env via python-dotenv)

# Step 7 — Build and smoke test
docker compose build
docker compose up -d
docker compose run --rm bot python -c "print('container OK')"
docker compose run --rm bot pytest tests/ -v

# Step 8 — Only after Phase 0 acceptance criteria pass: begin Phase 1
```

**IMPORTANT**: Never write `pip install` commands for the host machine. All dependencies live in `requirements.txt` and are installed inside the Docker image at build time.

See `CLAUDE.md` for full project structure and all Docker commands reference.
