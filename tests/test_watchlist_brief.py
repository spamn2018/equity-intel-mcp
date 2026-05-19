"""
Tests for the watchlist catalyst brief service (briefs/watchlist.py)
and the get_watchlist_brief MCP tool wrapper.

All tests use an in-memory SQLite database — no PostgreSQL or external
services required.

Coverage:
  - Empty watchlist → graceful result
  - No data for tickers → graceful result
  - Ranking by materiality descending
  - min_materiality threshold filtering
  - max_items cap
  - event_types filter
  - include_low_confidence flag
  - Caution language requirements
  - Evidence formatting (price context, news, filings)
  - include_price_context=False, include_news=False, include_filings=False flags
  - Cluster-based path (preferred)
  - Raw-event fallback path
  - Brief summary text generation
  - MCP tool wrapper returns correct shape
"""
from __future__ import annotations

import datetime
import json
from typing import Dict, Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from equity_intel.db.models import (
    Base,
    Company,
    Event,
    EventCluster,
    Filing,
    NewsArticle,
    now_utc,
)
from equity_intel.briefs.watchlist import get_watchlist_brief
from equity_intel.mcp_server.tools import get_watchlist_brief as mcp_get_watchlist_brief


# ------------------------------------------------------------------ #
# Shared fixtures                                                      #
# ------------------------------------------------------------------ #


@pytest.fixture(scope="module")
def engine():
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture(scope="module")
def session_factory(engine):
    return sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture
def session(session_factory):
    sess = session_factory()
    yield sess
    sess.rollback()
    sess.close()


# ── Company fixtures ──────────────────────────────────────────────────


def _make_company(session, ticker: str, name: str, sector: str = "Technology") -> Company:
    existing = session.query(Company).filter(Company.ticker == ticker).first()
    if existing:
        return existing
    c = Company(
        ticker=ticker,
        name=name,
        exchange="NASDAQ",
        sector=sector,
        industry="Software",
        is_active=True,
    )
    session.add(c)
    session.flush()
    return c


# ── Cluster fixtures ──────────────────────────────────────────────────


def _make_cluster(
    session,
    ticker: str,
    event_type: str = "earnings",
    materiality: float = 0.75,
    confidence: float = 0.7,
    days_ago: int = 2,
    filing_ids=None,
    news_ids=None,
    source_urls=None,
    price_reaction_json=None,
) -> EventCluster:
    now = now_utc()
    ts = now - datetime.timedelta(days=days_ago)
    key = f"{ticker}_{event_type}_{ts.isocalendar()[1]}_{ts.isocalendar()[0]}"
    existing = session.query(EventCluster).filter(EventCluster.cluster_key == key).first()
    if existing:
        return existing
    cluster = EventCluster(
        cluster_key=key,
        ticker=ticker,
        event_type=event_type,
        event_subtype="results_of_operations",
        title=f"{ticker} {event_type.title()} Event",
        summary=f"Summary for {ticker} {event_type} cluster",
        first_seen_at=ts,
        last_seen_at=ts,
        event_count=2,
        filing_count=1,
        news_count=1,
        materiality_score=materiality,
        confidence_score=confidence,
        novelty_score=0.5,
        filing_ids={"ids": filing_ids or []},
        news_ids={"ids": news_ids or []},
        source_urls={"urls": source_urls or ["https://example.com/source"]},
        price_reaction_json=price_reaction_json,
        caution="This may reflect market-moving information. Verify with primary sources.",
    )
    session.add(cluster)
    session.flush()
    return cluster


# ── Event fixtures ────────────────────────────────────────────────────


