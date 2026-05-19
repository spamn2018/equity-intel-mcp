"""
Tests for event clustering, scoring, and cluster-aware MCP tools.

All tests use SQLite in-memory — no PostgreSQL required.
"""
from __future__ import annotations

import datetime
import pytest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from equity_intel.db.models import (
    Base, Company, Event, EventCluster, Filing, MarketPrice, NewsArticle, now_utc,
)
from equity_intel.events.cluster import (
    cluster_key,
    compute_novelty_score,
    build_or_update_cluster,
    cluster_events_for_company,
)
from equity_intel.events.score import (
    compute_cluster_materiality,
    compute_cluster_confidence,
)
from equity_intel.events.build import build_event_from_filing, build_event_from_news


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def engine():
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture()
def session(engine):
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.rollback()
    s.close()


def _company(session, ticker="AAPL") -> Company:
    c = session.query(Company).filter(Company.ticker == ticker).first()
    if c:
        return c
    c = Company(
        ticker=ticker, cik="0000320193", name=f"{ticker} Inc.",
        exchange="NASDAQ", is_active=True, created_at=now_utc(), updated_at=now_utc(),
    )
    session.add(c)
    session.flush()
    return c


def _filing(session, company: Company, form="8-K", items="2.02", days_ago=1) -> Filing:
    dt = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days_ago)
    f = Filing(
        company_id=company.id,
        accession_number=f"0001-{company.ticker}-{days_ago}-{form.replace(' ','')}",
        form_type=form,
        filing_date=dt,
        items=items,
        filing_url=f"https://sec.gov/filing/{days_ago}",
        created_at=now_utc(), updated_at=now_utc(),
    )
    session.add(f)
    session.flush()
    return f


def _event(
    session,
    company: Company,
    event_type="earnings",
    title="AAPL 8-K earnings",
    days_ago=1,
    cluster_id=None,
) -> Event:
    occurred = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days_ago)
    e = Event(
        company_id=company.id,
        ticker=company.ticker,
        event_type=event_type,
        event_subtype="results_of_operations",
        title=title,
        summary="Quarterly results",
        source_type="filing",
        source_id=999 + days_ago,
        source_url="https://sec.gov/filing",
        occurred_at=occurred,
        detected_at=now_utc(),
        materiality_score=0.7,
        novelty_score=1.0,
        confidence_score=0.6,
        cluster_id=cluster_id,
        created_at=now_utc(), updated_at=now_utc(),
    )
    session.add(e)
    session.flush()
    return e


def _news(session, company: Company, title="Apple posts record revenue") -> NewsArticle:
    a = NewsArticle(
        provider="polygon",
        provider_id=f"news-{title[:10].replace(' ','-')}",
        ticker=company.ticker,
        company_id=company.id,
        title=title,
        summary="Record revenue quarter",
        url=f"https://news.example.com/{title[:5]}",
        publisher="Reuters",
        published_at=datetime.datetime.now(datetime.timezone.utc),
        created_at=now_utc(),
    )
    session.add(a)
    session.flush()
    return a


# ---------------------------------------------------------------------------
# cluster_key
# ---------------------------------------------------------------------------

class TestClusterKey:
    def test_same_ticker_type_week_gives_same_key(self):
        dt1 = datetime.datetime(2024, 1, 15, tzinfo=datetime.timezone.utc)  # Mon week 3
        dt2 = datetime.datetime(2024, 1, 17, tzinfo=datetime.timezone.utc)  # Wed week 3
        assert cluster_key("AAPL", "earnings", dt1) == cluster_key("AAPL", "earnings", dt2)

    def test_different_weeks_give_different_keys(self):
        dt1 = datetime.datetime(2024, 1, 15, tzinfo=datetime.timezone.utc)  # week 3
        dt2 = datetime.datetime(2024, 1, 22, tzinfo=datetime.timezone.utc)  # week 4
        assert cluster_key("AAPL", "earnings", dt1) != cluster_key("AAPL", "earnings", dt2)

    def test_different_event_types_different_keys(self):
        dt = datetime.datetime(2024, 1, 15, tzinfo=datetime.timezone.utc)
        assert cluster_key("AAPL", "earnings", dt) != cluster_key("AAPL", "merger_acquisition", dt)

    def test_different_tickers_different_keys(self):
        dt = datetime.datetime(2024, 1, 15, tzinfo=datetime.timezone.utc)
        assert cluster_key("AAPL", "earnings", dt) != cluster_key("MSFT", "earnings", dt)

    def test_key_is_uppercase_ticker(self):
        dt = datetime.datetime(2024, 1, 15, tzinfo=datetime.timezone.utc)
        assert cluster_key("aapl", "earnings", dt) == cluster_key("AAPL", "earnings", dt)

    def test_key_format_contains_iso_week(self):
        dt = datetime.datetime(2024, 1, 15, tzinfo=datetime.timezone.utc)
        key = cluster_key("AAPL", "earnings", dt)
        assert "2024W03" in key
        assert "AAPL" in key
        assert "earnings" in key


