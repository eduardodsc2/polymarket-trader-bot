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

    # ── News ─────────────────────────────────────────────────────────────────
    newsapi_key: str = ""
    reddit_client_id: str = ""
    reddit_client_secret: str = ""
    reddit_user_agent: str = "polymarket-bot/1.0"

    # ── Database ─────────────────────────────────────────────────────────────
    db_password: str = "changeme"
    database_url: str = "postgresql+asyncpg://polymarket:changeme@db:5432/polymarket_bot"
    database_url_sync: str = "postgresql+psycopg2://polymarket:changeme@db:5432/polymarket_bot"

    # ── Jupyter ──────────────────────────────────────────────────────────────
    jupyter_token: str = "changeme"

    # ── Bot config ────────────────────────────────────────────────────────────
    bot_mode: str = "paper"               # paper | live
    initial_capital_usd: float = 500.0

    # ── Reproducibility ───────────────────────────────────────────────────────
    random_seed: int = 42

    # ── Circuit breakers ──────────────────────────────────────────────────────
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

    # ── Risk rules ────────────────────────────────────────────────────────────
    min_market_volume_usd: float = 10_000.0
    min_edge_pct: float = 0.03           # Only enter if estimated edge > 3%
    kelly_fraction: float = 0.25         # Fractional Kelly (25% of full Kelly)
    max_correlated_positions: int = 3    # Same topic/category


settings = Settings()
