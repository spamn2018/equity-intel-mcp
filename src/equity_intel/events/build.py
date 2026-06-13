"""
Event builder.

Creates Event records from filings and news articles, then clusters them.
Deduplicates events from the same source (filing id / news article id).
"""
from __future__ import annotations

import datetime
import re
from typing import List, Optional

from sqlalchemy.orm import Session

from equity_intel.db.models import (
    Company, Event, EventCluster, Filing, FilingDocument, NewsArticle, now_utc,
)
from equity_intel.events.classify import classify_filing_event, KEYWORD_OVERRIDE_MAP
from equity_intel.events.llm_scorer import score_document
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


_GENERIC_TITLE_PATTERNS: tuple = (
    r"\bwill\b.{0,40}\bopen\s+up\s+or\s+down\b",
    r"\bstock\s+market\b.{0,20}\btoday\b",
    r"\bmarkets\s+today\b",
    r"\bpremarket\b.{0,30}\b(today|morning|open)\b",
    r"\bfutures\s+(rise|fall|slip|gain|drop|higher|lower|tick)\b",
    r"\bs&p\s*500\b.{0,60}\b(open|close|today|week|month|year|rally|drop|fall|rise)\b",
    r"\bdow\s+jones\b.{0,30}\b(open|close|today|rally|drop|fall|rise)\b",
    r"\bnasdaq\b.{0,30}\b(open|close|today|rally|drop|fall|rise)\b",
    r"\bstocks?\s+to\s+(watch|buy|sell|avoid|consider)\b.{0,20}\b(today|this week|now)\b",
    r"\bbest\s+stocks?\s+to\b",
    r"\bweekly\s+(market|stock|wall\s+street)\s+(recap|wrap|summary|review)\b",
    r"\bmarket\s+(recap|wrap|summary|review|update)\b",
    r"\bwall\s+street\b.{0,20}\b(recap|wrap|summary|week|today)\b",
    r"\banalyst\s+(upgrades?\s+and\s+downgrades?|ratings?\s+changes?)\b",
    r"\bupgrades?\s+and\s+downgrades?\b",
    r"\bearnings\s+(calendar|season\s+preview|preview|recap)\b",
    r"\b(top|biggest|major)\s+(movers?|gainers?|losers?|winners?)\b.{0,20}\btoday\b",
    r"\bdividend\s+(calendar|stocks?\s+this\s+week)\b",
    r"\bwhat\s+to\s+expect\s+(this|next)\s+week\b",
    r"\bkey\s+(events?|data|reports?)\s+(this|next)\s+week\b",
)

_GENERIC_PATTERNS_COMPILED = [re.compile(p, re.IGNORECASE) for p in _GENERIC_TITLE_PATTERNS]
_ROUNDUP_TICKER_THRESHOLD = 7


def _is_generic_market_article(title: str, article_ticker: str, tickers_json) -> bool:
    for pattern in _GENERIC_PATTERNS_COMPILED:
        if pattern.search(title):
            return True
    if tickers_json and isinstance(tickers_json, dict):
        tagged = tickers_json.get("tickers", [])
        if (
            isinstance(tagged, list)
            and len(tagged) >= _ROUNDUP_TICKER_THRESHOLD
            and article_ticker.upper() not in title.upper()
        ):
            return True
    return False


_FORM_LABELS: dict = {
    "4":       "Insider Transaction",
    "144":     "Insider Shares-for-Sale Notice",
    "DEF 14A": "Proxy Statement",
    "8-K":     "Material Event (8-K)",
    "10-K":    "Annual Report (10-K)",
    "10-Q":    "Quarterly Report (10-Q)",
    "S-1":     "IPO Registration",
    "S-3":     "Shelf Registration",
    "424B1":   "Prospectus Supplement",
    "424B2":   "Prospectus Supplement",
    "424B3":   "Prospectus Supplement",
    "424B4":   "IPO Prospectus",
    "424B5":   "Prospectus Supplement",
    "SC 13D":  "Activist Stake Disclosure",
    "13D":     "Activist Stake Disclosure",
    "SC 13G":  "Passive Stake Disclosure",
    "13G":     "Passive Stake Disclosure",
}


def _filing_title(filing: Filing) -> str:
    form = filing.form_type or "Filing"
    label = _FORM_LABELS.get(form, form)
    if filing.items:
        return label + " -- Items " + filing.items
    return label


def _news_title(article: NewsArticle) -> str:
    return article.title or "News from " + (article.publisher or "unknown")