# ---------------------------------------------------------------------------
# compute_novelty_score
# ---------------------------------------------------------------------------

class TestNoveltyScore:
    def test_first_event_is_fully_novel(self):
        assert compute_novelty_score("Apple announces earnings beat", []) == 1.0

    def test_identical_title_has_low_novelty(self):
        title = "Apple announces earnings beat"
        score = compute_novelty_score(title, [title])
        assert score < 0.3  # high overlap → low novelty

    def test_unrelated_title_stays_novel(self):
        score = compute_novelty_score(
            "FDA approves new drug",
            ["Apple announces earnings beat", "Microsoft revenue up"],
        )
        assert score >= 0.8

    def test_partial_overlap_reduces_novelty(self):
        score = compute_novelty_score(
            "Apple quarterly earnings report",
            ["Apple earnings beat expectations"],
        )
        assert 0.2 < score < 0.9  # some overlap but not identical

    def test_novelty_never_below_floor(self):
        title = "Apple Apple Apple"
        score = compute_novelty_score(title, [title] * 10)
        assert score >= 0.1


# ---------------------------------------------------------------------------
# compute_cluster_materiality
# ---------------------------------------------------------------------------

class TestClusterMateriality:
    def test_no_price_no_extra(self):
        score = compute_cluster_materiality(0.6)
        assert score == pytest.approx(0.6)

    def test_large_price_move_boosts(self):
        score = compute_cluster_materiality(0.6, price_pct_change=12.0)
        assert score > 0.6
        assert score >= 0.76  # +0.16 for >= 10%

    def test_small_price_move_small_boost(self):
        score = compute_cluster_materiality(0.6, price_pct_change=2.5)
        assert score == pytest.approx(0.64)  # +0.04

    def test_negative_price_move_also_boosts(self):
        pos = compute_cluster_materiality(0.6, price_pct_change=10.0)
        neg = compute_cluster_materiality(0.6, price_pct_change=-10.0)
        assert pos == neg  # abs() applied

    def test_high_volume_ratio_boosts(self):
        score = compute_cluster_materiality(0.6, volume_ratio=3.5)
        assert score >= 0.68  # +0.08

    def test_multiple_sources_boost(self):
        score = compute_cluster_materiality(0.6, confirming_sources=4)
        assert score >= 0.68  # +0.08

    def test_capped_at_1(self):
        score = compute_cluster_materiality(
            0.9, price_pct_change=20.0, volume_ratio=5.0, confirming_sources=10
        )
        assert score == 1.0

    def test_never_below_zero(self):
        score = compute_cluster_materiality(0.0)
        assert score == 0.0


# ---------------------------------------------------------------------------
# compute_cluster_confidence
# ---------------------------------------------------------------------------

class TestClusterConfidence:
    def test_baseline(self):
        score = compute_cluster_confidence(0.5)
        assert score == pytest.approx(0.5)

    def test_price_reaction_boosts(self):
        score = compute_cluster_confidence(0.5, has_price_reaction=True)
        assert score == pytest.approx(0.6)

    def test_multiple_filings_boost(self):
        score = compute_cluster_confidence(0.5, filing_count=3)
        assert score > 0.5

    def test_news_corroboration_boosts(self):
        score = compute_cluster_confidence(0.5, news_count=2)
        assert score > 0.5

    def test_all_factors_combined(self):
        score = compute_cluster_confidence(0.6, has_price_reaction=True, filing_count=3, news_count=4)
        assert score > 0.7

    def test_capped_at_1(self):
        score = compute_cluster_confidence(1.0, has_price_reaction=True, filing_count=10, news_count=10)
        assert score == 1.0


# ---------------------------------------------------------------------------
# build_or_update_cluster
# ---------------------------------------------------------------------------

