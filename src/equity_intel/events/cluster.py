"""
Event clustering engine.

Groups related events into EventCluster records by:
  - Same ticker
  - Same event_type
  - Same ISO calendar week (Mon-Sun)

Within a cluster, novelty_score drops as more similar events arrive.
The cluster accumulates filing_ids, news_ids, source_urls, and aggregate
materiality/confidence scores that improve with each new piece of evidence.

Cross-week deduplication
------------------------
A hard ISO-week boundary can split the same story into two clusters —
for example, an earnings release announced Friday evening (week N) and
the resulting news coverage starting Monday morning (week N+1).

To handle this, build_or_update_cluster now calls find_similar_cluster
(from equity_intel.events.dedup) before creating a new cluster.  If a
near-duplicate cluster is found within a 10-day window, the event is
merged into the existing cluster rather than creating a new one.

This is intentionally conservative: the dedup threshold (0.60 Jaccard
on normalized titles) is calibrated to avoid merging truly distinct events.

Source-quality weighting
------------------------
Each cluster tracks a primary_source_quality score — the best (highest)
quality score among all sources that contributed to the cluster.  This is
passed to compute_cluster_confidence() to give a modest confidence boost
when the cluster is anchored by a primary SEC filing versus news-only.
"""
from __future__ import annotations

import datetime
from typing import Any, Dict, List, Optional, Set

from sqlalchemy.orm import Session

from equity_intel.db.models import (
    Company,
    Event,
    EventCluster,
    Filing,
    MarketPrice,
    NewsArticle,
    now_utc,
)
from equity_intel.events.score import compute_cluster_materiality, compute_cluster_confidence
from equity_intel.events.source_quality import source_quality_score, SOURCE_TIER_SCORES, SourceTier
from equity_intel.logging_config import get_logger

logger = get_logger(__name__)

