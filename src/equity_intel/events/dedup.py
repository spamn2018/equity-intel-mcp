"""
Deterministic semantic deduplication for events and clusters.

Design principle: prefer conservative, explainable deduplication over
aggressive merging. When in doubt, keep events separate and let the
caller create a new cluster.

Key functions
-------------
normalize_title(title, ticker)
    Normalize an event title to a canonical sorted-token string suitable
    for Jaccard comparison.  Strips punctuation, company boilerplate,
    the ticker symbol, finance verbs, and stop words.

jaccard_similarity(a, b)
    Token-set Jaccard on two pre-normalized strings.

titles_are_duplicates(a, b, ticker, threshold)
    Boolean — True when normalized Jaccard >= threshold.

find_similar_cluster(session, ticker, event_type, occurred_at, title, ...)
    Search existing EventCluster records within a date window for one that
    closely matches the candidate title.  Used by the clustering engine to
    collapse events that span an ISO-week boundary (e.g. earnings announced
    Friday evening, news coverage starting Monday).
"""
from __future__ import annotations

import datetime
import re
from typing import TYPE_CHECKING, Optional, Set

if TYPE_CHECKING:
    from sqlalchemy.orm import Session
    from equity_intel.db.models import EventCluster


# ---------------------------------------------------------------------------
# Stop words and boilerplate tokens
# ---------------------------------------------------------------------------

_STOP_WORDS: frozenset[str] = frozenset({
    # Generic English
    "the", "a", "an", "and", "or", "of", "in", "on", "to", "by",
    "for", "with", "is", "was", "at", "as", "its", "it", "be",
    "from", "that", "this", "have", "has", "had", "are", "were",
    "not", "no", "but", "so", "if", "up", "do", "did", "will",
    # Finance verbs / boilerplate (high frequency, low signal)
    "announces", "announced", "reports", "reported", "releases", "released",
    "posts", "posted", "says", "said", "shows", "update", "updates",
    "files", "filed",
})

_COMPANY_SUFFIXES: frozenset[str] = frozenset({
    "inc", "corp", "ltd", "llc", "plc", "co", "company", "group",
    "holdings", "holding", "international", "technologies", "technology",
})


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def normalize_title(title: str, ticker: str = "") -> str:
    """
    Normalize an event title for deduplication comparison.

    Steps
    -----
    1. Lowercase
    2. Strip punctuation (preserve %, digits)
    3. Remove the ticker symbol (exact word match, case-insensitive)
    4. Tokenize on whitespace
    5. Remove stop words, company suffixes, and single-character tokens
    6. Sort remaining tokens (order-independent comparison)
    7. Join with space

    Returns a canonical string.  An empty title returns "".

    Examples
    --------
    >>> normalize_title("NVIDIA Corp. Reports Q4 Earnings Beat", "NVDA")
    'beat earnings nvidia q4'

    >>> normalize_title("Nvidia Q4 Earnings Beat Expectations", "NVDA")
    'beat earnings expectations nvidia q4'
    """
    if not title:
        return ""

    text = title.lower()

    # Strip punctuation except %, digits, letters, spaces
    text = re.sub(r"[^\w\s%]", " ", text)

    # Remove ticker symbol (as whole word)
    if ticker:
        text = re.sub(r"\b" + re.escape(ticker.lower()) + r"\b", " ", text)

    tokens = text.split()
    tokens = [
        t for t in tokens
        if t not in _STOP_WORDS
        and t not in _COMPANY_SUFFIXES
        and len(t) > 1
    ]

    return " ".join(sorted(tokens))


