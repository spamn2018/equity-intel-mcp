"""
Worker: sync daily price bars from the configured provider.

Usage:
    python -m equity_intel.workers.sync_prices
    python -m equity_intel.workers.sync_prices --tickers AAPL,NVDA --days 30
"""
from __future__ import annotations

import asyncio
import datetime
from typing import Any, Dict, List, Optional

import click

from equity_intel.config import settings
from equity_intel.db.models import Company, MarketPrice, now_utc
from equity_intel.db.session import get_session
from equity_intel.logging_config import configure_logging, get_logger

logger = get_logger(__name__)


def _get_price_provider():
    """Return the configured price provider, or None."""
    provider = settings.price_provider.lower()
    if provider == "polygon":
        if not settings.polygon_api_key:
            logger.error("polygon_api_key_not_set")
            return None
        from equity_intel.prices.polygon import PolygonPriceProvider
        return PolygonPriceProvider(api_key=settings.polygon_api_key)
    if provider == "none":
        logger.info("price_provider_not_configured")
        return None
    logger.warning("unknown_price_provider", provider=provider)
    return None


def upsert_price_bar(session, bar: Dict[str, Any]) -> bool:
    """Insert a price bar if it doesn't exist. Returns True if inserted."""
    existing = (
        session.query(MarketPrice)
        .filter(
            MarketPrice.ticker == bar["ticker"],
            MarketPrice.timestamp == bar["timestamp"],
            MarketPrice.interval == bar.get("interval", "1d"),
        )
        .first()
    )
    if existing:
        return False

    now = now_utc()
    mp = MarketPrice(
        ticker=bar["ticker"],
        timestamp=bar["timestamp"],
        open=bar.get("open"),
        high=bar.get("high"),
        low=bar.get("low"),
        close=bar.get("close"),
        volume=bar.get("volume"),
        adjusted_close=bar.get("adjusted_close"),
        interval=bar.get("interval", "1d"),
        provider=bar.get("provider", "polygon"),
        raw_json=bar.get("raw", {}),
        created_at=now,
    )
    session.add(mp)
    return True


async def run(
    tickers: Optional[List[str]] = None,
    days: int = 90,
) -> None:
    configure_logging(settings.log_level)

    provider = _get_price_provider()
    if provider is None:
        logger.info("no_price_provider_configured_skipping")
        return

    end = datetime.date.today()
    start = end - datetime.timedelta(days=days)

    with get_session() as session:
        query = session.query(Company).filter(Company.is_active == True)
        if tickers:
            query = query.filter(Company.ticker.in_([t.upper() for t in tickers]))
        companies = query.all()

        if not companies:
            logger.warning("no_companies_found")
            return

        total_bars = 0
        async with provider:
            for company in companies:
                logger.info("syncing_prices", ticker=company.ticker)
                try:
                    bars = await provider.fetch_daily_bars(company.ticker, start, end)
                except Exception as exc:
                    logger.error("price_fetch_failed", ticker=company.ticker, error=str(exc))
                    continue

                inserted = 0
                for bar in bars:
                    if upsert_price_bar(session, bar):
                        inserted += 1

                session.flush()
                total_bars += inserted
                logger.info(
                    "prices_synced",
                    ticker=company.ticker,
                    fetched=len(bars),
                    inserted=inserted,
                )

        logger.info("price_sync_complete", companies=len(companies), bars_inserted=total_bars)


@click.command()
@click.option("--tickers", default=None, help="Comma-separated tickers")
@click.option("--days", default=90, show_default=True, help="Look-back window in days")
def main(tickers: Optional[str], days: int) -> None:
    """Sync daily price bars from the configured provider."""
    ticker_list = [t.strip().upper() for t in tickers.split(",")] if tickers else None
    asyncio.run(run(ticker_list, days=days))


if __name__ == "__main__":
    main()
