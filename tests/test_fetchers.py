"""Unit tests for data fetchers.

All tests mock HTTP and never hit the network, DB, or filesystem.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from config.schemas import Market, OrderLevel, OrderbookSnapshot, PricePoint
from data.fetchers.clob_fetcher import CLOBFetcher, _parse_price_point
from data.fetchers.gamma_fetcher import GammaFetcher, _to_float


# ── GammaFetcher unit tests ────────────────────────────────────────────────────

class TestToFloat:
    def test_float_string(self) -> None:
        assert _to_float("123.45") == pytest.approx(123.45)

    def test_int(self) -> None:
        assert _to_float(100) == pytest.approx(100.0)

    def test_none(self) -> None:
        assert _to_float(None) is None

    def test_invalid_string(self) -> None:
        assert _to_float("not-a-number") is None


class TestGammaFetcherParseMarket:
    def test_full_market(self) -> None:
        raw = {
            "condition_id": "cond_abc",
            "question": "Will BTC reach $100k?",
            "category": "crypto",
            "end_date_iso": "2024-12-31T00:00:00Z",
            "closed": True,
            "resolution_source": "coinbase",
            "outcome": "YES",
            "volume": "50000.0",
            "liquidity": "12000.5",
            "tokens": [
                {"token_id": "yes_tok", "outcome": "Yes"},
                {"token_id": "no_tok", "outcome": "No"},
            ],
        }
        market = GammaFetcher._parse_market(raw)

        assert isinstance(market, Market)
        assert market.condition_id == "cond_abc"
        assert market.question == "Will BTC reach $100k?"
        assert market.category == "crypto"
        assert market.resolved is True
        assert market.outcome == "YES"
        assert market.volume_usd == pytest.approx(50000.0)
        assert market.liquidity_usd == pytest.approx(12000.5)
        assert market.yes_token_id == "yes_tok"
        assert market.no_token_id == "no_tok"

    def test_minimal_market(self) -> None:
        raw = {"condition_id": "cond_min", "question": "Simple question?"}
        market = GammaFetcher._parse_market(raw)

        assert market.condition_id == "cond_min"
        assert market.end_date is None
        assert market.volume_usd is None
        assert market.resolved is False

    def test_missing_tokens(self) -> None:
        raw = {"condition_id": "cond_notok", "question": "No tokens?"}
        market = GammaFetcher._parse_market(raw)
        assert market.yes_token_id is None
        assert market.no_token_id is None

    def test_bad_end_date_does_not_raise(self) -> None:
        raw = {"condition_id": "cond_bad", "question": "Bad date?", "end_date_iso": "not-a-date"}
        market = GammaFetcher._parse_market(raw)
        assert market.end_date is None


class TestGammaFetcherGetActiveMarkets:
    def test_filters_by_min_volume(self) -> None:
        page1 = {
            "data": [
                {"condition_id": "c1", "question": "Q1", "volume": "50000"},
                {"condition_id": "c2", "question": "Q2", "volume": "5000"},
                {"condition_id": "c3", "question": "Q3", "volume": "25000"},
            ],
            "next_cursor": None,
        }

        mock_client = MagicMock()
        mock_client.get.return_value = MagicMock(
            status_code=200,
            json=MagicMock(return_value=page1),
            raise_for_status=MagicMock(),
        )

        fetcher = GammaFetcher(http_client=mock_client)
        markets = fetcher.get_active_markets(min_volume=10_000)

        assert len(markets) == 2
        assert {m.condition_id for m in markets} == {"c1", "c3"}

    def test_pagination(self) -> None:
        pages = [
            {"data": [{"condition_id": "c1", "question": "Q1", "volume": "20000"}], "next_cursor": "page2"},
            {"data": [{"condition_id": "c2", "question": "Q2", "volume": "30000"}], "next_cursor": None},
        ]

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = MagicMock(side_effect=pages)
        mock_client = MagicMock()
        mock_client.get.return_value = mock_resp

        fetcher = GammaFetcher(http_client=mock_client)
        markets = fetcher.get_active_markets(min_volume=0)

        assert len(markets) == 2


# ── CLOBFetcher unit tests ─────────────────────────────────────────────────────

class TestParsePricePoint:
    def test_basic(self) -> None:
        row = {"t": "1704067200", "p": "0.65"}
        pp = _parse_price_point("tok1", row)

        assert isinstance(pp, PricePoint)
        assert pp.token_id == "tok1"
        assert pp.price == pytest.approx(0.65)
        assert pp.timestamp == datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

    def test_price_clamped_above_one(self) -> None:
        row = {"t": "1704067200", "p": "1.0001"}
        pp = _parse_price_point("tok1", row)
        assert pp.price == pytest.approx(1.0)

    def test_price_clamped_below_zero(self) -> None:
        row = {"t": "1704067200", "p": "-0.001"}
        pp = _parse_price_point("tok1", row)
        assert pp.price == pytest.approx(0.0)


class TestCLOBFetcherGetPriceHistory:
    def test_returns_price_points(self) -> None:
        history_data = {
            "history": [
                {"t": "1704067200", "p": "0.60"},
                {"t": "1704070800", "p": "0.62"},
                {"t": "1704074400", "p": "0.65"},
            ]
        }

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = MagicMock(return_value=history_data)
        mock_http = MagicMock()
        mock_http.get.return_value = mock_resp

        fetcher = CLOBFetcher(http_client=mock_http, clob_client=MagicMock())
        prices = fetcher.get_price_history("tok_yes", 1704067200, 1704074400, fidelity=60)

        assert len(prices) == 3
        assert all(isinstance(p, PricePoint) for p in prices)
        assert prices[0].price == pytest.approx(0.60)
        assert prices[2].price == pytest.approx(0.65)

    def test_empty_history(self) -> None:
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = MagicMock(return_value={"history": []})
        mock_http = MagicMock()
        mock_http.get.return_value = mock_resp

        fetcher = CLOBFetcher(http_client=mock_http, clob_client=MagicMock())
        prices = fetcher.get_price_history("tok_yes", 0, 9999999999)

        assert prices == []


class TestCLOBFetcherGetOrderbook:
    def _make_order_summary(self, price: str, size: str) -> MagicMock:
        s = MagicMock()
        s.price = price
        s.size = size
        return s

    def test_returns_orderbook_snapshot(self) -> None:
        mock_book = MagicMock()
        mock_book.bids = [self._make_order_summary("0.48", "100"), self._make_order_summary("0.47", "200")]
        mock_book.asks = [self._make_order_summary("0.52", "80"), self._make_order_summary("0.53", "150")]

        mock_clob = MagicMock()
        mock_clob.get_order_book.return_value = mock_book

        fetcher = CLOBFetcher(http_client=MagicMock(), clob_client=mock_clob)
        snap = fetcher.get_orderbook("tok_yes")

        assert isinstance(snap, OrderbookSnapshot)
        assert snap.token_id == "tok_yes"
        assert len(snap.bids) == 2
        assert len(snap.asks) == 2
        assert snap.mid_price == pytest.approx(0.50)
        assert snap.spread == pytest.approx(0.04)

    def test_empty_book(self) -> None:
        mock_book = MagicMock()
        mock_book.bids = []
        mock_book.asks = []

        mock_clob = MagicMock()
        mock_clob.get_order_book.return_value = mock_book

        fetcher = CLOBFetcher(http_client=MagicMock(), clob_client=mock_clob)
        snap = fetcher.get_orderbook("tok_empty")

        assert snap.mid_price is None
        assert snap.spread is None
