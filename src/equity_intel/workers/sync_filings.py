"""
Worker: sync recent SEC filings for all tracked companies.

Usage:
    python -m equity_intel.workers.sync_filings
    python -m equity_intel.workers.sync_filings --tickers AAPL,TSLA --days 30
"""
from __future__ import annotations

import asyncio
from typing import List, Optional, Set

import click

from equity_intel.config import settings
from equity_intel.db.models import Company
from equity_intel.db.session import get_session
from equity_intel.logging_config import configure_logging, get_logger
from equity_intel.sec.client import SECClient
from equity_intel.sec.filings import PRIORITY_FORMS, sync_company_filings

logger = get_logger(__name__)


async def run(
    tickers: Optional[List[str]] = None,
    days: int = 90,
    form_filter: Optional[Set[str]] = None,
) -> None:
    configure_logging(settings.log_level)

    async with SECClient() as client:
        with get_session() as session:
            query = session.query(Company).filter(Company.is_active == True)
            if tickers:
                upper = [t.upper() for t in tickers]
                query = query.filter(Company.ticker.in_(upper))
            companies = query.all()

            if not companies:
                logger.warning("no_companies_found_run_sync_companies_first")
                return

            total_filings = 0
            for company in companies:
                filings = await sync_company_filings(
                    session=session,
                    client=client,
                    company=company,
                    days=days,
                    form_filter=form_filter or PRIORITY_FORMS,
                )
                total_filings += len(filings)
                session.flush()

            logger.info("sync_filings_complete", companies=len(companies), filings=total_filings)


@click.command()
@click.option("--tickers", default=None, help="Comma-separated tickers")
@click.option("--days", default=90, show_default=True, help="Look-back window in days")
@click.option("--forms", default=None, help="Comma-separated form types to include")
def main(tickers: Optional[str], days: int, forms: Optional[str]) -> None:
    """Sync recent SEC filings."""
    ticker_list = [t.strip().upper() for t in tickers.split(",")] if tickers else None
    form_set: Optional[Set[str]] = (
        {f.strip() for f in forms.split(",")} if forms else None
    )
    asyncio.run(run(ticker_list, days=days, form_filter=form_set))


if __name__ == "__main__":
    main()
