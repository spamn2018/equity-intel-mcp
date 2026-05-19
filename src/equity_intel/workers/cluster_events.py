"""
Worker: cluster all existing events into EventCluster records.

Run this as a one-time backfill after upgrading from the pre-cluster schema,
or re-run at any time to pick up unclustered events.

Usage:
    python -m equity_intel.workers.cluster_events
    python -m equity_intel.workers.cluster_events --tickers AAPL,MSFT
"""
from __future__ import annotations

from typing import List, Optional

import click

from equity_intel.config import settings
from equity_intel.db.models import Company
from equity_intel.db.session import get_session
from equity_intel.events.cluster import cluster_events_for_company
from equity_intel.logging_config import configure_logging, get_logger

logger = get_logger(__name__)


def run(tickers: Optional[List[str]] = None) -> None:
    configure_logging(settings.log_level)

    with get_session() as session:
        query = session.query(Company).filter(Company.is_active == True)
        if tickers:
            query = query.filter(Company.ticker.in_([t.upper() for t in tickers]))
        companies = query.all()

        if not companies:
            logger.warning("no_companies_found")
            return

        total = 0
        for company in companies:
            logger.info("clustering_events", ticker=company.ticker)
            try:
                n = cluster_events_for_company(session, company)
                total += n
            except Exception as exc:
                logger.error("cluster_failed", ticker=company.ticker, error=str(exc))

        logger.info("cluster_events_complete", companies=len(companies), events_clustered=total)


@click.command()
@click.option("--tickers", default=None, help="Comma-separated tickers (default: all active)")
def main(tickers: Optional[str]) -> None:
    """Cluster existing events into EventCluster records."""
    ticker_list = [t.strip().upper() for t in tickers.split(",")] if tickers else None
    run(ticker_list)


if __name__ == "__main__":
    main()