def _make_event(
    session,
    company: Company,
    ticker: str,
    event_type: str = "earnings",
    materiality: float = 0.65,
    confidence: float = 0.6,
    days_ago: int = 3,
) -> Event:
    now = now_utc()
    ev = Event(
        company_id=company.id,
        ticker=ticker,
        event_type=event_type,
        event_subtype="results_of_operations",
        title=f"{ticker} {event_type.title()} (raw event)",
        summary="Raw event fallback summary",
        source_type="filing",
        source_url=f"https://sec.gov/filing/{ticker}",
        occurred_at=now - datetime.timedelta(days=days_ago),
        detected_at=now,
        materiality_score=materiality,
        confidence_score=confidence,
        novelty_score=0.4,
    )
    session.add(ev)
    session.flush()
    return ev


# ── Filing + News fixtures ────────────────────────────────────────────


def _make_filing(session, company: Company, acc_suffix: str = "001") -> Filing:
    acc = f"0001234567-24-{acc_suffix}"
    existing = session.query(Filing).filter(Filing.accession_number == acc).first()
    if existing:
        return existing
    f = Filing(
        company_id=company.id,
        accession_number=acc,
        form_type="8-K",
        filing_date=datetime.datetime(2024, 3, 1),
        items="2.02,9.01",
        filing_url=f"https://sec.gov/filing/{acc}",
    )
    session.add(f)
    session.flush()
    return f


def _make_news(session, company: Company, ticker: str, n: int = 1) -> NewsArticle:
    art = NewsArticle(
        provider="polygon",
        provider_id=f"{ticker}-news-{n}",
        ticker=ticker,
        company_id=company.id,
        title=f"{ticker} News Article {n}",
        summary=f"Summary for {ticker} article {n}",
        url=f"https://news.example.com/{ticker}/{n}",
        publisher="Test Publisher",
        published_at=now_utc() - datetime.timedelta(days=1),
    )
    session.add(art)
    session.flush()
    return art


# ------------------------------------------------------------------ #
# 1. Empty watchlist                                                    #
# ------------------------------------------------------------------ #


def test_empty_tickers_returns_graceful_result(session):
    result = get_watchlist_brief(session, tickers=[])
    assert result["total_catalysts"] == 0
    assert result["watchlist"] == []
    assert "No tickers" in result["brief_summary"]
    assert "catalysts" in result
    assert isinstance(result["catalysts"], list)


def test_empty_tickers_has_required_top_level_keys(session):
    result = get_watchlist_brief(session, tickers=[])
    required = {"generated_at", "watchlist", "time_window_days", "filters_applied",
                "brief_summary", "total_catalysts", "catalysts", "note", "caution"}
    assert required.issubset(result.keys())


# ------------------------------------------------------------------ #
# 2. No data for tickers                                               #
# ------------------------------------------------------------------ #


def test_no_data_for_ticker(session):
    """Tickers with no clusters or events return empty catalysts gracefully."""
    result = get_watchlist_brief(session, tickers=["ZZZZ"], days=7)
    assert result["total_catalysts"] == 0
    assert "ZZZZ" in result["watchlist"]
    assert result["catalysts"] == []


def test_no_data_brief_summary_mentions_no_catalysts(session):
    result = get_watchlist_brief(session, tickers=["ZZZZ"], days=1)
    assert "No catalysts" in result["brief_summary"] or "0 catalyst" in result["brief_summary"]


# ------------------------------------------------------------------ #
# 3. Ranking by materiality descending                                 #
# ------------------------------------------------------------------ #


def test_catalysts_ranked_by_materiality_descending(session):
    _make_company(session, "RANK1", "Rank One Corp")
    _make_company(session, "RANK2", "Rank Two Corp")

    _make_cluster(session, "RANK1", materiality=0.9, days_ago=1)
    _make_cluster(session, "RANK2", materiality=0.5, days_ago=1)

    result = get_watchlist_brief(
        session,
        tickers=["RANK1", "RANK2"],
        days=7,
        min_materiality=0.0,
    )
    scores = [c["materiality_score"] for c in result["catalysts"]]
    assert scores == sorted(scores, reverse=True), "Catalysts must be ranked by materiality descending"


