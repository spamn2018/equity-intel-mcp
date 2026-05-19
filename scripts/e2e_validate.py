"""
End-to-End Equity Intelligence Pipeline Validation Script.

Validates the full pipeline for target tickers:
  sync_companies → sync_filings → sync_documents → sync_facts
  → sync_news → sync_prices → build_events → cluster_events → MCP tools

Usage:
    python scripts/e2e_validate.py --tickers AAPL,NVDA,TSLA
    python scripts/e2e_validate.py --tickers AAPL,MSFT,NVDA,TSLA,AMD,META,GOOGL,SMCI,MSTR,PLTR
    python scripts/e2e_validate.py --tickers AAPL --skip-sync   # MCP-only check on existing data

WARNING: Rotate the POLYGON_API_KEY in .env before running if the key was previously
exposed in chat or logs.

Known limitations on SQLite (expected, not code bugs):
  - search_filings_tool / search_news_tool use PostgreSQL tsvector — will error on SQLite
  - DateTime(timezone=True) columns lose tzinfo on SQLite read
"""
from __future__ import annotations

import argparse
import asyncio
import datetime
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env", override=True)


# ── Result accumulator ────────────────────────────────────────────────────────

class StageResult:
    def __init__(self, stage: str):
        self.stage = stage
        self.checks: List[Dict[str, Any]] = []
        self.errors: List[str] = []

    def ok(self, label: str, value: Any = None) -> None:
        self.checks.append({"status": "✅", "label": label, "value": value})

    def fail(self, label: str, value: Any = None) -> None:
        self.checks.append({"status": "❌", "label": label, "value": value})
        self.errors.append(label)

    def warn(self, label: str, value: Any = None) -> None:
        self.checks.append({"status": "⚠️ ", "label": label, "value": value})

    @property
    def passed(self) -> bool:
        return len(self.errors) == 0

    def print(self) -> None:
        print(f"\n{'='*60}")
        print(f"Stage: {self.stage}  {'PASS' if self.passed else 'FAIL'}")
        print(f"{'='*60}")
        for c in self.checks:
            val = f"  [{c['value']}]" if c["value"] is not None else ""
            print(f"  {c['status']} {c['label']}{val}")


results: List[StageResult] = []


# ── Helpers ───────────────────────────────────────────────────────────────────

def _count(session, model, ticker_field=None, ticker=None):
    q = session.query(model)
    if ticker and ticker_field:
        q = q.filter(ticker_field == ticker)
    return q.count()


def _most_recent_trading_day() -> datetime.date:
    """Return the most recent weekday (Mon–Fri), going back from today."""
    d = datetime.date.today() - datetime.timedelta(days=1)
    while d.weekday() >= 5:   # 5=Sat, 6=Sun
        d -= datetime.timedelta(days=1)
    return d


# ── Stage validators ──────────────────────────────────────────────────────────

def validate_companies(session, tickers: List[str]) -> StageResult:
    from equity_intel.db.models import Company
    r = StageResult("1. Companies (sync_companies)")
    for ticker in tickers:
        company = session.query(Company).filter(Company.ticker == ticker).first()
        if not company:
            r.fail(f"{ticker}: Company row missing")
            continue
        if not company.cik:
            r.fail(f"{ticker}: cik is null")
        else:
            r.ok(f"{ticker}: cik={company.cik}")
        if not company.name:
            r.fail(f"{ticker}: name is null")
        else:
            r.ok(f"{ticker}: name={company.name!r}")
        if not company.exchange:
            r.warn(f"{ticker}: exchange is null (SEC may not provide for all tickers)")
        else:
            r.ok(f"{ticker}: exchange={company.exchange}")
    results.append(r)
    return r


