from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Polymarket ────────────────────────────────────────────────────────────
    polymarket_api_key: str = ""
    polymarket_api_secret: str = ""
    polymarket_api_passphrase: str = ""
    polymarket_private_key: str = ""

    # ── Blockchain ────────────────────────────────────────────────────────────
    polygon_rpc_url: str = ""

    # ── LLM ──────────────────────────────────────────────────────────────────
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    llm_daily_spend_limit_usd: float = 1.00
    llm_model: str = "claude-haiku-4-5-20251001"  # Haiku: 8x cheaper than Sonnet, sufficient for binary probability estimation

    # ── News ─────────────────────────────────────────────────────────────────
    newsapi_key: str = ""
    reddit_client_id: str = ""
    reddit_client_secret: str = ""
    reddit_user_agent: str = "polymarket-bot/1.0"

    # ── News pipeline (Phase 4) ───────────────────────────────────────────────
    news_lookback_hours: int = 48
    news_min_relevance_score: float = 0.35
    news_max_articles_per_prompt: int = 3   # was 5 — fewer articles = fewer input tokens
    use_semantic_relevance: bool = False
    llm_news_skip_below_hours: float = 0.5  # skip news fetch for markets resolving in < 30min (no relevant articles exist)
    llm_max_resolution_hours: float = 0.25  # only evaluate markets resolving within this window (0.25 = 15min)

    # ── LLM pipeline (Phase 4) ───────────────────────────────────────────────
    llm_cache_ttl_hours: int = 12          # was 6 — stable markets don't change in 12h
    llm_min_volume_usd: float = 50_000.0

    # ── Database ─────────────────────────────────────────────────────────────
    db_password: str = "changeme"
    database_url: str = "postgresql+asyncpg://polymarket:changeme@db:5432/polymarket_bot"
    database_url_sync: str = "postgresql+psycopg2://polymarket:changeme@db:5432/polymarket_bot"

    # ── Jupyter ──────────────────────────────────────────────────────────────
    jupyter_token: str = "changeme"

    # ── Bot config ────────────────────────────────────────────────────────────
    bot_mode: str = "paper"               # paper | live
    paper_strategy: str = "market_maker"  # market_maker | sum_to_one_arb | calibration_betting
    initial_capital_usd: float = 500.0

    # ── Reproducibility ───────────────────────────────────────────────────────
    random_seed: int = 42

    # ── Circuit breaker (order executor) ─────────────────────────────────────
    circuit_breaker_failure_threshold: int = Field(
        default=3,
        description="Consecutive fill failures before entering OPEN state",
    )
    circuit_breaker_cooldown_seconds: int = Field(
        default=300,
        description="Seconds to wait in OPEN state before moving to HALF_OPEN",
    )

    # ── Risk circuit breakers ─────────────────────────────────────────────────
    circuit_breaker_daily_loss_pct: float = Field(
        default=0.05,
        description="Auto-halt if daily PnL drops below this fraction of capital",
    )
    circuit_breaker_max_positions: int = Field(
        default=20,
        description="Maximum number of simultaneous open positions",
    )
    circuit_breaker_max_position_pct: float = Field(
        default=0.05,
        description="Maximum single position size as a fraction of total capital",
    )

    # ── WebSocket (live data stream) ──────────────────────────────────────────
    clob_ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    ws_reconnect_delay_seconds: float = 5.0
    ws_ping_interval_seconds: float = 30.0
    ws_stall_timeout_seconds: float = 90.0  # reconecta se nenhum tick em 90s

    # ── Dashboard ─────────────────────────────────────────────────────────────
    dashboard_db_url: str = "postgresql+asyncpg://polymarket:changeme@db:5432/polymarket_bot"

    # ── Alerting (Telegram) ───────────────────────────────────────────────────
    telegram_bot_token: str = ""
    telegram_chat_id: str   = ""
    daily_pnl_alert_threshold: float = 0.02  # alert if day PnL drops > 2%
    telegram_report_interval_minutes: int = 30  # periodic portfolio report cadence

    # ── Risk rules ────────────────────────────────────────────────────────────
    min_market_volume_usd: float = 10_000.0
    min_edge_pct: float = 0.03           # Only enter if estimated edge > 3%
    kelly_fraction: float = 0.25         # Fractional Kelly (25% of full Kelly)
    max_correlated_positions: int = 3    # Same topic/category


settings = Settings()
