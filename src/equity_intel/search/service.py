"""
Full-text search service using PostgreSQL tsvector.

Provides search over:
- filing_documents (plain_text)
- news_articles (title + summary + body)
- events (title + summary)
"""
from __future__ import annotations

import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from equity_intel.logging_config import get_logger

logger = get_logger(__name__)


def search_filings(
    session: Session,
    query: str,
    ticker: Optional[str] = None,
    form_types: Optional[List[str]] = None,
    start_date: Optional[datetime.date] = None,
    end_date: Optional[datetime.date] = None,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """
    Full-text search over filing documents.

    Returns list of dicts with: accession_number, form_type, filing_date,
    ticker, company_name, snippet, filing_url, primary_document_url
    """
    sql = """
    SELECT
        f.accession_number,
        f.form_type,
        f.filing_date,
        f.items,
        f.filing_url,
        f.primary_document_url,
        c.ticker,
        c.name AS company_name,
        ts_headline(
            'english',
            coalesce(fd.plain_text, ''),
            plainto_tsquery('english', :query),
            'MaxFragments=3, MaxWords=50, MinWords=15'
        ) AS snippet,
        ts_rank(
            to_tsvector('english', coalesce(fd.plain_text, '')),
            plainto_tsquery('english', :query)
        ) AS rank
    FROM filings f
    JOIN companies c ON c.id = f.company_id
    LEFT JOIN filing_documents fd ON fd.filing_id = f.id
    WHERE to_tsvector('english', coalesce(fd.plain_text, '')) @@ plainto_tsquery('english', :query)
    """

    params: Dict[str, Any] = {"query": query, "limit": limit}

    if ticker:
        sql += " AND c.ticker = :ticker"
        params["ticker"] = ticker.upper()

    if form_types:
        sql += " AND f.form_type = ANY(:form_types)"
        params["form_types"] = form_types

    if start_date:
        sql += " AND f.filing_date >= :start_date"
        params["start_date"] = start_date

    if end_date:
        sql += " AND f.filing_date <= :end_date"
        params["end_date"] = end_date

    sql += " ORDER BY rank DESC, f.filing_date DESC LIMIT :limit"

    try:
        rows = session.execute(text(sql), params).mappings().all()
        return [dict(r) for r in rows]
    except Exception as exc:
        logger.error("search_filings_error", error=str(exc))
        # Fallback: ILIKE-based search
        return _search_filings_ilike(session, query, ticker, form_types, start_date, end_date, limit)


def _search_filings_ilike(
    session: Session,
    query: str,
    ticker: Optional[str],
    form_types: Optional[List[str]],
    start_date: Optional[datetime.date],
    end_date: Optional[datetime.date],
    limit: int,
) -> List[Dict[str, Any]]:
    """Fallback ILIKE search for non-PostgreSQL or missing FTS indexes."""
    sql = """
    SELECT
        f.accession_number,
        f.form_type,
        f.filing_date,
        f.items,
        f.filing_url,
        f.primary_document_url,
        c.ticker,
        c.name AS company_name,
        substring(fd.plain_text, 1, 300) AS snippet
    FROM filings f
    JOIN companies c ON c.id = f.company_id
    LEFT JOIN filing_documents fd ON fd.filing_id = f.id
    WHERE fd.plain_text LIKE :query_like
    """
    params: Dict[str, Any] = {"query_like": f"%{query}%", "limit": limit}

    if ticker:
        sql += " AND c.ticker = :ticker"
        params["ticker"] = ticker.upper()
    if form_types:
        sql += " AND f.form_type = ANY(:form_types)"
        params["form_types"] = form_types
    if start_date:
        sql += " AND f.filing_date >= :start_date"
        params["start_date"] = start_date
    if end_date:
        sql += " AND f.filing_date <= :end_date"
        params["end_date"] = end_date

    sql += " ORDER BY f.filing_date DESC LIMIT :limit"

    rows = session.execute(text(sql), params).mappings().all()
    return [dict(r) for r in rows]


def search_news(
    session: Session,
    query: str,
    ticker: Optional[str] = None,
    start_date: Optional[datetime.date] = None,
    end_date: Optional[datetime.date] = None,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """Full-text search over news articles."""
    sql = """
    SELECT
        na.id,
        na.ticker,
        na.title,
        na.summary,
        na.url,
        na.publisher,
        na.published_at,
        na.provider,
        ts_headline(
            'english',
            coalesce(na.title, '') || ' ' || coalesce(na.summary, ''),
            plainto_tsquery('english', :query),
            'MaxFragments=2, MaxWords=40, MinWords=10'
        ) AS snippet,
        ts_rank(
            to_tsvector('english', coalesce(na.title, '') || ' ' || coalesce(na.summary, '') || ' ' || coalesce(na.body, '')),
            plainto_tsquery('english', :query)
        ) AS rank
    FROM news_articles na
    WHERE to_tsvector('english', coalesce(na.title, '') || ' ' || coalesce(na.summary, '') || ' ' || coalesce(na.body, ''))
          @@ plainto_tsquery('english', :query)
    """
    params: Dict[str, Any] = {"query": query, "limit": limit}

    if ticker:
        sql += " AND na.ticker = :ticker"
        params["ticker"] = ticker.upper()
    if start_date:
        sql += " AND na.published_at >= :start_date"
        params["start_date"] = start_date
    if end_date:
        sql += " AND na.published_at <= :end_date"
        params["end_date"] = end_date

    sql += " ORDER BY rank DESC, na.published_at DESC LIMIT :limit"

    try:
        rows = session.execute(text(sql), params).mappings().all()
        return [dict(r) for r in rows]
    except Exception as exc:
        logger.error("search_news_error", error=str(exc))
        return []


def get_recent_filings_for_ticker(
    session: Session,
    ticker: str,
    form_types: Optional[List[str]] = None,
    days: int = 90,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """Get recent filings for a ticker without full-text search."""
    from equity_intel.db.models import Company, Filing

    cutoff = datetime.datetime.now() - datetime.timedelta(days=days)
    query = (
        session.query(
            Filing.accession_number,
            Filing.form_type,
            Filing.filing_date,
            Filing.items,
            Filing.filing_url,
            Filing.primary_document_url,
            Filing.sec_index_url,
            Company.ticker,
            Company.name.label("company_name"),
            Company.cik,
        )
        .join(Company, Company.id == Filing.company_id)
        .filter(Company.ticker == ticker.upper())
        .filter(Filing.filing_date >= cutoff)
    )

    if form_types:
        query = query.filter(Filing.form_type.in_(form_types))

    rows = query.order_by(Filing.filing_date.desc()).limit(limit).all()
    return [dict(r._mapping) for r in rows]


def get_events_for_ticker(
    session: Session,
    ticker: str,
    event_types: Optional[List[str]] = None,
    days: int = 90,
    min_materiality: float = 0.0,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """Get recent events for a ticker, ranked by materiality."""
    from equity_intel.db.models import Event

    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)
    query = (
        session.query(
            Event.id,
            Event.ticker,
            Event.event_type,
            Event.event_subtype,
            Event.title,
            Event.summary,
            Event.source_type,
            Event.source_url,
            Event.occurred_at,
            Event.materiality_score,
            Event.confidence_score,
            Event.evidence_json,
        )
        .filter(Event.ticker == ticker.upper())
        .filter(Event.occurred_at >= cutoff)
        .filter(Event.materiality_score >= min_materiality)
    )

    if event_types:
        query = query.filter(Event.event_type.in_(event_types))

    rows = query.order_by(Event.materiality_score.desc(), Event.occurred_at.desc()).limit(limit).all()
    return [dict(r._mapping) for r in rows]
