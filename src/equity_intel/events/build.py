"""
Event builder.

Creates Event records from filings and news articles, then clusters them.
Deduplicates events from the same source (filing id / news article id).
"""
from __future__ import annotations

import datetime
from typing import List, Optional

from sqlalchemy.orm import Session

from equity_intel.db.models import Company, Event, EventCluster, Filing, FilingDocument, NewsArticle, now_utc
from equity_intel.events.classify import classify_filing_event, KEYWORD_OVERRIDE_MAP
from equity_intel.events.score import compute_confidence_score, compute_materiality_score
from equity_intel.events.source_quality import source_quality_metadata, source_quality_score
from equity_intel.logging_config import get_logger

logger = get_logger(__name__)


def _event_exists(session: Session, source_type: str, source_id: int) -> bool:
    return (
        session.query(Event)
        .filter(Event.source_type == source_type, Event.source_id == source_id)
        .first()
        is not None
    )


# Human-readable labels for common form types
_FORM_LABELS: dict[str, str] = {
    "4":      "Insider Transaction",
    "144":    "Insider Shares-for-Sale Notice",
    "DEF 14A": "Proxy Statement",
    "8-K":    "Material Event (8-K)",
    "10-K":   "Annual Report (10-K)",
    "10-Q":   "Quarterly Report (10-Q)",
    "S-1":    "IPO Registration",
    "S-3":    "Shelf Registration",
    "424B1":  "Prospectus Supplement",
    "424B2":  "Prospectus Supplement",
    "424B3":  "Prospectus Supplement",
    "424B4":  "IPO Prospectus",
    "424B5":  "Prospectus Supplement",
    "SC 13D": "Activist Stake Disclosure",
    "13D":    "Activist Stake Disclosure",
    "SC 13G": "Passive Stake Disclosure",
    "13G":    "Passive Stake Disclosure",
}


def _filing_title(filing: Filing) -> str:
    form = filing.form_type or "Filing"
    label = _FORM_LABELS.get(form, form)
    if filing.items:
        return f"{label} — Items {filing.items}"
    return label


def _news_title(article: NewsArticle) -> str:
    return article.title or f"News from {article.publisher or 'unknown'}"


def _to_datetime(d: object) -> Optional[datetime.datetime]:
    if d is None:
        return None
    if isinstance(d, datetime.datetime):
        return d.replace(tzinfo=datetime.timezone.utc) if d.tzinfo is None else d
    if isinstance(d, datetime.date):
        return datetime.datetime.combine(d, datetime.time.min, tzinfo=datetime.timezone.utc)
    return None


def build_event_from_filing(
    session: Session,
    filing: Filing,
    company: Company,
    document: Optional[FilingDocument] = None,
    run_clustering: bool = True,
) -> Optional[Event]:
    """
    Create an Event from a Filing.
    Returns None if the event already exists for this source.
    """
    if _event_exists(session, "filing", filing.id):
        return None

    keywords: List[str] = []
    sections = {}
    if document and document.parsed_sections_json:
        keywords = document.parsed_sections_json.get("keywords", [])
        sections = document.parsed_sections_json.get("sections", {})

    event_type, event_subtype = classify_filing_event(
        form_type=filing.form_type or "",
        items=filing.items,
        keywords=keywords,
    )

    materiality = compute_materiality_score(
        form_type=filing.form_type,
        items=filing.items,
        keywords=keywords,
        occurred_at=_to_datetime(filing.filing_date),
    )

    sq = source_quality_score(
        source_type="filing",
        url=filing.filing_url,
    )
    confidence = compute_confidence_score(
        has_parsed_text=document is not None,
        keyword_count=len(keywords),
        source_quality=sq,
    )

    summary_parts = [f"{filing.form_type} filed by {company.ticker}"]
    if filing.items:
        summary_parts.append(f"Items: {filing.items}")
    if keywords:
        summary_parts.append(f"Key signals: {', '.join(keywords[:5])}")
    if filing.filing_date:
        summary_parts.append(f"Filed: {str(filing.filing_date)[:10]}")

    occurred_at = _to_datetime(filing.filing_date)
    now = now_utc()

    sq_meta = source_quality_metadata(source_type="filing", url=filing.filing_url)

    event = Event(
        company_id=company.id,
        ticker=company.ticker,
        event_type=event_type,
        event_subtype=event_subtype,
        title=_filing_title(filing),
        summary=". ".join(summary_parts),
        source_type="filing",
        source_id=filing.id,
        source_url=filing.filing_url,
        occurred_at=occurred_at,
        detected_at=now,
        materiality_score=materiality,
        novelty_score=1.0,
        confidence_score=confidence,
        evidence_json={
            "accession_number": filing.accession_number,
            "form_type": filing.form_type,
            "filing_url": filing.filing_url,
            "primary_document_url": filing.primary_document_url,
            "items": filing.items,
            "keywords": keywords,
            "sections_extracted": list(sections.keys()),
            **sq_meta,
        },
        created_at=now,
        updated_at=now,
    )
    session.add(event)

    if run_clustering:
        session.flush()
        _attach_cluster(session, event, filing=filing)

    return event


