# Polymarket Trader Bot — Project Guide

## Project Overview

This project builds an automated, research-grade trading system for [Polymarket](https://polymarket.com) — a decentralized prediction market on the Polygon blockchain. The system is designed with three pillars: rigorous backtesting, quantitative strategy evaluation, and modular bot deployment.

The target is not just a working bot, but a **platform** from which multiple strategies can be tested, compared, and deployed safely.

---

## Code Quality Rules (Non-Negotiable)

These rules apply to every file written in this project. Claude Code must follow them without exception.

### 1. Pure Functions & Decoupled Classes

- **Every function must do exactly one thing.** If you need "and" to describe what it does, split it.
- **No side effects in computation functions.** Functions that calculate (metrics, Kelly, probability) must be pure: same input → same output, no I/O, no DB calls, no logging inside.
- **Separate I/O from logic.** Fetch data in one layer, transform in another, persist in a third. Never mix.
- **Dependency injection over global state.** Classes receive their dependencies (DB session, HTTP client, config) via constructor — never import them globally inside a function.
- **No circular imports.** Layer dependency direction is strict: `config` ← `data` ← `strategies` ← `backtest` ← `live`. Higher layers never import lower ones in reverse.

```python
# ✅ CORRECT — pure, testable, no side effects
def compute_kelly_fraction(p_win: float, odds: float) -> float:
    q_lose = 1.0 - p_win
    return (p_win * odds - q_lose) / odds

def compute_sharpe(returns: list[float], risk_free: float = 0.0) -> float:
    import numpy as np
    excess = np.array(returns) - risk_free
    return float(excess.mean() / excess.std()) if excess.std() > 0 else 0.0

# ❌ WRONG — mixes computation with I/O and side effects
def compute_and_save_sharpe(strategy_id: str, db):
    trades = db.query(f"SELECT * FROM trades WHERE strategy='{strategy_id}'")
    returns = [t.pnl for t in trades]
    sharpe = sum(returns) / len(returns)  # wrong formula + impure
    db.execute(f"UPDATE strategies SET sharpe={sharpe}")  # side effect inside computation
    print(f"Sharpe saved: {sharpe}")      # logging inside computation
    return sharpe
```

### 2. Typing & Data Contracts (Pydantic v2)

- All function signatures must have type hints: parameters and return type.
- Use **Pydantic `BaseModel`** for all data structures that cross layer boundaries (API responses, DB rows surfaced to strategies, LLM outputs, order requests, trade records). No raw dicts between layers.
- Use `@dataclass` only for internal, purely in-memory structures that never touch I/O or cross module boundaries.
- Use `Optional[T]` (or `T | None`) explicitly — never return `None` silently without declaring it.
- Pydantic models provide free validation, serialization, and FastAPI integration — do not duplicate this logic manually.

```python
# ✅ CORRECT — Pydantic model for data crossing layer boundaries
from pydantic import BaseModel, field_validator

class OrderRequest(BaseModel):
    strategy: str
    condition_id: str
    token_id: str
    side: Literal["BUY", "SELL"]
    size_usd: float
    limit_price: float | None = None

    @field_validator("size_usd")
    @classmethod
    def size_must_be_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("size_usd must be positive")
        return v

class ProbabilityEstimate(BaseModel):
    probability: float          # 0.0–1.0
    confidence: Literal["LOW", "MEDIUM", "HIGH"]
    reasoning: str
    sources_used: list[str] = []

# ❌ WRONG — raw dict crossing module boundary
def build_order(market_data: dict) -> dict:
    return {"side": "BUY", "size": market_data["size"]}  # no validation, no type safety
```

**Schemas that must be Pydantic models** (defined in `config/schemas.py`):
- `Article` — news pipeline output
- `SentimentReading` — LunarCrush / VADER output
- `NewsFeatures` — computed feature vector per market
- `ProbabilityEstimate` — LLM estimator output
- `OrderRequest` — strategy → executor
- `OrderFill` — executor → portfolio
- `Trade` — persisted trade record
- `PortfolioSnapshot` — portfolio state at a point in time
- `BacktestMetrics` — full metrics report
- `ReconciliationReport` — Blockscout on-chain audit result

### 3. Error Handling & Circuit Breaker

- Never use bare `except:` or `except Exception:` to swallow errors silently.
- External API calls (CLOB, LLM, news sources, Blockscout) must be wrapped with specific exception types and logged.
- Use `Result` pattern or explicit `raise` — never return `None` to signal failure in functions that should return data.
- **The executor must implement a circuit breaker.** If 3 consecutive order submissions fail (network error, auth error, or timeout), the executor automatically enters a `OPEN` state: it stops submitting orders, logs a `CRITICAL` alert, notifies the dashboard, and waits `CIRCUIT_BREAKER_COOLDOWN_SECONDS` (default: 300s) before retrying. This prevents runaway retry loops and potential duplicate orders during connectivity issues.

```python
# live/circuit_breaker.py — injectable, pure state machine
from enum import Enum

class CircuitState(Enum):
    CLOSED = "closed"      # Normal operation
    OPEN   = "open"        # Fault detected — submissions blocked
    HALF   = "half_open"   # Testing recovery — one probe allowed

@dataclass
class CircuitBreaker:
    failure_threshold: int   = 3      # consecutive failures to open
    cooldown_seconds:  int   = 300    # seconds before attempting recovery
    state:             CircuitState = CircuitState.CLOSED
    failure_count:     int   = 0
    opened_at:         float | None = None

    def record_success(self) -> None: ...
    def record_failure(self) -> None: ...
    def is_open(self) -> bool: ...
    def can_attempt(self) -> bool: ...
```

**Circuit breaker governs only `live/executor.py`** — backtest and paper trading modes are exempt.

### 4. Testing

- Every pure function in `backtest/metrics.py`, `backtest/fill_model.py`, and `strategies/` must have a unit test.
- Tests must not hit the network, DB, or filesystem. Use fixtures and mocks.
- Target: 80%+ coverage on `backtest/` and `strategies/` modules before Phase 3 ends.

### 5. Configuration & Reproducibility

- Zero hardcoded values in business logic. Every threshold, limit, URL, and parameter lives in `config/settings.py` or `config/strategies.yaml`.
- Settings are loaded once at startup and injected. Never call `os.getenv()` inside strategy or backtest code.
- **All random seeds must be set via config and applied at startup.** Any function that uses `random`, `numpy.random`, `torch`, or any other source of randomness must receive its seed through `config/settings.py → RANDOM_SEED`. This is mandatory for backtest reproducibility — the same seed must always produce identical results.

```python
# config/settings.py
RANDOM_SEED: int = 42  # Set in .env; applies to all random operations

# backtest/engine.py — apply at engine init, never inside strategy logic
import random, numpy as np
random.seed(settings.RANDOM_SEED)
np.random.seed(settings.RANDOM_SEED)
```

- When a backtest produces an unexpected result, the first debugging step is always re-running with the same `RANDOM_SEED`. If results differ, there is an unseeded random call — find and fix it before proceeding.

### 6. Logging

- Use `loguru` exclusively. No `print()` statements in production code.
- Log levels: `DEBUG` for tick-by-tick data, `INFO` for decisions and trades, `WARNING` for risk alerts, `ERROR` for recoverable failures, `CRITICAL` for halt conditions.
- Every log entry in live trading must include: `strategy`, `market_id`, `timestamp`, `action`.

---

## Docker-First Architecture

This project runs entirely inside Docker containers. There is no local Python environment — all development, testing, backtesting, and live trading happens inside containers. This ensures reproducibility across environments (WSL on Windows, Linux VPS, CI).

**Container topology** (defined in `docker-compose.yml`):

| Service | Image | Role |
|---|---|---|
| `bot` | `./Dockerfile` | Main Python app: strategies, backtest engine, live trading |
| `db` | `postgres:16-alpine` | Primary database |
| `dashboard` | `./dashboard/Dockerfile` | FastAPI + WebSocket live monitoring dashboard (port 8080) |
| `jupyter` | `./Dockerfile` (jupyter profile) | Notebooks for research and analysis (port 8888) |
| `scheduler` | `./Dockerfile` (scheduler profile) | APScheduler process for cron-style tasks |

In development (WSL), `docker compose up` starts all services. In production (VPS), only `bot`, `db`, and `scheduler` run.

**Volume mounts** (data persists on the host):
```
./data/raw        → /app/data/raw       (downloaded Parquet files)
./data/processed  → /app/data/processed (cleaned datasets)
./results         → /app/results        (backtest outputs, plots)
./.env            → /app/.env           (secrets, never in image)
```

---

## Repository Structure

```
polymarket-trader-bot/
├── CLAUDE.md                  # This file — always read first
├── PLAN.md                    # Full phased development plan
├── README.md                  # User-facing documentation
│
├── Dockerfile                 # Multi-stage image (base, jupyter, scheduler)
├── docker-compose.yml         # All services: bot, db, jupyter, scheduler
├── docker-compose.prod.yml    # Production overrides (no jupyter, restart policies)
├── .env.example               # Template for secrets — copy to .env, never commit
├── .dockerignore              # Exclude data/, results/, .env from image
│
├── data/                      # Data layer
│   ├── raw/                   # Raw downloads (CLOB snapshots, Parquet files)
│   ├── processed/             # Cleaned, feature-engineered datasets
│   └── fetchers/              # Scripts to pull data from APIs
│       ├── gamma_fetcher.py   # Polymarket Gamma API (market metadata)
│       ├── clob_fetcher.py    # CLOB API (orderbook, prices, trades)
│       └── pmxt_fetcher.py    # pmxt archive (free historical snapshots)
│
├── strategies/                # Strategy implementations
│   ├── base_strategy.py       # Abstract base class all strategies inherit
│   ├── sum_to_one_arb.py      # Sum-to-one arbitrage
│   ├── market_maker.py        # Passive market making / spread capture
│   ├── value_betting.py       # LLM-assisted mispricing detection
│   └── momentum.py            # Trend-following on price series
│
├── backtest/                  # Backtesting engine
│   ├── engine.py              # Event-driven backtest loop
│   ├── portfolio.py           # Portfolio state tracker (positions, PnL)
│   ├── fill_model.py          # Order fill simulation (slippage, liquidity)
│   └── metrics.py             # Sharpe, Brier, drawdown, Kelly, win rate
│
├── live/                      # Live trading layer
│   ├── executor.py            # Order execution via py-clob-client
│   ├── risk_manager.py        # Pre-trade and position-level risk checks
│   └── monitor.py             # Exposes metrics to Prometheus (scraped by dashboard)
│
├── dashboard/                 # Live monitoring web dashboard
│   ├── app.py                 # FastAPI app — REST + WebSocket endpoints
│   ├── static/                # HTML/CSS/JS frontend (single-page, no framework)
│   │   ├── index.html         # Main dashboard page
│   │   ├── style.css
│   │   └── dashboard.js       # WebSocket client + Chart.js charts
│   └── Dockerfile             # Lightweight image (python:3.11-slim + fastapi)
│
├── news/                      # News pipeline — context for LLM estimator
│   ├── fetcher.py             # Collect articles from RSS, NewsAPI, Reddit
│   ├── relevance.py           # Match articles to a market question (keyword + semantic)
│   ├── store.py               # SQLite cache with TTL per article
│   ├── sentiment.py           # Numeric sentiment features (VADER, local)
│   └── sources/               # Per-source adapters
│       ├── rss.py             # Generic RSS parser (Reuters, AP, CoinDesk, BBC…)
│       ├── newsapi.py         # NewsAPI.org wrapper (free tier)
│       └── reddit.py          # Reddit PRAW wrapper
│
├── llm/                       # LLM integration for probability estimation
│   ├── estimator.py           # Query LLM with news context, parse output
│   ├── prompt_builder.py      # Format prompt with news context + market data
│   ├── response_parser.py     # Extract probability, confidence, sources from LLM output
│   ├── decision_engine.py     # Compare LLM probability to market price → trade signal
│   ├── cache.py               # SQLite-backed cache with TTL (avoid re-querying)
│   ├── prompts/               # Prompt templates by market category
│   │   ├── base.txt           # Default template
│   │   ├── crypto.txt         # Crypto-specific (on-chain data, technicals)
│   │   ├── politics.txt       # Politics-specific (polling, electoral history)
│   │   ├── sports.txt         # Sports-specific (stats, injuries, h2h)
│   │   └── ai_tech.txt        # AI/Tech markets (product releases, regulation)
│   └── calibration.py        # Platt scaling calibration; compare LLM vs LLM+news
│
├── config/
│   ├── settings.py            # Environment variables, API keys, constants
│   └── strategies.yaml        # Per-strategy parameters (Kelly fraction, etc.)
│
├── notebooks/
│   ├── 01_data_exploration.ipynb
│   ├── 02_strategy_analysis.ipynb
│   └── 03_backtest_results.ipynb
│
└── tests/
    ├── test_metrics.py
    ├── test_engine.py
    └── test_strategies.py
```

---

## Key Concepts (Always Keep in Mind)

### How Polymarket Works
- Markets are binary: YES or NO tokens, each priced $0–$1
- Price implies probability: YES @ $0.65 = 65% chance the event happens
- Settlement: winning token pays $1, losing token pays $0
- All settlement in USDC.e on Polygon blockchain
- Trading engine is a CLOB (Central Limit Order Book), like a real exchange

### Core APIs
| API | Base URL | Purpose |
|---|---|---|
| Gamma API | `https://gamma-api.polymarket.com` | Market metadata, categories, search |
| CLOB API | `https://clob.polymarket.com` | Orderbook, prices, trade history, order placement |
| CLOB WebSocket | `wss://ws-subscriptions-clob.polymarket.com/ws/` | Real-time price/trade streams |
| Data API | `https://data-api.polymarket.com` | Aggregated market data |

### Python SDKs
```bash
pip install py-clob-client       # Official Polymarket CLOB client
pip install polymarket-apis      # Unofficial wrapper (CLOB + Gamma + WS + GraphQL)
```

### Free Historical Data
- **pmxt archive**: https://archive.pmxt.dev — hourly orderbook snapshots in Parquet format (free)
- **Official timeseries**: `GET /prices-history` on CLOB API
- **Commercial**: PolymarketData.co — 1-min L2 orderbook depth (paid)

---

## Strategies Overview

| Strategy | Edge | Complexity | Risk |
|---|---|---|---|
| Sum-to-one Arbitrage | Risk-free when YES+NO < $1 | Low | Very Low (but rare) |
| Market Making | Earn spread on liquid markets | Medium | Medium (inventory risk) |
| Value Betting (LLM) | Find mispriced markets via AI | High | High (model risk) |
| Momentum | Ride price trends to resolution | Medium | Medium |
| Whale Tracking | Copy profitable wallets | Low | Medium (lag risk) |

---

## Key Metrics

Every strategy MUST be evaluated on all of these:

```
Brier Score       = mean((p_hat - outcome)^2)         # Calibration quality [0-1, lower=better]
Sharpe Ratio      = (mean_return - rf) / std_return   # Risk-adjusted return [>1.5 = good]
Sortino Ratio     = (mean_return - rf) / downside_std # Penalizes only downside [>2 = good]
Max Drawdown      = max(peak - trough) / peak         # Worst loss from peak [lower=better]
Win Rate          = wins / total_trades               # % of profitable trades
Expected Value    = sum(p_i * payoff_i)              # Must be > 0 per trade
Kelly Fraction    = (p*b - q) / b                    # Optimal bet size [use 25% = fractional]
CAGR              = (final / initial)^(1/years) - 1  # Compound annual growth rate
```

---

## Risk Rules (Non-Negotiable)

These rules are enforced by `live/risk_manager.py` and must never be disabled:

1. **Max single position**: 5% of total capital (configurable, never above 10%)
2. **Kelly cap**: Never bet more than 25% of Kelly optimal (fractional Kelly)
3. **Min market liquidity**: Only trade markets with >$10k volume
4. **Min edge**: Only enter if estimated edge > 3% (after fees)
5. **Max open positions**: 20 simultaneous positions
6. **Daily loss limit**: Auto-halt if daily PnL drops below -5% of capital
7. **Correlation limit**: Don't hold >3 correlated positions (same topic/category)

---

## Development Commands

All commands run inside Docker. Never install Python packages or run scripts directly on the host.

```bash
# ── First-time setup (WSL or Linux) ──────────────────────────────────────────
cp .env.example .env           # Fill in API keys
docker compose build           # Build images (~3–5 min first time)
docker compose up -d           # Start all services (bot, db, jupyter)

# ── Day-to-day development ───────────────────────────────────────────────────
docker compose up -d           # Start everything
docker compose logs -f bot     # Tail bot logs
docker compose down            # Stop everything

# ── Run a one-off command inside the bot container ───────────────────────────
docker compose run --rm bot python data/fetchers/pmxt_fetcher.py --start 2024-01-01 --end 2024-12-31
docker compose run --rm bot python backtest/engine.py --strategy sum_to_one_arb --start 2024-01-01 --end 2024-06-01
docker compose run --rm bot python backtest/metrics.py --results results/backtest_20240101.json
docker compose run --rm bot pytest tests/ -v

# ── Jupyter notebooks (research) ─────────────────────────────────────────────
# Jupyter runs at http://localhost:8888 (token in .env or logs)
docker compose up jupyter

# ── Paper trading ────────────────────────────────────────────────────────────
docker compose run --rm bot python live/executor.py --mode paper --strategy market_maker

# ── Live trading (production) ────────────────────────────────────────────────
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d

# ── Database shell ───────────────────────────────────────────────────────────
docker compose exec db psql -U polymarket -d polymarket_bot

# ── Rebuild after requirements change ────────────────────────────────────────
docker compose build --no-cache bot

# ── Dashboard ─────────────────────────────────────────────────────────────────
# Dashboard runs at http://localhost:8080
docker compose up dashboard
```

---

## Environment Variables

```bash
# .env (never commit this file)
POLYMARKET_API_KEY=
POLYMARKET_API_SECRET=
POLYMARKET_API_PASSPHRASE=
POLYMARKET_PRIVATE_KEY=          # Polygon wallet private key
POLYGON_RPC_URL=                  # e.g., Alchemy endpoint
ANTHROPIC_API_KEY=                # For LLM value betting strategy
OPENAI_API_KEY=                   # Optional: multi-model ensemble
```

---

## Technology Stack

| Layer | Technology |
|---|---|
| Language | Python 3.11+ |
| **Containerization** | **Docker + Docker Compose (WSL2 on Windows → VPS on prod)** |
| Blockchain | Polygon (MATIC) via Alchemy or Infura |
| CLOB client | `py-clob-client` (official) |
| Backtest engine | Custom event-driven engine (inside `bot` container) |
| Data storage | PostgreSQL 16 (via `db` container) |
| LLM | Claude API (Anthropic) primary + OpenAI fallback |
| News sources | RSS (feedparser), NewsAPI.org (free), Reddit (PRAW) |
| Sentiment scoring | VADER (local, free) — no API required |
| Semantic relevance | sentence-transformers / all-MiniLM-L6-v2 (local, ~80MB) |
| Scheduling | APScheduler (inside `scheduler` container) |
| Notebooks | JupyterLab (inside `jupyter` container) |
| **Dashboard** | **FastAPI + WebSocket + Chart.js (live, browser-based, port 8080)** |
| Monitoring | loguru (structured logs); Prometheus metrics exposed by bot |
| Testing | pytest + hypothesis (run inside `bot` container) |

---

## MCP Integrations (Claude Code)

Three MCPs are connected to this project and must be used by Claude Code at the appropriate phases. Never call external APIs for things these MCPs already provide.

---

### 1. Context7 — Library Documentation
**Use in**: Every phase, from Phase 0 onwards.

Before writing any code that uses an external library, always resolve the library ID and query its current documentation. This prevents hallucinated APIs and outdated usage patterns.

```
# Workflow for Claude Code:
1. Call resolve-library-id with the library name (e.g., "py-clob-client", "sqlalchemy", "feedparser")
2. Call query-docs with the returned ID and a specific question
3. Use the documented API — never guess method signatures
```

**Libraries to always look up before coding:**
- `py-clob-client` — CLOB order placement, authentication, orderbook methods
- `sqlalchemy` — async session management, PostgreSQL-specific features
- `feedparser` — RSS feed parsing edge cases
- `sentence-transformers` — model loading, encode() API
- `apscheduler` — scheduler configuration inside Docker
- `docker` / `docker-compose` — Dockerfile syntax if in doubt
- `anthropic` — latest Claude API call patterns, tool_use format

---

### 2. LunarCrush — Social Sentiment Data
**Use in**: Phase 4 (News Pipeline), Phase 5–6 (live signal enrichment).

LunarCrush provides real-time social media sentiment and engagement metrics for crypto assets and topics. This is a premium signal for Polymarket crypto markets that RSS/NewsAPI cannot replicate.

**Available tools:**
- `Topic` — full social metrics for any topic, keyword, or crypto symbol (e.g., "bitcoin", "polymarket", "donald trump")
- `Topic_Posts` — most recent social posts for a topic
- `Topic_Time_Series` — hourly social metrics time series
- `Cryptocurrencies` — ranked list of crypto by sentiment, galaxy score, alt rank, etc.
- `Search` — search across all social topics

**Integration points in code:**

```python
# news/sources/lunarcrush.py  (new source adapter — Phase 4)
# Fetches social sentiment for a market's underlying asset

class LunarCrushSource:
    """
    Uses LunarCrush MCP to fetch:
    - sentiment score (-1 to +1)
    - social volume (posts_active)
    - engagement (interactions)
    - galaxy_score (overall asset health)
    for crypto-related Polymarket markets.
    """

    def get_sentiment(self, topic: str) -> SentimentReading:
        # → call LunarCrush Topic tool via MCP
        # → extract: sentiment, posts_active, interactions, galaxy_score
        # → normalize to NewsFeatures schema

    def get_time_series(self, topic: str, hours: int = 48) -> list[SentimentReading]:
        # → call LunarCrush Topic_Time_Series tool
        # → returns hourly sentiment history for backtesting
```

**When to call LunarCrush vs. RSS:**
- Crypto markets (BTC, ETH price predictions, exchange-related) → LunarCrush
- Politics / sports / general events → RSS + NewsAPI
- Any market with a clear on-chain/social asset → LunarCrush as primary, RSS as secondary

**NewsFeatures fields populated by LunarCrush:**
```
avg_sentiment_score   ← LunarCrush sentiment (normalized)
article_count_24h     ← posts_active (social volume proxy)
article_count_delta   ← posts_active delta vs. prior 24h
```

---

### 3. Blockscout — Polygon Blockchain Explorer
**Use in**: Phase 5 (paper trading audit), Phase 6 (live trading verification).

Blockscout gives direct on-chain visibility into the Polygon network. Use it to verify that orders executed correctly, audit wallet balances, and track USDC transfers.

**Available tools:**
- `get_address_info` — balance, contract status, first tx date for any address
- `get_transactions_by_address` — full tx history for a wallet
- `get_token_transfers_by_address` — USDC transfer history (critical for PnL audit)
- `get_transaction_info` — detailed info for a specific tx hash
- `get_block_info` / `get_block_number` — current chain state
- `lookup_token_by_symbol` — resolve USDC.e contract address on Polygon
- `read_contract` — read state from any verified Polygon contract
- `get_chains_list` — verify Polygon chain ID (137)

**Integration points in code:**

```python
# live/monitor.py — on-chain balance reconciliation (Phase 5+)
# After every live session, call Blockscout to verify:
# 1. Wallet USDC.e balance matches internal portfolio tracker
# 2. All submitted tx hashes confirmed on-chain
# 3. No unexpected token transfers (security check)

def reconcile_onchain_balance(wallet_address: str) -> ReconciliationReport:
    # → Blockscout get_token_transfers_by_address (chain_id="137")
    # → compare with trades table in DB
    # → flag any discrepancy > $0.10
```

**Polygon chain ID**: always use `"137"` in Blockscout tool calls.

**USDC.e contract on Polygon**: `0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174`

---

## Current Phase

**PHASE 0 — Docker & Infrastructure**

See `PLAN.md` for the full phased roadmap.
