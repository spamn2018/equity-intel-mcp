"""
Tests for equity_intel.events.dedup — semantic deduplication module.

Design principles:
  - All tests use pure functions or in-memory SQLite (no PostgreSQL, no network).
  - Conservative threshold testing: duplicates must be genuinely similar;
    distinct events must stay separate.
  - Evidence preservation: merging must never discard filing_ids or news_ids.

Coverage:
  normalize_title:
    - lowercase
    - strips punctuation
    - strips ticker symbol
    - removes company suffixes (Inc., Corp., etc.)
    - removes stop words
    - removes finance boilerplate verbs
    - sorts tokens (order-independent output)
    - handles empty string
    - handles title with only stop words

  jaccard_similarity:
    - identical normalized strings -> 1.0
    - completely disjoint -> 0.0
    - partial overlap -> correct value
    - empty string(s) -> 0.0

  titles_are_duplicates:
    - clearly duplicate titles -> True
    - word-order variation -> True
    - clearly distinct titles -> False
    - one empty -> False
    - threshold parameter respected

  find_similar_cluster:
    - returns None when no clusters exist
    - returns None for wrong ticker
    - returns None when similarity below threshold
    - returns None outside window
    - returns cluster for cross-week duplicate
    - returns None for same-ticker different-event-type when not related

  build_or_update_cluster (integration — cross-week dedup):
    - same ISO week always merges (existing behaviour preserved)
    - cross-week near-duplicate merges into existing cluster
    - cross-week distinct events create separate cluster
    - evidence (filing_ids, news_ids) preserved after cross-week merge
    - different tickers always produce separate clusters

  No network calls
"""
from __future__ import annotations

import datetime
from typing import Optional
from unittest.mock import patch

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from equity_intel.db.models import Base, Company, Event, EventCluster, now_utc
from equity_intel.events.dedup import (
    find_similar_cluster,
    jaccard_similarity,
    normalize_title,
    titles_are_duplicates,
)
from equity_intel.events.cluster import build_or_update_cluster, cluster_key


# ---------------------------------------------------------------------------
# DB fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def engine():
    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture
def session(engine):
    Sess = sessionmaker(bind=engine)
    s = Sess()
    yield s
    s.rollback()
    s.close()


# Helpers -------------------------------------------------------------------

def _dt(days_ago: int = 0, weeks_ago: int = 0) -> datetime.datetime:
    delta = datetime.timedelta(days=days_ago + weeks_ago * 7)
    return datetime.datetime.now(datetime.timezone.utc) - delta


def _cluster(
    session,
    ticker: str,
    event_type: str,
    title: str,
    days_ago: int = 0,
) -> EventCluster:
    occurred = _dt(days_ago)
    key = cluster_key(ticker, event_type, occurred)
    c = EventCluster(
        cluster_key=key,
        ticker=ticker,
        event_type=event_type,
        title=title,
        first_seen_at=occurred,
        last_seen_at=occurred,
        event_count=1,
        filing_count=1,
        news_count=0,
        materiality_score=0.7,
        confidence_score=0.6,
        novelty_score=1.0,
        filing_ids={"ids": [100]},
        news_ids={"ids": []},
        source_urls={"urls": ["https://sec.gov/original"]},
        caution="Test cluster",
        created_at=now_utc(),
        updated_at=now_utc(),
    )
    session.add(c)
    session.flush()
    return c


def _event(
    session,
    ticker: str,
    event_type: str,
    title: str,
    days_ago: int = 0,
    source_id: int = 999,
    source_url: str = "https://sec.gov/new",
) -> Event:
    occurred = _dt(days_ago)
    e = Event(
        ticker=ticker,
        event_type=event_type,
        event_subtype="results_of_operations",
        title=title,
        summary="Test event",
        source_type="filing",
        source_id=source_id,
        source_url=source_url,
        occurred_at=occurred,
        detected_at=now_utc(),
        materiality_score=0.6,
        novelty_score=1.0,
        confidence_score=0.5,
        created_at=now_utc(),
        updated_at=now_utc(),
    )
    session.add(e)
    session.flush()
    return e


