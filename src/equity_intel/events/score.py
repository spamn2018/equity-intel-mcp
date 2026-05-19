"""
Event materiality scoring.

Produces a materiality_score in [0, 1] based on:
- Form type importance
- 8-K item importance
- Keyword severity
- Source quality (via source_type)
- Recency

Confidence scoring also accepts an explicit source_quality float
(from equity_intel.events.source_quality) for finer-grained weighting.
"""
from __future__ import annotations

import datetime
from typing import List, Optional


# Form type base scores
FORM_BASE_SCORE: dict[str, float] = {
    # Institutional ownership disclosures (quarterly 13F-HR)
    "13F-HR": 0.35,
    "13F-HR/A": 0.32,
    "8-K": 0.6,
    "10-K": 0.5,
    "10-Q": 0.45,
    "S-1": 0.55,
    "S-3": 0.45,
    "424B1": 0.4,
    "424B2": 0.4,
    "424B3": 0.4,
    "424B4": 0.4,
    "424B5": 0.4,
    "SC 13D": 0.65,
    "13D": 0.65,
    "SC 13G": 0.5,
    "13G": 0.5,
    "4": 0.22,
    "144": 0.18,
    "DEF 14A": 0.3,
}

# 8-K item scores (delta added on top of form base)
ITEM_SCORE_DELTA: dict[str, float] = {
    "1.01": 0.1,
    "1.02": 0.1,
    "1.03": 0.25,
    "2.01": 0.15,
    "2.02": 0.15,
    "2.03": 0.05,
    "2.04": 0.05,
    "2.05": 0.08,
    "2.06": 0.12,
    "3.01": 0.20,
    "3.02": 0.10,
    "4.01": 0.12,
    "4.02": 0.20,
    "5.01": 0.08,
    "5.02": 0.10,
    "5.07": 0.05,
    "7.01": 0.05,
    "8.01": 0.02,
    "9.01": 0.01,
}

# Keyword score deltas
KEYWORD_SCORE_DELTA: dict[str, float] = {
    "bankruptcy": 0.25,
    "going concern": 0.22,
    "restatement": 0.20,
    "restated": 0.20,
    "material weakness": 0.18,
    "sec investigation": 0.22,
    "doj": 0.20,
    "subpoena": 0.15,
    "class action": 0.12,
    "securities fraud": 0.18,
    "fda approval": 0.18,
    "fda rejection": 0.18,
    "complete response letter": 0.18,
    "merger": 0.15,
    "acquisition": 0.15,
    "tender offer": 0.18,
    "offering": 0.10,
    "dilution": 0.10,
    "reverse split": 0.15,
    "delisting": 0.18,
    "strategic alternatives": 0.15,
    "guidance raised": 0.12,
    "guidance lowered": 0.14,
    "guidance cut": 0.14,
    "write-down": 0.10,
    "impairment": 0.10,
    "resignation": 0.08,
    "termination": 0.08,
    "investigation": 0.12,
}


def compute_materiality_score(
    form_type: Optional[str] = None,
    items: Optional[str] = None,
    keywords: Optional[List[str]] = None,
    occurred_at: Optional[datetime.datetime] = None,
    source_type: str = "filing",
) -> float:
    """
    Compute a materiality score in [0, 1].

    Higher = more likely to be material to investors.
    """
    score = FORM_BASE_SCORE.get(form_type or "", 0.3)

    # Add item-based deltas
    if items:
        item_list = [i.strip() for i in str(items).split(",") if i.strip()]
        for item in item_list:
            delta = ITEM_SCORE_DELTA.get(item, 0.0)
            score += delta

    # Add keyword deltas (pick highest for each keyword)
    if keywords:
        kw_delta = max(
            (KEYWORD_SCORE_DELTA.get(kw, 0.0) for kw in keywords),
            default=0.0,
        )
        score += kw_delta

    # Recency boost
    if occurred_at:
        age_hours = (
            datetime.datetime.now(datetime.timezone.utc) - occurred_at.replace(tzinfo=datetime.timezone.utc)
        ).total_seconds() / 3600
        if age_hours < 24:
            score += 0.05
        elif age_hours < 72:
            score += 0.02

    # Source quality: news is secondary evidence
    if source_type == "news":
        score *= 0.85

    return min(1.0, max(0.0, round(score, 4)))


