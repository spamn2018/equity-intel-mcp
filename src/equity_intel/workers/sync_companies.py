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


def _universe_tickers() -> List[str]:
    """Return all unique tickers from the research universe, excluding prohibited ones."""
    try:
        from equity_intel.research_universe import load_research_universe
        universe = load_research_universe()
        prohibited = set(settings.prohibited_tickers_list)
        seen: set = set()
        result: List[str] = []
        for cat_data in universe.get("categories", {}).values():
            for entry in cat_data.get("tickers", []):
                if not isinstance(entry, dict):
                    continue
                ticker = (entry.get("ticker") or "").strip().upper()
                if ticker and ticker not in prohibited and ticker not in seen:
                    seen.add(ticker)
                    result.append(ticker)
        if result:
            return result
    except Exception as exc:
        logger.warning("research_universe_load_failed_falling_back", error=str(exc))
    # Fallback to DEFAULT_TICKERS if universe file is missing
    return settings.tickers_list


async def run(tickers: Optional[List[str]] = None) -> None:
    configure_logging(settings.log_level)
    reconcile_active_universe = tickers is None
    if tickers is None:
        tickers = _universe_tickers()

    async with SECClient() as client:
        with get_session() as session:
            result = await sync_company_universe(
                session=session,
                client=client,
                tickers=tickers,
                reconcile_active_universe=reconcile_active_universe,
            )
            logger.info("sync_companies_complete", synced=len(result))


@click.command()
@click.option("--tickers", default=None, help="Comma-separated list of tickers to sync")
def main(tickers: Optional[str]) -> None:
    """Sync company universe from SEC EDGAR."""
    ticker_list: Optional[List[str]] = None
    if tickers:
        ticker_list = [t.strip().upper() for t in tickers.split(",") if t.strip()]

    asyncio.run(run(ticker_list))


if __name__ == "__main__":
    main()