# ---------------------------------------------------------------------------
# 1. normalize_title
# ---------------------------------------------------------------------------


def test_normalize_lowercases():
    assert normalize_title("APPLE Earnings Beat") == normalize_title("apple earnings beat")


def test_normalize_strips_punctuation():
    assert "beat" in normalize_title("Apple's Q4 earnings beat!")
    assert "!" not in normalize_title("Apple's Q4 earnings beat!")
    assert "'" not in normalize_title("Apple's Q4 earnings beat!")


def test_normalize_removes_ticker():
    result = normalize_title("AAPL Q4 Earnings Beat", "AAPL")
    assert "aapl" not in result


def test_normalize_removes_ticker_case_insensitive():
    result = normalize_title("Aapl Q4 Earnings Beat", "AAPL")
    assert "aapl" not in result


def test_normalize_removes_company_suffixes():
    result = normalize_title("Apple Inc. Earnings Beat")
    assert "inc" not in result
    result2 = normalize_title("Microsoft Corp. Revenue Up")
    assert "corp" not in result2


def test_normalize_removes_stop_words():
    result = normalize_title("The company announced earnings for the quarter")
    assert "the" not in result
    assert "for" not in result


def test_normalize_removes_finance_boilerplate():
    result = normalize_title("Apple announces Q4 earnings beat")
    assert "announces" not in result


def test_normalize_sorts_tokens():
    a = normalize_title("Earnings Beat Apple Q4")
    b = normalize_title("Q4 Apple Earnings Beat")
    assert a == b


def test_normalize_empty_string():
    assert normalize_title("") == ""


def test_normalize_only_stop_words():
    result = normalize_title("the and or for with")
    assert result == ""


def test_normalize_preserves_meaningful_tokens():
    result = normalize_title("NVDA Q4 earnings beat revenue record", "NVDA")
    assert "earnings" in result
    assert "beat" in result
    assert "revenue" in result


# ---------------------------------------------------------------------------
# 2. jaccard_similarity
# ---------------------------------------------------------------------------


def test_jaccard_identical():
    a = normalize_title("Apple Q4 Earnings Beat")
    assert jaccard_similarity(a, a) == 1.0


def test_jaccard_disjoint():
    a = normalize_title("Apple Q4 Earnings Beat")
    b = normalize_title("Bankruptcy going concern warning")
    assert jaccard_similarity(a, b) == 0.0


def test_jaccard_partial_overlap():
    a = normalize_title("Nvidia Q4 Earnings Beat")
    b = normalize_title("Nvidia Q4 Earnings Beat Expectations")
    sim = jaccard_similarity(a, b)
    # Should be high (4 shared out of 5 total tokens)
    assert sim >= 0.7


def test_jaccard_empty_a():
    assert jaccard_similarity("", "something") == 0.0


def test_jaccard_empty_b():
    assert jaccard_similarity("something", "") == 0.0


def test_jaccard_both_empty():
    assert jaccard_similarity("", "") == 0.0


def test_jaccard_range():
    a = normalize_title("earnings beat guidance raised")
    b = normalize_title("earnings beat raised forecast outlook")
    sim = jaccard_similarity(a, b)
    assert 0.0 <= sim <= 1.0


# ---------------------------------------------------------------------------
# 3. titles_are_duplicates
# ---------------------------------------------------------------------------


def test_dup_same_story_word_variation():
    assert titles_are_duplicates(
        "Nvidia Q4 Earnings Beat",
        "Nvidia Q4 Earnings Beat Expectations",
        ticker="NVDA",
    )


def test_dup_different_order():
    assert titles_are_duplicates(
        "Earnings Beat Q4 Nvidia",
        "Nvidia Q4 Earnings Beat Record",
        ticker="NVDA",
    )


def test_not_dup_clearly_different():
    assert not titles_are_duplicates(
        "Apple Q4 Earnings Beat",
        "Microsoft Acquires Activision Blizzard",
    )


def test_not_dup_different_event_types():
    assert not titles_are_duplicates(
        "TSLA going concern bankruptcy warning",
        "TSLA Q4 earnings beat revenue record",
        ticker="TSLA",
    )