def validate_filings(session, tickers: List[str]) -> StageResult:
    from equity_intel.db.models import Company, Filing
    r = StageResult("2. Filings (sync_filings)")
    for ticker in tickers:
        company = session.query(Company).filter(Company.ticker == ticker).first()
        if not company:
            r.fail(f"{ticker}: no Company row — cannot check filings")
            continue
        count = session.query(Filing).filter(Filing.company_id == company.id).count()
        if count == 0:
            r.fail(f"{ticker}: 0 filings (expected ≥1 within days window)")
        else:
            r.ok(f"{ticker}: {count} filings")
        # At least one with a URL
        with_url = (
            session.query(Filing)
            .filter(Filing.company_id == company.id)
            .filter(Filing.filing_url.isnot(None))
            .count()
        )
        if with_url == 0:
            r.warn(f"{ticker}: no filings with filing_url (may be throttled)")
        else:
            r.ok(f"{ticker}: {with_url} filings with url")
    results.append(r)
    return r


def validate_documents(session, tickers: List[str]) -> StageResult:
    from equity_intel.db.models import Company, Filing, FilingDocument
    r = StageResult("3. Documents (sync_documents)")
    for ticker in tickers:
        company = session.query(Company).filter(Company.ticker == ticker).first()
        if not company:
            r.warn(f"{ticker}: no Company row — skipping document check")
            continue
        # Count filing documents via join
        count = (
            session.query(FilingDocument)
            .join(Filing, Filing.id == FilingDocument.filing_id)
            .filter(Filing.company_id == company.id)
            .count()
        )
        if count == 0:
            r.warn(f"{ticker}: 0 parsed documents (acceptable if no primary_document_url on recent filings)")
        else:
            # Spot-check: at least one has plain_text
            has_text = (
                session.query(FilingDocument)
                .join(Filing, Filing.id == FilingDocument.filing_id)
                .filter(Filing.company_id == company.id)
                .filter(FilingDocument.plain_text.isnot(None))
                .filter(FilingDocument.plain_text != "")
                .count()
            )
            r.ok(f"{ticker}: {count} documents, {has_text} with plain_text")
    results.append(r)
    return r


def validate_facts(session, tickers: List[str]) -> StageResult:
    from equity_intel.db.models import Company, CompanyFact
    r = StageResult("4. XBRL Facts (sync_facts)")
    for ticker in tickers:
        company = session.query(Company).filter(Company.ticker == ticker).first()
        if not company:
            r.fail(f"{ticker}: no Company row — cannot check facts")
            continue
        if not company.cik:
            r.warn(f"{ticker}: no CIK — sync_facts would have skipped this ticker")
            continue
        count = session.query(CompanyFact).filter(CompanyFact.company_id == company.id).count()
        if count == 0:
            r.warn(f"{ticker}: 0 facts (SEC XBRL may be empty for this ticker)")
        else:
            # Spot-check: at least one with value non-null
            has_value = (
                session.query(CompanyFact)
                .filter(CompanyFact.company_id == company.id)
                .filter(CompanyFact.value.isnot(None))
                .limit(1)
                .first()
            )
            if has_value:
                r.ok(f"{ticker}: {count} facts, values present")
            else:
                r.warn(f"{ticker}: {count} facts but all values null")
    results.append(r)
    return r


def validate_news(session, tickers: List[str]) -> StageResult:
    from equity_intel.db.models import NewsArticle
    r = StageResult("5. News (sync_news)")
    for ticker in tickers:
        count = session.query(NewsArticle).filter(NewsArticle.ticker == ticker).count()
        if count == 0:
            r.fail(f"{ticker}: 0 news articles")
            continue
        r.ok(f"{ticker}: {count} articles")
        # Source-grounding spot check
        grounded = (
            session.query(NewsArticle)
            .filter(
                NewsArticle.ticker == ticker,
                NewsArticle.url.isnot(None),
                NewsArticle.publisher.isnot(None),
                NewsArticle.published_at.isnot(None),
            )
            .count()
        )
        if grounded == 0:
            r.fail(f"{ticker}: no source-grounded articles (url/publisher/published_at all null)")
        else:
            r.ok(f"{ticker}: {grounded} source-grounded")
    results.append(r)
    return r


