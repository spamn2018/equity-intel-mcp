"""
Worker: sync news articles from the configured provider.

Usage:
    python -m equity_intel.workers.sync_news
    python -m equity_intel.workers.sync_news --tickers AAPL,MSFT --days 7
"""
from __future__ import annotations

import asyncio
import datetime
from typing import Any, Dict, List, Optional

import click

from equity_intel.config import settings
from equity_intel.db.models import Company, NewsArticle, now_utc
from equity_intel.db.session import get_session
from equity_intel.logging_config import configure_logging, get_logger
from equity_intel.news.source_filter import filter_articles

logger = get_logger(__name__)


def _get_news_provider():
    """Return the configured news provider, or None if not configured."""
    provider = settings.news_provider.lower()
    if provider == "polygon":
        if not settings.polygon_api_key:
            logger.error("polygon_api_key_not_set")
            return None
        from equity_intel.news.polygon import PolygonNewsProvider
        return PolygonNewsProvider(api_key=settings.polygon_api_key)
    if provider == "none":
        logger.info("news_provider_not_configured")
        return None
    logger.warning("unknown_news_provider", provider=provider)
    return None


def upsert_news_article(session, article: Dict[str, Any], company: Optional[Company]) -> bool:
    """Insert a news article if it doesn't already exist. Returns True if inserted."""
    provider = article.get("provider", "")
    provider_id = article.get("provider_id", "")

    if provider and provider_id:
        existing = (
            session.query(NewsArticle)
            .filter(NewsArticle.provider == provider, NewsArticle.provider_id == provider_id)
            .first()
        )
        if existing:
            return False

    # Also check by URL to catch duplicates across providers
    url = article.get("url", "")
    if url:
        existing = session.query(NewsArticle).filter(NewsArticle.url == url).first()
        if existing:
            return False

    tickers = article.get("tickers", [])
    sentiment = article.get("sentiment")
    sentiment_json = {"polygon_sentiment": sentiment} if sentiment else None

    now = now_utc()
    na = NewsArticle(
        provider=provider,
        provider_id=provider_id or None,
        ticker=article.get("ticker", "").upper() or None,
        company_id=company.id if company else None,
        title=article.get("title", ""),
        summary=article.get("summary", ""),
        body=article.get("body", ""),
        url=url or None,
        publisher=article.get("publisher", ""),
        author=article.get("author", ""),
        published_at=article.get("published_at"),
        tickers_json={"tickers": tickers} if tickers else None,
        sentiment_json=sentiment_json,
        raw_json=article.get("raw", {}),
        created_at=now,
    )
    session.add(na)
    return True


async def run(
    tickers: Optional[List[str]] = None,
    days: int = 7,
) -> None:
    configure_logging(settings.log_level)

    provider = _get_news_provider()
    if provider is None:
        logger.info("no_news_provider_configured_skipping")
        return

    with get_session() as session:
        query = session.query(Company).filter(Company.is_active == True)
        if tickers:
            query = query.filter(Company.ticker.in_([t.upper() for t in tickers]))
        companies = query.all()

        if not companies:
            logger.warning("no_companies_found")
            return

        ticker_to_company = {c.ticker: c for c in companies}
        ticker_list = list(ticker_to_company.keys())

        logger.info("syncing_news", tickers=ticker_list, days=days)

        async with provider:
            articles = await provider.fetch_news_multi(
                tickers=ticker_list,
                days=days,
                limit_per_ticker=100,
            )

        # Apply source filter — drop blocked publishers before touching the DB
        articles = filter_articles(articles)

        inserted = 0
        for article in articles:
            # The provider already guarantees the ticker appears in the title.
            # Use only the article's own ticker field — no fallback iteration
            # over the full tickers list, which was the source of cross-
            # contamination (e.g. 13F articles filing under every holding).
            ticker = article.get("ticker", "").upper()
            company = ticker_to_company.get(ticker)
            was_inserted = upsert_news_article(session, article, company)
            if was_inserted:
                inserted += 1

        logger.info("news_sync_complete", fetched=len(articles), inserted=inserted)


@click.command()
@click.option("--tickers", default=None, help="Comma-separated tickers")
@click.option("--days", default=7, show_default=True, help="Look-back window in days")
def main(tickers: Optional[str], days: int) -> None:
    """Sync news articles from the configured provider."""
    ticker_list = [t.strip().upper() for t in tickers.split(",")] if tickers else None
    asyncio.run(run(ticker_list, days=days))


if __name__ == "__main__":
    main()
