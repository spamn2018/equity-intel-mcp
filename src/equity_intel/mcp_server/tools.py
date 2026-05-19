"""
MCP tool implementations.

Each function corresponds to one MCP tool exposed to AI agents.
All functions receive a SQLAlchemy Session and return serializable dicts.
"""
from __future__ import annotations

import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from equity_intel.briefs.watchlist import get_watchlist_brief as _get_watchlist_brief
from equity_intel.db.models import Company, CompanyFact, Event, EventCluster, Filing, FilingDocument, InstitutionalHolding, NewsArticle
from equity_intel.logging_config import get_logger
from equity_intel.search.service import (
    get_events_for_ticker,
    get_recent_filings_for_ticker,
    search_filings,
    search_news,
)

logger = get_logger(__name__)

SOURCE_NOTE = (
    "Source URLs are provided for all results. "
    "Dates are in UTC. Summaries are AI-generated from filing text. "
    "This is not investment advice."
)


def _serialize_date(d: Any) -> Optional[str]:
    if d is None:
        return None
    if isinstance(d, datetime.datetime):
        return d.isoformat()
    if isinstance(d, datetime.date):
        return d.isoformat()
    return str(d)


# ------------------------------------------------------------------ #
# get_company                                                          #
# ------------------------------------------------------------------ #


def get_company(session: Session, ticker: str) -> Dict[str, Any]:
    """Return company profile and latest filing dates."""
    ticker = ticker.upper().strip()
    company = session.query(Company).filter(Company.ticker == ticker).first()

    if not company:
        return {"error": f"Company '{ticker}' not found. Run sync_companies first.", "ticker": ticker}

    # Latest filings per form type (ORM, dialect-agnostic)
    from sqlalchemy import func as _func
    from equity_intel.db.models import Filing as _Filing

    latest_rows_orm = (
        session.query(_Filing.form_type, _func.max(_Filing.filing_date).label("latest_date"))
        .filter(_Filing.company_id == company.id)
        .group_by(_Filing.form_type)
        .order_by(_func.max(_Filing.filing_date).desc())
        .limit(10)
        .all()
    )
    latest_rows = [{"form_type": r.form_type, "latest_date": r.latest_date} for r in latest_rows_orm]

    return {
        "ticker": company.ticker,
        "cik": company.cik,
        "name": company.name,
        "exchange": company.exchange,
        "sic": company.sic,
        "sector": company.sector,
        "industry": company.industry,
        "fiscal_year_end": company.fiscal_year_end,
        "is_active": company.is_active,
        "latest_filings": [
            {"form_type": r["form_type"], "latest_date": _serialize_date(r["latest_date"])}
            for r in latest_rows
        ],
        "sec_url": f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={company.cik}"
        if company.cik
        else None,
        "source": "SEC EDGAR",
        "note": SOURCE_NOTE,
    }


# ------------------------------------------------------------------ #
# get_recent_filings                                                   #
# ------------------------------------------------------------------ #


def get_recent_filings(
    session: Session,
    ticker: str,
    form_types: Optional[List[str]] = None,
    days: int = 90,
    limit: int = 20,
) -> Dict[str, Any]:
    """Return recent filings for a ticker with source links."""
    ticker = ticker.upper().strip()
    rows = get_recent_filings_for_ticker(
        session, ticker, form_types=form_types, days=days, limit=limit
    )

    if not rows:
        return {
            "ticker": ticker,
            "filings": [],
            "message": f"No filings found for {ticker} in the last {days} days. "
            "Run sync_companies and sync_filings workers first.",
        }

    filings = []
    for r in rows:
        items_list = []
        if r.get("items"):
            from equity_intel.sec.parser import EIGHT_K_ITEMS

            items_list = [
                {"item": i.strip(), "description": EIGHT_K_ITEMS.get(i.strip(), i.strip())}
                for i in str(r["items"]).split(",")
                if i.strip()
            ]

        filings.append(
            {
                "accession_number": r["accession_number"],
                "form_type": r["form_type"],
                "filing_date": _serialize_date(r["filing_date"]),
                "items": items_list,
                "filing_url": r["filing_url"],
                "primary_document_url": r["primary_document_url"],
                "sec_index_url": r["sec_index_url"],
            }
        )

    return {
        "ticker": ticker,
        "company_name": rows[0].get("company_name") if rows else None,
        "cik": rows[0].get("cik") if rows else None,
        "days_window": days,
        "total": len(filings),
        "filings": filings,
        "source": "SEC EDGAR",
        "note": SOURCE_NOTE,
    }


# ------------------------------------------------------------------ #
# get_filing                                                           #
# ------------------------------------------------------------------ #