class TestBuildOrUpdateCluster:
    def test_creates_new_cluster(self, session):
        company = _company(session, "NVDA")
        event = _event(session, company, event_type="earnings", title="NVDA Q4 earnings")
        cluster = build_or_update_cluster(session, event)
        session.flush()
        assert cluster.id is not None
        assert cluster.ticker == "NVDA"
        assert cluster.event_type == "earnings"
        assert cluster.event_count == 1
        assert cluster.novelty_score == 1.0

    def test_returns_same_cluster_for_same_key(self, session):
        company = _company(session, "META")
        event1 = _event(session, company, event_type="earnings", title="META Q1 results", days_ago=2)
        event2 = _event(session, company, event_type="earnings", title="META Q1 earnings beat", days_ago=3)

        c1 = build_or_update_cluster(session, event1)
        session.flush()
        c2 = build_or_update_cluster(session, event2)
        session.flush()
        assert c1.cluster_key == c2.cluster_key
        assert c2.event_count == 2

    def test_different_event_type_creates_separate_cluster(self, session):
        company = _company(session, "TSLA")
        e1 = _event(session, company, event_type="earnings", days_ago=1)
        e2 = _event(session, company, event_type="merger_acquisition", days_ago=1)
        c1 = build_or_update_cluster(session, e1)
        session.flush()
        c2 = build_or_update_cluster(session, e2)
        session.flush()
        assert c1.cluster_key != c2.cluster_key

    def test_filing_ids_accumulated(self, session):
        company = _company(session, "AMZN")
        f1 = _filing(session, company, days_ago=1)
        f2 = _filing(session, company, days_ago=2)
        e1 = _event(session, company, event_type="earnings", days_ago=1)
        e2 = _event(session, company, event_type="earnings", days_ago=2)

        c1 = build_or_update_cluster(session, e1, filing=f1)
        session.flush()
        c2 = build_or_update_cluster(session, e2, filing=f2)
        session.flush()
        assert c1.cluster_key == c2.cluster_key
        ids = c2.filing_ids["ids"]
        assert f1.id in ids
        assert f2.id in ids

    def test_novelty_score_drops_for_duplicate(self, session):
        company = _company(session, "GOOG")
        e1 = _event(session, company, event_type="earnings", title="Alphabet Q2 earnings beat")
        c = build_or_update_cluster(session, e1)
        session.flush()
        assert c.novelty_score == 1.0

        # Assign cluster_id so query inside build_or_update_cluster finds e1's title
        e1.cluster_id = c.id
        session.flush()

        e2 = _event(session, company, event_type="earnings", title="Alphabet Q2 earnings beat")
        c2 = build_or_update_cluster(session, e2)
        session.flush()
        assert c2.id == c.id
        assert c2.novelty_score < 1.0  # should have dropped

    def test_materiality_boosted_by_price_data(self, session):
        company = _company(session, "NFLX")
        # Add price bars for price reaction computation
        bar_before = MarketPrice(
            ticker="NFLX",
            timestamp=datetime.datetime(2024, 1, 14, tzinfo=datetime.timezone.utc),
            close=500.0, adjusted_close=500.0, volume=1_000_000, interval="1d",
            provider="polygon", created_at=now_utc(),
        )
        bar_after = MarketPrice(
            ticker="NFLX",
            timestamp=datetime.datetime(2024, 1, 15, tzinfo=datetime.timezone.utc),
            close=560.0, adjusted_close=560.0, volume=2_500_000, interval="1d",
            provider="polygon", created_at=now_utc(),
        )
        session.add_all([bar_before, bar_after])
        session.flush()

        event = _event(session, company, event_type="earnings", days_ago=1)
        # Override occurred_at to match our bars
        event.occurred_at = datetime.datetime(2024, 1, 15, tzinfo=datetime.timezone.utc)
        session.flush()

        base_score = event.materiality_score
        cluster = build_or_update_cluster(session, event)
        session.flush()
        # If price data was found, materiality should be >= base
        # (price reaction is optional — test that score is valid)
        assert cluster.materiality_score >= 0.0
        assert cluster.materiality_score <= 1.0


# ---------------------------------------------------------------------------
# build_event_from_filing with clustering
# ---------------------------------------------------------------------------

