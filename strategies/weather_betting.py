"""
Weather market edge detection strategy.

Edge source: Open-Meteo ensemble forecasts systematically outperform
single-app forecasts used by most Polymarket weather traders.

Polymarket weather markets are fee-free (since March 2026), removing
the main cost drag that affects other strategies.

Flow per price tick:
  1. Filter: is this a temperature market? (is_weather_market)
  2. Filter: already entered this condition_id? (deduplication)
  3. Identify city from market question
  4. Fetch forecast (in-memory cache, TTL 6h — Open-Meteo updates every 6h)
  5. Parse buckets from market question (parse_weather_buckets)
  6. Compute Monte Carlo probability per bucket (compute_bucket_probability)
  7. select_ladder_legs → legs with sufficient edge
  8. compute_kelly_size per leg
  9. Return list[OrderRequest]

All computation functions are pure — same input, same output, no I/O.
Only fetch_daily_max_forecast() performs I/O (injected http_client).
"""
from __future__ import annotations

import math
import time
import uuid
from datetime import date, datetime, timezone
from typing import Any

from loguru import logger

import numpy as np

from backtest.events import MarketResolutionEvent, PriceUpdateEvent
from config.schemas import Market, OrderRequest, PortfolioSnapshot, WeatherBucket, WeatherForecast
from config.settings import Settings
from data.fetchers.weather_fetcher import (
    build_weather_forecast,
    fetch_daily_max_forecast,
    parse_weather_buckets,
)
from strategies.base_strategy import BaseStrategy


# ── Pure functions ────────────────────────────────────────────────────────────

def is_weather_market(question: str) -> bool:
    """
    Pure. True if this Polymarket question is about temperature.

    Checks for °C, °F, or temperature/high temp keywords in the question.
    """
    q = question.lower()
    return any(kw in q for kw in ("°c", "°f", "temperature", "high temp", "max temp"))


def compute_bucket_probability(
    lo: float,
    hi: float,
    mu: float,
    sigma: float,
    n_samples: int = 50_000,
    random_seed: int = 42,
) -> float:
    """
    Pure. Monte Carlo estimate of P(lo <= daily_max < hi).

    Models daily maximum temperature as N(mu, sigma).

    Args:
        lo:           Lower bound (°C), inclusive.
        hi:           Upper bound (°C), exclusive. Use math.inf for last bucket.
        mu:           Point forecast (°C).
        sigma:        Forecast uncertainty (°C).
        n_samples:    Monte Carlo sample count.
        random_seed:  From config — must be set via settings.random_seed.

    Returns:
        Probability in [0.0, 1.0].
    """
    rng = np.random.default_rng(random_seed)
    samples = rng.normal(mu, sigma, size=n_samples)
    hi_val = hi if not math.isinf(hi) else float("inf")
    return float(((samples >= lo) & (samples < hi_val)).mean())


def normalize_bucket_probabilities(probs: list[float]) -> list[float]:
    """
    Pure. Normalize bucket probabilities to sum to 1.0.

    Raises:
        ValueError: if sum of probs is zero or negative.
    """
    total = sum(probs)
    if total <= 0:
        raise ValueError(f"Sum of bucket probabilities must be positive, got {total}")
    return [p / total for p in probs]


def compute_edge(model_prob: float, market_price: float) -> float:
    """
    Pure. Edge = model probability minus market-implied probability.

    Positive edge means model thinks the bucket is underpriced.
    No fee adjustment needed — weather markets are fee-free.
    """
    return model_prob - market_price


def select_ladder_legs(
    buckets: list[WeatherBucket],
    min_edge_pct: float,
    max_legs: int,
    min_price: float,
    max_price: float,
) -> list[WeatherBucket]:
    """
    Pure. Select which buckets to trade from a market.

    Rules:
    - Only buckets where edge > min_edge_pct / 100
    - Only buckets where min_price <= market_price <= max_price
    - At most max_legs buckets, ordered by edge descending

    Returns:
        Filtered and sorted list of WeatherBucket (may be empty).
    """
    min_edge = min_edge_pct / 100.0
    candidates = [
        b for b in buckets
        if b.edge > min_edge and min_price <= b.market_price <= max_price
    ]
    candidates.sort(key=lambda b: b.edge, reverse=True)
    return candidates[:max_legs]