def get_filing(session: Session, accession_number: str) -> Dict[str, Any]:
    """Return filing metadata and parsed document text."""
    acc = accession_number.strip().replace(" ", "")
    filing = session.query(Filing).filter(Filing.accession_number == acc).first()

    if not filing:
        return {"error": f"Filing '{acc}' not found.", "accession_number": acc}

    company = session.query(Company).filter(Company.id == filing.company_id).first()
    document = (
        session.query(FilingDocument)
        .filter(FilingDocument.filing_id == filing.id)
        .first()
    )

    sections = {}
    keywords = []
    plain_text_preview = None

    if document:
        if document.parsed_sections_json:
            sections = document.parsed_sections_json.get("sections", {})
            keywords = document.parsed_sections_json.get("keywords", [])
        if document.plain_text:
            plain_text_preview = document.plain_text[:5000]

    from equity_intel.sec.parser import EIGHT_K_ITEMS

    items_detail = []
    if filing.items:
        for item in str(filing.items).split(","):
            item = item.strip()
            items_detail.append(
                {"item": item, "description": EIGHT_K_ITEMS.get(item, item)}
            )

    return {
        "accession_number": acc,
        "ticker": company.ticker if company else None,
        "company_name": company.name if company else None,
        "cik": company.cik if company else None,
        "form_type": filing.form_type,
        "filing_date": _serialize_date(filing.filing_date),
        "report_date": _serialize_date(filing.report_date),
        "items": items_detail,
        "filing_url": filing.filing_url,
        "primary_document_url": filing.primary_document_url,
        "sec_index_url": filing.sec_index_url,
        "has_parsed_document": document is not None,
        "keywords_detected": keywords,
        "plain_text_preview": plain_text_preview,
        "sections": {
            k: v[:2000] for k, v in sections.items()
        },
        "source": "SEC EDGAR",
        "note": SOURCE_NOTE,
    }


# ------------------------------------------------------------------ #
# search_filings tool                                                  #
# ------------------------------------------------------------------ #


def search_filings_tool(
    session: Session,
    query: str,
    ticker: Optional[str] = None,
    form_types: Optional[List[str]] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    limit: int = 20,
) -> Dict[str, Any]:
    """Full-text search over filing documents."""
    start_dt = datetime.date.fromisoformat(start_date) if start_date else None
    end_dt = datetime.date.fromisoformat(end_date) if end_date else None

    rows = search_filings(
        session,
        query=query,
        ticker=ticker,
        form_types=form_types,
        start_date=start_dt,
        end_date=end_dt,
        limit=limit,
    )

    results = []
    for r in rows:
        results.append(
            {
                "accession_number": r.get("accession_number"),
                "ticker": r.get("ticker"),
                "company_name": r.get("company_name"),
                "form_type": r.get("form_type"),
                "filing_date": _serialize_date(r.get("filing_date")),
                "items": r.get("items"),
                "filing_url": r.get("filing_url"),
                "primary_document_url": r.get("primary_document_url"),
                "snippet": r.get("snippet"),
            }
        )

    return {
        "query": query,
        "filters": {
            "ticker": ticker,
            "form_types": form_types,
            "start_date": start_date,
            "end_date": end_date,
        },
        "total": len(results),
        "results": results,
        "source": "SEC EDGAR full-text search",
        "note": SOURCE_NOTE,
    }


# ------------------------------------------------------------------ #
# get_company_facts                                                    #
# ------------------------------------------------------------------ #


def get_company_facts(
    session: Session,
    ticker: str,
    concepts: Optional[List[str]] = None,
    fiscal_periods: Optional[List[str]] = None,
    limit: int = 50,
) -> Dict[str, Any]:
    """Return normalized XBRL facts for a company."""
    ticker = ticker.upper().strip()
    company = session.query(Company).filter(Company.ticker == ticker).first()
    if not company:
        return {"error": f"Company '{ticker}' not found.", "ticker": ticker}

    query = session.query(CompanyFact).filter(CompanyFact.company_id == company.id)

    if concepts:
        query = query.filter(CompanyFact.concept.in_(concepts))
    if fiscal_periods:
        query = query.filter(CompanyFact.fiscal_period.in_(fiscal_periods))

    facts = (
        query.order_by(CompanyFact.end_date.desc(), CompanyFact.concept)
        .limit(limit)
        .all()
    )

    return {
        "ticker": ticker,
        "company_name": company.name,
        "cik": company.cik,
        "total": len(facts),
        "facts": [
            {
                "concept": f.concept,
                "label": f.label,
                "taxonomy": f.taxonomy,
                "unit": f.unit,
                "value": f.value,
                "fiscal_year": f.fiscal_year,
                "fiscal_period": f.fiscal_period,
                "form_type": f.form_type,
                "end_date": _serialize_date(f.end_date),
                "filed_date": _serialize_date(f.filed_date),
                "accession_number": f.accession_number,
                "source_url": f"https://data.sec.gov/api/xbrl/companyfacts/CIK{company.cik}.json"
                if company.cik
                else None,
            }
            for f in facts
        ],
        "source": "SEC EDGAR XBRL",
        "note": SOURCE_NOTE,
    }


