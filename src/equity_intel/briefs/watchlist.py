"""
Watchlist Catalyst Brief Service.

Generates a ranked, evidence-backed research brief answering:
"What are the most important stock-moving catalysts across my watchlist right now?"

Key design choices:
- Prefers EventCluster data (multi-source, price-enriched) over raw Events.
- Falls back to raw Events when no clusters exist yet.
- Uses cautious language throughout ("likely related", "may reflect", etc.)
- Never asserts causation unless directly sourced.
- All monetary/score fields are [0, 1] or clearly documented.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import time
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from equity_intel.db.models import (
    Company,
    Event,
    EventCluster,
    Filing,
    NewsArticle,
)
from equity_intel.logging_config import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Brief output cache (avoids re-pulling articles on every run)
# ---------------------------------------------------------------------------

def _brief_cache_key(tickers: List[str], days: int, min_materiality: float) -> str:
    """Stable cache key based on the parameters that affect brief content."""
    payload = json.dumps({"t": sorted(tickers), "d": days, "m": round(min_materiality, 4)},
                         sort_keys=True)
    return hashlib.sha1(payload.encode()).hexdigest()[:16]


def _brief_cache_path(key: str) -> str:
    cache_dir = os.path.join(".cache", "brief")
    os.makedirs(cache_dir, exist_ok=True)
    return os.path.join(cache_dir, f"{key}.json")


def _brief_cache_get(key: str, ttl_seconds: int) -> Optional[Dict[str, Any]]:
    """Return cached brief if it exists and is fresh, else None."""
    path = _brief_cache_path(key)
    if not os.path.exists(path):
        return None
    age = time.time() - os.path.getmtime(path)
    if age > ttl_seconds:
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        logger.info("brief_cache_hit", key=key, age_seconds=int(age), ttl=ttl_seconds)
        return data
    except Exception:
        return None


def _brief_cache_set(key: str, brief: Dict[str, Any]) -> None:
    path = _brief_cache_path(key)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(brief, f, default=str)
    except Exception as exc:
        logger.warning("brief_cache_write_failed", error=str(exc))

# ------------------------------------------------------------------ #
# Research universe metadata enrichment                                #
# ------------------------------------------------------------------ #

_PROBE_NOTE = (
    "This is an early-stage research candidate (probe). "
    "Treat with extra caution — primary-source confirmation required before "
    "drawing any conclusions. Not comparable to established names."
)


def _load_research_meta() -> Dict[str, Any]:
    """
    Load the research universe ticker metadata dict (cached).

    Returns an empty dict if the research_universe module is unavailable
    or the config file is missing, so callers never crash.
    """
    try:
        from equity_intel.research_universe import get_ticker_metadata  # noqa: PLC0415
        return get_ticker_metadata()
    except Exception:
        return {}


def _enrich_catalyst_with_research_meta(
    catalyst: Dict[str, Any],
    research_meta: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Attach available research-universe metadata to a catalyst item.

    Added fields (only when present in the universe config):
        research_stage   – probe / watch / active / core / archived
        thesis_tags      – list of thesis theme strings
        risk_tags        – list of risk factor strings
        research_note    – plain-language note for probe-stage names

    This function is non-destructive — it never removes existing keys.
    """
    ticker = (catalyst.get("ticker") or "").upper()
    meta = research_meta.get(ticker)
    if not meta:
        return catalyst

    if "stage" in meta:
        catalyst["research_stage"] = meta["stage"]
        if meta["stage"] == "probe":
            catalyst["research_note"] = _PROBE_NOTE

    if "thesis_tags" in meta:
        catalyst["thesis_tags"] = meta["thesis_tags"]

    if "risk_tags" in meta:
        catalyst["risk_tags"] = meta["risk_tags"]

    return catalyst


# ------------------------------------------------------------------ #
# Constants                                                            #
# ------------------------------------------------------------------ #

SOURCE_NOTE = (
    "Source URLs are provided for all results. "
    "Dates are in UTC. Summaries are AI-generated from filing text. "
    "This is not investment advice."
)

CAUTION_DEFAULT = (
    "This brief shows correlation, not causation. "
    "Events are described as 'likely related to' or 'may reflect' market moves — "
    "not as confirmed causes. Always verify with primary sources before acting."
)


# ------------------------------------------------------------------ #
# Helpers                                                              #
# ------------------------------------------------------------------ #