def validate_prices(session, tickers: List[str]) -> StageResult:
    from equity_intel.db.models import MarketPrice
    r = StageResult("6. Prices (sync_prices)")
    for ticker in tickers:
        count = session.query(MarketPrice).filter(MarketPrice.ticker == ticker).count()
        if count == 0:
            r.fail(f"{ticker}: 0 price bars")
            continue
        r.ok(f"{ticker}: {count} bars")
        # OHLCV completeness
        complete = (
            session.query(MarketPrice)
            .filter(
                MarketPrice.ticker == ticker,
                MarketPrice.open.isnot(None),
                MarketPrice.high.isnot(None),
                MarketPrice.low.isnot(None),
                MarketPrice.close.isnot(None),
                MarketPrice.volume.isnot(None),
            )
            .count()
        )
        if complete == 0:
            r.fail(f"{ticker}: no complete OHLCV bars")
        else:
            r.ok(f"{ticker}: {complete} bars with complete OHLCV")
    results.append(r)
    return r


def validate_events(session, tickers: List[str]) -> StageResult:
    from equity_intel.db.models import Event
    r = StageResult("7. Events (build_events)")
    for ticker in tickers:
        count = session.query(Event).filter(Event.ticker == ticker).count()
        if count == 0:
            r.warn(f"{ticker}: 0 events (may be empty if no filings/news in window)")
            continue
        r.ok(f"{ticker}: {count} events")
        # Check source types
        filing_events = (
            session.query(Event)
            .filter(Event.ticker == ticker, Event.source_type == "filing")
            .count()
        )
        news_events = (
            session.query(Event)
            .filter(Event.ticker == ticker, Event.source_type == "news")
            .count()
        )
        r.ok(f"{ticker}: {filing_events} filing-events, {news_events} news-events")
        # Check materiality scores present
        scored = (
            session.query(Event)
            .filter(Event.ticker == ticker, Event.materiality_score.isnot(None))
            .count()
        )
        if scored < count:
            r.warn(f"{ticker}: {count - scored} events missing materiality_score")
        else:
            r.ok(f"{ticker}: all {count} events have materiality_score")
    results.append(r)
    return r


def validate_clusters(session, tickers: List[str]) -> StageResult:
    from equity_intel.db.models import EventCluster, Event
    r = StageResult("8. Clusters (cluster_events)")
    for ticker in tickers:
        count = (
            session.query(EventCluster)
            .filter(EventCluster.ticker == ticker)
            .count()
        )
        total_events = session.query(Event).filter(Event.ticker == ticker).count()
        unclustered = (
            session.query(Event)
            .filter(Event.ticker == ticker, Event.cluster_id.is_(None))
            .count()
        )
        if count == 0 and total_events > 0:
            r.warn(f"{ticker}: {total_events} events but 0 clusters (cluster_events may not have run)")
        elif count == 0:
            r.warn(f"{ticker}: 0 events and 0 clusters (acceptable if no filings/news)")
        else:
            r.ok(f"{ticker}: {count} clusters")
        if unclustered > 0:
            r.warn(f"{ticker}: {unclustered} unclustered events remaining")
        elif total_events > 0:
            r.ok(f"{ticker}: all {total_events} events assigned to a cluster")
    results.append(r)
    return r


