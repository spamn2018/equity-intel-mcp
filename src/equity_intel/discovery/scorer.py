"""
Weekly aggregation and scoring engine for the Ticker Discovery Radar.

Scoring formula
---------------
total_score =
    0.35 * acceleration_score
  + 0.25 * mention_volume_score
  + 0.20 * source_quality_score
  + 0.10 * breadth_score
  + 0.10 * novelty_score

Promotion rule
--------------
Mark recommendation = 'probe_candidate' when:
    mention_count    >= 8
    total_score      >= 0.70
    acceleration_score >= 0.50
    (unique_source_count >= 3 OR unique_source_ticker_count >= 3)
    exclusion_flag is False
"""
from __future__ import annotations

import datetime
import math
from collections import defaultdict
from typing import Any, Dict, List, Optional, Set, Tuple

from equity_intel.config import settings
from equity_intel.logging_config import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Source quality weights
# ---------------------------------------------------------------------------
_SOURCE_QUALITY: Dict[str, float] = {
    "filing_document": 1.0,   # SEC primary source
    "event": 0.85,            # derived from SEC/news
    "event_cluster": 0.80,
    "news": 0.75,
    "gemini_news_block": 0.65,
    "lm_synthesis": 0.50,
    "podcast_intelligence": 0.45,
}

# Volume cap for mention_volume_score (mentions above this get score = 1.0)
_VOLUME_CAP = 30

# Weights
_W_ACCEL = 0.35
_W_VOL = 0.25
_W_QUAL = 0.20
_W_BREADTH = 0.10
_W_NOVELTY = 0.10

# Promotion thresholds
_PROMO_MIN_MENTIONS = 8
_PROMO_MIN_SCORE = 0.70
_PROMO_MIN_ACCEL = 0.50
_PROMO_MIN_SOURCES = 3


def _iso_week_offset(week_key: str, delta_weeks: int) -> str:
    """Return the ISO week key that is ``delta_weeks`` before/after ``week_key``."""
    # Parse YYYY-WNN
    year_str, week_str = week_key.split("-W")
    year, week = int(year_str), int(week_str)
    # Convert to a date, shift, and back
    jan4 = datetime.date(year, 1, 4)  # Jan 4 is always in week 1
    week1_monday = jan4 - datetime.timedelta(days=jan4.weekday())
    target_monday = week1_monday + datetime.timedelta(weeks=week - 1, days=delta_weeks * 7)
    iso = target_monday.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def _current_week_key() -> str:
    now = datetime.datetime.now(datetime.timezone.utc)
    iso = now.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def compute_acceleration_score(
    current: int,
    prior: int,
    four_week_avg: float,
) -> float:
    """
    Score acceleration from 0.0 to 1.0.

    Weights two signals equally:
      - current vs prior week  (week-over-week growth)
      - current vs 4-week average (trend deviation)

    Edge cases:
      - If prior == 0 and current > 0: full acceleration
      - Negative growth → 0.0
    """
    # WoW component
    if prior == 0:
        wow = 1.0 if current > 0 else 0.0
    else:
        ratio = current / prior
        # logistic scaling so 2× is ~0.5, 5× is ~0.8, 10× is ~0.95
        wow = 1.0 - 1.0 / (1.0 + max(0.0, ratio - 1.0))

    # Trend component
    if four_week_avg == 0:
        trend = 1.0 if current > 0 else 0.0
    else:
        ratio = current / four_week_avg
        trend = 1.0 - 1.0 / (1.0 + max(0.0, ratio - 1.0))

    return round((wow + trend) / 2.0, 4)


def compute_mention_volume_score(mention_count: int) -> float:
    """Sigmoid-like score that saturates at _VOLUME_CAP mentions."""
    return round(min(1.0, mention_count / _VOLUME_CAP), 4)


def compute_source_quality_score(source_types_seen: Set[str]) -> float:
    """Average quality weight across source types that contributed mentions."""
    if not source_types_seen:
        return 0.0
    weights = [_SOURCE_QUALITY.get(st, 0.4) for st in source_types_seen]
    return round(sum(weights) / len(weights), 4)


def compute_breadth_score(
    unique_source_count: int,
    unique_source_ticker_count: int,
    unique_source_types: int,
) -> float:
    """
    Score 0–1 based on how many distinct sources, monitored tickers, and
    source types contributed the mentions.

    Caps:
      sources            5 → 1.0
      source tickers     5 → 1.0
      source types       4 → 1.0
    """
    s_score = min(1.0, unique_source_count / 5.0)
    t_score = min(1.0, unique_source_ticker_count / 5.0)
    type_score = min(1.0, unique_source_types / 4.0)
    return round((s_score + t_score + type_score) / 3.0, 4)


def compute_novelty_score(
    ticker: str,
    default_tickers: Set[str],
    universe_tickers: Optional[Set[str]] = None,
) -> float:
    """
    Higher novelty when the ticker is NOT already in any known universe.
    1.0 = completely unknown
    0.3 = already in research universe (probe/watch)
    0.0 = in default_tickers (should also be excluded elsewhere)
    """
    if ticker in default_tickers:
        return 0.0
    if universe_tickers and ticker in universe_tickers:
        return 0.3
    return 1.0


# ---------------------------------------------------------------------------
# Aggregation over mentions table
# ---------------------------------------------------------------------------