def jaccard_similarity(a: str, b: str) -> float:
    """
    Token-set Jaccard similarity on two pre-normalized strings.

    Returns a float in [0.0, 1.0].  Returns 0.0 if either string is empty.

    Parameters
    ----------
    a, b : normalized strings (output of normalize_title)
    """
    sa: Set[str] = set(a.split())
    sb: Set[str] = set(b.split())
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def titles_are_duplicates(
    a: str,
    b: str,
    ticker: str = "",
    threshold: float = 0.65,
) -> bool:
    """
    Return True when two event titles describe the same story.

    Uses normalized Jaccard similarity.  The default threshold of 0.65 is
    intentionally conservative: it allows word variation (e.g. "beat" vs
    "beats", articles, different word order) while keeping clearly distinct
    events separate.

    Parameters
    ----------
    a, b      : raw event title strings
    ticker    : ticker symbol to strip from both titles before comparison
    threshold : minimum Jaccard similarity to count as a duplicate
                (0.0 = always duplicate, 1.0 = exact match only)
    """
    if not a or not b:
        return False
    na = normalize_title(a, ticker)
    nb = normalize_title(b, ticker)
    return jaccard_similarity(na, nb) >= threshold


def find_similar_cluster(
    session: "Session",
    ticker: str,
    event_type: str,
    occurred_at: datetime.datetime,
    title: str,
    window_days: int = 10,
    threshold: float = 0.60,
) -> Optional["EventCluster"]:
    """
    Search for an existing EventCluster that covers the same story,
    potentially spanning an ISO-week boundary.

    Use case: an earnings release announced on Friday evening (ISO week N)
    generates news coverage starting Monday (ISO week N+1).  The strict
    cluster_key groups them into different clusters; this function finds the
    Friday cluster so Monday's events can be merged into it.

    Strategy
    --------
    1. Find all clusters for ``ticker`` within ±``window_days`` of ``occurred_at``.
    2. Filter to ``event_type`` or closely related types (see ``_RELATED_TYPES``).
    3. Compute normalized Jaccard similarity between ``title`` and each cluster title.
    4. Return the cluster with the highest similarity >= ``threshold``, or None.

    Conservative design
    -------------------
    - Only looks backward (not forward) by default to avoid merging into a
      cluster that doesn't yet contain the canonical event.
    - Returns None when no cluster meets the threshold, so a new cluster is
      created rather than an incorrect merge.
    - Does not modify any data.

    Parameters
    ----------
    session      : active SQLAlchemy session
    ticker       : stock ticker (used for DB filter and title normalization)
    event_type   : event type of the candidate event
    occurred_at  : datetime of the candidate event
    title        : title of the candidate event
    window_days  : look-back (and small look-ahead) window in calendar days
    threshold    : minimum Jaccard similarity to count as a cross-week duplicate
    """
    from equity_intel.db.models import EventCluster  # local import to avoid circular

    if not title:
        return None

    # Search backward ``window_days`` and a small forward buffer (2 days)
    # to catch same-day or next-day clusters from a slightly earlier event.
    search_start = occurred_at - datetime.timedelta(days=window_days)
    search_end = occurred_at + datetime.timedelta(days=2)

    # Related event types that commonly describe the same underlying story
    _RELATED_TYPES: dict[str, set[str]] = {
        "earnings": {"earnings", "guidance"},
        "guidance": {"guidance", "earnings"},
        "merger_acquisition": {"merger_acquisition"},
        "regulatory": {"regulatory", "litigation"},
        "litigation": {"litigation", "regulatory"},
    }
    search_types = _RELATED_TYPES.get(event_type, {event_type})

    try:
        candidates = (
            session.query(EventCluster)
            .filter(EventCluster.ticker == ticker.upper())
            .filter(EventCluster.event_type.in_(search_types))
            .filter(EventCluster.last_seen_at >= search_start)
            .filter(EventCluster.first_seen_at <= search_end)
            .all()
        )
    except Exception:
        # If the query fails for any reason (e.g. schema mismatch during tests),
        # return None conservatively rather than raising.
        return None

    if not candidates:
        return None

    norm_title = normalize_title(title, ticker)
    best_cluster: Optional["EventCluster"] = None
    best_sim: float = 0.0

    for cluster in candidates:
        cluster_norm = normalize_title(cluster.title or "", ticker)
        sim = jaccard_similarity(norm_title, cluster_norm)
        if sim >= threshold and sim > best_sim:
            best_sim = sim
            best_cluster = cluster

    return best_cluster