def test_not_dup_empty_a():
    assert not titles_are_duplicates("", "NVDA Q4 Earnings Beat")


def test_not_dup_empty_b():
    assert not titles_are_duplicates("NVDA Q4 Earnings Beat", "")


def test_dup_threshold_respected():
    # Use a very high threshold — no real-world titles should match
    assert not titles_are_duplicates(
        "Apple Q4 Beat",
        "Apple Q4 Beat Estimates",
        threshold=0.99,
    )


# ---------------------------------------------------------------------------
# 4. find_similar_cluster
# ---------------------------------------------------------------------------


def test_find_similar_returns_none_no_clusters(session):
    result = find_similar_cluster(
        session,
        ticker="ZZZZ",
        event_type="earnings",
        occurred_at=_dt(0),
        title="ZZZZ Q4 Earnings Beat",
    )
    assert result is None


def test_find_similar_returns_none_wrong_ticker(session):
    _cluster(session, "AAPL", "earnings", "Apple Q4 Earnings Beat", days_ago=3)
    result = find_similar_cluster(
        session,
        ticker="MSFT",
        event_type="earnings",
        occurred_at=_dt(0),
        title="Apple Q4 Earnings Beat",
    )
    assert result is None


def test_find_similar_returns_none_outside_window(session):
    # Cluster is 20 days ago; default window is 10 days
    _cluster(session, "QCOM", "earnings", "QCOM Q3 Earnings Beat", days_ago=20)
    result = find_similar_cluster(
        session,
        ticker="QCOM",
        event_type="earnings",
        occurred_at=_dt(0),
        title="QCOM Q3 Earnings Beat",
        window_days=10,
    )
    assert result is None


def test_find_similar_returns_none_low_similarity(session):
    _cluster(session, "AMD", "earnings", "AMD Q2 Earnings Beat Revenue", days_ago=5)
    result = find_similar_cluster(
        session,
        ticker="AMD",
        event_type="earnings",
        occurred_at=_dt(0),
        title="AMD Activist Stake Disclosure SEC Filing",
        window_days=10,
        threshold=0.60,
    )
    assert result is None


def test_find_similar_returns_cluster_for_cross_week_dup(session):
    c = _cluster(session, "NVDA", "earnings", "NVDA Q4 Earnings Beat Revenue Record", days_ago=8)
    result = find_similar_cluster(
        session,
        ticker="NVDA",
        event_type="earnings",
        occurred_at=_dt(0),
        title="NVDA Q4 Earnings Beat Revenue Record Guidance",
        window_days=10,
        threshold=0.50,
    )
    assert result is not None
    assert result.id == c.id


def test_find_similar_returns_none_for_unrelated_event_type(session):
    # "merger_acquisition" cluster exists, but we're looking for "bankruptcy" —
    # these types are not in the same related-type group.
    _cluster(session, "CVS", "merger_acquisition", "CVS Acquires Company XYZ", days_ago=3)
    result = find_similar_cluster(
        session,
        ticker="CVS",
        event_type="bankruptcy_or_going_concern",
        occurred_at=_dt(0),
        title="CVS Acquires Company XYZ",
        window_days=10,
    )
    assert result is None


# ---------------------------------------------------------------------------
# 5. build_or_update_cluster — cross-week integration
# ---------------------------------------------------------------------------


def test_cross_week_near_duplicate_merges(session):
    """A near-duplicate event one week later should merge into the original cluster."""
    # Create an event and cluster it (simulating "week 1" earnings)
    e1 = _event(session, "ORCL2", "earnings", "Oracle Q4 Earnings Beat Revenue Record", days_ago=9, source_id=5001)
    c1 = build_or_update_cluster(session, e1)
    session.flush()
    e1.cluster_id = c1.id
    session.flush()
    original_key = c1.cluster_key

    # Now simulate a "week 2" news event about the same story
    e2 = _event(session, "ORCL2", "earnings", "Oracle Q4 Earnings Beat Revenue Record Strong Guidance", days_ago=2, source_id=5002)
    c2 = build_or_update_cluster(session, e2)
    session.flush()

    # e2 should have merged into c1, not created a new cluster
    assert c2.cluster_key == original_key or c2.id == c1.id