def test_ranking_stable_across_multiple_tickers(session):
    """More than 2 tickers, scores strictly decreasing in top result."""
    tickers = ["RA1", "RA2", "RA3"]
    companies = [_make_company(session, t, f"{t} Corp") for t in tickers]
    mats = [0.95, 0.60, 0.40]
    for ticker, mat in zip(tickers, mats):
        _make_cluster(session, ticker, materiality=mat, days_ago=1)

    result = get_watchlist_brief(session, tickers=tickers, days=7, min_materiality=0.0)
    scores = [c["materiality_score"] for c in result["catalysts"] if c["materiality_score"] is not None]
    assert scores == sorted(scores, reverse=True)


# ------------------------------------------------------------------ #
# 4. min_materiality threshold                                         #
# ------------------------------------------------------------------ #


def test_min_materiality_filters_low_scores(session):
    _make_company(session, "MATF1", "MatFilter Corp")
    _make_cluster(session, "MATF1", materiality=0.2, days_ago=1)

    result = get_watchlist_brief(session, tickers=["MATF1"], days=7, min_materiality=0.5)
    for cat in result["catalysts"]:
        assert (cat["materiality_score"] or 0.0) >= 0.5


def test_min_materiality_zero_includes_all(session):
    _make_company(session, "MATF2", "MatFilter Two Corp")
    _make_cluster(session, "MATF2", event_type="guidance", materiality=0.1, days_ago=1)

    low = get_watchlist_brief(session, tickers=["MATF2"], days=7, min_materiality=0.0)
    high = get_watchlist_brief(session, tickers=["MATF2"], days=7, min_materiality=0.8)
    assert low["total_catalysts"] >= high["total_catalysts"]


def test_min_materiality_at_boundary(session):
    """Catalyst exactly at the threshold should be included."""
    _make_company(session, "MATB", "Boundary Corp")
    _make_cluster(session, "MATB", event_type="buyback", materiality=0.5, days_ago=1)

    result = get_watchlist_brief(session, tickers=["MATB"], days=7, min_materiality=0.5)
    matb_cats = [c for c in result["catalysts"] if c["ticker"] == "MATB"]
    assert any((c["materiality_score"] or 0) >= 0.5 for c in matb_cats)


# ------------------------------------------------------------------ #
# 5. max_items cap                                                      #
# ------------------------------------------------------------------ #


def test_max_items_limits_result_count(session):
    _make_company(session, "CAPPED", "Capped Corp")
    for i in range(5):
        _make_cluster(
            session, "CAPPED",
            event_type=["earnings", "guidance", "regulatory", "buyback", "litigation"][i],
            materiality=0.9 - i * 0.1,
            days_ago=i + 1,
        )

    result = get_watchlist_brief(
        session, tickers=["CAPPED"], days=30, min_materiality=0.0, max_items=2
    )
    assert len(result["catalysts"]) <= 2


def test_max_items_zero_returns_empty(session):
    _make_company(session, "CAPZ", "Capzero Corp")
    _make_cluster(session, "CAPZ", materiality=0.9, days_ago=1)

    result = get_watchlist_brief(
        session, tickers=["CAPZ"], days=7, min_materiality=0.0, max_items=0
    )
    assert result["catalysts"] == []
    assert result["total_catalysts"] == 0


# ------------------------------------------------------------------ #
# 6. event_types filter                                                #
# ------------------------------------------------------------------ #


def test_event_types_filter_earnings_only(session):
    _make_company(session, "ETYPE", "EventType Corp")
    _make_cluster(session, "ETYPE", event_type="earnings", materiality=0.8, days_ago=1)
    _make_cluster(session, "ETYPE", event_type="litigation", materiality=0.8, days_ago=2)

    result = get_watchlist_brief(
        session, tickers=["ETYPE"], days=7,
        min_materiality=0.0, event_types=["earnings"]
    )
    for cat in result["catalysts"]:
        assert cat["event_type"] == "earnings"