# ------------------------------------------------------------------ #
# get_recent_news                                                      #
# ------------------------------------------------------------------ #


def get_recent_news(
    session: Session,
    ticker: Optional[str] = None,
    query: Optional[str] = None,
    days: int = 7,
    limit: int = 20,
) -> Dict[str, Any]:
    """Return recent news articles."""
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)

    db_query = session.query(NewsArticle).filter(NewsArticle.published_at >= cutoff)

    if ticker:
        db_query = db_query.filter(NewsArticle.ticker == ticker.upper())
    if query:
        # Simple title ILIKE fallback — FTS available via search_news
        db_query = db_query.filter(
            NewsArticle.title.ilike(f"%{query}%")
            | NewsArticle.summary.ilike(f"%{query}%")
        )

    articles = db_query.order_by(NewsArticle.published_at.desc()).limit(limit).all()

    return {
        "ticker": ticker,
        "days_window": days,
        "total": len(articles),
        "articles": [
            {
                "title": a.title,
                "publisher": a.publisher,
                "author": a.author,
                "url": a.url,
                "published_at": _serialize_date(a.published_at),
                "ticker": a.ticker,
                "summary": a.summary,
                "provider": a.provider,
            }
            for a in articles
        ],
        "note": SOURCE_NOTE,
    }


# ------------------------------------------------------------------ #
# search_news tool                                                     #
# ------------------------------------------------------------------ #


def search_news_tool(
    session: Session,
    query: str,
    ticker: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    limit: int = 20,
) -> Dict[str, Any]:
    """Full-text search over news articles."""
    start_dt = datetime.date.fromisoformat(start_date) if start_date else None
    end_dt = datetime.date.fromisoformat(end_date) if end_date else None

    rows = search_news(session, query=query, ticker=ticker, start_date=start_dt, end_date=end_dt, limit=limit)

    return {
        "query": query,
        "total": len(rows),
        "results": [
            {
                "ticker": r.get("ticker"),
                "title": r.get("title"),
                "publisher": r.get("publisher"),
                "url": r.get("url"),
                "published_at": _serialize_date(r.get("published_at")),
                "snippet": r.get("snippet"),
                "summary": r.get("summary"),
            }
            for r in rows
        ],
        "source": "news_articles table",
        "note": SOURCE_NOTE,
    }


# ------------------------------------------------------------------ #
# get_events                                                           #
# ------------------------------------------------------------------ #


def get_events(
    session: Session,
    ticker: Optional[str] = None,
    event_types: Optional[List[str]] = None,
    days: int = 30,
    min_materiality: float = 0.0,
    limit: int = 20,
) -> Dict[str, Any]:
    """
    Return ranked market events.

    Prefers EventCluster records (richer, multi-source) when they exist,
    falling back to raw Event records for unclustered data.
    """
    import datetime as _dt

    cutoff = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=days)

    # ── Try clusters first ───────────────────────────────────────────────
    cq = (
        session.query(EventCluster)
        .filter(EventCluster.last_seen_at >= cutoff)
        .filter(EventCluster.materiality_score >= min_materiality)
    )
    if ticker:
        cq = cq.filter(EventCluster.ticker == ticker.upper())
    if event_types:
        cq = cq.filter(EventCluster.event_type.in_(event_types))
    cq = cq.order_by(EventCluster.materiality_score.desc(), EventCluster.last_seen_at.desc()).limit(limit)
    clusters = cq.all()

    if clusters:
        return {
            "ticker": ticker,
            "event_types": event_types,
            "days_window": days,
            "min_materiality": min_materiality,
            "total": len(clusters),
            "source": "event_clusters",
            "events": [_serialize_cluster(c) for c in clusters],
            "note": SOURCE_NOTE,
        }

    # ── Fallback: raw events (no clusters yet) ───────────────────────────
    if ticker:
        rows = get_events_for_ticker(
            session,
            ticker=ticker,
            event_types=event_types,
            days=days,
            min_materiality=min_materiality,
            limit=limit,
        )
    else:
        q = (
            session.query(
                Event.id, Event.ticker, Event.event_type, Event.event_subtype,
                Event.title, Event.summary, Event.source_type, Event.source_url,
                Event.occurred_at, Event.materiality_score, Event.confidence_score,
                Event.novelty_score, Event.evidence_json, Event.cluster_id,
            )
            .filter(Event.occurred_at >= cutoff)
            .filter(Event.materiality_score >= min_materiality)
        )
        if event_types:
            q = q.filter(Event.event_type.in_(event_types))
        q = q.order_by(Event.materiality_score.desc(), Event.occurred_at.desc()).limit(limit)
        rows = [dict(r._mapping) for r in q.all()]

    return {
        "ticker": ticker,
        "event_types": event_types,
        "days_window": days,
        "min_materiality": min_materiality,
        "total": len(rows),
        "source": "events",
        "events": [
            {
                "id": r.get("id"),
                "cluster_id": r.get("cluster_id"),
                "ticker": r.get("ticker"),
                "event_type": r.get("event_type"),
                "event_subtype": r.get("event_subtype"),
                "title": r.get("title"),
                "summary": r.get("summary"),
                "source_type": r.get("source_type"),
                "source_url": r.get("source_url"),
                "occurred_at": _serialize_date(r.get("occurred_at")),
                "materiality_score": r.get("materiality_score"),
                "novelty_score": r.get("novelty_score"),
                "confidence_score": r.get("confidence_score"),
                "evidence": r.get("evidence_json"),
            }
            for r in rows
        ],
        "note": SOURCE_NOTE,
    }


