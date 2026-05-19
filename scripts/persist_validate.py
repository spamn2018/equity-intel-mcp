"""
Persistence + idempotency validation script.

Creates a fresh SQLite database, seeds company rows, runs sync_news and
sync_prices, then verifies rows written correctly and that a second run
produces no duplicates.

Usage:
    python scripts/persist_validate.py --tickers AAPL,NVDA
    python scripts/persist_validate.py --tickers AAPL,MSFT,NVDA,TSLA,AMD,META,GOOGL,SMCI,MSTR,PLTR
"""
from __future__ import annotations

import argparse
import asyncio
import datetime
import os
import sys
import tempfile
from pathlib import Path
from typing import List

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env", override=True)

# ── Minimal company metadata for seeding ────────────────────────────────────

COMPANY_META = {
    "AAPL":  {"name": "Apple Inc.",             "cik": "0000320193", "exchange": "NASDAQ"},
    "MSFT":  {"name": "Microsoft Corporation",  "cik": "0000789019", "exchange": "NASDAQ"},
    "NVDA":  {"name": "NVIDIA Corporation",     "cik": "0001045810", "exchange": "NASDAQ"},
    "TSLA":  {"name": "Tesla, Inc.",             "cik": "0001318605", "exchange": "NASDAQ"},
    "AMD":   {"name": "Advanced Micro Devices", "cik": "0000002488", "exchange": "NASDAQ"},
    "META":  {"name": "Meta Platforms, Inc.",   "cik": "0001326801", "exchange": "NASDAQ"},
    "GOOGL": {"name": "Alphabet Inc.",           "cik": "0001652044", "exchange": "NASDAQ"},
    "SMCI":  {"name": "Super Micro Computer",   "cik": "0000886136", "exchange": "NASDAQ"},
    "MSTR":  {"name": "MicroStrategy Inc.",     "cik": "0001050446", "exchange": "NASDAQ"},
    "PLTR":  {"name": "Palantir Technologies",  "cik": "0001321655", "exchange": "NYSE"},
}


def seed_companies(session, tickers: List[str]) -> None:
    """Insert minimal Company rows so sync workers can find them."""
    from equity_intel.db.models import Company, now_utc

    for ticker in tickers:
        existing = session.query(Company).filter(Company.ticker == ticker).first()
        if existing:
            continue
        meta = COMPANY_META.get(ticker, {"name": ticker, "cik": None, "exchange": None})
        c = Company(
            ticker=ticker,
            cik=meta.get("cik"),
            name=meta.get("name"),
            exchange=meta.get("exchange"),
            is_active=True,
            created_at=now_utc(),
            updated_at=now_utc(),
        )
        session.add(c)
    session.commit()
    print(f"  Seeded {len(tickers)} company row(s).")


def count_rows(session, model, ticker=None):
    q = session.query(model)
    if ticker:
        q = q.filter(model.ticker == ticker)
    return q.count()


def check_news_fields(session, model, ticker):
    """Verify required source-grounding fields are non-null on at least one row."""
    from equity_intel.db.models import NewsArticle
    rows = session.query(NewsArticle).filter(
        NewsArticle.ticker == ticker,
        NewsArticle.url.isnot(None),
        NewsArticle.publisher.isnot(None),
        NewsArticle.published_at.isnot(None),
    ).limit(1).all()
    return len(rows) > 0


def check_price_fields(session, model, ticker):
    """Verify OHLCV completeness on at least one row."""
    from equity_intel.db.models import MarketPrice
    rows = session.query(MarketPrice).filter(
        MarketPrice.ticker == ticker,
        MarketPrice.open.isnot(None),
        MarketPrice.high.isnot(None),
        MarketPrice.low.isnot(None),
        MarketPrice.close.isnot(None),
        MarketPrice.volume.isnot(None),
    ).limit(1).all()
    return len(rows) > 0


async def run_sync(tickers: List[str], db_url: str, api_key: str, days: int = 7) -> None:
    """Run news and price sync for the given tickers against a specific DB."""
    import equity_intel.config as cfg_mod
    from equity_intel.config import Settings

    # Override settings for this run
    os.environ["DATABASE_URL"] = db_url
    os.environ["NEWS_PROVIDER"] = "polygon"
    os.environ["PRICE_PROVIDER"] = "polygon"
    os.environ["POLYGON_API_KEY"] = api_key
    cfg_mod.settings = Settings()

    from equity_intel.workers.sync_news import run as news_run
    from equity_intel.workers.sync_prices import run as prices_run

    print("  Running sync_news...")
    await news_run(tickers=tickers, days=days)
    print("  Running sync_prices...")
    await prices_run(tickers=tickers, days=days)