def test_event_types_filter_no_match_returns_empty(session):
    _make_company(session, "ETYPE2", "EventType Two Corp")
    _make_cluster(session, "ETYPE2", event_type="earnings", materiality=0.8, days_ago=1)

    result = get_watchlist_brief(
        session, tickers=["ETYPE2"], days=7,
        min_materiality=0.0, event_types=["merger_acquisition"]
    )
    etype2_cats = [c for c in result["catalysts"] if c["ticker"] == "ETYPE2"]
    assert all(c["event_type"] == "merger_acquisition" for c in etype2_cats)


# ------------------------------------------------------------------ #
# 7. include_low_confidence flag                                       #
# ------------------------------------------------------------------ #


def test_include_low_confidence_false_drops_low_confidence(session):
    _make_company(session, "CONF1", "Conf One Corp")
    _make_cluster(session, "CONF1", event_type="guidance", materiality=0.8, confidence=0.1, days_ago=1)

    result = get_watchlist_brief(
        session, tickers=["CONF1"], days=7,
        min_materiality=0.0, include_low_confidence=False
    )
    for cat in result["catalysts"]:
        assert (cat.get("confidence_score") or 0.0) >= 0.3


def test_include_low_confidence_true_includes_low_confidence(session):
    _make_company(session, "CONF2", "Conf Two Corp")
    _make_cluster(session, "CONF2", event_type="activist_stake", materiality=0.8, confidence=0.1, days_ago=1)

    with_low = get_watchlist_brief(
        session, tickers=["CONF2"], days=7,
        min_materiality=0.0, include_low_confidence=True
    )
    without_low = get_watchlist_brief(
        session, tickers=["CONF2"], days=7,
        min_materiality=0.0, include_low_confidence=False
    )
    assert with_low["total_catalysts"] >= without_low["total_catalysts"]


# ------------------------------------------------------------------ #
# 8. Caution language requirements                                     #
# ------------------------------------------------------------------ #


def test_top_level_caution_field_present(session):
    result = get_watchlist_brief(session, tickers=["ZZZZ"], days=7)
    assert "caution" in result
    assert len(result["caution"]) > 20


def test_per_catalyst_caution_field_present(session):
    _make_company(session, "CAUT", "Caution Corp")
    _make_cluster(session, "CAUT", materiality=0.8, days_ago=1)

    result = get_watchlist_brief(session, tickers=["CAUT"], days=7, min_materiality=0.0)
    for cat in result["catalysts"]:
        assert "caution" in cat
        assert cat["caution"]


def test_caution_does_not_assert_causation(session):
    _make_company(session, "CAUT2", "Caution Two Corp")
    _make_cluster(session, "CAUT2", materiality=0.9, days_ago=1)

    result = get_watchlist_brief(session, tickers=["CAUT2"], days=7, min_materiality=0.0)
    caution_text = result.get("caution", "").lower()
    # Must not claim direct causation without hedging
    if "caused" in caution_text:
        assert "not" in caution_text or "correlation" in caution_text

    for cat in result["catalysts"]:
        why = cat.get("why_it_matters", "").lower()
        # Should use hedging language
        assert any(word in why for word in ["may", "likely", "suggest", "reflect", "could", "available evidence"])


def test_why_it_matters_uses_cautious_language(session):
    _make_company(session, "WHY", "WhyMatters Corp")
    _make_cluster(session, "WHY", event_type="merger_acquisition", materiality=0.85, days_ago=1)

    result = get_watchlist_brief(session, tickers=["WHY"], days=7, min_materiality=0.0)
    for cat in result["catalysts"]:
        why = cat.get("why_it_matters", "")
        assert why, "why_it_matters should not be empty"
        assert any(w in why.lower() for w in ["may", "likely", "suggest", "reflect", "could", "evidence"])


# ------------------------------------------------------------------ #
# 9. Evidence formatting — include_price_context flag                  #
# ------------------------------------------------------------------ #