def _serialize_date(d: Any) -> Optional[str]:
    if d is None:
        return None
    if isinstance(d, datetime.datetime):
        return d.isoformat()
    if isinstance(d, datetime.date):
        return d.isoformat()
    return str(d)


def _cutoff(days: int) -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)


def _safe_round(v: Any, digits: int = 4) -> Optional[float]:
    try:
        return round(float(v), digits)
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ #
# Per-catalyst evidence block builder                                  #
# ------------------------------------------------------------------ #


def _build_filing_evidence(session: Session, filing_ids: List[int]) -> List[Dict[str, Any]]:
    if not filing_ids:
        return []
    filings = session.query(Filing).filter(Filing.id.in_(filing_ids)).all()
    return [
        {
            "accession_number": f.accession_number,
            "form_type": f.form_type,
            "filing_date": _serialize_date(f.filing_date),
            "items": f.items,
            "url": f.filing_url,
        }
        for f in filings
    ]


def _build_news_evidence(session: Session, news_ids: List[int]) -> List[Dict[str, Any]]:
    if not news_ids:
        return []
    articles = session.query(NewsArticle).filter(NewsArticle.id.in_(news_ids)).all()
    return [
        {
            "title": a.title,
            "publisher": a.publisher,
            "published_at": _serialize_date(a.published_at),
            "url": a.url,
            "summary": a.summary,
        }
        for a in articles
    ]


# ------------------------------------------------------------------ #
# Source summary helper                                                #
# ------------------------------------------------------------------ #


def _build_source_summary(filing_count: int, news_count: int) -> str:
    """
    Build a concise, human-readable summary of what sources back a catalyst.

    Examples
    --------
    "1 SEC filing"
    "1 SEC filing, 3 news articles"
    "2 news articles"
    "(no sources)"
    """
    parts: List[str] = []
    if filing_count:
        parts.append(f"{filing_count} SEC filing{'s' if filing_count != 1 else ''}")
    if news_count:
        parts.append(f"{news_count} news article{'s' if news_count != 1 else ''}")
    return ", ".join(parts) if parts else "(no sources)"


# ------------------------------------------------------------------ #
# Price context helper                                                 #
# ------------------------------------------------------------------ #