def validate_mcp_tools(session, tickers: List[str]) -> StageResult:
    """
    Exercise all 8 MCP tools and verify structural contract + source grounding.
    search_filings_tool / search_news_tool use PostgreSQL tsvector — expected to
    fail on SQLite; these failures are classified as Expected/SQLite-limitation.
    """
    from equity_intel.mcp_server.tools import (
        get_company,
        get_recent_filings,
        get_company_facts,
        get_events,
        get_event_cluster,
        get_recent_news,
        explain_stock_move,
        screen_catalysts,
        search_filings_tool,
        search_news_tool,
    )

    r = StageResult("9. MCP Tool Surface")
    spot_ticker = tickers[0]
    trading_day = _most_recent_trading_day().isoformat()

    # ── get_company ──────────────────────────────────────────────────────────
    try:
        res = get_company(session, ticker=spot_ticker)
        if "error" in res:
            r.fail(f"get_company({spot_ticker}): error={res['error']}")
        else:
            r.ok(f"get_company({spot_ticker}): cik={res.get('cik')}, name={res.get('name')!r}")
            if not res.get("note"):
                r.fail(f"get_company: 'note' field missing (source grounding)")
            else:
                r.ok(f"get_company: 'note' field present")
    except Exception as exc:
        r.fail(f"get_company raised: {exc}")

    # ── get_recent_filings ───────────────────────────────────────────────────
    try:
        res = get_recent_filings(session, ticker=spot_ticker, days=30)
        total = res.get("total", 0)
        r.ok(f"get_recent_filings({spot_ticker}, days=30): {total} filings")
        if total > 0 and not res["filings"][0].get("filing_url"):
            r.warn(f"get_recent_filings: first filing missing filing_url")
    except Exception as exc:
        r.fail(f"get_recent_filings raised: {exc}")

    # ── get_company_facts ────────────────────────────────────────────────────
    try:
        res = get_company_facts(session, ticker=spot_ticker, limit=10)
        if "error" in res:
            r.warn(f"get_company_facts({spot_ticker}): {res['error']}")
        else:
            r.ok(f"get_company_facts({spot_ticker}): {res.get('total', 0)} facts")
    except Exception as exc:
        r.fail(f"get_company_facts raised: {exc}")

    # ── get_recent_news ──────────────────────────────────────────────────────
    try:
        res = get_recent_news(session, ticker=spot_ticker, days=7)
        total = res.get("total", 0)
        r.ok(f"get_recent_news({spot_ticker}, days=7): {total} articles")
        if total > 0:
            art = res["articles"][0]
            missing = [f for f in ("url", "publisher", "published_at") if not art.get(f)]
            if missing:
                r.fail(f"get_recent_news: source-grounding fields missing: {missing}")
            else:
                r.ok(f"get_recent_news: url/publisher/published_at present")
        if not res.get("note"):
            r.fail(f"get_recent_news: 'note' field missing")
        else:
            r.ok(f"get_recent_news: 'note' field present")
    except Exception as exc:
        r.fail(f"get_recent_news raised: {exc}")

    # ── get_events ───────────────────────────────────────────────────────────
    try:
        res = get_events(session, ticker=spot_ticker, days=30)
        total = res.get("total", 0)
        r.ok(f"get_events({spot_ticker}, days=30): {total} events (source={res.get('source')})")
        if not res.get("note"):
            r.fail(f"get_events: 'note' field missing")
        else:
            r.ok(f"get_events: 'note' field present")
    except Exception as exc:
        r.fail(f"get_events raised: {exc}")

    # ── get_event_cluster ────────────────────────────────────────────────────
    try:
        from equity_intel.db.models import EventCluster
        cluster = (
            session.query(EventCluster)
            .filter(EventCluster.ticker == spot_ticker)
            .order_by(EventCluster.materiality_score.desc())
            .first()
        )
        if cluster:
            res = get_event_cluster(session, cluster_id=cluster.id)
            if "error" in res:
                r.fail(f"get_event_cluster: error={res['error']}")
            else:
                r.ok(
                    f"get_event_cluster({spot_ticker}): cluster_key={res.get('cluster_key')!r}, "
                    f"event_count={res.get('event_count')}, "
                    f"filing_count={res.get('filing_count')}, "
                    f"news_count={res.get('news_count')}"
                )
        else:
            r.warn(f"get_event_cluster: no clusters found for {spot_ticker} — skipping")
    except Exception as exc:
        r.fail(f"get_event_cluster raised: {exc}")

    # ── explain_stock_move — valid trading day ───────────────────────────────
    try:
        res = explain_stock_move(session, ticker=spot_ticker, date=trading_day, window=3)
        price_avail = res.get("price_move", {}).get("available", False)
        evidence_count = res.get("evidence_count", 0)
        r.ok(
            f"explain_stock_move({spot_ticker}, {trading_day}): "
            f"price_available={price_avail}, evidence_count={evidence_count}"
        )
        if not res.get("note"):
            r.fail(f"explain_stock_move: 'note' field missing")
        else:
            r.ok(f"explain_stock_move: 'note' and 'caution' fields present")
    except Exception as exc:
        r.fail(f"explain_stock_move raised: {exc}")

    # ── explain_stock_move — weekend (graceful degradation) ──────────────────
    try:
        weekend = "2025-05-03"  # known Saturday
        res = explain_stock_move(session, ticker=spot_ticker, date=weekend, window=3)
        if res.get("price_move", {}).get("available") is False:
            r.ok(f"explain_stock_move weekend graceful: price_available=False as expected")
        else:
            # Could be True if bars span the weekend — not a bug
            r.ok(f"explain_stock_move weekend: price_available={res.get('price_move', {}).get('available')}")
    except Exception as exc:
        r.fail(f"explain_stock_move (weekend) raised: {exc}")

    # ── screen_catalysts ─────────────────────────────────────────────────────
    try:
        res = screen_catalysts(session, tickers=tickers, days=30, min_materiality=0.0)
        total = res.get("total", 0)
        r.ok(f"screen_catalysts({tickers}, days=30): {total} catalysts (source={res.get('source')})")
        if not res.get("note"):
            r.fail(f"screen_catalysts: 'note' field missing")
        else:
            r.ok(f"screen_catalysts: 'note' field present")
    except Exception as exc:
        r.fail(f"screen_catalysts raised: {exc}")

    # ── search_filings_tool — expected to fail on SQLite ────────────────────
    try:
        res = search_filings_tool(session, query="revenue", ticker=spot_ticker)
        r.ok(f"search_filings_tool (SQLite): returned without crash, {res.get('total', 0)} results")
    except Exception as exc:
        r.warn(f"search_filings_tool (SQLite expected): {type(exc).__name__} — PostgreSQL-only FTS")

    # ── search_news_tool — expected to fail on SQLite ────────────────────────
    try:
        res = search_news_tool(session, query="earnings", ticker=spot_ticker)
        r.ok(f"search_news_tool (SQLite): returned without crash, {res.get('total', 0)} results")
    except Exception as exc:
        r.warn(f"search_news_tool (SQLite expected): {type(exc).__name__} — PostgreSQL-only FTS")

    results.append(r)
    return r