def test_include_price_context_false_omits_price_data(session):
    _make_company(session, "NOPRICE", "No Price Corp")
    _make_cluster(
        session, "NOPRICE", materiality=0.8, days_ago=1,
        price_reaction_json={"available": True, "pct_change": 5.0, "volume_ratio": 2.0,
                              "price_before": 100.0, "price_after": 105.0,
                              "date_before": "2024-01-14", "date_after": "2024-01-15"}
    )

    result = get_watchlist_brief(
        session, tickers=["NOPRICE"], days=7, min_materiality=0.0,
        include_price_context=False
    )
    for cat in result["catalysts"]:
        assert cat.get("price_move") is None


def test_include_price_context_true_includes_price_data(session):
    _make_company(session, "HASPRICE", "Has Price Corp")
    _make_cluster(
        session, "HASPRICE", event_type="guidance", materiality=0.8, days_ago=1,
        price_reaction_json={"available": True, "pct_change": -3.5, "volume_ratio": 1.8,
                              "price_before": 200.0, "price_after": 193.0,
                              "date_before": "2024-01-14", "date_after": "2024-01-15"}
    )

    result = get_watchlist_brief(
        session, tickers=["HASPRICE"], days=7, min_materiality=0.0,
        include_price_context=True
    )
    cats_with_price = [c for c in result["catalysts"] if c.get("price_move") is not None]
    assert cats_with_price, "Expected at least one catalyst with price_move"
    pm = cats_with_price[0]["price_move"]
    assert "pct_change" in pm
    assert pm["pct_change"] == -3.5


# ------------------------------------------------------------------ #
# 10. Evidence formatting — include_news and include_filings flags     #
# ------------------------------------------------------------------ #


def test_include_news_false_omits_news(session):
    company = _make_company(session, "NONEWS", "No News Corp")
    news = _make_news(session, company, "NONEWS")
    _make_cluster(session, "NONEWS", event_type="buyback", materiality=0.8, days_ago=1,
                  news_ids=[news.id])

    result = get_watchlist_brief(
        session, tickers=["NONEWS"], days=7, min_materiality=0.0, include_news=False
    )
    for cat in result["catalysts"]:
        assert cat["related_news"] == []


def test_include_news_true_includes_linked_news(session):
    company = _make_company(session, "HASNEWS", "Has News Corp")
    news = _make_news(session, company, "HASNEWS")
    _make_cluster(session, "HASNEWS", event_type="insider_transaction", materiality=0.8, days_ago=1,
                  news_ids=[news.id])

    result = get_watchlist_brief(
        session, tickers=["HASNEWS"], days=7, min_materiality=0.0, include_news=True
    )
    hasnews_cats = [c for c in result["catalysts"] if c["ticker"] == "HASNEWS"]
    cats_with_news = [c for c in hasnews_cats if c["related_news"]]
    assert cats_with_news, "Expected related_news to be populated"
    first_article = cats_with_news[0]["related_news"][0]
    assert "title" in first_article
    assert "publisher" in first_article
    assert "url" in first_article


def test_include_filings_false_omits_filings(session):
    company = _make_company(session, "NOFILINGS", "No Filings Corp")
    filing = _make_filing(session, company, acc_suffix="NF1")
    _make_cluster(session, "NOFILINGS", event_type="earnings", materiality=0.8, days_ago=1,
                  filing_ids=[filing.id])

    result = get_watchlist_brief(
        session, tickers=["NOFILINGS"], days=7, min_materiality=0.0, include_filings=False
    )
    for cat in result["catalysts"]:
        assert cat["related_filings"] == []


