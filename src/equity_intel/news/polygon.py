"""
Polygon.io (Massive) news provider.

Docs: https://polygon.io/docs/rest/stocks/news
Endpoint: GET https://api.polygon.io/v2/reference/news
"""
from __future__ import annotations

import datetime
from typing import Any, Dict, List, Optional

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from equity_intel.news.base import NewsProvider
from equity_intel.logging_config import get_logger

logger = get_logger(__name__)

POLYGON_NEWS_URL = "https://api.polygon.io/v2/reference/news"


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (429, 500, 502, 503, 504)
    return isinstance(exc, (httpx.TimeoutException, httpx.ConnectError))


def _parse_published_at(ts: Optional[str]) -> Optional[datetime.datetime]:
    if not ts:
        return None
    try:
        # Polygon returns ISO 8601: "2024-01-15T12:34:56Z"
        return datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


class PolygonNewsProvider(NewsProvider):
    """Fetch news from Polygon.io (Massive) REST API."""

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

    async def __aenter__(self) -> "PolygonNewsProvider":
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
    async def _get(self, url: str, params: Dict[str, Any]) -> Dict[str, Any]:
        client = self._client or self._make_client()
        params["apiKey"] = self.api_key
        resp = await client.get(url, params=params)
        if resp.status_code == 429:
            logger.warning("polygon_rate_limited")
            import asyncio
            await asyncio.sleep(10)
            resp.raise_for_status()
        resp.raise_for_status()
        return resp.json()

    async def fetch_news(
        self,
        ticker: str,
        days: int = 7,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """
        Fetch recent news articles for a ticker from Polygon.

        Returns normalized list matching NewsProvider contract.
        """
        published_utc_gte = (
            datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")

        params: Dict[str, Any] = {
            "ticker": ticker.upper(),
            "published_utc.gte": published_utc_gte,
            "order": "desc",
            "limit": min(limit, 1000),
            "sort": "published_utc",
        }

        all_results: List[Dict[str, Any]] = []
        next_url: Optional[str] = None

        try:
            data = await self._get(POLYGON_NEWS_URL, params)
        except Exception as exc:
            logger.error("polygon_news_fetch_failed", ticker=ticker, error=str(exc))
            return []

        results = data.get("results", [])
        all_results.extend(results)

        # Follow pagination if needed
        next_url = data.get("next_url")
        while next_url and len(all_results) < limit:
            try:
                client = self._client or self._make_client()
                resp = await client.get(next_url, params={"apiKey": self.api_key})
                resp.raise_for_status()
                page = resp.json()
                all_results.extend(page.get("results", []))
                next_url = page.get("next_url")
            except Exception:
                break

        normalized = []
        for article in all_results[:limit]:
            # Polygon article tickers list
            tickers = article.get("tickers", [])
            primary_ticker = ticker.upper()
            if tickers and primary_ticker not in tickers:
                primary_ticker = tickers[0] if tickers else ticker.upper()

            normalized.append(
                {
                    "provider": "polygon",
                    "provider_id": article.get("id", ""),
                    "ticker": primary_ticker,
                    "title": article.get("title", ""),
                    "summary": article.get("description", ""),
                    "url": article.get("article_url", ""),
                    "publisher": article.get("publisher", {}).get("name", ""),
                    "author": article.get("author", ""),
                    "published_at": _parse_published_at(article.get("published_utc")),
                    "tickers": tickers,
                    "sentiment": article.get("insights", [{}])[0].get("sentiment") if article.get("insights") else None,
                    "raw": article,
                }
            )

        logger.info(
            "polygon_news_fetched",
            ticker=ticker,
            count=len(normalized),
        )
        return normalized

    async def fetch_news_multi(
        self,
        tickers: List[str],
        days: int = 7,
        limit_per_ticker: int = 50,
    ) -> List[Dict[str, Any]]:
        """Fetch news for multiple tickers sequentially to respect rate limits."""
        import asyncio

        all_articles: List[Dict[str, Any]] = []
        for i, ticker in enumerate(tickers):
            try:
                articles = await self.fetch_news(ticker, days=days, limit=limit_per_ticker)
                all_articles.extend(articles)
            except Exception as exc:
                logger.error("polygon_news_ticker_failed", ticker=ticker, error=str(exc))
            # Small delay between tickers to avoid hitting rate limits
            if i < len(tickers) - 1:
                await asyncio.sleep(1.0)

        # Deduplicate by article id
        seen: set = set()
        deduped = []
        for article in all_articles:
            pid = article.get("provider_id", "")
            if pid and pid not in seen:
                seen.add(pid)
                deduped.append(article)
            elif not pid:
                deduped.append(article)

        return deduped