def compute_confidence_score(
    has_parsed_text: bool = False,
    has_price_reaction: bool = False,
    keyword_count: int = 0,
    source_quality: float = 0.5,
) -> float:
    """
    Confidence that this event is accurately classified.

    Parameters
    ----------
    has_parsed_text  : whether filing/article body was parsed successfully
    has_price_reaction : whether price-reaction data is available
    keyword_count    : number of high-signal keywords detected
    source_quality   : quality score from source_quality_score() [0, 1].
                       Modestly adjusts confidence by mapping [0, 1] → [-0.05, +0.10].
                       Defaults to 0.5 (neutral, same as before).
    """
    score = 0.5
    if has_parsed_text:
        score += 0.2
    if has_price_reaction:
        score += 0.15
    score += min(keyword_count * 0.03, 0.15)

    # Source-quality adjustment: modest ±0.10 range.
    # quality=1.0 (SEC filing) → +0.10
    # quality=0.5 (neutral)    →  0.00
    # quality=0.3 (unknown)    → -0.04
    quality_adj = (source_quality - 0.5) * 0.20
    score += quality_adj

    return min(1.0, max(0.0, round(score, 4)))


# ---------------------------------------------------------------------------
# Cluster-level scoring
# ---------------------------------------------------------------------------

def compute_cluster_materiality(
    base_score: float,
    price_pct_change: Optional[float] = None,
    volume_ratio: Optional[float] = None,
    confirming_sources: int = 1,
) -> float:
    """
    Adjust a base materiality score using price move, volume anomaly, and
    the number of independent sources confirming the same event.
    """
    score = base_score

    # Abnormal price move boost
    if price_pct_change is not None:
        abs_move = abs(price_pct_change)
        if abs_move >= 15:
            score += 0.22
        elif abs_move >= 10:
            score += 0.16
        elif abs_move >= 5:
            score += 0.10
        elif abs_move >= 2:
            score += 0.04

    # Abnormal volume boost
    if volume_ratio is not None:
        if volume_ratio >= 4.0:
            score += 0.12
        elif volume_ratio >= 3.0:
            score += 0.08
        elif volume_ratio >= 2.0:
            score += 0.04

    # Multi-source corroboration
    if confirming_sources >= 5:
        score += 0.12
    elif confirming_sources >= 3:
        score += 0.08
    elif confirming_sources >= 2:
        score += 0.04

    return min(1.0, max(0.0, round(score, 4)))


def compute_cluster_confidence(
    base_confidence: float = 0.5,
    has_price_reaction: bool = False,
    filing_count: int = 0,
    news_count: int = 0,
    primary_source_quality: float = 0.5,
) -> float:
    """
    Cluster-level confidence score.

    Starts from the best individual event confidence, then boosts based on:
    - Price reaction corroboration
    - Multiple filings on the same topic
    - News corroboration
    - Source quality of the best source in the cluster

    Parameters
    ----------
    base_confidence       : best individual confidence among events in the cluster
    has_price_reaction    : whether a price reaction was found for this cluster
    filing_count          : number of linked SEC filings
    news_count            : number of linked news articles
    primary_source_quality: quality score of the highest-quality source in the
                            cluster (from source_quality_score()). Defaults to 0.5.
                            A cluster backed by SEC filings gets a +0.05 boost;
                            an unknown-only cluster gets -0.02.
    """
    score = base_confidence

    if has_price_reaction:
        score += 0.10

    # Additional corroborating filings
    if filing_count >= 2:
        score += min((filing_count - 1) * 0.03, 0.12)

    # News corroboration
    if news_count >= 3:
        score += 0.08
    elif news_count >= 1:
        score += 0.04

    # Primary source quality adjustment: modest ±0.05 range
    # quality=1.0 (SEC filing)  → +0.05
    # quality=0.5 (neutral)     →  0.00
    # quality=0.3 (unknown)     → -0.02
    quality_adj = (primary_source_quality - 0.5) * 0.10
    score += quality_adj

    return min(1.0, max(0.0, round(score, 4)))