def _serialize_cluster(c: EventCluster) -> Dict[str, Any]:
    """Convert an EventCluster ORM object to a serializable dict."""
    price = c.price_reaction_json or {}
    filing_ids = (c.filing_ids or {}).get("ids", [])
    news_ids = (c.news_ids or {}).get("ids", [])
    source_urls = (c.source_urls or {}).get("urls", [])

    result: Dict[str, Any] = {
        "cluster_id": c.id,
        "cluster_key": c.cluster_key,
        "ticker": c.ticker,
        "event_type": c.event_type,
        "event_subtype": c.event_subtype,
        "title": c.title,
        "summary": c.summary,
        "first_seen_at": _serialize_date(c.first_seen_at),
        "last_seen_at": _serialize_date(c.last_seen_at),
        "event_count": c.event_count,
        "filing_count": c.filing_count,
        "news_count": c.news_count,
        "materiality_score": c.materiality_score,
        "confidence_score": c.confidence_score,
        "novelty_score": c.novelty_score,
        "source_urls": source_urls[:5],          # cap for readability
        "filing_ids": filing_ids,
        "news_ids": news_ids,
        "evidence": c.evidence_json,
        "caution": c.caution,
    }

    if price.get("available"):
        result["price_reaction"] = {
            "pct_change": price.get("pct_change"),
            "volume_ratio": price.get("volume_ratio"),
            "price_before": price.get("price_before"),
            "price_after": price.get("price_after"),
            "date_before": price.get("date_before"),
            "date_after": price.get("date_after"),
        }
    else:
        result["price_reaction"] = None

    return result


# ------------------------------------------------------------------ #
# get_event_cluster                                                    #
# ------------------------------------------------------------------ #