class WeeklyAggregate:
    """In-memory aggregate for one (ticker, week_key) bucket."""

    def __init__(self, ticker: str, week_key: str) -> None:
        self.ticker = ticker
        self.week_key = week_key
        self.mention_count = 0
        self.source_ids: Set[str] = set()
        self.source_tickers: Set[str] = set()
        self.source_types: Set[str] = set()
        self.max_confidence = 0.0
        self.evidence: List[Dict[str, Any]] = []
        self.exclusion_flag = False

    def add(self, row: Any) -> None:
        """Incorporate one TickerMention row."""
        self.mention_count += 1
        if row.source_id:
            self.source_ids.add(row.source_id)
        if row.source_ticker:
            self.source_tickers.add(row.source_ticker)
        if row.source_type:
            self.source_types.add(row.source_type)
        if row.confidence and row.confidence > self.max_confidence:
            self.max_confidence = row.confidence
        if row.exclusion_flag:
            self.exclusion_flag = True
        if len(self.evidence) < 5 and row.context:
            self.evidence.append(
                {
                    "source_type": row.source_type,
                    "source_ticker": row.source_ticker,
                    "context": (row.context or "")[:200],
                    "occurred_at": row.occurred_at.isoformat() if row.occurred_at else None,
                    "url": row.url,
                }
            )


def aggregate_mentions(session, week_key: str) -> Dict[str, WeeklyAggregate]:
    """
    Pull all ticker_mentions rows for the given week_key and build aggregates.
    """
    from equity_intel.db.models import TickerMention

    rows = (
        session.query(TickerMention)
        .filter(TickerMention.week_key == week_key)
        .all()
    )
    buckets: Dict[str, WeeklyAggregate] = {}
    for row in rows:
        key = row.mentioned_ticker
        if key not in buckets:
            buckets[key] = WeeklyAggregate(key, week_key)
        buckets[key].add(row)
    return buckets


def _get_week_mention_count(session, ticker: str, week_key: str) -> int:
    """Return mention_count for a ticker in a specific week from the mentions table."""
    from equity_intel.db.models import TickerMention
    from sqlalchemy import func

    result = (
        session.query(func.count(TickerMention.id))
        .filter(
            TickerMention.mentioned_ticker == ticker,
            TickerMention.week_key == week_key,
        )
        .scalar()
    )
    return result or 0


def compute_scores(
    session,
    week_key: str,
    default_tickers: Set[str],
    universe_tickers: Optional[Set[str]] = None,
) -> List[Dict[str, Any]]:
    """
    Aggregate mentions for *week_key*, compute all score components,
    determine promotion recommendation, and return a list of score dicts
    ready for upsert into ticker_discovery_scores.
    """
    aggregates = aggregate_mentions(session, week_key)
    scored: List[Dict[str, Any]] = []

    prior_week = _iso_week_offset(week_key, -1)

    for ticker, agg in aggregates.items():
        # Historical counts
        prior_count = _get_week_mention_count(session, ticker, prior_week)

        # 4-week average (weeks -4 to -1)
        week_counts = []
        for delta in range(-4, 0):
            wk = _iso_week_offset(week_key, delta)
            week_counts.append(_get_week_mention_count(session, ticker, wk))
        four_week_avg = sum(week_counts) / 4.0 if week_counts else 0.0

        # Score components
        accel = compute_acceleration_score(agg.mention_count, prior_count, four_week_avg)
        vol = compute_mention_volume_score(agg.mention_count)
        qual = compute_source_quality_score(agg.source_types)
        breadth = compute_breadth_score(
            unique_source_count=len(agg.source_ids),
            unique_source_ticker_count=len(agg.source_tickers),
            unique_source_types=len(agg.source_types),
        )
        novelty = compute_novelty_score(ticker, default_tickers, universe_tickers)

        total = round(
            _W_ACCEL * accel
            + _W_VOL * vol
            + _W_QUAL * qual
            + _W_BREADTH * breadth
            + _W_NOVELTY * novelty,
            4,
        )

        # Promotion
        if agg.exclusion_flag:
            recommendation = "excluded"
        elif (
            agg.mention_count >= _PROMO_MIN_MENTIONS
            and total >= _PROMO_MIN_SCORE
            and accel >= _PROMO_MIN_ACCEL
            and (
                len(agg.source_ids) >= _PROMO_MIN_SOURCES
                or len(agg.source_tickers) >= _PROMO_MIN_SOURCES
            )
        ):
            recommendation = "probe_candidate"
        else:
            recommendation = "watch"

        scored.append(
            {
                "ticker": ticker,
                "week_key": week_key,
                "mention_count": agg.mention_count,
                "unique_source_count": len(agg.source_ids),
                "unique_source_ticker_count": len(agg.source_tickers),
                "prior_week_count": prior_count,
                "four_week_avg": round(four_week_avg, 2),
                "acceleration_score": accel,
                "mention_volume_score": vol,
                "source_quality_score": qual,
                "breadth_score": breadth,
                "novelty_score": novelty,
                "total_score": total,
                "recommendation": recommendation,
                "exclusion_flag": agg.exclusion_flag,
                "evidence_json": agg.evidence,
            }
        )

    return scored


def upsert_scores(session, scores: List[Dict[str, Any]]) -> int:
    """Upsert score dicts into ticker_discovery_scores. Returns rows saved."""
    from equity_intel.db.models import TickerDiscoveryScore, now_utc

    saved = 0
    for s in scores:
        existing = (
            session.query(TickerDiscoveryScore)
            .filter(
                TickerDiscoveryScore.ticker == s["ticker"],
                TickerDiscoveryScore.week_key == s["week_key"],
            )
            .first()
        )
        if existing:
            for field, value in s.items():
                if field not in ("ticker", "week_key"):
                    setattr(existing, field, value)
            existing.updated_at = now_utc()
        else:
            row = TickerDiscoveryScore(**s)
            session.add(row)
        saved += 1
    return saved
