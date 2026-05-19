"""
Polygon.io (Massive) price data provider.

Docs: https://polygon.io/docs/rest/stocks/aggregates
Endpoint: GET https://api.polygon.io/v2/aggs/ticker/{ticker}/range/{mult}/{span}/{from}/{to}
"""
from __future__ import annotations

import datetime
from typing import Any, Dict, List, Optional

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from equity_intel.prices.base import PriceProvider
from equity_intel.logging_config import get_logger

logger = get_logger(__name__)

POLYGON_AGGS_URL = "https://api.polygon.io/v2/aggs/ticker/{ticker}/range/{multiplier}/{timespan}/{from_date}/{to_date}"
POLYGON_PREV_CLOSE_URL = "https://api.polygon.io/v2/aggs/ticker/{ticker}/prev"


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (429, 500, 502, 503, 504)
    return isinstance(exc, (httpx.TimeoutException, httpx.ConnectError))


def _ms_to_datetime(ms: Optional[int]) -> Optional[datetime.datetime]:
    """Convert Polygon millisecond timestamp to UTC datetime."""
    if ms is None:
        return None
    try:
        return datetime.datetime.fromtimestamp(ms / 1000, tz=datetime.timezone.utc)
    except (ValueError, OSError, OverflowError):
        return None


class PolygonPriceProvider(PriceProvider):
    """Fetch OHLCV price bars from Polygon.io (Massive) REST API."""

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key
        self._client: Optional[httpx.AsyncClient] = None

    @property
    def name(self) -> str:
        return "polygon"

    def _make_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            headers={"Accept": "application/json"},
            timeout=httpx.Timeout(30.0),
            follow_redirects=True,
        )

    async def __aenter__(self) -> "PolygonPriceProvider":
        self._client = self._make_client()
        return self

    async def __aexit__(self, *args: Any) -> None:
        if self._client:
            await self._client.aclose()

    @retry(
        retry=retry_if_exception(_is_retryable),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        stop=stop_after_attempt(4),
        reraise=True,
    )
    async def _get(self, url: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        client = self._client or self._make_client()
        p = dict(params or {})
        p["apiKey"] = self.api_key
        resp = await client.get(url, params=p)
        if resp.status_code == 429:
            logger.warning("polygon_prices_rate_limited")
            import asyncio
            await asyncio.sleep(12)
            resp.raise_for_status()
        resp.raise_for_status()
        return resp.json()

    async def fetch_daily_bars(
        self,
        ticker: str,
        start: datetime.date,
        end: datetime.date,
    ) -> List[Dict[str, Any]]:
        """
        Fetch daily OHLCV bars for a ticker.

        Returns normalized list matching PriceProvider contract.
        """
        url = POLYGON_AGGS_URL.format(
            ticker=ticker.upper(),
            multiplier=1,
            timespan="day",
            from_date=start.strftime("%Y-%m-%d"),
            to_date=end.strftime("%Y-%m-%d"),
        )

        params = {
            "adjusted": "true",
            "sort": "asc",
            "limit": 5000,
        }

        try:
            data = await self._get(url, params)
        except Exception as exc:
            logger.error("polygon_prices_fetch_failed", ticker=ticker, error=str(exc))
            return []

        results = data.get("results", [])
        bars = []
        for r in results:
            ts = _ms_to_datetime(r.get("t"))
            if ts is None:
                continue
            bars.append(
                {
                    "ticker": ticker.upper(),
                    "timestamp": ts,
                    "open": r.get("o"),
                    "high": r.get("h"),
                    "low": r.get("l"),
                    "close": r.get("c"),
                    "volume": r.get("v"),
                    "adjusted_close": r.get("c"),  # Polygon returns adjusted when adjusted=true
                    "interval": "1d",
                    "provider": "polygon",
                    "raw": r,
                }
            )

        logger.info("polygon_prices_fetched", ticker=ticker, bars=len(bars))
        return bars

    async def fetch_previous_close(self, ticker: str) -> Optional[Dict[str, Any]]:
        """Fetch the previous trading day's close bar."""
        url = POLYGON_PREV_CLOSE_URL.format(ticker=ticker.upper())
        try:
            data = await self._get(url, {"adjusted": "true"})
            results = data.get("results", [])
            if not results:
                return None
            r = results[0]
            ts = _ms_to_datetime(r.get("t"))
            return {
                "ticker": ticker.upper(),
                "timestamp": ts,
                "open": r.get("o"),
                "high": r.get("h"),
                "low": r.get("l"),
                "close": r.get("c"),
                "volume": r.get("v"),
                "adjusted_close": r.get("c"),
                "interval": "1d",
                "provider": "polygon",
                "raw": r,
            }
        except Exception as exc:
            logger.error("polygon_prev_close_failed", ticker=ticker, error=str(exc))
            return None

    async def compute_event_window_reaction(
        self,
        ticker: str,
        event_date: datetime.date,
        window_days: int = 5,
    ) -> Dict[str, Any]:
        """
        Fetch a price window around an event and compute the move.

        Returns a dict with price_before, price_after, pct_change, volume_ratio.
        """
        start = event_date - datetime.timedelta(days=window_days + 5)
        end = event_date + datetime.timedelta(days=window_days + 2)
        bars = await self.fetch_daily_bars(ticker, start, end)

        if not bars:
            return {"available": False}

        # Find bar on or just before event_date
        before_bars = [b for b in bars if b["timestamp"].date() < event_date]
        after_bars = [b for b in bars if b["timestamp"].date() >= event_date]

        if not before_bars or not after_bars:
            return {"available": False, "bars_count": len(bars)}

        price_before = before_bars[-1]["close"]
        price_after = after_bars[0]["close"]
        vol_before = before_bars[-1]["volume"]
        vol_after = after_bars[0]["volume"]

        pct_change = ((price_after - price_before) / price_before * 100) if price_before else None
        vol_ratio = (vol_after / vol_before) if vol_before else None

        return {
            "available": True,
            "ticker": ticker,
            "event_date": event_date.isoformat(),
            "price_before": round(price_before, 4) if price_before else None,
            "price_after": round(price_after, 4) if price_after else None,
            "pct_change": round(pct_change, 2) if pct_change is not None else None,
            "volume_before": int(vol_before) if vol_before else None,
            "volume_after": int(vol_after) if vol_after else None,
            "volume_ratio": round(vol_ratio, 2) if vol_ratio else None,
            "window_days": window_days,
            "note": "Price reaction is correlation with event date, not confirmed causation.",
        }
