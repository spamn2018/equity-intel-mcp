"""
Tests for Polygon news and price providers.

All HTTP calls are intercepted by respx — no real network requests.
"""
from __future__ import annotations

import datetime
import json
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import respx

from equity_intel.news.polygon import PolygonNewsProvider
from equity_intel.prices.polygon import PolygonPriceProvider, _ms_to_datetime

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

API_KEY = "test_api_key_12345"

# Polygon /v2/reference/news response shape
def make_news_response(articles: list[Dict[str, Any]], next_url: str | None = None) -> Dict:
    resp: Dict[str, Any] = {
        "status": "OK",
        "request_id": "abc123",
        "count": len(articles),
        "results": articles,
    }
    if next_url:
        resp["next_url"] = next_url
    return resp


def make_article(
    id_: str = "art1",
    ticker: str = "AAPL",
    title: str = "Apple releases new product",
    published_utc: str = "2024-01-15T10:30:00Z",
) -> Dict:
    return {
        "id": id_,
        "title": title,
        "article_url": f"https://example.com/news/{id_}",
        "published_utc": published_utc,
        "description": f"Summary for {title}",
        "amp_url": None,
        "image_url": None,
        "author": "Jane Doe",
        "publisher": {"name": "Example News", "homepage_url": "https://example.com"},
        "tickers": [ticker, "SPY"],
        "insights": [
            {"ticker": ticker, "sentiment": "positive", "sentiment_reasoning": "Bullish"}
        ],
    }


# Polygon /v2/aggs/ticker/{t}/range/1/day response shape
def make_aggs_response(bars: list[Dict[str, Any]]) -> Dict:
    return {
        "ticker": "AAPL",
        "status": "OK",
        "queryCount": len(bars),
        "resultsCount": len(bars),
        "adjusted": True,
        "results": bars,
        "request_id": "xyz789",
    }


def make_bar(
    t_ms: int = 1705276800000,  # 2024-01-15 00:00:00 UTC
    o: float = 185.0,
    h: float = 188.0,
    lo: float = 184.0,
    c: float = 187.5,
    v: float = 55_000_000.0,
) -> Dict:
    return {"t": t_ms, "o": o, "h": h, "l": lo, "c": c, "v": v, "vw": c, "n": 500}


# ---------------------------------------------------------------------------
# _ms_to_datetime
# ---------------------------------------------------------------------------

class TestMsToDatetime:
    def test_known_timestamp(self):
        ms = 1705276800000  # 2024-01-15 00:00:00 UTC
        dt = _ms_to_datetime(ms)
        assert dt is not None
        assert dt.year == 2024
        assert dt.month == 1
        assert dt.day == 15
        assert dt.tzinfo is not None

    def test_none_returns_none(self):
        assert _ms_to_datetime(None) is None

    def test_zero_timestamp(self):
        dt = _ms_to_datetime(0)
        assert dt is not None
        assert dt.year == 1970


# ---------------------------------------------------------------------------
# PolygonNewsProvider
# ---------------------------------------------------------------------------