def _extract_price_context(
    price_json: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Extract a compact price-reaction dict from a cluster's price_reaction_json."""
    if not price_json or not price_json.get("available"):
        return None
    return {
        "pct_change": _safe_round(price_json.get("pct_change"), 2),
        "volume_ratio": _safe_round(price_json.get("volume_ratio"), 2),
        "price_before": _safe_round(price_json.get("price_before"), 4),
        "price_after": _safe_round(price_json.get("price_after"), 4),
        "date_before": price_json.get("date_before"),
        "date_after": price_json.get("date_after"),
    }


# ------------------------------------------------------------------ #
# "Why it matters" narrative generator                                 #
# ------------------------------------------------------------------ #


def _why_it_matters(
    event_type: Optional[str],
    event_subtype: Optional[str],
    materiality_score: Optional[float],
    price_context: Optional[Dict[str, Any]],
) -> str:
    """
    Generate a concise, cautious rationale for why the catalyst may be significant.
    """
    mat = materiality_score or 0.0
    mat_label = "high" if mat >= 0.7 else "moderate" if mat >= 0.4 else "low"

    base_reasons: Dict[str, str] = {
        "earnings": "may reflect a material change in revenue or profitability expectations",
        "guidance": "may signal a shift in forward earnings expectations",
        "merger_acquisition": "may reflect a structural change in the company's business or valuation",
        "offering_or_dilution": "may indicate capital structure changes that could affect existing shareholders",
        "insider_transaction": "may provide a signal about insider sentiment toward the stock",
        "activist_stake": "may indicate external pressure for strategic changes",
        "management_change": "may reflect a shift in company strategy or execution risk",
        "regulatory": "may affect the company's operating environment or legal standing",
        "litigation": "may create contingent liabilities or reputational risk",
        "bankruptcy_or_going_concern": "may indicate material financial distress",
        "restatement": "may undermine confidence in previously reported financial figures",
        "buyback": "may signal management confidence and reduce share count",
    }

    reason = base_reasons.get(
        event_type or "",
        "available evidence suggests this event may be material to the stock",
    )

    narrative = f"This {mat_label}-materiality event {reason}."

    if price_context and price_context.get("pct_change") is not None:
        pct = price_context["pct_change"]
        direction = "up" if pct >= 0 else "down"
        narrative += (
            f" The available evidence suggests the stock moved {direction} "
            f"{abs(pct):.2f}% around this event, though this may reflect "
            "broader market factors rather than this catalyst alone."
        )

    return narrative


# ------------------------------------------------------------------ #
# Cluster → catalyst item                                              #
# ------------------------------------------------------------------ #


def _cluster_to_catalyst(
    cluster: EventCluster,
    company: Optional[Company],
    session: Session,
    include_price_context: bool,
    include_news: bool,
    include_filings: bool,
) -> Dict[str, Any]:
    """Convert one EventCluster ORM object into a catalyst brief item."""
    price_ctx = _extract_price_context(cluster.price_reaction_json) if include_price_context else None

    filing_ids: List[int] = (cluster.filing_ids or {}).get("ids", [])
    news_ids: List[int] = (cluster.news_ids or {}).get("ids", [])
    source_urls: List[str] = (cluster.source_urls or {}).get("urls", [])

    filing_count = cluster.filing_count or 0
    news_count = cluster.news_count or 0

    related_filings = _build_filing_evidence(session, filing_ids) if include_filings else []
    related_news = _build_news_evidence(session, news_ids) if include_news else []

    # Source-quality transparency fields
    has_primary_source: bool = filing_count > 0
    source_summary: str = _build_source_summary(filing_count, news_count)

    return {
        "cluster_id": cluster.id,
        "cluster_key": cluster.cluster_key,
        "ticker": cluster.ticker,
        "company_name": company.name if company else None,
        "sector": company.sector if company else None,
        "title": cluster.title,
        "event_type": cluster.event_type,
        "event_subtype": cluster.event_subtype,
        "why_it_matters": _why_it_matters(
            cluster.event_type,
            cluster.event_subtype,
            cluster.materiality_score,
            price_ctx,
        ),
        "materiality_score": _safe_round(cluster.materiality_score, 4),
        "confidence_score": _safe_round(cluster.confidence_score, 4),
        "novelty_score": _safe_round(cluster.novelty_score, 4),
        "first_seen_at": _serialize_date(cluster.first_seen_at),
        "last_seen_at": _serialize_date(cluster.last_seen_at),
        "event_count": cluster.event_count,
        "filing_count": filing_count,
        "news_count": news_count,
        "has_primary_source": has_primary_source,
        "source_summary": source_summary,
        "price_move": price_ctx,
        "volume_context": (
            f"Volume was approximately {price_ctx['volume_ratio']:.1f}× normal around this event"
            if price_ctx and price_ctx.get("volume_ratio")
            else None
        ),
        "source_links": source_urls[:5],
        "related_filing_ids": filing_ids,
        "related_news_ids": news_ids,
        "related_filings": related_filings,
        "related_news": related_news,
        "caution": cluster.caution or CAUTION_DEFAULT,
        "data_source": "event_clusters",
    }


# ------------------------------------------------------------------ #
# Raw event → catalyst item (fallback)                                 #
# ------------------------------------------------------------------ #


def _event_to_catalyst(
    event_row: Dict[str, Any],
    company: Optional[Company],
) -> Dict[str, Any]:
    """Convert a raw Event row dict into a catalyst brief item."""
    source_type = event_row.get("source_type", "")
    filing_count = 1 if source_type == "filing" else 0
    news_count = 1 if source_type == "news" else 0

    # Source-quality transparency fields
    has_primary_source: bool = filing_count > 0
    source_summary: str = _build_source_summary(filing_count, news_count)

    # Extract source quality from evidence_json if present
    ev = event_row.get("evidence_json") or {}
    source_quality_tier = ev.get("source_quality_tier")
    source_quality_label = ev.get("source_quality_label")

    return {
        "cluster_id": None,
        "cluster_key": None,
        "ticker": event_row.get("ticker"),
        "company_name": company.name if company else None,
        "sector": company.sector if company else None,
        "title": event_row.get("title"),
        "event_type": event_row.get("event_type"),
        "event_subtype": event_row.get("event_subtype"),
        "why_it_matters": _why_it_matters(
            event_row.get("event_type"),
            event_row.get("event_subtype"),
            event_row.get("materiality_score"),
            None,
        ),
        "materiality_score": _safe_round(event_row.get("materiality_score"), 4),
        "confidence_score": _safe_round(event_row.get("confidence_score"), 4),
        "novelty_score": _safe_round(event_row.get("novelty_score"), 4),
        "first_seen_at": _serialize_date(event_row.get("occurred_at")),
        "last_seen_at": _serialize_date(event_row.get("occurred_at")),
        "event_count": 1,
        "filing_count": filing_count,
        "news_count": news_count,
        "has_primary_source": has_primary_source,
        "source_summary": source_summary,
        "price_move": None,
        "volume_context": None,
        "source_links": [event_row["source_url"]] if event_row.get("source_url") else [],
        "related_filing_ids": [],
        "related_news_ids": [],
        "related_filings": [],
        "related_news": [],
        "caution": CAUTION_DEFAULT,
        "data_source": "events",
        **({"source_quality_tier": source_quality_tier} if source_quality_tier else {}),
        **({"source_quality_label": source_quality_label} if source_quality_label else {}),
    }


# ------------------------------------------------------------------ #
# Brief summary generator                                              #
# ------------------------------------------------------------------ #


def _generate_brief_summary(
    tickers: List[str],
    catalysts: List[Dict[str, Any]],
    days: int,
) -> str:
    """
    Generate a top-level prose summary for the brief.
    Uses cautious, factual language.
    """
    if not catalysts:
        return (
            f"No catalysts meeting the specified criteria were found for "
            f"{', '.join(tickers)} over the past {days} day(s)."
        )

    top = catalysts[0]
    ticker_set = sorted({c["ticker"] for c in catalysts if c.get("ticker")})
    ticker_str = ", ".join(ticker_set[:5])
    if len(ticker_set) > 5:
        ticker_str += f" and {len(ticker_set) - 5} more"

    high_mat = [c for c in catalysts if (c.get("materiality_score") or 0) >= 0.7]
    moderate_mat = [c for c in catalysts if 0.4 <= (c.get("materiality_score") or 0) < 0.7]

    summary = (
        f"Over the past {days} day(s), {len(catalysts)} catalyst(s) were identified "
        f"across {len(ticker_set)} ticker(s) ({ticker_str}). "
    )

    if high_mat:
        summary += (
            f"{len(high_mat)} are rated high-materiality (score ≥ 0.7). "
        )
    if moderate_mat:
        summary += (
            f"{len(moderate_mat)} are moderate-materiality (0.4–0.7). "
        )

    summary += (
        f"The top-ranked catalyst is '{top.get('title', 'unknown')}' "
        f"for {top.get('ticker', 'unknown')} "
        f"(materiality {top.get('materiality_score', 0):.2f}). "
        "The available evidence suggests these events may be material; "
        "always verify with primary sources."
    )

    return summary


# ------------------------------------------------------------------ #
# Main service function                                                #
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
    Generate a ranked catalyst brief for a watchlist of tickers.

    Parameters
    ----------
    session : SQLAlchemy Session
    tickers : list of ticker symbols (e.g. ["AAPL", "MSFT", "NVDA"])
    days : look-back window in calendar days (default 7)
    min_materiality : minimum materiality score [0, 1] to include (default 0.3)
    include_low_confidence : if False, drops catalysts with confidence_score < 0.3
    max_items : maximum number of catalysts to return (default 20)
    event_types : optional list of event type filters
    include_price_context : whether to include price move data (default True)
    include_news : whether to include linked news articles (default True)
    include_filings : whether to include linked SEC filings (default True)

    Returns
    -------
    dict with:
        generated_at, watchlist, time_window_days, filters_applied,
        brief_summary, total_catalysts, catalysts (ranked list)
    """
    # --- Cache check ---
    try:
        from equity_intel.config import settings as _s
        ttl = getattr(_s, "daily_brief_cache_ttl_seconds", 0)
    except Exception:
        ttl = 0
    if ttl > 0:
        _key = _brief_cache_key(tickers, days, min_materiality)
        _cached = _brief_cache_get(_key, ttl)
        if _cached is not None:
            return _cached

    generated_at = datetime.datetime.now(datetime.timezone.utc)
    tickers_upper = [t.strip().upper() for t in tickers if t.strip()]

    if not tickers_upper:
        return {
            "generated_at": _serialize_date(generated_at),
            "watchlist": [],
            "time_window_days": days,
            "filters_applied": {
                "min_materiality": min_materiality,
                "include_low_confidence": include_low_confidence,
                "event_types": event_types,
            },
            "brief_summary": "No tickers provided.",
            "total_catalysts": 0,
            "catalysts": [],
            "note": SOURCE_NOTE,
            "caution": CAUTION_DEFAULT,
        }

    # Build company lookup map: ticker → Company ORM object
    companies_orm = (
        session.query(Company)
        .filter(Company.ticker.in_(tickers_upper))
        .all()
    )
    company_map: Dict[str, Company] = {c.ticker: c for c in companies_orm}

    cutoff_dt = _cutoff(days)
    catalysts: List[Dict[str, Any]] = []

    # ── Phase 1: Cluster-based results (preferred) ──────────────────
    cq = (
        session.query(EventCluster)
        .filter(EventCluster.ticker.in_(tickers_upper))
        .filter(EventCluster.last_seen_at >= cutoff_dt)
        .filter(EventCluster.materiality_score >= min_materiality)
    )
    if event_types:
        cq = cq.filter(EventCluster.event_type.in_(event_types))
    if not include_low_confidence:
        cq = cq.filter(EventCluster.confidence_score >= 0.3)

    clusters = cq.order_by(
        EventCluster.materiality_score.desc(),
        EventCluster.last_seen_at.desc(),
    ).all()

    tickers_with_clusters: set[str] = set()
    for cluster in clusters:
        tickers_with_clusters.add(cluster.ticker)
        company = company_map.get(cluster.ticker)
        item = _cluster_to_catalyst(
            cluster, company, session,
            include_price_context=include_price_context,
            include_news=include_news,
            include_filings=include_filings,
        )
        catalysts.append(item)

    # ── Phase 2: Raw event fallback for tickers with no clusters ────
    unclustered_tickers = [t for t in tickers_upper if t not in tickers_with_clusters]
    if unclustered_tickers:
        eq = (
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
                Event.novelty_score,
                Event.evidence_json,
                Event.cluster_id,
            )
            .filter(Event.ticker.in_(unclustered_tickers))
            .filter(Event.occurred_at >= cutoff_dt)
            .filter(Event.materiality_score >= min_materiality)
        )
        if event_types:
            eq = eq.filter(Event.event_type.in_(event_types))
        if not include_low_confidence:
            eq = eq.filter(Event.confidence_score >= 0.3)

        raw_events = [dict(r._mapping) for r in eq.order_by(
            Event.materiality_score.desc(),
            Event.occurred_at.desc(),
        ).all()]

        for row in raw_events:
            company = company_map.get(row.get("ticker", ""))
            item = _event_to_catalyst(row, company)
            catalysts.append(item)

    # ── Sort all catalysts by materiality descending ─────────────────
    catalysts.sort(key=lambda c: (c.get("materiality_score") or 0.0), reverse=True)

    # ── Apply max_items cap ──────────────────────────────────────────
    catalysts = catalysts[:max_items]

    # ── Enrich with research-universe metadata (stage, tags, etc.) ──
    try:
        research_meta = _load_research_meta()
        if research_meta:
            catalysts = [
                _enrich_catalyst_with_research_meta(c, research_meta)
                for c in catalysts
            ]
    except Exception as _enrich_exc:  # never break the brief on enrichment failure
        logger.warning("research_meta_enrich_failed", error=str(_enrich_exc))

    brief_summary = _generate_brief_summary(tickers_upper, catalysts, days)

    result = {
        "generated_at": _serialize_date(generated_at),
        "watchlist": tickers_upper,
        "time_window_days": days,
        "filters_applied": {
            "min_materiality": min_materiality,
            "include_low_confidence": include_low_confidence,
            "max_items": max_items,
            "event_types": event_types,
            "include_price_context": include_price_context,
            "include_news": include_news,
            "include_filings": include_filings,
        },
        "brief_summary": brief_summary,
        "total_catalysts": len(catalysts),
        "catalysts": catalysts,
        "note": SOURCE_NOTE,
        "caution": CAUTION_DEFAULT,
    }

    # Write to cache if TTL is configured
    if ttl > 0:
        _brief_cache_set(_key, result)
        logger.info("brief_cache_written", key=_key, ttl=ttl)

    return result

# ------------------------------------------------------------------ #
# Markdown renderer (thin delegation to CLI renderer)                  #
# ------------------------------------------------------------------ #


def _render_markdown_from_brief(brief: dict) -> str:
    """
    Convert a get_watchlist_brief result dict into a Markdown report.

    Delegates to the shared renderer in generate_watchlist_brief so
    formatting logic is not duplicated.
    """
    # Import lazily to avoid circular dependency at module level
    from equity_intel.workers.generate_watchlist_brief import _render_markdown  # noqa: PLC0415
    return _render_markdown(brief)