def build_event_from_news(
    session: Session,
    article: NewsArticle,
    company: Company,
    run_clustering: bool = True,
) -> Optional[Event]:
    """
    Create an Event from a NewsArticle.
    Returns None if the event already exists for this source.
    """
    if _event_exists(session, "news", article.id):
        return None

    title_lower = (article.title or "").lower()
    keywords: List[str] = []
    for kw in KEYWORD_OVERRIDE_MAP:
        if kw in title_lower:
            keywords.append(kw)

    event_type, event_subtype = classify_filing_event(
        form_type="",
        items=None,
        keywords=keywords or None,
    )
    if event_type == "other" and not keywords:
        sentiment = None
        if article.sentiment_json:
            sentiment = article.sentiment_json.get("polygon_sentiment")
        if sentiment == "negative":
            event_type, event_subtype = "other", "negative_news"
        elif sentiment == "positive":
            event_type, event_subtype = "other", "positive_news"
        else:
            event_type, event_subtype = "other", "news"

    occurred_at = article.published_at
    materiality = compute_materiality_score(
        form_type=None,
        keywords=keywords or None,
        occurred_at=occurred_at,
        source_type="news",
    )

    sq = source_quality_score(
        source_type="news",
        provider=article.provider,
        publisher=article.publisher,
        url=article.url,
    )
    confidence = compute_confidence_score(
        has_parsed_text=bool(article.body),
        keyword_count=len(keywords),
        source_quality=sq,
    )

    now = now_utc()

    sq_meta = source_quality_metadata(
        source_type="news",
        provider=article.provider,
        publisher=article.publisher,
        url=article.url,
    )

    event = Event(
        company_id=company.id,
        ticker=company.ticker,
        event_type=event_type,
        event_subtype=event_subtype,
        title=_news_title(article),
        summary=article.summary or article.title or "",
        source_type="news",
        source_id=article.id,
        source_url=article.url,
        occurred_at=occurred_at,
        detected_at=now,
        materiality_score=materiality,
        novelty_score=1.0,
        confidence_score=confidence,
        evidence_json={
            "provider": article.provider,
            "provider_id": article.provider_id,
            "publisher": article.publisher,
            "url": article.url,
            "keywords": keywords,
            **sq_meta,
        },
        created_at=now,
        updated_at=now,
    )
    session.add(event)

    if run_clustering:
        session.flush()
        _attach_cluster(session, event, news_article=article)

    return event


def _attach_cluster(
    session: Session,
    event: Event,
    filing: Optional[Filing] = None,
    news_article: Optional[NewsArticle] = None,
) -> None:
    from equity_intel.events.cluster import build_or_update_cluster
    cluster = build_or_update_cluster(session, event, filing=filing, news_article=news_article)
    session.flush()
    # Keep event's own raw materiality/novelty scores intact.
    # Enhanced cluster-level scores live in event_clusters and are surfaced
    # by MCP tools that query EventCluster first.
    event.cluster_id = cluster.id


def build_events_for_company(
    session: Session,
    company: Company,
    days: int = 90,
    include_news: bool = True,
) -> int:
    """
    Build and cluster events for all unprocessed filings (and optionally news
    articles) of a company. Returns total events created.
    """
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)

    filings = (
        session.query(Filing)
        .filter(Filing.company_id == company.id)
        .filter(Filing.filing_date >= cutoff)
        .order_by(Filing.filing_date.desc())
        .all()
    )

    count = 0
    for filing in filings:
        if _event_exists(session, "filing", filing.id):
            continue
        document = (
            session.query(FilingDocument)
            .filter(FilingDocument.filing_id == filing.id)
            .first()
        )
        event = build_event_from_filing(session, filing, company, document, run_clustering=True)
        if event:
            count += 1

    if include_news:
        articles = (
            session.query(NewsArticle)
            .filter(NewsArticle.ticker == company.ticker)
            .filter(NewsArticle.published_at >= cutoff)
            .order_by(NewsArticle.published_at.desc())
            .all()
        )
        for article in articles:
            if _event_exists(session, "news", article.id):
                continue
            event = build_event_from_news(session, article, company, run_clustering=True)
            if event:
                count += 1

    logger.info("events_built", ticker=company.ticker, count=count)
    return count