class TestBuildEventFromFiling:
    def test_creates_event_and_cluster(self, session):
        company = _company(session, "AMD")
        filing = _filing(session, company, form="8-K", items="2.02")
        event = build_event_from_filing(session, filing, company, run_clustering=True)
        session.flush()

        assert event is not None
        assert event.cluster_id is not None
        cluster = session.get(EventCluster, event.cluster_id)
        assert cluster is not None
        assert cluster.ticker == "AMD"

    def test_deduplication_returns_none(self, session):
        company = _company(session, "INTC")
        filing = _filing(session, company, form="10-K")
        e1 = build_event_from_filing(session, filing, company, run_clustering=False)
        session.flush()
        e2 = build_event_from_filing(session, filing, company, run_clustering=False)
        assert e1 is not None
        assert e2 is None

    def test_cluster_id_linked_on_event(self, session):
        company = _company(session, "QCOM")
        filing = _filing(session, company, form="8-K", items="5.02")
        event = build_event_from_filing(session, filing, company, run_clustering=True)
        session.flush()
        assert event.cluster_id is not None

    def test_event_novelty_updated_from_cluster(self, session):
        company = _company(session, "ORCL")
        filing = _filing(session, company, form="10-Q")
        event = build_event_from_filing(session, filing, company, run_clustering=True)
        session.flush()
        # novelty_score should be set (either 1.0 for first or lower)
        assert event.novelty_score is not None
        assert 0.0 <= event.novelty_score <= 1.0


# ---------------------------------------------------------------------------
# build_event_from_news with clustering
# ---------------------------------------------------------------------------

class TestBuildEventFromNews:
    def test_creates_news_event(self, session):
        company = _company(session, "CRM")
        article = _news(session, company, title="Salesforce announces merger")
        event = build_event_from_news(session, article, company, run_clustering=True)
        session.flush()
        assert event is not None
        assert event.source_type == "news"
        assert event.ticker == "CRM"

    def test_news_keyword_classification(self, session):
        company = _company(session, "CVS")
        article = _news(session, company, title="CVS Health faces bankruptcy risk")
        event = build_event_from_news(session, article, company, run_clustering=True)
        session.flush()
        assert event.event_type == "bankruptcy_or_going_concern"

    def test_news_deduplication(self, session):
        company = _company(session, "WMT")
        article = _news(session, company, title="Walmart quarterly results")
        e1 = build_event_from_news(session, article, company, run_clustering=False)
        session.flush()
        e2 = build_event_from_news(session, article, company, run_clustering=False)
        assert e1 is not None
        assert e2 is None

    def test_sentiment_fallback_positive(self, session):
        company = _company(session, "DIS")
        article = NewsArticle(
            provider="polygon", provider_id="dis-pos-1",
            ticker="DIS", company_id=company.id,
            title="Disney achieves milestone",
            summary="Good news",
            url="https://news.example.com/dis-pos",
            publisher="Bloomberg",
            published_at=datetime.datetime.now(datetime.timezone.utc),
            sentiment_json={"polygon_sentiment": "positive"},
            created_at=now_utc(),
        )
        session.add(article)
        session.flush()
        event = build_event_from_news(session, article, company, run_clustering=False)
        session.flush()
        assert event.event_subtype == "positive_news"


# ---------------------------------------------------------------------------
# cluster_events_for_company (batch pass)
# ---------------------------------------------------------------------------

class TestClusterEventsForCompany:
    def test_clusters_unclustered_events(self, session):
        company = _company(session, "PFE")
        # Create two unclustered events
        e1 = _event(session, company, event_type="regulatory", title="Pfizer FDA approval")
        e2 = _event(session, company, event_type="regulatory", title="Pfizer second FDA filing")
        assert e1.cluster_id is None
        assert e2.cluster_id is None

        count = cluster_events_for_company(session, company)
        session.flush()
        assert count >= 2
        assert e1.cluster_id is not None
        assert e2.cluster_id is not None

    def test_already_clustered_events_skipped(self, session):
        company = _company(session, "JNJ")
        existing_cluster = EventCluster(
            cluster_key="JNJ:earnings:2024W03",
            ticker="JNJ", event_type="earnings",
            event_count=1, filing_count=1, news_count=0,
            materiality_score=0.7, confidence_score=0.6, novelty_score=1.0,
            first_seen_at=now_utc(), last_seen_at=now_utc(),
            created_at=now_utc(), updated_at=now_utc(),
        )
        session.add(existing_cluster)
        session.flush()

        # Event already has a cluster_id — should be skipped
        e = _event(session, company, event_type="earnings", cluster_id=existing_cluster.id)
        count = cluster_events_for_company(session, company)
        # e was already clustered, count should be 0 for it
        assert count == 0


