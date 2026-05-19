"""
Worker: sync company universe (ticker → CIK mapping).

Usage:
    python -m equity_intel.workers.sync_companies
    python -m equity_intel.workers.sync_companies --tickers AAPL,MSFT,NVDA
"""
from __future__ import annotations

import asyncio
import sys
from typing import List, Optional

import click

from equity_intel.config import settings
from equity_intel.db.session import get_session
from equity_intel.logging_config import configure_logging, get_logger
from equity_intel.sec.cik import sync_company_universe
from equity_intel.sec.client import SECClient

logger = get_logger(__name__)


async def run(tickers: Optional[List[str]] = None) -> None:
    configure_logging(settings.log_level)
    async with SECClient() as client:
        with get_session() as session:
            result = await sync_company_universe(
                session=session,
                client=client,
                tickers=tickers,
            )
            logger.info("sync_companies_complete", synced=len(result))


@click.command()
@click.option("--tickers", default=None, help="Comma-separated list of tickers to sync")
def main(tickers: Optional[str]) -> None:
    """Sync company universe from SEC EDGAR."""
    ticker_list: Optional[List[str]] = None
    if tickers:
        ticker_list = [t.strip().upper() for t in tickers.split(",") if t.strip()]
    else:
        ticker_list = settings.tickers_list

    asyncio.run(run(ticker_list))


if __name__ == "__main__":
    main()
