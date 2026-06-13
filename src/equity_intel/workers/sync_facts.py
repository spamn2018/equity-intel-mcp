"""
Worker: sync XBRL company facts for all tracked companies.

Usage:
    python -m equity_intel.workers.sync_facts
    python -m equity_intel.workers.sync_facts --tickers AAPL,MSFT
"""
from __future__ import annotations

import asyncio
from typing import List, Optional

import click

from equity_intel.config import settings
from equity_intel.db.models import Company
from equity_intel.db.session import get_session
from equity_intel.logging_config import configure_logging, get_logger
from equity_intel.sec.client import SECClient
from equity_intel.sec.facts import sync_company_facts

logger = get_logger(__name__)


async def run(tickers: Optional[List[str]] = None) -> None:
    configure_logging(settings.log_level)
    prohibited = set(settings.prohibited_tickers_list)

    async with SECClient() as client:
        with get_session() as session:
            query = session.query(Company).filter(
                Company.is_active == True, Company.cik.isnot(None)
            )
            if prohibited:
                query = query.filter(Company.ticker.notin_(prohibited))
            if tickers:
                query = query.filter(
                    Company.ticker.in_([t.upper() for t in tickers if t.upper() not in prohibited])
                )
            companies = query.all()

            total = 0
            for company in companies:
                count = await sync_company_facts(session=session, client=client, company=company)
                total += count
                session.flush()

            logger.info("facts_sync_complete", companies=len(companies), facts=total)


@click.command()
@click.option("--tickers", default=None, help="Comma-separated tickers")
def main(tickers: Optional[str]) -> None:
    """Sync XBRL company facts from SEC EDGAR."""
    ticker_list = [t.strip().upper() for t in tickers.split(",")] if tickers else None
    asyncio.run(run(ticker_list))


if __name__ == "__main__":
    main()