def get_event_cluster(
    session: Session,
    cluster_id: Optional[int] = None,
    cluster_key: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Return full details for a single EventCluster, including all linked
    filings, news articles, and price reaction data.
    """
    if cluster_id:
        cluster = session.get(EventCluster, cluster_id)
    elif cluster_key:
        cluster = session.query(EventCluster).filter(EventCluster.cluster_key == cluster_key).first()
    else:
        return {"error": "Provide cluster_id or cluster_key"}

    if not cluster:
        return {"error": "Cluster not found", "cluster_id": cluster_id, "cluster_key": cluster_key}

    # Enrich with linked filing details
    filing_ids: List[int] = (cluster.filing_ids or {}).get("ids", [])
    linked_filings = []
    if filing_ids:
        filings = session.query(Filing).filter(Filing.id.in_(filing_ids)).all()
        for f in filings:
            linked_filings.append({
                "accession_number": f.accession_number,
                "form_type": f.form_type,
                "filing_date": _serialize_date(f.filing_date),
                "items": f.items,
                "url": f.filing_url,
            })

    # Enrich with linked news article details
    news_ids: List[int] = (cluster.news_ids or {}).get("ids", [])
    linked_news = []
    if news_ids:
        articles = session.query(NewsArticle).filter(NewsArticle.id.in_(news_ids)).all()
        for a in articles:
            linked_news.append({
                "title": a.title,
                "publisher": a.publisher,
                "published_at": _serialize_date(a.published_at),
                "url": a.url,
                "summary": a.summary,
            })

    result = _serialize_cluster(cluster)
    result["linked_filings"] = linked_filings
    result["linked_news"] = linked_news
    result["note"] = SOURCE_NOTE
    return result


# ------------------------------------------------------------------ #
# explain_stock_move                                                   #
# ------------------------------------------------------------------ #


def _compute_price_move(
    session: "Session",
    ticker: str,
    target_date: "datetime.date",
    window: int,
) -> Dict[str, Any]:
    """
    Fetch price bars from the DB and compute move metrics around target_date.

    Returns a dict with keys: available, bars, price_before, price_after,
    pct_change, volume_before, volume_after, volume_ratio.
    """
    from equity_intel.db.models import MarketPrice

    # Fetch a wider band so we can find bars just before the window
    fetch_start = target_date - datetime.timedelta(days=window + 10)
    fetch_end = target_date + datetime.timedelta(days=window + 2)

    rows_orm = (
        session.query(
            MarketPrice.timestamp,
            MarketPrice.open,
            MarketPrice.close,
            MarketPrice.volume,
            MarketPrice.adjusted_close,
        )
        .filter(MarketPrice.ticker == ticker)
        .filter(MarketPrice.interval == "1d")
        .filter(MarketPrice.timestamp >= datetime.datetime.combine(fetch_start, datetime.time.min))
        .filter(MarketPrice.timestamp <= datetime.datetime.combine(fetch_end, datetime.time.max))
        .order_by(MarketPrice.timestamp)
        .all()
    )

    bars = [
        {
            "date": r.timestamp.date().isoformat() if hasattr(r.timestamp, "date") else str(r.timestamp)[:10],
            "open": r.open,
            "close": r.close,
            "volume": r.volume,
            "adjusted_close": r.adjusted_close,
        }
        for r in rows_orm
    ]

    if not bars:
        return {"available": False, "bars": []}

    # Split into before/on-or-after target_date
    before = [b for b in bars if b["date"] < target_date.isoformat()]
    after = [b for b in bars if b["date"] >= target_date.isoformat()]

    if not before or not after:
        return {"available": False, "bars": bars, "reason": "Insufficient bars around target date"}

    price_before = before[-1]["adjusted_close"] or before[-1]["close"]
    price_after = after[0]["adjusted_close"] or after[0]["close"]
    vol_before = before[-1]["volume"]
    vol_after = after[0]["volume"]

    pct_change: Optional[float] = None
    if price_before and price_after:
        pct_change = round((price_after - price_before) / price_before * 100, 2)

    vol_ratio: Optional[float] = None
    if vol_before and vol_after:
        vol_ratio = round(vol_after / vol_before, 2)

    # Trim bars to just within the user window for display
    window_bars = [
        b for b in bars
        if (target_date - datetime.timedelta(days=window)).isoformat() <= b["date"]
        <= (target_date + datetime.timedelta(days=window)).isoformat()
    ]

    return {
        "available": True,
        "bars": window_bars,
        "price_before": round(price_before, 4) if price_before else None,
        "price_after": round(price_after, 4) if price_after else None,
        "pct_change": pct_change,
        "volume_before": int(vol_before) if vol_before else None,
        "volume_after": int(vol_after) if vol_after else None,
        "volume_ratio": vol_ratio,
        "date_before": before[-1]["date"],
        "date_after": after[0]["date"],
    }


def explain_stock_move(
    session: Session,
    ticker: str,
    date: Optional[str] = None,
    window: int = 3,
) -> Dict[str, Any]:
    """
    Attempt to explain a price move using nearby filings and news.

    Uses "likely related to" language — does not assert causality.
    """
    ticker = ticker.upper().strip()
    target_date = datetime.date.fromisoformat(date) if date else datetime.date.today()
    start = target_date - datetime.timedelta(days=window)
    end = target_date + datetime.timedelta(days=window)

    # ── Price move analysis ──────────────────────────────────────────────
    move = _compute_price_move(session, ticker, target_date, window)

    # ── Nearby filings ───────────────────────────────────────────────────
    lookback_days = max((datetime.date.today() - start).days + 1, window * 2 + 10)
    filing_rows = get_recent_filings_for_ticker(session, ticker, days=lookback_days, limit=20)
    nearby_filings = []
    for r in filing_rows:
        fd = r.get("filing_date")
        if fd is None:
            continue
        fd_str = _serialize_date(fd)[:10] if fd else None
        if fd_str and start.isoformat() <= fd_str <= end.isoformat():
            nearby_filings.append(r)

    # ── Nearby events ────────────────────────────────────────────────────
    event_rows = get_events_for_ticker(session, ticker, days=window * 2 + 10, limit=20)
    nearby_events = []
    for r in event_rows:
        occ = r.get("occurred_at")
        if occ is None:
            continue
        occ_str = _serialize_date(occ)[:10]
        if start.isoformat() <= occ_str <= end.isoformat():
            nearby_events.append(r)

    # ── Nearby news ──────────────────────────────────────────────────────
    from equity_intel.db.models import NewsArticle
    news_rows_orm = (
        session.query(NewsArticle.title, NewsArticle.publisher, NewsArticle.url, NewsArticle.published_at, NewsArticle.summary)
        .filter(NewsArticle.ticker == ticker)
        .filter(NewsArticle.published_at >= datetime.datetime.combine(start, datetime.time.min))
        .filter(NewsArticle.published_at <= datetime.datetime.combine(end, datetime.time.max))
        .order_by(NewsArticle.published_at.desc())
        .limit(10)
        .all()
    )
    nearby_news = [dict(r._mapping) for r in news_rows_orm]

    # ── Build evidence list ──────────────────────────────────────────────
    evidence: List[Dict[str, Any]] = []
    for f in nearby_filings:
        evidence.append({
            "type": "filing",
            "form_type": f.get("form_type"),
            "date": _serialize_date(f.get("filing_date")),
            "description": f"SEC {f.get('form_type')} filing",
            "url": f.get("filing_url"),
            "items": f.get("items"),
        })
    for e in nearby_events:
        evidence.append({
            "type": "event",
            "event_type": e.get("event_type"),
            "event_subtype": e.get("event_subtype"),
            "date": _serialize_date(e.get("occurred_at")),
            "description": e.get("title"),
            "url": e.get("source_url"),
            "materiality_score": e.get("materiality_score"),
        })
    for n in nearby_news:
        evidence.append({
            "type": "news",
            "date": _serialize_date(n.get("published_at")),
            "description": n.get("title"),
            "publisher": n.get("publisher"),
            "url": n.get("url"),
            "summary": n.get("summary"),
        })

    # ── Confidence score ─────────────────────────────────────────────────
    has_evidence = bool(evidence)
    confidence = 0.4 + (min(len(evidence), 5) * 0.08) if has_evidence else 0.1
    if move.get("available"):
        confidence = min(confidence + 0.1, 1.0)

    # ── Interpretation ───────────────────────────────────────────────────
    pct = move.get("pct_change")
    if pct is not None:
        direction = "up" if pct >= 0 else "down"
        move_summary = f"{ticker} moved {direction} {abs(pct):.2f}% around {target_date}."
    else:
        move_summary = f"Price data not available for {ticker} around {target_date}."

    if has_evidence:
        interpretation = (
            f"{move_summary} "
            f"Move is likely related to {len(evidence)} nearby event(s) "
            f"({len(nearby_filings)} filing(s), {len(nearby_events)} event(s), {len(nearby_news)} news item(s))."
        )
    else:
        interpretation = (
            f"{move_summary} "
            f"No nearby filings, events, or news found for {ticker} around {target_date}. "
            "The move may be driven by macro factors, sector rotation, or events not yet ingested."
        )

    return {
        "ticker": ticker,
        "target_date": target_date.isoformat(),
        "window_days": window,
        "price_move": move,
        "evidence_count": len(evidence),
        "evidence": evidence,
        "confidence_score": round(confidence, 2),
        "interpretation": interpretation,
        "caution": (
            "This analysis shows correlation, not causation. "
            "Events shown are 'likely related to' the move, not confirmed causes."
        ),
        "note": SOURCE_NOTE,
    }


# ------------------------------------------------------------------ #
# screen_catalysts                                                     #
# ------------------------------------------------------------------ #


def screen_catalysts(
    session: Session,
    event_types: Optional[List[str]] = None,
    days: int = 7,
    min_materiality: float = 0.5,
    tickers: Optional[List[str]] = None,
    sectors: Optional[List[str]] = None,
    limit: int = 30,
) -> Dict[str, Any]:
    """
    Cross-market catalyst screening ranked by materiality.

    Prefers cluster-level results (multi-source, price-enriched) when available.
    """
    import datetime as _dt

    cutoff = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=days)

    # Cluster-based screening (preferred)
    cq = (
        session.query(EventCluster, Company)
        .outerjoin(Company, Company.ticker == EventCluster.ticker)
        .filter(EventCluster.last_seen_at >= cutoff)
        .filter(EventCluster.materiality_score >= min_materiality)
    )
    if event_types:
        cq = cq.filter(EventCluster.event_type.in_(event_types))
    if tickers:
        cq = cq.filter(EventCluster.ticker.in_([t.upper() for t in tickers]))
    if sectors:
        cq = cq.filter(Company.sector.in_(sectors))
    cq = cq.order_by(EventCluster.materiality_score.desc(), EventCluster.last_seen_at.desc()).limit(limit)
    cluster_rows = cq.all()

    if cluster_rows:
        catalysts = []
        for cluster, company in cluster_rows:
            price = cluster.price_reaction_json or {}
            cat: Dict[str, Any] = {
                "cluster_id": cluster.id,
                "ticker": cluster.ticker,
                "company_name": company.name if company else None,
                "sector": company.sector if company else None,
                "exchange": company.exchange if company else None,
                "event_type": cluster.event_type,
                "event_subtype": cluster.event_subtype,
                "title": cluster.title,
                "summary": cluster.summary,
                "source_urls": (cluster.source_urls or {}).get("urls", [])[:3],
                "first_seen_at": _serialize_date(cluster.first_seen_at),
                "last_seen_at": _serialize_date(cluster.last_seen_at),
                "materiality_score": cluster.materiality_score,
                "confidence_score": cluster.confidence_score,
                "novelty_score": cluster.novelty_score,
                "evidence_count": cluster.event_count,
                "filing_count": cluster.filing_count,
                "news_count": cluster.news_count,
                "caution": cluster.caution,
            }
            if price.get("available"):
                cat["price_reaction"] = {
                    "pct_change": price.get("pct_change"),
                    "volume_ratio": price.get("volume_ratio"),
                    "price_before": price.get("price_before"),
                    "price_after": price.get("price_after"),
                }
            else:
                cat["price_reaction"] = None
            catalysts.append(cat)

        return {
            "filters": {
                "event_types": event_types,
                "days": days,
                "min_materiality": min_materiality,
                "tickers": tickers,
                "sectors": sectors,
            },
            "source": "event_clusters",
            "total": len(catalysts),
            "catalysts": catalysts,
            "note": SOURCE_NOTE,
        }

    # Fallback: raw events (no clusters yet)
    q = (
        session.query(
            Event.ticker, Event.event_type, Event.event_subtype,
            Event.title, Event.summary, Event.source_url,
            Event.occurred_at, Event.materiality_score, Event.confidence_score,
            Company.name.label("company_name"), Company.sector, Company.exchange,
        )
        .outerjoin(Company, Company.id == Event.company_id)
        .filter(Event.occurred_at >= cutoff)
        .filter(Event.materiality_score >= min_materiality)
    )
    if event_types:
        q = q.filter(Event.event_type.in_(event_types))
    if tickers:
        q = q.filter(Event.ticker.in_([t.upper() for t in tickers]))
    if sectors:
        q = q.filter(Company.sector.in_(sectors))
    q = q.order_by(Event.materiality_score.desc(), Event.occurred_at.desc()).limit(limit)
    rows = [dict(r._mapping) for r in q.all()]

    return {
        "filters": {
            "event_types": event_types,
            "days": days,
            "min_materiality": min_materiality,
            "tickers": tickers,
            "sectors": sectors,
        },
        "source": "events",
        "total": len(rows),
        "catalysts": [
            {
                "ticker": r["ticker"],
                "company_name": r["company_name"],
                "sector": r["sector"],
                "exchange": r["exchange"],
                "event_type": r["event_type"],
                "event_subtype": r["event_subtype"],
                "title": r["title"],
                "summary": r["summary"],
                "source_url": r["source_url"],
                "occurred_at": _serialize_date(r["occurred_at"]),
                "materiality_score": r["materiality_score"],
                "confidence_score": r["confidence_score"],
                "price_reaction": None,
            }
            for r in rows
        ],
        "note": SOURCE_NOTE,
    }


# ------------------------------------------------------------------ #
# get_watchlist_brief                                                  #
# ------------------------------------------------------------------ #


def get_watchlist_brief(
    session: Session,
    tickers: List[str],
    days: int = 7,
    min_materiality: float = 0.3,
    include_low_confidence: bool = False,
    max_items: int = 20,
    event_types: Optional[List[str]] = None,
    include_price_context: bool = True,
    include_news: bool = True,
    include_filings: bool = True,
) -> Dict[str, Any]:
    """
    Generate a ranked, evidence-backed catalyst brief for a watchlist of tickers.

    Answers: "What are the most important stock-moving catalysts across my watchlist right now?"

    Prefers EventCluster data (multi-source, price-enriched); falls back to raw Events.
    Uses cautious language throughout — never asserts causation.
    """
    return _get_watchlist_brief(
        session=session,
        tickers=tickers,
        days=days,
        min_materiality=min_materiality,
        include_low_confidence=include_low_confidence,
        max_items=max_items,
        event_types=event_types,
        include_price_context=include_price_context,
        include_news=include_news,
        include_filings=include_filings,
    )


# ------------------------------------------------------------------ #
# get_institutional_holders                                            #
# ------------------------------------------------------------------ #


def get_institutional_holders(
    session,
    ticker: str,
    quarters: int = 4,
    limit: int = 25,
):
    """Return institutional holders for a ticker across recent quarters."""
    from equity_intel.db.models import InstitutionalHolding, Company

    ticker = ticker.upper().strip()
    company = session.query(Company).filter(Company.ticker == ticker).first()

    if not company:
        return {
            "error": f"Company '{ticker}' not found. Run sync_companies first.",
            "ticker": ticker,
        }

    dates_rows = (
        session.query(InstitutionalHolding.report_date)
        .filter(InstitutionalHolding.ticker == ticker)
        .filter(InstitutionalHolding.report_date.isnot(None))
        .distinct()
        .order_by(InstitutionalHolding.report_date.desc())
        .limit(quarters)
        .all()
    )
    report_dates = [r.report_date for r in dates_rows]

    if not report_dates:
        return {
            "ticker": ticker,
            "company_name": company.name,
            "holders": [],
            "message": (
                "No 13F-HR holdings found for this ticker. "
                "Run the sync_13f worker with relevant manager CIKs first."
            ),
            "note": SOURCE_NOTE,
        }

    quarters_data = []
    for rdate in report_dates:
        rows = (
            session.query(InstitutionalHolding)
            .filter(InstitutionalHolding.ticker == ticker)
            .filter(InstitutionalHolding.report_date == rdate)
            .order_by(InstitutionalHolding.value_usd.desc())
            .limit(limit)
            .all()
        )
        holders = [
            {
                "manager_name": h.manager_name,
                "manager_cik": h.manager_cik,
                "shares": h.shares,
                "value_usd_thousands": h.value_usd,
                "value_usd": (h.value_usd or 0) * 1000,
                "share_type": h.share_type,
                "put_call": h.put_call,
                "investment_discretion": h.investment_discretion,
                "filing_id": h.filing_id,
            }
            for h in rows
        ]
        quarters_data.append({
            "report_date": _serialize_date(rdate),
            "holder_count": len(holders),
            "holders": holders,
        })

    return {
        "ticker": ticker,
        "company_name": company.name,
        "cik": company.cik,
        "quarters_returned": len(quarters_data),
        "quarters": quarters_data,
        "source": "SEC EDGAR 13F-HR filings",
        "note": SOURCE_NOTE,
    }


# ------------------------------------------------------------------ #
# get_manager_holdings                                                 #
# ------------------------------------------------------------------ #


def get_manager_holdings(
    session,
    manager_cik=None,
    manager_name=None,
    report_date=None,
    limit: int = 50,
):
    """Return all equity holdings for a specific institutional manager."""
    import datetime as _dt
    from equity_intel.db.models import InstitutionalHolding
    from equity_intel.sec.client import normalize_cik

    if not manager_cik and not manager_name:
        return {"error": "Provide manager_cik or manager_name."}

    q = session.query(InstitutionalHolding)

    if manager_cik:
        q = q.filter(InstitutionalHolding.manager_cik == normalize_cik(manager_cik))
    elif manager_name:
        q = q.filter(InstitutionalHolding.manager_name.ilike(f"%{manager_name}%"))

    if report_date:
        rd = _dt.date.fromisoformat(report_date)
        # Use datetime for comparison — SQLite stores DateTime as datetime strings
        rd_dt = _dt.datetime(rd.year, rd.month, rd.day)
        q = q.filter(InstitutionalHolding.report_date == rd_dt)
    else:
        latest_row = q.order_by(InstitutionalHolding.report_date.desc()).first()
        if not latest_row:
            return {
                "manager_cik": manager_cik,
                "manager_name": manager_name,
                "holdings": [],
                "message": (
                    "No holdings found for this manager. "
                    "Run the sync_13f worker with this manager's CIK first."
                ),
                "note": SOURCE_NOTE,
            }
        q = q.filter(InstitutionalHolding.report_date == latest_row.report_date)
        report_date = _serialize_date(latest_row.report_date)

    rows = q.order_by(InstitutionalHolding.value_usd.desc()).limit(limit).all()

    if not rows:
        return {
            "manager_cik": manager_cik,
            "manager_name": manager_name,
            "report_date": report_date,
            "holdings": [],
            "message": "No holdings found for the specified manager and period.",
            "note": SOURCE_NOTE,
        }

    resolved_name = rows[0].manager_name
    total_value_thousands = sum(r.value_usd or 0 for r in rows)

    holdings = [
        {
            "issuer_name": r.issuer_name,
            "cusip": r.cusip,
            "ticker": r.ticker,
            "title_of_class": r.title_of_class,
            "shares": r.shares,
            "value_usd_thousands": r.value_usd,
            "value_usd": (r.value_usd or 0) * 1000,
            "share_type": r.share_type,
            "put_call": r.put_call,
            "investment_discretion": r.investment_discretion,
            "company_id": r.company_id,
        }
        for r in rows
    ]

    return {
        "manager_cik": rows[0].manager_cik,
        "manager_name": resolved_name,
        "report_date": report_date,
        "total_positions": len(holdings),
        "total_value_usd": total_value_thousands * 1000,
        "holdings": holdings,
        "source": "SEC EDGAR 13F-HR filings",
        "note": SOURCE_NOTE,
    }
