"""
CIK utilities and company universe sync.

Maps ticker symbols to SEC CIK numbers using the official SEC ticker map.
"""
from __future__ import annotations

import datetime
from typing import Dict, List, Optional, Tuple

from sqlalchemy.orm import Session
from sqlalchemy.dialects.postgresql import insert as pg_insert

from equity_intel.db.models import Company, now_utc
from equity_intel.logging_config import get_logger
from equity_intel.sec.client import SECClient, normalize_cik

logger = get_logger(__name__)

# ETFs and funds tracked for news/price context only.
# They are not in the SEC's equity ticker map and have no useful 10-K/8-K
# filings — their SEC filings are N-PORT / N-CEN (fund reports), which are
# out of scope. Upsert with known metadata but skip the WARNING log.
KNOWN_ETFS: Dict[str, Dict] = {
    "BOTZ": {"name": "Global X Robotics & Artificial Intelligence ETF", "exchange": "NASDAQ"},
    "ROBO": {"name": "ROBO Global Robotics and Automation Index ETF",   "exchange": "NYSE"},
}


async def fetch_ticker_cik_map(client: SECClient) -> Dict[str, str]:
    """
    Fetch the SEC ticker-to-CIK mapping.

    Returns dict of uppercase_ticker -> 10-digit CIK string.
    """
    data = await client.get_ticker_map()
    result: Dict[str, str] = {}
    for entry in data.values():
        ticker = str(entry.get("ticker", "")).upper().strip()
        cik = entry.get("cik_str") or entry.get("cik")
        if ticker and cik:
            result[ticker] = normalize_cik(cik)
    return result


async def fetch_ticker_exchange_map(client: SECClient) -> Dict[str, Dict]:
    """
    Fetch the extended SEC ticker-exchange map.

    Returns dict of uppercase_ticker -> {cik, name, exchange, ...}
    """
    data = await client.get_ticker_exchange_map()
    result: Dict[str, Dict] = {}
    fields = data.get("fields", [])
    rows = data.get("data", [])
    for row in rows:
        row_dict = dict(zip(fields, row))
        ticker = str(row_dict.get("ticker", "")).upper().strip()
        cik = row_dict.get("cik")
        if ticker and cik:
            row_dict["cik"] = normalize_cik(cik)
            result[ticker] = row_dict
    return result


def upsert_company(
    session: Session,
    ticker: str,
    cik: Optional[str] = None,
    name: Optional[str] = None,
    exchange: Optional[str] = None,
    **kwargs,
) -> Company:
    """Upsert a company record by ticker. Returns the Company ORM object."""
    ticker = ticker.upper().strip()
    existing = session.query(Company).filter(Company.ticker == ticker).first()
    now = now_utc()

    if existing:
        if cik and existing.cik != cik:
            existing.cik = cik
        if name and not existing.name:
            existing.name = name
        if exchange and not existing.exchange:
            existing.exchange = exchange
        for k, v in kwargs.items():
            if v is not None and hasattr(existing, k):
                setattr(existing, k, v)
        existing.updated_at = now
        return existing

    company = Company(
        ticker=ticker,
        cik=cik,
        name=name,
        exchange=exchange,
        created_at=now,
        updated_at=now,
        **{k: v for k, v in kwargs.items() if hasattr(Company, k)},
    )
    session.add(company)
    return company


async def sync_company_universe(
    session: Session,
    client: SECClient,
    tickers: Optional[List[str]] = None,
) -> Dict[str, str]:
    """
    Sync ticker-to-CIK mapping from SEC.

    If tickers is provided, only upsert those tickers.
    Returns dict of ticker -> CIK for all synced companies.
    """
    logger.info("fetching_sec_ticker_map")

    try:
        exchange_map = await fetch_ticker_exchange_map(client)
    except Exception as exc:
        logger.warning("exchange_map_failed", error=str(exc))
        exchange_map = {}

    # Fall back to simple ticker map if exchange map fails
    if not exchange_map:
        simple_map = await fetch_ticker_cik_map(client)
        exchange_map = {t: {"cik": c} for t, c in simple_map.items()}

    if tickers:
        tickers_upper = {t.upper() for t in tickers}
        filtered = {t: v for t, v in exchange_map.items() if t in tickers_upper}
        # Also add any tickers not found in map
        for t in tickers_upper:
            if t not in filtered:
                if t in KNOWN_ETFS:
                    # ETFs are intentionally absent from the SEC equity map —
                    # they file N-PORT/N-CEN, not 10-K/8-K. Not a problem.
                    logger.debug("etf_skipped_no_sec_cik", ticker=t)
                    filtered[t] = {**KNOWN_ETFS[t], "cik": None}
                else:
                    logger.warning("ticker_not_found_in_sec_map", ticker=t)
                    filtered[t] = {}
        exchange_map = filtered

    result: Dict[str, str] = {}
    for ticker, info in exchange_map.items():
        cik = info.get("cik")
        name = info.get("name") or info.get("title")
        exchange = info.get("exchange")
        upsert_company(session, ticker=ticker, cik=cik, name=name, exchange=exchange)
        if cik:
            result[ticker] = cik

    logger.info("company_universe_synced", count=len(exchange_map))
    return result
