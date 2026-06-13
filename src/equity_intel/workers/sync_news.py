"""
Worker: sync news articles from the configured provider.

Usage:
    python -m equity_intel.workers.sync_news
    python -m equity_intel.workers.sync_news --tickers AAPL,MSFT --days 7
"""
from __future__ import annotations

import asyncio
import datetime
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import click

from equity_intel.config import settings
from equity_intel.db.models import Company, NewsArticle, now_utc
from equity_intel.db.session import get_session
from equity_intel.logging_config import configure_logging, get_logger
from equity_intel.news.source_filter import filter_articles

logger = get_logger(__name__)

# Stamp file at project root -- skips re-fetch within TTL
# parents[3] = Stocks/ (src/equity_intel/workers -> src/equity_intel -> src -> Stocks)
_STAMP_FILE = Path(__file__).resolve().parents[3] / "news_sync_last_run.txt"
_SYNC_TTL_HOURS = 24


def _is_fresh() -> bool:
    """Return True if news was synced within the last _SYNC_TTL_HOURS hours."""
    if not _STAMP_FILE.exists():
        return False
    age_hours = (time.time() - _STAMP_FILE.stat().st_mtime) / 3600
    return age_hours < _SYNC_TTL_HOURS


def _write_stamp() -> None:
    try:
        _STAMP_FILE.write_text(datetime.datetime.utcnow().isoformat(), encoding="utf-8")
    except Exception:
        pass


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

    url = article.get("url", "")
    article_ticker = article.get("ticker", "").upper() or None
    if url:
        existing = (
            session.query(NewsArticle)
            .filter(NewsArticle.url == url, NewsArticle.ticker == article_ticker)
            .first()
        )
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
    days: int = 1,
    force: bool = False,
) -> None:
    configure_logging(settings.log_level)

    if not force and _is_fresh():
        logger.info(
            "news_sync_skipped_fresh",
            reason="Last sync under " + str(_SYNC_TTL_HOURS) + "h ago -- delete news_sync_last_run.txt to force",
        )
        return

    prohibited = set(settings.prohibited_tickers_list)

    provider = _get_news_provider()
    if provider is None:
        logger.info("no_news_provider_configured_skipping")
        return

    with get_session() as session:
        query = session.query(Company).filter(Company.is_active == True)
        if prohibited:
            query = query.filter(Company.ticker.notin_(prohibited))
        if tickers:
            query = query.filter(
                Company.ticker.in_([t.upper() for t in tickers if t.upper() not in prohibited])
            )
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
                limit_per_ticker=10,
            )

        rss_feeds = [
            url.strip()
            for url in settings.rss_news_feeds.split(",")
            if url.strip()
        ]
        if rss_feeds:
            from equity_intel.news.rss import fetch_rss_articles

            articles.extend(
                await fetch_rss_articles(
                    feed_urls=rss_feeds,
                    companies=companies,
                    days=days,
                )
            )

        articles = filter_articles(articles)

        inserted = 0
        for article in articles:
            ticker = article.get("ticker", "").upper()
            company = ticker_to_company.get(ticker)
            was_inserted = upsert_news_article(session, article, company)
            if was_inserted:
                inserted += 1

        logger.info("news_sync_complete", fetched=len(articles), inserted=inserted)

    _write_stamp()


@click.command()
@click.option("--tickers", default=None, help="Comma-separated tickers")
@click.option("--days", default=1, show_default=True, help="Look-back window in days")
@click.option("--force", is_flag=True, default=False, help="Force re-fetch even if synced recently")
def main(tickers: Optional[str], days: int, force: bool) -> None:
    """Sync news articles from the configured provider."""
    ticker_list = [t.strip().upper() for t in tickers.split(",")] if tickers else None
    asyncio.run(run(ticker_list, days=days, force=force))


if __name__ == "__main__":
    main()