def _to_utc(dt: Optional[datetime.datetime]) -> Optional[datetime.datetime]:
    """Normalize a naive or aware datetime to UTC-aware (SQLite returns naive)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=datetime.timezone.utc)
    return dt

_CAUTION = (
    "This cluster groups events that are correlated by ticker, event type, "
    "and time window. Price reactions shown are correlations, not confirmed causes."
)


# ---------------------------------------------------------------------------
# Cluster key
# ---------------------------------------------------------------------------

def cluster_key(ticker: str, event_type: str, dt: datetime.datetime) -> str:
    """
    Stable cluster key: ticker:event_type:YYYYWww

    Events in the same ISO week and sharing ticker + event_type are grouped
    into the same cluster.
    """
    iso = dt.isocalendar()
    return f"{ticker.upper()}:{event_type}:{iso.year}W{iso.week:02d}"


# ---------------------------------------------------------------------------
# Title similarity (word-level Jaccard, normalized)
# ---------------------------------------------------------------------------

def _word_jaccard(a: str, b: str) -> float:
    """
    Jaccard similarity on word sets, ignoring common stop words.

    Delegates to the dedup module's normalize_title + jaccard_similarity
    for consistent normalization across the codebase.
    """
    from equity_intel.events.dedup import normalize_title, jaccard_similarity  # noqa: PLC0415
    na = normalize_title(a)
    nb = normalize_title(b)
    return jaccard_similarity(na, nb)


def compute_novelty_score(title: str, existing_titles: List[str], ticker: str = "") -> float:
    """
    1.0 = completely novel event (no similar titles in cluster).
    Decreases as word overlap with prior cluster titles increases.

    Title comparison uses normalize_title() from the dedup module so that
    punctuation, company boilerplate, and stop words do not inflate novelty.

    Parameters
    ----------
    title          : title of the incoming event
    existing_titles: titles of events already in the cluster
    ticker         : ticker symbol to strip from titles before comparison
    """
    from equity_intel.events.dedup import normalize_title, jaccard_similarity  # noqa: PLC0415

    if not existing_titles:
        return 1.0

    norm_title = normalize_title(title, ticker)
    norm_existing = [normalize_title(t, ticker) for t in existing_titles]

    max_sim = max(jaccard_similarity(norm_title, t) for t in norm_existing)
    # novelty = 1 - similarity, floored at 0.1 (never totally irrelevant)
    return max(0.1, round(1.0 - max_sim, 4))


# ---------------------------------------------------------------------------
# Source-quality helpers
# ---------------------------------------------------------------------------

def _event_source_quality(event: Event) -> float:
    """
    Extract the source_quality_score stored in the event's evidence_json, or
    derive it from source_type if the field is missing (backward-compatible).
    """
    ev = event.evidence_json or {}
    stored = ev.get("source_quality_score")
    if stored is not None:
        try:
            return float(stored)
        except (TypeError, ValueError):
            pass
    # Fallback: derive from source_type (no publisher/url available here)
    return source_quality_score(source_type=event.source_type or "unknown")


def _primary_source_quality(
    filing_ids: List[int],
    news_ids: List[int],
    incoming_event: Event,
) -> float:
    """
    Compute the best (highest) source quality across all sources in a cluster.

    - If the cluster has at least one SEC filing, the primary quality is 1.0
      (SEC_FILING tier), regardless of other sources.
    - Otherwise, use the incoming event's own source quality (which was already
      stored in evidence_json by build.py) or fall back to deriving it.

    This is intentionally simple: the presence of a primary SEC filing is the
    strongest quality signal we have at the cluster level.
    """
    if filing_ids:
        return SOURCE_TIER_SCORES[SourceTier.SEC_FILING]  # 1.0
    return _event_source_quality(incoming_event)


# ---------------------------------------------------------------------------
# Price reaction helper
# ---------------------------------------------------------------------------

def _fetch_price_reaction(
    session: Session,
    ticker: str,
    occurred_at: Optional[datetime.datetime],
    window_days: int = 3,
) -> Optional[Dict[str, Any]]:
    """
    Fetch price bars around `occurred_at` from the local DB and compute the
    move. Returns None if no price data is available.
    """
    if not occurred_at:
        return None

    target_date = occurred_at.date() if hasattr(occurred_at, "date") else occurred_at

    fetch_start = target_date - datetime.timedelta(days=window_days + 5)
    fetch_end = target_date + datetime.timedelta(days=window_days + 2)

    rows = (
        session.query(
            MarketPrice.timestamp,
            MarketPrice.close,
            MarketPrice.adjusted_close,
            MarketPrice.volume,
        )
        .filter(MarketPrice.ticker == ticker)
        .filter(MarketPrice.interval == "1d")
        .filter(MarketPrice.timestamp >= datetime.datetime.combine(fetch_start, datetime.time.min))
        .filter(MarketPrice.timestamp <= datetime.datetime.combine(fetch_end, datetime.time.max))
        .order_by(MarketPrice.timestamp)
        .all()
    )

    if not rows:
        return None

    target_str = target_date.isoformat()
    before = [r for r in rows if r.timestamp.date().isoformat() < target_str]
    after = [r for r in rows if r.timestamp.date().isoformat() >= target_str]

    if not before or not after:
        return None

    price_before = before[-1].adjusted_close or before[-1].close
    price_after = after[0].adjusted_close or after[0].close
    vol_before = before[-1].volume
    vol_after = after[0].volume

    if not price_before or not price_after:
        return None

    pct_change = round((price_after - price_before) / price_before * 100, 2)
    vol_ratio = round(vol_after / vol_before, 2) if vol_before and vol_after else None

    return {
        "available": True,
        "pct_change": pct_change,
        "volume_ratio": vol_ratio,
        "price_before": round(price_before, 4),
        "price_after": round(price_after, 4),
        "date_before": before[-1].timestamp.date().isoformat(),
        "date_after": after[0].timestamp.date().isoformat(),
        "window_days": window_days,
    }


# ---------------------------------------------------------------------------
# Cluster find-or-create + update
# ---------------------------------------------------------------------------

def build_or_update_cluster(
    session: Session,
    event: Event,
    filing: Optional[Filing] = None,
    news_article: Optional[NewsArticle] = None,
) -> EventCluster:
    """
    Find an existing cluster for this event's (ticker, event_type, ISO-week) or
    create a new one. Updates aggregate scores, filing_ids/news_ids, and
    price reaction on every call.

    Cross-week deduplication
    ------------------------
    Before creating a genuinely new cluster, this function calls
    find_similar_cluster() to search for a near-duplicate cluster within a
    10-day window.  If a match is found (normalized title Jaccard >= 0.60),
    the event is merged into that cluster instead.

    Source-quality weighting
    ------------------------
    primary_source_quality is derived from the best source in the cluster
    (SEC filing > company IR > reputable news > syndicated > unknown) and
    passed to compute_cluster_confidence() for a modest confidence boost.

    Returns the cluster (already added to session but not yet committed).
    """
    if not event.occurred_at:
        occurred_at = event.detected_at or now_utc()
    else:
        occurred_at = event.occurred_at

    key = cluster_key(event.ticker or "", event.event_type or "other", occurred_at)

    cluster = (
        session.query(EventCluster)
        .filter(EventCluster.cluster_key == key)
        .first()
    )

    if cluster is None:
        # -- Cross-week dedup check ---------------------------------------
        # Before creating a new cluster, look for a near-duplicate cluster
        # in the adjacent time window (handles Friday-announced / Monday-covered
        # stories that would otherwise land in different ISO weeks).
        from equity_intel.events.dedup import find_similar_cluster  # noqa: PLC0415

        similar = find_similar_cluster(
            session=session,
            ticker=event.ticker or "",
            event_type=event.event_type or "other",
            occurred_at=occurred_at,
            title=event.title or "",
        )
        if similar is not None:
            # Reuse the existing cross-week cluster instead of creating a new one.
            logger.debug(
                "cross_week_dedup",
                ticker=event.ticker,
                new_key=key,
                existing_key=similar.cluster_key,
                event_title=event.title,
            )
            cluster = similar
        else:
            # ── Create new cluster ----------------------------------------
            price_reaction = _fetch_price_reaction(session, event.ticker or "", occurred_at)

            base_mat = event.materiality_score or 0.3
            base_conf = event.confidence_score or 0.5

            enhanced_mat = compute_cluster_materiality(
                base_score=base_mat,
                price_pct_change=price_reaction.get("pct_change") if price_reaction else None,
                volume_ratio=price_reaction.get("volume_ratio") if price_reaction else None,
                confirming_sources=1,
            )

            now = now_utc()
            cluster = EventCluster(
                cluster_key=key,
                ticker=event.ticker,
                event_type=event.event_type,
                event_subtype=event.event_subtype,
                title=event.title,
                summary=event.summary,
                first_seen_at=occurred_at,
                last_seen_at=occurred_at,
                event_count=1,
                filing_count=1 if filing else 0,
                news_count=1 if news_article else 0,
                materiality_score=enhanced_mat,
                confidence_score=base_conf,
                novelty_score=1.0,
                price_reaction_json=price_reaction,
                filing_ids={"ids": [filing.id] if filing else []},
                news_ids={"ids": [news_article.id] if news_article else []},
                source_urls={"urls": [url for url in [event.source_url] if url]},
                evidence_json=_build_evidence_json(event, filing, news_article, price_reaction),
                caution=_CAUTION,
                created_at=now,
                updated_at=now,
            )
            session.add(cluster)
            return cluster

    # ── Update existing cluster (same key OR cross-week match) ----------
    # Collect existing titles for novelty scoring
    existing_titles = [
        e.title for e in
        session.query(Event.title)
        .filter(Event.cluster_id == cluster.id)
        .limit(20)
        .all()
        if e.title
    ]
    novelty = compute_novelty_score(event.title or "", existing_titles, ticker=event.ticker or "")

    # Merge filing/news IDs
    filing_ids: List[int] = (cluster.filing_ids or {}).get("ids", [])
    news_ids: List[int] = (cluster.news_ids or {}).get("ids", [])
    source_urls: List[str] = (cluster.source_urls or {}).get("urls", [])

    if filing and filing.id not in filing_ids:
        filing_ids = filing_ids + [filing.id]
    if news_article and news_article.id not in news_ids:
        news_ids = news_ids + [news_article.id]
    if event.source_url and event.source_url not in source_urls:
        source_urls = source_urls + [event.source_url]

    # Recompute price reaction if not already present
    price_reaction = cluster.price_reaction_json
    if not price_reaction or not price_reaction.get("available"):
        price_reaction = _fetch_price_reaction(session, event.ticker or "", occurred_at)

    # Re-score with updated source count.
    total_sources = len(filing_ids) + len(news_ids)
    from sqlalchemy import func as _func
    max_raw = session.query(_func.max(Event.materiality_score)).filter(
        Event.cluster_id == cluster.id
    ).scalar()
    best_base = max(max_raw or 0.0, event.materiality_score or 0.0)
    enhanced_mat = compute_cluster_materiality(
        base_score=best_base,
        price_pct_change=price_reaction.get("pct_change") if price_reaction else None,
        volume_ratio=price_reaction.get("volume_ratio") if price_reaction else None,
        confirming_sources=total_sources,
    )

    # Compute primary_source_quality: best quality in the cluster after merging
    primary_sq = _primary_source_quality(filing_ids, news_ids, event)

    enhanced_conf = compute_cluster_confidence(
        base_confidence=max(cluster.confidence_score or 0.5, event.confidence_score or 0.5),
        has_price_reaction=bool(price_reaction and price_reaction.get("available")),
        filing_count=len(filing_ids),
        news_count=len(news_ids),
        primary_source_quality=primary_sq,
    )

    now = now_utc()
    cluster.event_count = (cluster.event_count or 0) + 1
    cluster.filing_count = len(filing_ids)
    cluster.news_count = len(news_ids)
    cluster.materiality_score = enhanced_mat
    cluster.confidence_score = enhanced_conf
    cluster.novelty_score = min(cluster.novelty_score or 1.0, novelty)
    _last = _to_utc(cluster.last_seen_at) or _to_utc(occurred_at)
    _occ  = _to_utc(occurred_at)
    cluster.last_seen_at = max(_last, _occ) if _last and _occ else _last or _occ
    cluster.price_reaction_json = price_reaction
    cluster.filing_ids = {"ids": filing_ids}
    cluster.news_ids = {"ids": news_ids}
    cluster.source_urls = {"urls": source_urls}
    cluster.evidence_json = _build_evidence_json(event, filing, news_article, price_reaction)
    cluster.updated_at = now

    return cluster


def _build_evidence_json(
    event: Event,
    filing: Optional[Filing],
    news: Optional[NewsArticle],
    price_reaction: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    evidence: Dict[str, Any] = {
        "event_type": event.event_type,
        "event_subtype": event.event_subtype,
    }
    if filing:
        evidence["filing"] = {
            "accession_number": filing.accession_number,
            "form_type": filing.form_type,
            "url": filing.filing_url,
            "items": filing.items,
        }
    if news:
        evidence["news"] = {
            "title": news.title,
            "publisher": news.publisher,
            "url": news.url,
            "published_at": news.published_at.isoformat() if news.published_at else None,
        }
    if price_reaction and price_reaction.get("available"):
        evidence["price_reaction"] = price_reaction
    return evidence


# ---------------------------------------------------------------------------
# Cluster all unclustered events for a company (batch pass)
# ---------------------------------------------------------------------------

def cluster_events_for_company(session: Session, company: Company) -> int:
    """
    Cluster all events for a company that don't yet have a cluster_id.
    Returns the number of events clustered.
    """
    unclustered = (
        session.query(Event)
        .filter(Event.ticker == company.ticker)
        .filter(Event.cluster_id.is_(None))
        .order_by(Event.occurred_at)
        .all()
    )

    count = 0
    for event in unclustered:
        # Fetch associated filing if this is a filing event
        filing: Optional[Filing] = None
        news: Optional[NewsArticle] = None

        if event.source_type == "filing" and event.source_id:
            from equity_intel.db.models import Filing as _Filing
            filing = session.get(_Filing, event.source_id)
        elif event.source_type == "news" and event.source_id:
            from equity_intel.db.models import NewsArticle as _NewsArticle
            news = session.get(_NewsArticle, event.source_id)

        cluster = build_or_update_cluster(session, event, filing=filing, news_article=news)
        session.flush()  # ensure cluster.id is populated

        event.cluster_id = cluster.id
        count += 1

    logger.info("events_clustered", ticker=company.ticker, count=count)
    return count