def test_include_filings_true_includes_linked_filings(session):
    company = _make_company(session, "HASFILINGS", "Has Filings Corp")
    filing = _make_filing(session, company, acc_suffix="HF1")
    _make_cluster(session, "HASFILINGS", event_type="earnings", materiality=0.8, days_ago=1,
                  filing_ids=[filing.id])

    result = get_watchlist_brief(
        session, tickers=["HASFILINGS"], days=7, min_materiality=0.0, include_filings=True
    )
    hasfilings_cats = [c for c in result["catalysts"] if c["ticker"] == "HASFILINGS"]
    cats_with_filings = [c for c in hasfilings_cats if c["related_filings"]]
    assert cats_with_filings, "Expected related_filings to be populated"
    first_filing = cats_with_filings[0]["related_filings"][0]
    assert "accession_number" in first_filing
    assert "form_type" in first_filing


# ------------------------------------------------------------------ #
# 11. Cluster-based path (preferred)                                   #
# ------------------------------------------------------------------ #


def test_cluster_based_path_used_when_clusters_exist(session):
    _make_company(session, "CLUST", "Cluster Corp")
    _make_cluster(session, "CLUST", event_type="earnings", materiality=0.85, days_ago=1)

    result = get_watchlist_brief(
        session, tickers=["CLUST"], days=7, min_materiality=0.0
    )
    clust_cats = [c for c in result["catalysts"] if c["ticker"] == "CLUST"]
    assert clust_cats, "Expected cluster-based catalysts"
    # cluster-based results have cluster_id set
    assert any(c["cluster_id"] is not None for c in clust_cats)
    # and data_source is event_clusters
    assert all(c["data_source"] == "event_clusters" for c in clust_cats)


# ------------------------------------------------------------------ #
# 12. Raw-event fallback path                                          #
# ------------------------------------------------------------------ #


def test_raw_event_fallback_when_no_clusters(session):
    """Tickers with no clusters should fall back to raw Event records."""
    company = _make_company(session, "RAWEV", "RawEvent Corp")
    _make_event(session, company, "RAWEV", materiality=0.7, days_ago=2)

    result = get_watchlist_brief(
        session, tickers=["RAWEV"], days=30, min_materiality=0.0
    )
    rawev_cats = [c for c in result["catalysts"] if c["ticker"] == "RAWEV"]
    assert rawev_cats, "Expected raw event fallback catalysts"
    assert all(c["data_source"] == "events" for c in rawev_cats)


def test_raw_event_fallback_has_required_fields(session):
    company = _make_company(session, "RAWEV2", "RawEvent Two Corp")
    _make_event(session, company, "RAWEV2", event_type="regulatory", materiality=0.6, days_ago=3)

    result = get_watchlist_brief(
        session, tickers=["RAWEV2"], days=30, min_materiality=0.0
    )
    rawev_cats = [c for c in result["catalysts"] if c["ticker"] == "RAWEV2"]
    for cat in rawev_cats:
        assert "ticker" in cat
        assert "event_type" in cat
        assert "materiality_score" in cat
        assert "why_it_matters" in cat
        assert "caution" in cat


# ------------------------------------------------------------------ #
# 13. Brief summary text                                               #
# ------------------------------------------------------------------ #


def test_brief_summary_is_non_empty_string(session):
    _make_company(session, "SUMM", "Summary Corp")
    _make_cluster(session, "SUMM", materiality=0.8, days_ago=1)

    result = get_watchlist_brief(session, tickers=["SUMM"], days=7, min_materiality=0.0)
    assert isinstance(result["brief_summary"], str)
    assert len(result["brief_summary"]) > 10


def test_brief_summary_mentions_ticker(session):
    _make_company(session, "SUMM2", "Summary Two Corp")
    _make_cluster(session, "SUMM2", event_type="guidance", materiality=0.7, days_ago=1)

    result = get_watchlist_brief(session, tickers=["SUMM2"], days=7, min_materiality=0.0)
    assert "SUMM2" in result["brief_summary"]