async def main(tickers: List[str], days: int = 7) -> None:
    from equity_intel.config import Settings

    s = Settings()
    if not s.polygon_api_key:
        print("ERROR: POLYGON_API_KEY not set", file=sys.stderr)
        sys.exit(1)

    api_key = s.polygon_api_key

    # Create a fresh temp SQLite DB for this validation run
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db_url = f"sqlite:///{tmp.name}"
    print(f"Validation DB: {tmp.name}")

    # Create schema
    import equity_intel.config as cfg_mod
    os.environ["DATABASE_URL"] = db_url
    cfg_mod.settings = cfg_mod.Settings()

    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from equity_intel.db.models import Base, NewsArticle, MarketPrice

    engine = create_engine(db_url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        print(f"\n[Setup] Seeding {tickers}")
        seed_companies(session, tickers)

    # ── Run 1 ────────────────────────────────────────────────────────────────
    print("\n[Run 1] Syncing news + prices...")
    await run_sync(tickers, db_url, api_key, days=days)

    with Session(engine) as session:
        print("\n[Run 1 Results]")
        all_ok = True
        for ticker in tickers:
            news_n = count_rows(session, NewsArticle, ticker)
            price_n = count_rows(session, MarketPrice, ticker)
            news_fields_ok = check_news_fields(session, NewsArticle, ticker)
            price_fields_ok = check_price_fields(session, MarketPrice, ticker)
            status = "✓" if (news_n > 0 and price_n > 0) else "✗"
            if news_n == 0 or price_n == 0:
                all_ok = False
            print(
                f"  {status} {ticker}: news={news_n} rows  prices={price_n} bars  "
                f"news_source_grounded={news_fields_ok}  price_ohlcv_complete={price_fields_ok}"
            )

        run1_news_totals = {t: count_rows(session, NewsArticle, t) for t in tickers}
        run1_price_totals = {t: count_rows(session, MarketPrice, t) for t in tickers}

    # ── Run 2 (idempotency check) ────────────────────────────────────────────
    print("\n[Run 2] Re-running sync (idempotency check)...")
    await run_sync(tickers, db_url, api_key, days=days)

    print("\n[Idempotency Results]")
    idempotency_ok = True
    with Session(engine) as session:
        for ticker in tickers:
            news_n2 = count_rows(session, NewsArticle, ticker)
            price_n2 = count_rows(session, MarketPrice, ticker)
            news_delta = news_n2 - run1_news_totals[ticker]
            price_delta = price_n2 - run1_price_totals[ticker]
            news_ok = news_delta == 0
            price_ok = price_delta == 0
            if not news_ok or not price_ok:
                idempotency_ok = False
            symbol = "✓" if (news_ok and price_ok) else "✗"
            print(
                f"  {symbol} {ticker}: news {run1_news_totals[ticker]}→{news_n2} "
                f"(+{news_delta} new)  prices {run1_price_totals[ticker]}→{price_n2} "
                f"(+{price_delta} new)"
            )

    # ── MCP surface check ─────────────────────────────────────────────────────
    print("\n[MCP Surface Check]")
    with Session(engine) as session:
        from equity_intel.mcp_server.tools import get_recent_news, explain_stock_move

        # Patch session in config so MCP tools can be tested directly
        for ticker in tickers[:2]:  # spot-check first 2
            news_result = get_recent_news(session, ticker=ticker, days=days)
            articles = news_result.get("articles", [])
            has_url = any(a.get("url") for a in articles)
            has_pub = any(a.get("publisher") for a in articles)
            has_ts = any(a.get("published_at") for a in articles)
            print(
                f"  get_recent_news({ticker}): {len(articles)} articles  "
                f"url={has_url}  publisher={has_pub}  published_at={has_ts}"
            )

            explain_result = explain_stock_move(
                session, ticker=ticker,
                date=(datetime.date.today() - datetime.timedelta(days=2)).isoformat(),
                window=3,
            )
            price_avail = explain_result.get("price_move", {}).get("available", False)
            has_caution = "caution" in explain_result
            print(
                f"  explain_stock_move({ticker}): price_available={price_avail}  "
                f"has_caution={has_caution}  evidence_count={explain_result.get('evidence_count', 0)}"
            )

    # ── Summary ───────────────────────────────────────────────────────────────
    print("\n=== SUMMARY ===")
    print(f"Tickers validated:  {tickers}")
    print(f"Persistence (Run 1): {'PASS' if all_ok else 'FAIL'}")
    print(f"Idempotency (Run 2): {'PASS' if idempotency_ok else 'FAIL'}")

    # Cleanup
    os.unlink(tmp.name)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--tickers", default="AAPL,NVDA")
    parser.add_argument("--days", type=int, default=7)
    args = parser.parse_args()
    tickers = [t.strip().upper() for t in args.tickers.split(",")]
    asyncio.run(main(tickers, days=args.days))