def _to_datetime(d) -> Optional[datetime.datetime]:
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

    Scoring: LLM reads the document text and returns materiality + confidence.
    When no text is available, neutral 0.5 scores are used -- the signal still
    flows. Rule-based keyword scoring is NOT used as the primary scorer.
    """
    if _event_exists(session, "filing", filing.id):
        return None

    # Extract text for LLM scoring
    text: Optional[str] = None
    sections: dict = {}
    if document:
        if document.plain_text:
            text = document.plain_text
        elif document.parsed_sections_json:
            secs = document.parsed_sections_json.get("sections", {})
            text = "\n\n".join(str(v) for v in secs.values() if v) or None
            sections = secs

    # LLM scores the content; neutral fallback when no text
    scores = score_document(text, ticker=company.ticker, source_type="filing")

    # Event type: form/items give structural classification (not content scoring)
    event_type, event_subtype = classify_filing_event(
        form_type=filing.form_type or "",
        items=filing.items,
        keywords=None,  # no keyword override -- LLM handles content
    )
    # If LLM suggests a more specific type, prefer it
    hint = scores.get("event_type_hint")
    if hint and event_type in ("other", "earnings"):
        event_type = hint

    materiality = scores["materiality"]
    confidence = scores["confidence"]

    # Source quality still modestly adjusts confidence (filing vs news)
    sq = source_quality_score(source_type="filing", url=filing.filing_url)
    sq_adj = (sq - 0.5) * 0.10
    confidence = max(0.0, min(1.0, round(confidence + sq_adj, 4)))

    # Build summary
    llm_summary = scores.get("summary")
    if llm_summary:
        summary = llm_summary
    else:
        parts = [str(filing.form_type) + " filed by " + company.ticker]
        if filing.items:
            parts.append("Items: " + filing.items)
        if filing.filing_date:
            parts.append("Filed: " + str(filing.filing_date)[:10])
        summary = ". ".join(parts)

    occurred_at = _to_datetime(filing.filing_date)
    now = now_utc()
    sq_meta = source_quality_metadata(source_type="filing", url=filing.filing_url)

    event = Event(
        company_id=company.id,
        ticker=company.ticker,
        event_type=event_type,
        event_subtype=event_subtype,
        title=_filing_title(filing),
        summary=summary,
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
            "sections_extracted": list(sections.keys()),
            "llm_scored": scores.get("llm_scored", False),
            "llm_sentiment": scores.get("sentiment"),
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

    Scoring: LLM reads the article body/title and returns materiality + confidence.
    Neutral 0.5 fallback when no text. Signal still flows.
    """
    if _event_exists(session, "news", article.id):
        return None

    title = article.title or ""
    if _is_generic_market_article(title, company.ticker, article.tickers_json):
        logger.debug("news_event_skipped_generic", ticker=company.ticker, title=title[:100])
        return None

    # Build text for LLM (prefer body, fall back to title + summary)
    text: Optional[str] = None
    if article.body:
        text = article.body[:4000]
    elif article.summary:
        text = (title + "\n\n" + article.summary) if title else article.summary
    elif title:
        text = title

    scores = score_document(text, ticker=company.ticker, source_type="news")

    # Event type: use LLM hint first, then sentiment fallback
    hint = scores.get("event_type_hint")
    if hint:
        event_type = hint
        event_subtype = hint
    else:
        sentiment = scores.get("sentiment", "neutral")
        if sentiment == "bearish":
            event_type, event_subtype = "other", "negative_news"
        elif sentiment == "bullish":
            event_type, event_subtype = "other", "positive_news"
        else:
            event_type, event_subtype = "other", "news"

    materiality = scores["materiality"]
    confidence = scores["confidence"]

    # News source quality adjustment (modest)
    sq = source_quality_score(
        source_type="news",
        provider=article.provider,
        publisher=article.publisher,
        url=article.url,
    )
    sq_adj = (sq - 0.5) * 0.10
    confidence = max(0.0, min(1.0, round(confidence + sq_adj, 4)))
    # News is secondary evidence -- modest materiality discount
    materiality = max(0.0, min(1.0, round(materiality * 0.9, 4)))

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
        summary=scores.get("summary") or article.summary or article.title or "",
        source_type="news",
        source_id=article.id,
        source_url=article.url,
        occurred_at=article.published_at,
        detected_at=now,
        materiality_score=materiality,
        novelty_score=1.0,
        confidence_score=confidence,
        evidence_json={
            "provider": article.provider,
            "provider_id": article.provider_id,
            "publisher": article.publisher,
            "url": article.url,
            "llm_scored": scores.get("llm_scored", False),
            "llm_sentiment": scores.get("sentiment"),
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
    event.cluster_id = cluster.id


def build_events_for_company(
    session: Session,
    company: Company,
    days: int = 90,
    include_news: bool = True,
) -> int:
    """
    Build and cluster events for a company.

    Two filing passes:
    1. Most recent filing (any age) -- ensures every watchlist ticker always
       has at least one LLM-scored event regardless of when it last filed.
    2. All filings within the look-back window -- catches recent activity.

    Deduplication prevents double-processing.
    """
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)
    count = 0
    processed_filing_ids: set = set()

    # Pass 1: most recent filing regardless of age
    latest_filing = (
        session.query(Filing)
        .filter(Filing.company_id == company.id)
        .order_by(Filing.filing_date.desc())
        .first()
    )
    if latest_filing:
        if not _event_exists(session, "filing", latest_filing.id):
            document = (
                session.query(FilingDocument)
                .filter(FilingDocument.filing_id == latest_filing.id)
                .first()
            )
            event = build_event_from_filing(
                session, latest_filing, company, document, run_clustering=True
            )
            if event:
                count += 1
        processed_filing_ids.add(latest_filing.id)

    # Pass 2: windowed filings for recent activity
    windowed_filings = (
        session.query(Filing)
        .filter(Filing.company_id == company.id)
        .filter(Filing.filing_date >= cutoff)
        .order_by(Filing.filing_date.desc())
        .all()
    )
    for filing in windowed_filings:
        if filing.id in processed_filing_ids:
            continue
        if _event_exists(session, "filing", filing.id):
            processed_filing_ids.add(filing.id)
            continue
        document = (
            session.query(FilingDocument)
            .filter(FilingDocument.filing_id == filing.id)
            .first()
        )
        event = build_event_from_filing(
            session, filing, company, document, run_clustering=True
        )
        if event:
            count += 1
        processed_filing_ids.add(filing.id)

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