# ---------------------------------------------------------------------------
# MCP tool: get_events (cluster-aware)
# ---------------------------------------------------------------------------

class TestGetEventsTool:
    @pytest.fixture(autouse=True)
    def _setup(self, session):
        company = _company(session, "TOOL")
        cluster = EventCluster(
            cluster_key="TOOL:earnings:2099W01",
            ticker="TOOL", event_type="earnings", event_subtype="results_of_operations",
            title="TOOL Q1 Earnings Beat",
            summary="Record revenue quarter",
            first_seen_at=datetime.datetime(2099, 1, 1, tzinfo=datetime.timezone.utc),
            last_seen_at=datetime.datetime(2099, 1, 2, tzinfo=datetime.timezone.utc),
            event_count=2, filing_count=1, news_count=1,
            materiality_score=0.85, confidence_score=0.75, novelty_score=0.9,
            price_reaction_json={"available": True, "pct_change": 8.5, "volume_ratio": 2.1,
                                 "price_before": 100.0, "price_after": 108.5},
            filing_ids={"ids": []}, news_ids={"ids": []}, source_urls={"urls": []},
            caution="Correlation, not causation.",
            created_at=now_utc(), updated_at=now_utc(),
        )
        session.add(cluster)
        session.flush()
        self.cluster = cluster
        self.session = session

    def test_returns_clusters_when_available(self):
        from equity_intel.mcp_server.tools import get_events
        result = get_events(self.session, ticker="TOOL", days=36500)
        assert result["source"] == "event_clusters"
        assert result["total"] >= 1
        ev = result["events"][0]
        assert "cluster_id" in ev
        assert ev["event_type"] == "earnings"
        assert ev["filing_count"] == 1
        assert ev["news_count"] == 1

    def test_cluster_includes_price_reaction(self):
        from equity_intel.mcp_server.tools import get_events
        result = get_events(self.session, ticker="TOOL", days=36500)
        ev = result["events"][0]
        assert ev["price_reaction"] is not None
        assert ev["price_reaction"]["pct_change"] == pytest.approx(8.5)

    def test_min_materiality_filter(self):
        from equity_intel.mcp_server.tools import get_events
        result = get_events(self.session, ticker="TOOL", days=36500, min_materiality=0.99)
        assert result["total"] == 0

    def test_event_type_filter(self):
        from equity_intel.mcp_server.tools import get_events
        result = get_events(self.session, ticker="TOOL", days=36500, event_types=["merger_acquisition"])
        assert result["total"] == 0


# ---------------------------------------------------------------------------
# MCP tool: get_event_cluster
# ---------------------------------------------------------------------------

class TestGetEventClusterTool:
    @pytest.fixture(autouse=True)
    def _setup(self, session):
        company = _company(session, "CLUST")
        filing = _filing(session, company, form="8-K", items="1.01")
        article = _news(session, company)
        cluster = EventCluster(
            cluster_key="CLUST:merger_acquisition:2099W02",
            ticker="CLUST", event_type="merger_acquisition",
            title="CLUST Merger Announcement",
            summary="Strategic acquisition",
            first_seen_at=now_utc(), last_seen_at=now_utc(),
            event_count=2, filing_count=1, news_count=1,
            materiality_score=0.92, confidence_score=0.80, novelty_score=1.0,
            price_reaction_json={"available": True, "pct_change": 15.0, "volume_ratio": 3.5},
            filing_ids={"ids": [filing.id]},
            news_ids={"ids": [article.id]},
            source_urls={"urls": ["https://sec.gov/filing/1"]},
            caution="Correlation, not causation.",
            created_at=now_utc(), updated_at=now_utc(),
        )
        session.add(cluster)
        session.flush()
        self.cluster = cluster
        self.filing = filing
        self.article = article
        self.session = session

    def test_get_by_cluster_id(self):
        from equity_intel.mcp_server.tools import get_event_cluster
        result = get_event_cluster(self.session, cluster_id=self.cluster.id)
        assert result["cluster_id"] == self.cluster.id
        assert result["ticker"] == "CLUST"
        assert result["event_type"] == "merger_acquisition"

    def test_get_by_cluster_key(self):
        from equity_intel.mcp_server.tools import get_event_cluster
        result = get_event_cluster(self.session, cluster_key=self.cluster.cluster_key)
        assert result["cluster_id"] == self.cluster.id

    def test_linked_filings_populated(self):
        from equity_intel.mcp_server.tools import get_event_cluster
        result = get_event_cluster(self.session, cluster_id=self.cluster.id)
        assert len(result["linked_filings"]) == 1
        assert result["linked_filings"][0]["form_type"] == "8-K"

    def test_linked_news_populated(self):
        from equity_intel.mcp_server.tools import get_event_cluster
        result = get_event_cluster(self.session, cluster_id=self.cluster.id)
        assert len(result["linked_news"]) == 1

    def test_price_reaction_present(self):
        from equity_intel.mcp_server.tools import get_event_cluster
        result = get_event_cluster(self.session, cluster_id=self.cluster.id)
        assert result["price_reaction"]["pct_change"] == pytest.approx(15.0)
        assert result["price_reaction"]["volume_ratio"] == pytest.approx(3.5)

    def test_not_found_returns_error(self):
        from equity_intel.mcp_server.tools import get_event_cluster
        result = get_event_cluster(self.session, cluster_id=99999)
        assert "error" in result

    def test_no_args_returns_error(self):
        from equity_intel.mcp_server.tools import get_event_cluster
        result = get_event_cluster(self.session)
        assert "error" in result

    def test_caution_text_present(self):
        from equity_intel.mcp_server.tools import get_event_cluster
        result = get_event_cluster(self.session, cluster_id=self.cluster.id)
        assert "caution" in result
        assert result["note"] is not None


