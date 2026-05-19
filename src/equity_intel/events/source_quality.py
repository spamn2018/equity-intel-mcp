"""
Source-quality scoring for events and clusters.

Assigns each evidence source a quality tier and corresponding score.

Design principles
-----------------
- Deterministic: tier assignment uses only the source_type, publisher name,
  and URL — no ML, no network calls.
- Explainable: every score maps to a named tier with a human-readable label.
- Modest: scores influence confidence by at most ±0.10; they do not override
  materiality, event type severity, or price/volume signals.
- Transparent: tier and label are stored in evidence_json so callers can
  display them rather than hiding lower-quality sources.

Source tiers
------------
    SEC_FILING           — Primary SEC/EDGAR filings (direct regulatory disclosure)
    COMPANY_IR           — Official company press releases via wire services
    REPUTABLE_FINANCIAL  — Established financial news providers
    SYNDICATED           — Other named news providers
    UNKNOWN              — Missing or unrecognised source information

Quality scores by tier
----------------------
    SEC_FILING           : 1.00
    COMPANY_IR           : 0.80
    REPUTABLE_FINANCIAL  : 0.70
    SYNDICATED           : 0.50
    UNKNOWN              : 0.30
"""
from __future__ import annotations

from enum import Enum
from typing import Optional, Union


# ---------------------------------------------------------------------------
# Tier definition
# ---------------------------------------------------------------------------


class SourceTier(Enum):
    """Quality tier for an event source."""

    SEC_FILING = "sec_filing"
    COMPANY_IR = "company_ir"
    REPUTABLE_FINANCIAL = "reputable_financial"
    SYNDICATED = "syndicated"
    UNKNOWN = "unknown"


# Deterministic quality score per tier
SOURCE_TIER_SCORES: dict[SourceTier, float] = {
    SourceTier.SEC_FILING: 1.00,
    SourceTier.COMPANY_IR: 0.80,
    SourceTier.REPUTABLE_FINANCIAL: 0.70,
    SourceTier.SYNDICATED: 0.50,
    SourceTier.UNKNOWN: 0.30,
}

# Human-readable labels for transparent output
SOURCE_TIER_LABELS: dict[SourceTier, str] = {
    SourceTier.SEC_FILING: "Primary SEC filing",
    SourceTier.COMPANY_IR: "Official company press release",
    SourceTier.REPUTABLE_FINANCIAL: "Reputable financial news",
    SourceTier.SYNDICATED: "Syndicated news",
    SourceTier.UNKNOWN: "Unknown source",
}


# ---------------------------------------------------------------------------
# Publisher lookup sets
# ---------------------------------------------------------------------------

# Wire/PR services — typically carry official company press releases
_COMPANY_IR_PUBLISHERS: frozenset[str] = frozenset({
    "pr newswire", "business wire", "businesswire",
    "globe newswire", "globenewswire", "accesswire",
    "pr web", "cision", "businesswire.com",
})

# Established financial news providers
_REPUTABLE_PUBLISHERS: frozenset[str] = frozenset({
    "reuters", "bloomberg", "wall street journal", "wsj",
    "financial times", "ft", "cnbc", "marketwatch",
    "barron's", "barrons", "dow jones", "associated press",
    "ap", "ap financial", "nasdaq", "morningstar", "s&p global",
    "the economist", "fortune", "the financial times",
})

# SEC-related domains (used for URL-based fallback)
_SEC_DOMAINS: frozenset[str] = frozenset({
    "sec.gov", "data.sec.gov", "efts.sec.gov",
    "www.sec.gov", "edgaronline.com",
})


# ---------------------------------------------------------------------------
# Core tier assignment
# ---------------------------------------------------------------------------


def tier_for_source(
    source_type: str,
    provider: Optional[str] = None,
    publisher: Optional[str] = None,
    url: Optional[str] = None,
) -> SourceTier:
    """
    Determine the quality tier for a source.

    Parameters
    ----------
    source_type : "filing", "news", "press_release", or other
    provider    : upstream data provider (e.g. "polygon")
    publisher   : the actual news outlet or wire service (e.g. "Reuters")
    url         : source URL (used as final fallback to detect SEC pages)

    Returns
    -------
    SourceTier
    """
    st = (source_type or "").lower().strip()

    # -- SEC / EDGAR filings always rank highest --------------------------
    if st == "filing":
        return SourceTier.SEC_FILING

    # -- Press releases: company IR via wire services --------------------
    if st == "press_release":
        return SourceTier.COMPANY_IR

    # -- News articles: classify by publisher ----------------------------
    if st == "news":
        pub = (publisher or "").lower().strip()

        # Wire/PR services → company IR
        if pub in _COMPANY_IR_PUBLISHERS:
            return SourceTier.COMPANY_IR

        # Reputable financial outlets (exact match)
        if pub in _REPUTABLE_PUBLISHERS:
            return SourceTier.REPUTABLE_FINANCIAL

        # Partial match for compound names ("Reuters Health", "Bloomberg Tax", …)
        for rep in _REPUTABLE_PUBLISHERS:
            if len(rep) >= 4 and rep in pub:
                return SourceTier.REPUTABLE_FINANCIAL

        # URL fallback: article hosted on SEC domain counts as primary
        if url:
            url_lower = url.lower()
            for dom in _SEC_DOMAINS:
                if dom in url_lower:
                    return SourceTier.SEC_FILING

        # Named but unclassified publisher → syndicated
        if pub:
            return SourceTier.SYNDICATED

        # No publisher info at all
        return SourceTier.UNKNOWN

    # -- Unknown source_type: URL-based last resort ----------------------
    if url:
        url_lower = url.lower()
        for dom in _SEC_DOMAINS:
            if dom in url_lower:
                return SourceTier.SEC_FILING

    return SourceTier.UNKNOWN


# ---------------------------------------------------------------------------
# Public convenience functions
# ---------------------------------------------------------------------------


def source_quality_score(
    source_type: str,
    provider: Optional[str] = None,
    publisher: Optional[str] = None,
    url: Optional[str] = None,
) -> float:
    """
    Return a quality score in [0, 1] for a source.

    Delegates to ``tier_for_source()`` then looks up the tier score.
    """
    tier = tier_for_source(source_type, provider=provider, publisher=publisher, url=url)
    return SOURCE_TIER_SCORES[tier]


def source_quality_label(
    tier_or_source_type: Union[SourceTier, str],
    provider: Optional[str] = None,
    publisher: Optional[str] = None,
    url: Optional[str] = None,
) -> str:
    """
    Return a human-readable quality label for transparency in brief output.

    Can be called with either a ``SourceTier`` enum value directly, or with
    the same arguments as ``tier_for_source()`` to derive the tier first.
    """
    if isinstance(tier_or_source_type, SourceTier):
        tier = tier_or_source_type
    else:
        tier = tier_for_source(
            tier_or_source_type,
            provider=provider,
            publisher=publisher,
            url=url,
        )
    return SOURCE_TIER_LABELS[tier]


def source_quality_metadata(
    source_type: str,
    provider: Optional[str] = None,
    publisher: Optional[str] = None,
    url: Optional[str] = None,
) -> dict:
    """
    Return a dict suitable for embedding in evidence_json.

    Keys:
        source_quality_tier  : tier name (str, e.g. "sec_filing")
        source_quality_score : float score in [0, 1]
        source_quality_label : human-readable label
    """
    tier = tier_for_source(source_type, provider=provider, publisher=publisher, url=url)
    return {
        "source_quality_tier": tier.value,
        "source_quality_score": SOURCE_TIER_SCORES[tier],
        "source_quality_label": SOURCE_TIER_LABELS[tier],
    }