def compute_kelly_size(
    edge: float,
    market_price: float,
    kelly_fraction: float,
    capital_usd: float,
    max_position_usd: float,
) -> float:
    """
    Pure. Fractional Kelly position sizing for a binary $0/$1 outcome.

    Kelly formula for binary:
        odds = (1 / market_price) - 1   (net payout per dollar risked)
        kelly = (p_win * odds - q_lose) / odds
        size  = kelly_fraction * kelly * capital_usd

    Returns:
        Position size in USD, capped at max_position_usd. Zero if Kelly <= 0.
    """
    if market_price <= 0.0 or market_price >= 1.0:
        return 0.0

    p_win = market_price + edge
    q_lose = 1.0 - p_win
    odds = (1.0 / market_price) - 1.0

    if odds <= 0.0:
        return 0.0

    kelly_raw = (p_win * odds - q_lose) / odds
    if kelly_raw <= 0.0:
        return 0.0

    size = kelly_fraction * kelly_raw * capital_usd
    return min(size, max_position_usd)


def _identify_city(
    question: str,
    cities: list[dict],
) -> dict | None:
    """
    Pure. Find the city config whose polymarket_station appears in the question.
    Returns None if no city matches.
    """
    q_lower = question.lower()
    for city in cities:
        station = city.get("polymarket_station", "")
        if station.lower() in q_lower or city.get("name", "").lower() in q_lower:
            return city
    return None


# ── Strategy class ────────────────────────────────────────────────────────────

_FORECAST_CACHE_TTL_SECONDS = 6 * 3600  # 6h — Open-Meteo updates every 6h


