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
    return isinstance(exc, (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError))


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
        wait=wait_exponential(multiplier=2, min=15, max=120),
        stop=stop_after_attempt(6),
        reraise=True,
    )
    async def _get(self, url: str, params: Dict[str, Any]) -> Dict[str, Any]:
        client = self._client or self._make_client()
        params["apiKey"] = self.api_key
        resp = await client.get(url, params=params)
        if resp.status_code == 429:
            logger.warning("polygon_rate_limited")
            resp.raise_for_status()  # Let tenacity handle the wait
        resp.raise_for_status()
        return resp.json()


    async def fetch_news(
        self,
        ticker: str,
        days: int = 1,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """
        Fetch recent news articles for a ticker from Polygon.

        Default window is 24 hours (days=1). Default cap is 10 actionable
        stories per ticker. To survive the bias filter (articles with >15
        tickers are dropped as off-topic portfolio roundups), we over-fetch
        up to 5× limit raw results and then cap after filtering.

        Articles with more than 15 tickers in their tickers array are skipped —
        these are almost always 13F / hedge-fund portfolio roundups that list
        every holding and add no signal for a specific ticker. Genuine articles
        (e.g. "Nvidia launches new GPU") typically have 1-5 tickers.

        Returns normalized list matching NewsProvider contract.
        """
        published_utc_gte = (
            datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")

        # Over-fetch raw results so bias filter doesn't starve us of actionable stories.
        # We need `limit` articles after filtering; fetching 5× gives a wide safety margin.
        raw_fetch_limit = max(limit * 5, 50)

        params: Dict[str, Any] = {
            "ticker": ticker.upper(),
            "published_utc.gte": published_utc_gte,
            "order": "desc",
            "limit": min(raw_fetch_limit, 1000),
            "sort": "published_utc",
        }

        all_results: List[Dict[str, Any]] = []

        try:
            data = await self._get(POLYGON_NEWS_URL, params)
        except Exception as exc:
            logger.error("polygon_news_fetch_failed", ticker=ticker, error=str(exc))
            return []

        all_results.extend(data.get("results", []))

        # Follow pagination only if we still need more raw material
        next_url = data.get("next_url")
        while next_url and len(all_results) < raw_fetch_limit:
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
        ticker_upper = ticker.upper()
        skipped = 0
        for article in all_results:
            # Hard cap: once we have `limit` actionable stories, stop.
            if len(normalized) >= limit:
                break

            # Guard against 13F / hedge-fund portfolio articles that list 30+
            # tickers as holdings. A genuine article about this company will have
            # a small tickers list (typically 1-5). Articles with more than 15
            # tickers are almost certainly broad portfolio/holdings roundups and
            # add no signal for a specific ticker.
            article_tickers = article.get("tickers", [])
            if len(article_tickers) > 15:
                skipped += 1
                continue

            tickers = article.get("tickers", [])
            normalized.append(
                {
                    "provider": "polygon",
                    "provider_id": article.get("id", ""),
                    "ticker": ticker_upper,
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
            skipped_off_topic=skipped,
        )
        return normalized


    async def fetch_news_multi(
        self,
        tickers: List[str],
        days: int = 1,
        limit_per_ticker: int = 10,
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
            # Polygon free tier = 5 req/min. 60s / 5 = 12s minimum.
            # 13s gives a small buffer so we never hit 429 in the first place.
            if i < len(tickers) - 1:
                await asyncio.sleep(13.0)

        # Deduplicate by article id — keep the first occurrence (which will
        # already be filed under the correct ticker thanks to the title filter).
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