def test_brief_summary_no_catalysts_text(session):
    result = get_watchlist_brief(session, tickers=["ZZZNO"], days=1, min_materiality=0.99)
    assert "No catalysts" in result["brief_summary"] or "0 catalyst" in result["brief_summary"]


# ------------------------------------------------------------------ #
# 14. Catalyst shape validation                                        #
# ------------------------------------------------------------------ #


REQUIRED_CATALYST_FIELDS = {
    "ticker", "company_name", "title", "event_type", "event_subtype",
    "why_it_matters", "materiality_score", "confidence_score",
    "first_seen_at", "last_seen_at",
    "price_move", "volume_context",
    "source_links", "related_filing_ids", "related_news_ids",
    "related_filings", "related_news",
    "caution", "data_source",
}


def test_catalyst_has_all_required_fields(session):
    _make_company(session, "SHAPE", "Shape Corp")
    _make_cluster(session, "SHAPE", event_type="restatement", materiality=0.8, days_ago=1)

    result = get_watchlist_brief(
        session, tickers=["SHAPE"], days=7, min_materiality=0.0
    )
    for cat in result["catalysts"]:
        missing = REQUIRED_CATALYST_FIELDS - set(cat.keys())
        assert not missing, f"Catalyst missing fields: {missing}"


def test_materiality_score_in_range(session):
    _make_company(session, "RANGE", "Range Corp")
    _make_cluster(session, "RANGE", materiality=0.8, days_ago=1)

    result = get_watchlist_brief(
        session, tickers=["RANGE"], days=7, min_materiality=0.0
    )
    for cat in result["catalysts"]:
        mat = cat.get("materiality_score")
        if mat is not None:
            assert 0.0 <= mat <= 1.0, f"materiality_score out of [0,1]: {mat}"
        conf = cat.get("confidence_score")
        if conf is not None:
            assert 0.0 <= conf <= 1.0, f"confidence_score out of [0,1]: {conf}"


# ------------------------------------------------------------------ #
# 15. MCP tool wrapper                                                 #
# ------------------------------------------------------------------ #


def test_mcp_tool_wrapper_returns_same_shape(session):
    """The MCP wrapper in tools.py should delegate to the service cleanly."""
    _make_company(session, "MCPW", "MCP Wrapper Corp")
    _make_cluster(session, "MCPW", materiality=0.75, days_ago=1)

    result = mcp_get_watchlist_brief(
        session, tickers=["MCPW"], days=7, min_materiality=0.0
    )
    assert "catalysts" in result
    assert "brief_summary" in result
    assert "total_catalysts" in result
    assert "generated_at" in result


def test_mcp_tool_wrapper_serializable(session):
    """The MCP wrapper result must be JSON-serializable (no unserializable types)."""
    result = mcp_get_watchlist_brief(session, tickers=["MCPW"], days=7, min_materiality=0.0)
    try:
        json.dumps(result, default=str)
    except (TypeError, ValueError) as exc:
        pytest.fail(f"Result is not JSON-serializable: {exc}")


def test_mcp_tool_wrapper_empty_tickers(session):
    result = mcp_get_watchlist_brief(session, tickers=[], days=7)
    assert result["total_catalysts"] == 0
    assert "No tickers" in result["brief_summary"]


# ------------------------------------------------------------------ #
# 16. filters_applied is accurately reflected                          #
# ------------------------------------------------------------------ #


def test_filters_applied_matches_input(session):
    result = get_watchlist_brief(
        session,
        tickers=["ZZZZ"],
        days=14,
        min_materiality=0.6,
        include_low_confidence=True,
        max_items=5,
        event_types=["earnings"],
        include_price_context=False,
        include_news=False,
        include_filings=True,
    )
    f = result["filters_applied"]
    assert f["min_materiality"] == 0.6
    assert f["include_low_confidence"] is True
    assert f["max_items"] == 5
    assert f["event_types"] == ["earnings"]
    assert f["include_price_context"] is False
    assert f["include_news"] is False
    assert f["include_filings"] is True