class TestPolygonNewsProvider:
    @pytest.mark.asyncio
    @respx.mock
    async def test_fetch_news_single_page(self):
        """Single-page response with two articles."""
        articles = [make_article("a1", "AAPL"), make_article("a2", "AAPL", title="Apple Q4 earnings")]
        route = respx.get("https://api.polygon.io/v2/reference/news").mock(
            return_value=httpx.Response(200, json=make_news_response(articles))
        )

        provider = PolygonNewsProvider(api_key=API_KEY)
        async with provider:
            results = await provider.fetch_news(ticker="AAPL", days=7)

        assert route.called
        assert len(results) == 2
        assert results[0]["provider"] == "polygon"
        assert results[0]["provider_id"] == "a1"
        assert results[0]["ticker"] == "AAPL"
        assert results[0]["title"] == "Apple releases new product"
        assert results[0]["publisher"] == "Example News"
        assert results[0]["author"] == "Jane Doe"
        assert "url" in results[0]
        assert results[0]["sentiment"] == "positive"

    @pytest.mark.asyncio
    @respx.mock
    async def test_fetch_news_pagination(self):
        """next_url triggers a second page fetch."""
        page1 = [make_article("a1", "AAPL")]
        page2 = [make_article("a2", "AAPL", title="Apple Part 2")]

        call_count = 0

        def news_side_effect(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return httpx.Response(
                    200,
                    json=make_news_response(
                        page1,
                        next_url="https://api.polygon.io/v2/reference/news?cursor=xyz",
                    ),
                )
            return httpx.Response(200, json=make_news_response(page2))

        respx.get(url__startswith="https://api.polygon.io/v2/reference/news").mock(
            side_effect=news_side_effect
        )

        provider = PolygonNewsProvider(api_key=API_KEY)
        async with provider:
            results = await provider.fetch_news(ticker="AAPL", days=7, limit=50)

        assert len(results) == 2
        ids = {r["provider_id"] for r in results}
        assert ids == {"a1", "a2"}

    @pytest.mark.asyncio
    @respx.mock
    async def test_fetch_news_empty_response(self):
        """Empty results list returns empty list."""
        respx.get("https://api.polygon.io/v2/reference/news").mock(
            return_value=httpx.Response(200, json=make_news_response([]))
        )

        provider = PolygonNewsProvider(api_key=API_KEY)
        async with provider:
            results = await provider.fetch_news(ticker="AAPL", days=7)

        assert results == []

    @pytest.mark.asyncio
    @respx.mock
    async def test_fetch_news_http_error_returns_empty(self):
        """HTTP 500 is handled gracefully — returns empty list."""
        respx.get("https://api.polygon.io/v2/reference/news").mock(
            return_value=httpx.Response(500, json={"status": "ERROR"})
        )

        provider = PolygonNewsProvider(api_key=API_KEY)
        async with provider:
            # tenacity retries, but we override with stop_after_attempt already set;
            # after exhausting retries it should return [] not raise
            results = await provider.fetch_news(ticker="AAPL", days=7)

        assert results == []

    @pytest.mark.asyncio
    @respx.mock
    async def test_fetch_news_multi_deduplicates(self):
        """fetch_news_multi deduplicates articles that appear in multiple ticker responses."""
        shared_article = make_article("shared1", "AAPL")
        aapl_articles = [shared_article, make_article("aapl1", "AAPL", title="AAPL exclusive")]
        msft_articles = [shared_article, make_article("msft1", "MSFT", title="MSFT exclusive")]

        def route_by_ticker(request: httpx.Request) -> httpx.Response:
            url_str = str(request.url)
            if "ticker=AAPL" in url_str:
                return httpx.Response(200, json=make_news_response(aapl_articles))
            return httpx.Response(200, json=make_news_response(msft_articles))

        respx.get(url__startswith="https://api.polygon.io/v2/reference/news").mock(
            side_effect=route_by_ticker
        )

        provider = PolygonNewsProvider(api_key=API_KEY)
        async with provider:
            results = await provider.fetch_news_multi(tickers=["AAPL", "MSFT"], days=7)

        # "shared1" should appear only once
        ids = [r["provider_id"] for r in results]
        assert ids.count("shared1") == 1
        assert len(results) == 3  # shared1, aapl1, msft1

    @pytest.mark.asyncio
    @respx.mock
    async def test_news_normalization_fields(self):
        """Check all normalized fields are present."""
        article = make_article("norm1", "TSLA", title="Tesla delivery record")
        respx.get("https://api.polygon.io/v2/reference/news").mock(
            return_value=httpx.Response(200, json=make_news_response([article]))
        )

        provider = PolygonNewsProvider(api_key=API_KEY)
        async with provider:
            results = await provider.fetch_news(ticker="TSLA", days=3)

        assert len(results) == 1
        r = results[0]
        required_keys = {
            "provider", "provider_id", "ticker", "title", "summary",
            "url", "publisher", "author", "published_at", "tickers", "sentiment", "raw",
        }
        assert required_keys.issubset(r.keys()), f"Missing keys: {required_keys - r.keys()}"
        assert isinstance(r["published_at"], datetime.datetime)
        assert isinstance(r["tickers"], list)


# ---------------------------------------------------------------------------
# PolygonPriceProvider
# ---------------------------------------------------------------------------

class TestPolygonPriceProvider:
    @pytest.mark.asyncio
    @respx.mock
    async def test_fetch_daily_bars_basic(self):
        """Happy path: two bars returned."""
        bars = [
            make_bar(t_ms=1705276800000, c=187.5),  # 2024-01-15
            make_bar(t_ms=1705363200000, c=189.0),  # 2024-01-16
        ]
        respx.get(url__startswith="https://api.polygon.io/v2/aggs/ticker/AAPL/range").mock(
            return_value=httpx.Response(200, json=make_aggs_response(bars))
        )

        provider = PolygonPriceProvider(api_key=API_KEY)
        start = datetime.date(2024, 1, 15)
        end = datetime.date(2024, 1, 16)
        async with provider:
            result = await provider.fetch_daily_bars("AAPL", start, end)

        assert len(result) == 2
        assert result[0]["ticker"] == "AAPL"
        assert result[0]["close"] == 187.5
        assert result[0]["interval"] == "1d"
        assert result[0]["provider"] == "polygon"
        assert isinstance(result[0]["timestamp"], datetime.datetime)

    @pytest.mark.asyncio
    @respx.mock
    async def test_fetch_daily_bars_empty(self):
        """No results returns empty list."""
        respx.get(url__startswith="https://api.polygon.io/v2/aggs/ticker/AAPL/range").mock(
            return_value=httpx.Response(200, json=make_aggs_response([]))
        )

        provider = PolygonPriceProvider(api_key=API_KEY)
        async with provider:
            result = await provider.fetch_daily_bars("AAPL", datetime.date(2024, 1, 1), datetime.date(2024, 1, 5))

        assert result == []

    @pytest.mark.asyncio
    @respx.mock
    async def test_fetch_daily_bars_network_error_returns_empty(self):
        """Network errors return empty list — provider catches exceptions."""
        respx.get(url__startswith="https://api.polygon.io/v2/aggs/ticker/AAPL/range").mock(
            side_effect=httpx.ConnectError("connection refused")
        )

        provider = PolygonPriceProvider(api_key=API_KEY)
        async with provider:
            result = await provider.fetch_daily_bars("AAPL", datetime.date(2024, 1, 1), datetime.date(2024, 1, 5))

        assert result == []

    @pytest.mark.asyncio
    @respx.mock
    async def test_fetch_daily_bars_fields(self):
        """All OHLCV fields are present and typed correctly."""
        bar = make_bar(t_ms=1705276800000, o=185.0, h=188.0, lo=184.0, c=187.5, v=55_000_000.0)
        respx.get(url__startswith="https://api.polygon.io/v2/aggs/ticker/NVDA/range").mock(
            return_value=httpx.Response(200, json=make_aggs_response([bar]))
        )

        provider = PolygonPriceProvider(api_key=API_KEY)
        async with provider:
            result = await provider.fetch_daily_bars("NVDA", datetime.date(2024, 1, 15), datetime.date(2024, 1, 15))

        assert len(result) == 1
        r = result[0]
        assert r["open"] == 185.0
        assert r["high"] == 188.0
        assert r["low"] == 184.0
        assert r["close"] == 187.5
        assert r["volume"] == 55_000_000.0
        assert r["adjusted_close"] == 187.5  # same as close when adjusted=true

    @pytest.mark.asyncio
    @respx.mock
    async def test_fetch_previous_close(self):
        """fetch_previous_close returns normalized bar."""
        bar = make_bar(t_ms=1705276800000, c=190.0)
        respx.get("https://api.polygon.io/v2/aggs/ticker/AAPL/prev").mock(
            return_value=httpx.Response(200, json={"status": "OK", "results": [bar]})
        )

        provider = PolygonPriceProvider(api_key=API_KEY)
        async with provider:
            result = await provider.fetch_previous_close("AAPL")

        assert result is not None
        assert result["ticker"] == "AAPL"
        assert result["close"] == 190.0
        assert result["interval"] == "1d"
        assert result["provider"] == "polygon"

    @pytest.mark.asyncio
    @respx.mock
    async def test_fetch_previous_close_empty(self):
        """Empty results returns None."""
        respx.get("https://api.polygon.io/v2/aggs/ticker/AAPL/prev").mock(
            return_value=httpx.Response(200, json={"status": "OK", "results": []})
        )

        provider = PolygonPriceProvider(api_key=API_KEY)
        async with provider:
            result = await provider.fetch_previous_close("AAPL")

        assert result is None

    @pytest.mark.asyncio
    @respx.mock
    async def test_compute_event_window_reaction_basic(self):
        """compute_event_window_reaction returns pct_change and volume_ratio."""
        # Day before event: close=100, vol=1_000_000
        # Day of event: close=110, vol=2_000_000  (10% up, 2x volume)
        bars = [
            make_bar(t_ms=1705190400000, c=100.0, v=1_000_000.0),  # 2024-01-14
            make_bar(t_ms=1705276800000, c=110.0, v=2_000_000.0),  # 2024-01-15
            make_bar(t_ms=1705363200000, c=108.0, v=1_200_000.0),  # 2024-01-16
        ]
        respx.get(url__startswith="https://api.polygon.io/v2/aggs/ticker/AAPL/range").mock(
            return_value=httpx.Response(200, json=make_aggs_response(bars))
        )

        provider = PolygonPriceProvider(api_key=API_KEY)
        async with provider:
            result = await provider.compute_event_window_reaction(
                ticker="AAPL",
                event_date=datetime.date(2024, 1, 15),
                window_days=3,
            )

        assert result["available"] is True
        assert result["ticker"] == "AAPL"
        assert result["pct_change"] == pytest.approx(10.0, abs=0.1)
        assert result["volume_ratio"] == pytest.approx(2.0, abs=0.1)
        assert "note" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_compute_event_window_reaction_no_data(self):
        """No price data returns available=False."""
        respx.get(url__startswith="https://api.polygon.io/v2/aggs/ticker/AAPL/range").mock(
            return_value=httpx.Response(200, json=make_aggs_response([]))
        )

        provider = PolygonPriceProvider(api_key=API_KEY)
        async with provider:
            result = await provider.compute_event_window_reaction(
                ticker="AAPL",
                event_date=datetime.date(2024, 1, 15),
            )

        assert result["available"] is False

    @pytest.mark.asyncio
    @respx.mock
    async def test_ticker_uppercased_in_url(self):
        """Lowercase ticker in input is uppercased before hitting the API URL."""
        route = respx.get(url__startswith="https://api.polygon.io/v2/aggs/ticker/MSFT/range").mock(
            return_value=httpx.Response(200, json=make_aggs_response([]))
        )

        provider = PolygonPriceProvider(api_key=API_KEY)
        async with provider:
            await provider.fetch_daily_bars("msft", datetime.date(2024, 1, 1), datetime.date(2024, 1, 5))

        assert route.called


# ---------------------------------------------------------------------------
# explain_stock_move (integration with in-memory SQLite)
# ---------------------------------------------------------------------------

class TestExplainStockMove:
    """Test explain_stock_move using an in-memory SQLite DB with price data."""

    @pytest.fixture(autouse=True)
    def _db(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from equity_intel.db.models import Base, Company, MarketPrice, now_utc
        import datetime as dt

        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)
        self.session = Session()

        company = Company(
            ticker="AAPL", cik="0000320193", name="Apple Inc.",
            exchange="NASDAQ", is_active=True, created_at=now_utc(), updated_at=now_utc(),
        )
        self.session.add(company)
        self.session.flush()

        # Add price bars: day before event, event day, day after
        event_date = dt.date(2024, 1, 15)
        bars = [
            MarketPrice(
                ticker="AAPL",
                timestamp=dt.datetime(2024, 1, 14, 0, 0, 0),
                open=99.0, high=101.0, low=98.0, close=100.0,
                adjusted_close=100.0, volume=1_000_000, interval="1d",
                provider="polygon", created_at=now_utc(),
            ),
            MarketPrice(
                ticker="AAPL",
                timestamp=dt.datetime(2024, 1, 15, 0, 0, 0),
                open=102.0, high=115.0, low=101.0, close=112.0,
                adjusted_close=112.0, volume=3_000_000, interval="1d",
                provider="polygon", created_at=now_utc(),
            ),
            MarketPrice(
                ticker="AAPL",
                timestamp=dt.datetime(2024, 1, 16, 0, 0, 0),
                open=111.0, high=113.0, low=109.0, close=110.0,
                adjusted_close=110.0, volume=1_500_000, interval="1d",
                provider="polygon", created_at=now_utc(),
            ),
        ]
        for b in bars:
            self.session.add(b)
        self.session.commit()
        yield
        self.session.close()

    def test_explain_returns_price_move(self):
        from equity_intel.mcp_server.tools import explain_stock_move

        result = explain_stock_move(self.session, ticker="AAPL", date="2024-01-15", window=2)

        assert result["ticker"] == "AAPL"
        assert result["target_date"] == "2024-01-15"
        pm = result["price_move"]
        assert pm["available"] is True
        assert pm["pct_change"] == pytest.approx(12.0, abs=0.1)
        assert pm["volume_ratio"] == pytest.approx(3.0, abs=0.1)
        assert pm["price_before"] == pytest.approx(100.0)
        assert pm["price_after"] == pytest.approx(112.0)

    def test_explain_no_price_data(self):
        from equity_intel.mcp_server.tools import explain_stock_move

        result = explain_stock_move(self.session, ticker="MSFT", date="2024-01-15", window=2)

        assert result["ticker"] == "MSFT"
        pm = result["price_move"]
        assert pm["available"] is False

    def test_explain_has_caution_note(self):
        from equity_intel.mcp_server.tools import explain_stock_move

        result = explain_stock_move(self.session, ticker="AAPL", date="2024-01-15")
        assert "caution" in result
        assert "likely related" in result["caution"].lower()
        assert "note" in result

    def test_explain_interpretation_contains_direction(self):
        from equity_intel.mcp_server.tools import explain_stock_move

        result = explain_stock_move(self.session, ticker="AAPL", date="2024-01-15", window=2)
        assert "up" in result["interpretation"] or "down" in result["interpretation"]
        assert "12.00%" in result["interpretation"]
