"""
News source filter — allowlist/blocklist for publisher credibility.

Blocked sources are state-controlled propaganda outlets and known
unreliable publishers. Articles from these sources are rejected
before they enter the database.

Allowed tiers:
  TIER_1  — Primary sources (SEC, company IR, newswires)
  TIER_2  — Tier-1 financial press (Reuters, Bloomberg, WSJ, FT, AP, Barron's)
  TIER_3  — Acceptable financial media (CNBC, Fox Business, Yahoo Finance, etc.)
  TIER_4  — Lower-confidence (still admitted, but confidence score penalized)

Anything not on any list is admitted by default at TIER_4 confidence.
Anything on BLOCKED is dropped entirely and logged.
"""
from __future__ import annotations

import re
from typing import Optional, Tuple

from equity_intel.logging_config import get_logger

logger = get_logger(__name__)

# ── Blocked sources ───────────────────────────────────────────────────────────
# State-controlled propaganda and known unreliable outlets.
# Match is case-insensitive substring of the publisher field.

BLOCKED_PUBLISHERS: frozenset = frozenset([
    # Chinese state media
    "xinhua", "cgtn", "global times", "people's daily", "peoples daily",
    "china daily", "chinadaily", "china news service", "caixin",
    # Russian state media
    "rt ", "russia today", "sputnik", "tass", "rbk",
    # Iranian state media
    "press tv", "presstv", "al-mayadeen", "al mayadeen", "mehr news",
    # Other state-adjacent
    "al-alam", "hispan tv",
])

# ── Tier 2 — Tier 1 financial press ──────────────────────────────────────────
TIER_2_PUBLISHERS: frozenset = frozenset([
    "reuters", "associated press", " ap ", "bloomberg",
    "wall street journal", "wsj", "financial times", "ft.com",
    "barron", "dow jones",
])

# ── Tier 3 — Acceptable financial media ──────────────────────────────────────
TIER_3_PUBLISHERS: frozenset = frozenset([
    "cnbc", "fox business", "marketwatch", "yahoo finance", "yahoo",
    "axios", "investor's business daily", "ibd", "the information",
    "fortune", "forbes", "seeking alpha", "benzinga", "the street",
    "thestreet", "business insider", "insider", "morningstar",
    "pr newswire", "prnewswire", "business wire", "businesswire",
    "globenewswire", "globe newswire", "accesswire",
])

# ── Tier 4 — Lower confidence (admitted, penalised) ──────────────────────────
TIER_4_PUBLISHERS: frozenset = frozenset([
    "motley fool", "investorplace", "zacks", "tipranks",
    "stockanalysis", "simply wall st", "macroaxis",
])

# Confidence penalty applied to tier-4 articles (subtracted from base score)
TIER_4_CONFIDENCE_PENALTY: float = 0.15


def _normalise(name: str) -> str:
    """Lowercase + collapse whitespace for fuzzy matching."""
    return re.sub(r"\s+", " ", name.lower().strip())


def _matches(publisher_norm: str, terms: frozenset) -> bool:
    return any(term in publisher_norm for term in terms)


def classify_publisher(publisher: str) -> Tuple[str, float]:
    """
    Classify a publisher name.

    Returns:
        (tier, confidence_modifier)
        tier in {"tier_1", "tier_2", "tier_3", "tier_4", "unknown", "blocked"}
        confidence_modifier: float to ADD to base confidence score
    """
    if not publisher:
        return "unknown", 0.0

    norm = _normalise(publisher)

    if _matches(norm, BLOCKED_PUBLISHERS):
        return "blocked", 0.0

    if _matches(norm, TIER_2_PUBLISHERS):
        return "tier_2", 0.10   # small boost for top-tier

    if _matches(norm, TIER_3_PUBLISHERS):
        return "tier_3", 0.0

    if _matches(norm, TIER_4_PUBLISHERS):
        return "tier_4", -TIER_4_CONFIDENCE_PENALTY

    return "unknown", 0.0


def is_allowed(publisher: str) -> bool:
    """Return True if the article should be admitted to the database."""
    tier, _ = classify_publisher(publisher)
    return tier != "blocked"


def filter_articles(articles: list, log_blocked: bool = True) -> list:
    """
    Filter a list of article dicts, removing blocked publishers.

    Each article dict must have a 'publisher' key.
    Returns the filtered list.
    """
    allowed = []
    blocked_count = 0

    for article in articles:
        publisher = article.get("publisher", "")
        tier, modifier = classify_publisher(publisher)

        if tier == "blocked":
            blocked_count += 1
            if log_blocked:
                logger.warning(
                    "news_article_blocked",
                    publisher=publisher,
                    title=article.get("title", "")[:80],
                )
            continue

        # Attach source metadata for downstream scoring
        article["source_tier"] = tier
        article["source_confidence_modifier"] = modifier
        allowed.append(article)

    if blocked_count:
        logger.info("news_filter_summary", blocked=blocked_count, admitted=len(allowed))

    return allowed