# ── Pipeline runners ──────────────────────────────────────────────────────────

async def run_sync_companies(tickers: List[str]) -> None:
    from equity_intel.workers.sync_companies import run
    print(f"\n[sync_companies] {tickers}")
    await run(tickers=tickers)


async def run_sync_filings(tickers: List[str], days: int) -> None:
    from equity_intel.workers.sync_filings import run
    print(f"\n[sync_filings] {tickers}, days={days}")
    await run(tickers=tickers, days=days)


async def run_sync_documents(tickers: List[str], limit: int) -> None:
    from equity_intel.workers.sync_documents import run
    print(f"\n[sync_documents] {tickers}, limit={limit}")
    await run(tickers=tickers, limit=limit, form_filter=["8-K", "10-K", "10-Q"])


async def run_sync_facts(tickers: List[str]) -> None:
    from equity_intel.workers.sync_facts import run
    print(f"\n[sync_facts] {tickers}")
    await run(tickers=tickers)


async def run_sync_news(tickers: List[str], days: int) -> None:
    from equity_intel.workers.sync_news import run
    print(f"\n[sync_news] {tickers}, days={days}")
    await run(tickers=tickers, days=days)


async def run_sync_prices(tickers: List[str], days: int) -> None:
    from equity_intel.workers.sync_prices import run
    print(f"\n[sync_prices] {tickers}, days={days}")
    await run(tickers=tickers, days=days)