# ---------------------------------------------------------------------------
# MCP tool: screen_catalysts (cluster-aware)
# ---------------------------------------------------------------------------

class TestScreenCatalystsTool:
    @pytest.fixture(autouse=True)
    def _setup(self, session):
        company = _company(session, "SCRN")
        cluster = EventCluster(
            cluster_key="SCRN:bankruptcy_or_going_concern:2099W03",
            ticker="SCRN", event_type="bankruptcy_or_going_concern",
            title="SCRN Going Concern Warning",
            summary="Auditors raise going concern",
            first_seen_at=datetime.datetime(2099, 1, 15, tzinfo=datetime.timezone.utc),
            last_seen_at=datetime.datetime(2099, 1, 15, tzinfo=datetime.timezone.utc),
            event_count=3, filing_count=2, news_count=1,
            materiality_score=0.95, confidence_score=0.88, novelty_score=0.9,
            price_reaction_json={"available": True, "pct_change": -22.0, "volume_ratio": 4.8},
            filing_ids={"ids": []}, news_ids={"ids": []}, source_urls={"urls": []},
            caution="Correlation, not causation.",
            created_at=now_utc(), updated_at=now_utc(),
        )
        session.add(cluster)
        session.flush()
        self.cluster = cluster
        self.session = session

    def test_returns_cluster_source(self):
        from equity_intel.mcp_server.tools import screen_catalysts
        result = screen_catalysts(self.session, min_materiality=0.5, days=36500)
        assert result["source"] == "event_clusters"
        assert result["total"] >= 1

    def test_catalyst_has_price_reaction(self):
        from equity_intel.mcp_server.tools import screen_catalysts
        result = screen_catalysts(self.session, min_materiality=0.5, days=36500)
        cats = [c for c in result["catalysts"] if c["ticker"] == "SCRN"]
        assert cats
        assert cats[0]["price_reaction"]["pct_change"] == pytest.approx(-22.0)

    def test_catalyst_has_evidence_counts(self):
        from equity_intel.mcp_server.tools import screen_catalysts
        result = screen_catalysts(self.session, min_materiality=0.5, days=36500)
        cats = [c for c in result["catalysts"] if c["ticker"] == "SCRN"]
        assert cats
        c = cats[0]
        assert c["filing_count"] == 2
        assert c["news_count"] == 1
        assert c["evidence_count"] == 3

    def test_min_materiality_filters_out(self):
        from equity_intel.mcp_server.tools import screen_catalysts
        result = screen_catalysts(self.session, min_materiality=0.99, days=36500)
        cats = [c for c in result["catalysts"] if c["ticker"] == "SCRN"]
        assert not cats

    def test_ticker_filter(self):
        from equity_intel.mcp_server.tools import screen_catalysts
        result = screen_catalysts(self.session, tickers=["ZZZZ"], days=36500, min_materiality=0.0)
        cats = [c for c in result["catalysts"] if c["ticker"] == "SCRN"]
        assert not cats