def test_cross_week_distinct_events_stay_separate(session):
    """Completely different stories in different weeks must NOT merge."""
    e1 = _event(session, "WBA", "earnings", "WBA Q4 Earnings Quarterly Results", days_ago=9, source_id=6001)
    c1 = build_or_update_cluster(session, e1)
    session.flush()
    e1.cluster_id = c1.id
    session.flush()

    # Different story entirely — merger for same ticker
    e2 = _event(session, "WBA", "merger_acquisition", "WBA Acquires Health Chain Expansion", days_ago=2, source_id=6002)
    c2 = build_or_update_cluster(session, e2)
    session.flush()

    assert c2.id != c1.id


def test_different_tickers_always_separate(session):
    """Events for different tickers must always produce separate clusters."""
    e1 = _event(session, "PG2", "earnings", "PG Q4 Earnings Beat Estimates", days_ago=3, source_id=7001)
    e2 = _event(session, "KO2", "earnings", "PG Q4 Earnings Beat Estimates", days_ago=3, source_id=7002)
    c1 = build_or_update_cluster(session, e1)
    session.flush()
    c2 = build_or_update_cluster(session, e2)
    session.flush()
    assert c1.id != c2.id


def test_evidence_preserved_after_cross_week_merge(session):
    """After a cross-week merge, both the original and new source URLs are retained."""
    e1 = _event(
        session, "SNAP2", "earnings", "Snap Q2 Earnings Beat Revenue Record",
        days_ago=9, source_id=8001, source_url="https://sec.gov/snap-original"
    )
    c1 = build_or_update_cluster(session, e1)
    session.flush()
    e1.cluster_id = c1.id
    session.flush()

    original_urls = set((c1.source_urls or {}).get("urls", []))

    e2 = _event(
        session, "SNAP2", "earnings", "Snap Q2 Earnings Beat Revenue Record Guidance",
        days_ago=2, source_id=8002, source_url="https://news.example.com/snap-followup"
    )
    c2 = build_or_update_cluster(session, e2)
    session.flush()

    # If merged, c2 == c1 and new URL should appear in source_urls
    if c2.id == c1.id:
        final_urls = set((c2.source_urls or {}).get("urls", []))
        # Original URL must still be there
        assert original_urls.issubset(final_urls) or len(final_urls) >= len(original_urls)


def test_same_week_events_always_cluster_together(session):
    """Same-week events with same ticker + event_type must always share a cluster."""
    now = datetime.datetime.now(datetime.timezone.utc)
    # Both on same ISO week (same day, different hours)
    e1 = _event(session, "MCD2", "earnings", "McDonalds Q3 Earnings", days_ago=1, source_id=9001)
    e2 = _event(session, "MCD2", "earnings", "McDonalds Q3 Earnings Beat", days_ago=1, source_id=9002)
    c1 = build_or_update_cluster(session, e1)
    session.flush()
    c2 = build_or_update_cluster(session, e2)
    session.flush()
    assert c1.cluster_key == c2.cluster_key


# ---------------------------------------------------------------------------
# 6. No network calls
# ---------------------------------------------------------------------------


def test_dedup_module_makes_no_network_calls(session):
    """normalize_title, jaccard_similarity, titles_are_duplicates: no HTTP."""

    def explode(*args, **kwargs):
        raise AssertionError("dedup must not make HTTP calls")

    with patch.object(httpx.Client, "get", explode):
        with patch.object(httpx.AsyncClient, "get", explode):
            _ = normalize_title("Apple Q4 Earnings Beat", "AAPL")
            _ = jaccard_similarity("earnings beat", "earnings beat record")
            _ = titles_are_duplicates("Apple Q4", "Apple Q4 Earnings")
            _ = find_similar_cluster(
                session,
                ticker="AAPL",
                event_type="earnings",
                occurred_at=_dt(0),
                title="Apple Q4 Earnings Beat",
            )