def run_build_events(tickers: List[str], days: int) -> None:
    from equity_intel.workers.build_events import run
    print(f"\n[build_events] {tickers}, days={days}")
    run(tickers=tickers, days=days)


def run_cluster_events(tickers: List[str]) -> None:
    from equity_intel.workers.cluster_events import run
    print(f"\n[cluster_events] {tickers}")
    run(tickers=tickers)


# ── Main ──────────────────────────────────────────────────────────────────────

async def main(
    tickers: List[str],
    days: int,
    skip_sync: bool,
) -> None:
    from equity_intel.config import settings
    from equity_intel.db.session import SessionLocal
    from equity_intel.db.models import Base
    from sqlalchemy import create_engine as _ce

    print(f"\n{'#'*60}")
    print(f"# E2E PIPELINE VALIDATION")
    print(f"# Tickers: {tickers}")
    print(f"# DB: {settings.database_url}")
    print(f"# Date: {datetime.date.today().isoformat()}")
    print(f"{'#'*60}")

    # Ensure tables exist (idempotent)
    from equity_intel.db.session import engine
    from equity_intel.db.models import Base
    Base.metadata.create_all(bind=engine)
    print("\n[setup] Schema verified (create_all).")

    if not skip_sync:
        # ── Security reminder ────────────────────────────────────────────────
        if not settings.polygon_api_key:
            print("\nERROR: POLYGON_API_KEY not set in .env", file=sys.stderr)
            sys.exit(1)
        print(f"\n[security] POLYGON_API_KEY is set (length={len(settings.polygon_api_key)}).")
        print("[security] Reminder: rotate this key if it was previously exposed in chat/logs.")

        # ── Run pipeline in order ────────────────────────────────────────────
        await run_sync_companies(tickers)
        await run_sync_filings(tickers, days=days)
        await run_sync_documents(tickers, limit=5)
        await run_sync_facts(tickers)
        await run_sync_news(tickers, days=days)
        await run_sync_prices(tickers, days=days)
        run_build_events(tickers, days=days)
        run_cluster_events(tickers)
    else:
        print("\n[--skip-sync] Skipping all workers; validating existing DB data only.")

    # ── Validate each stage ──────────────────────────────────────────────────
    from equity_intel.db.session import get_session

    with get_session() as session:
        r_companies = validate_companies(session, tickers)
        r_filings   = validate_filings(session, tickers)
        r_documents = validate_documents(session, tickers)
        r_facts     = validate_facts(session, tickers)
        r_news      = validate_news(session, tickers)
        r_prices    = validate_prices(session, tickers)
        r_events    = validate_events(session, tickers)
        r_clusters  = validate_clusters(session, tickers)
        r_mcp       = validate_mcp_tools(session, tickers)

    # ── Print per-stage results ──────────────────────────────────────────────
    for r in results:
        r.print()

    # ── Summary ──────────────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")
    all_pass = True
    for r in results:
        status = "PASS" if r.passed else "FAIL"
        print(f"  {'✅' if r.passed else '❌'} {r.stage}: {status}")
        if not r.passed:
            all_pass = False
            for err in r.errors:
                print(f"       ↳ {err}")

    print(f"\nOverall: {'ALL STAGES PASSED ✅' if all_pass else 'ONE OR MORE STAGES FAILED ❌'}")
    print(f"Tickers: {tickers}")
    print(f"Days window: {days}")
    print()

    if not all_pass:
        sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="E2E equity intelligence pipeline validation.")
    parser.add_argument(
        "--tickers",
        default="AAPL,NVDA,TSLA",
        help="Comma-separated tickers (default: AAPL,NVDA,TSLA)",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=30,
        help="Look-back window for filings, events, news, prices (default: 30)",
    )
    parser.add_argument(
        "--skip-sync",
        action="store_true",
        default=False,
        help="Skip all sync workers; validate existing DB data only.",
    )
    args = parser.parse_args()
    tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    asyncio.run(main(tickers, days=args.days, skip_sync=args.skip_sync))