class WeatherBettingStrategy(BaseStrategy):
    """
    Weather market edge detection via meteorological model vs Polymarket prices.

    Args:
        market_data:  condition_id → Market metadata.
        http_client:  requests.Session (or compatible) instance for Open-Meteo calls.
        settings:     Injected Settings instance.
    """

    name = "weather_betting"

    def __init__(
        self,
        market_data: dict[str, Market],
        http_client: Any,
        settings: Settings,
    ) -> None:
        self._market_data = market_data
        self._http_client = http_client
        self._settings = settings

        # Load weather config from strategies.yaml
        import yaml, pathlib
        yaml_path = pathlib.Path(__file__).parent.parent / "config" / "strategies.yaml"
        with open(yaml_path) as f:
            cfg = yaml.safe_load(f).get("weather_betting", {})

        self._cities: list[dict] = cfg.get("cities", [])
        self._sigma: float = cfg.get("sigma_celsius", 1.5)
        self._n_samples: int = cfg.get("monte_carlo_samples", 50_000)
        self._max_hours: float = cfg.get("max_hours_to_close", 26.0)
        self._min_edge_pct: float = cfg.get("min_edge_pct", 5.0)
        self._max_buckets: int = cfg.get("max_buckets_per_market", 4)
        self._min_price: float = cfg.get("min_bucket_price", 0.01)
        self._max_price: float = cfg.get("max_bucket_price", 0.60)
        self._kelly_fraction: float = cfg.get("kelly_fraction", 0.25)
        self._max_position_usd: float = cfg.get("max_position_size_usd", 25.0)

        # In-memory forecast cache: (city_name, date) → (WeatherForecast, fetched_at_monotonic)
        self._forecast_cache: dict[tuple[str, date], tuple[WeatherForecast, float]] = {}

        # Deduplication: condition_ids we've already entered
        self._entered: set[str] = set()

    def on_start(self) -> None:
        logger.info(
            "WeatherBettingStrategy started | cities={cities} | min_edge={edge}% | max_pos=${pos}",
            cities=[c["name"] for c in self._cities],
            edge=self._min_edge_pct,
            pos=self._max_position_usd,
        )

    def on_price_update(
        self,
        event: PriceUpdateEvent,
        portfolio: PortfolioSnapshot,
    ) -> list[OrderRequest]:
        market = self._market_data.get(event.condition_id)
        if market is None:
            return []

        if not is_weather_market(market.question):
            return []

        if event.condition_id in self._entered:
            return []

        # Check market is within max_hours_to_close
        if market.end_date is not None:
            end_dt = (
                market.end_date
                if market.end_date.tzinfo
                else market.end_date.replace(tzinfo=timezone.utc)
            )
            hours_left = (end_dt - event.timestamp).total_seconds() / 3600
            if hours_left <= 0 or hours_left > self._max_hours:
                return []
            target_date = end_dt.date()
        else:
            target_date = event.timestamp.date()

        city_cfg = _identify_city(market.question, self._cities)
        if city_cfg is None:
            return []

        forecast = self._get_forecast(city_cfg, target_date)
        if forecast is None:
            return []

        bucket_defs = parse_weather_buckets(market.question)
        if not bucket_defs:
            return []

        # Compute probabilities for each bucket
        probs = [
            compute_bucket_probability(
                lo=lo,
                hi=hi,
                mu=forecast.point_forecast_celsius,
                sigma=self._sigma,
                n_samples=self._n_samples,
                random_seed=self._settings.random_seed,
            )
            for lo, hi, _ in bucket_defs
        ]

        try:
            probs = normalize_bucket_probabilities(probs)
        except ValueError:
            return []

        # Only YES token is available per bucket market
        token_id = market.yes_token_id
        if not token_id:
            return []

        buckets = [
            WeatherBucket(
                label=label,
                lo=lo,
                hi=hi,
                model_prob=prob,
                market_price=event.price,
                edge=compute_edge(prob, event.price),
                token_id=token_id,
            )
            for (lo, hi, label), prob in zip(bucket_defs, probs)
        ]

        legs = select_ladder_legs(
            buckets,
            min_edge_pct=self._min_edge_pct,
            max_legs=self._max_buckets,
            min_price=self._min_price,
            max_price=self._max_price,
        )

        if not legs:
            return []

        orders: list[OrderRequest] = []
        for leg in legs:
            size = compute_kelly_size(
                edge=leg.edge,
                market_price=leg.market_price,
                kelly_fraction=self._kelly_fraction,
                capital_usd=portfolio.total_value_usd,
                max_position_usd=self._max_position_usd,
            )
            if size < 1.0:
                continue

            orders.append(OrderRequest(
                order_id=str(uuid.uuid4()),
                strategy=self.name,
                condition_id=event.condition_id,
                token_id=leg.token_id,
                side="BUY",
                size_usd=size,
                limit_price=None,
                edge=leg.edge,
            ))
            logger.info(
                "WeatherBet signal | city={city} | label={label} | "
                "model={mp:.2f} | market={mkt:.2f} | edge={edge:.2f} | size=${size:.2f}",
                city=city_cfg["name"],
                label=leg.label,
                mp=leg.model_prob,
                mkt=leg.market_price,
                edge=leg.edge,
                size=size,
            )

        if orders:
            self._entered.add(event.condition_id)

        return orders

    def on_market_resolution(self, event: MarketResolutionEvent) -> None:
        self._entered.discard(event.condition_id)

    # ── Internal ──────────────────────────────────────────────────────────────

    def _get_forecast(self, city_cfg: dict, target_date: date) -> WeatherForecast | None:
        """
        Return cached forecast or fetch a new one from Open-Meteo.
        Cache TTL: 6h (Open-Meteo updates every 6h).
        """
        key = (city_cfg["name"], target_date)
        now_mono = time.monotonic()

        cached = self._forecast_cache.get(key)
        if cached is not None:
            forecast, fetched_at = cached
            if now_mono - fetched_at < _FORECAST_CACHE_TTL_SECONDS:
                return forecast

        try:
            raw = fetch_daily_max_forecast(
                lat=city_cfg["lat"],
                lon=city_cfg["lon"],
                forecast_date=target_date,
                http_client=self._http_client,
            )
            forecast = build_weather_forecast(
                city=city_cfg["name"],
                lat=city_cfg["lat"],
                lon=city_cfg["lon"],
                forecast_date=target_date,
                raw_api_response=raw,
                sigma_celsius=self._sigma,
            )
            self._forecast_cache[key] = (forecast, now_mono)
            logger.debug(
                "Forecast fetched | city={city} | date={date} | max={temp:.1f}°C",
                city=city_cfg["name"],
                date=target_date,
                temp=forecast.point_forecast_celsius,
            )
            return forecast
        except Exception as exc:
            logger.error(
                "Failed to fetch forecast | city={city} | error={error}",
                city=city_cfg["name"],
                error=exc,
            )
            return None
