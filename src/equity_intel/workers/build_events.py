"""
Worker: build events from filings and news.

Usage:
    python -m equity_intel.workers.build_events
    python -m equity_intel.workers.build_events --tickers AAPL --days 30
"""
from __future__ import annotations

import asyncio
from typing import List, Optional

import click

from equity_intel.config import settings
from equity_intel.db.models import Company
from equity_intel.db.session import get_session
from equity_intel.events.build import build_events_for_company
from equity_intel.logging_config import configure_logging, get_logger

logger = get_logger(__name__)


def run(tickers: Optional[List[str]] = None, days: int = 90) -> None:
    configure_logging(settings.log_level)

    with get_session() as session:
        query = session.query(Company).filter(Company.is_active == True)
        if tickers:
            query = query.filter(Company.ticker.in_([t.upper() for t in tickers]))
        companies = query.all()

        total = 0
        for company in companies:
            count = build_events_for_company(session, company, days=days)
            total += count
            session.flush()
            logger.info("events_built", ticker=company.ticker, new_events=count)

        logger.info("build_events_complete", companies=len(companies), total_events=total)


@click.command()
@click.option("--tickers", default=None, help="Comma-separated tickers")
@click.option("--days", default=90, show_default=True, help="Look-back window in days")
def main(tickers: Optional[str], days: int) -> None:
    """Build events from filings and news."""
    ticker_list = [t.strip().upper() for t in tickers.split(",")] if tickers else None
    run(ticker_list, days=days)


if __name__ == "__main__":
    main()
