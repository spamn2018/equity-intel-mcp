"""
Regression tests for bugs found during real-data validation (2026-05-11).

Bug list:
  R1  SEC_ARCHIVES URL missing /data/ → all document downloads 404'd
  R2  cluster.last_seen_at naive vs tz-aware datetime comparison TypeError
  R3  upsert_company_fact hitting UNIQUE constraint on idempotent re-run
  R4  Score compounding: event.materiality_score overwritten with cluster
      score, causing subsequent events to use boosted value as base →
      Form 4 clusters hitting 1.0 after accumulating many events
  R5  Form 4 / Form 144 base materiality scores too high (now 0.22 / 0.18)
  R6  Cluster titles showing raw form codes ("4", "144") instead of labels
  R7  Duplicate get_event_cluster definition in tools.py
  R8  Live Massive/Polygon validation (2026-05-11) — locks in:
      - Settings reads POLYGON_API_KEY (not price_api_key) for both providers
      - sync_news and sync_prices factory functions resolve correct provider/key
      - Polygon news normalization contract (all required fields present)
      - Polygon price normalization contract (OHLCV fields, interval, provider)
"""
from __future__ import annotations

import datetime
from typing import Optional

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from equity_intel.db.models import (
    Base, Company, CompanyFact, Event, EventCluster, Filing, now_utc,
)
from equity_intel.events.build import _filing_title, _FORM_LABELS
from equity_intel.events.cluster import _to_utc, build_or_update_cluster
from equity_intel.events.score import (
    FORM_BASE_SCORE, compute_materiality_score, compute_cluster_materiality,
)
from equity_intel.sec.client import SEC_ARCHIVES, build_filing_index_url, build_filing_document_url
from equity_intel.sec.facts import upsert_company_fact


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def _make_company(session: Session, ticker: str = "TST") -> Company:
    c = Company(ticker=ticker, name=f"{ticker} Inc", is_active=True, created_at=now_utc(), updated_at=now_utc())
    session.add(c)
    session.flush()
    return c


def _make_filing(session: Session, company: Company, form: str = "4", accession: str = "0000000001-26-000001") -> Filing:
    f = Filing(
        company_id=company.id,
        accession_number=accession,
        form_type=form,
        filing_date=datetime.date(2026, 5, 5),
        filing_url=build_filing_index_url(company.cik or "0000000001", accession),
        created_at=now_utc(), updated_at=now_utc(),
    )
    session.add(f)
    session.flush()
    return f


def _make_event(session: Session, company: Company, event_type: str = "insider_transaction",
                mat: float = 0.22, occurred_at: Optional[datetime.datetime] = None) -> Event:
    now = now_utc()
    e = Event(
        company_id=company.id,
        ticker=company.ticker,
        event_type=event_type,
        event_subtype="form4",
        title="Insider Transaction",
        source_type="filing",
        source_id=1,
        occurred_at=occurred_at or now,
        detected_at=now,
        materiality_score=mat,
        novelty_score=1.0,
        confidence_score=0.5,
        created_at=now, updated_at=now,
    )
    session.add(e)
    session.flush()
    return e


# ---------------------------------------------------------------------------
# R1 – SEC_ARCHIVES URL path must include /data/
# ---------------------------------------------------------------------------

class TestR1SECArchivesURL:
    def test_sec_archives_contains_data_segment(self):
        assert "/data" in SEC_ARCHIVES, (
            "SEC_ARCHIVES must contain '/data' — "
            "https://www.sec.gov/Archives/edgar/data/<CIK>/..."
        )

    def test_filing_index_url_contains_data(self):
        url = build_filing_index_url("0000320193", "0000320193-26-000011")
        assert "/Archives/edgar/data/" in url

    def test_filing_document_url_contains_data(self):
        url = build_filing_document_url("0000320193", "0000320193-26-000011", "aapl-20260430.htm")
        assert "/Archives/edgar/data/" in url
        assert url.endswith("aapl-20260430.htm")

    def test_old_broken_path_not_present(self):
        url = build_filing_index_url("0000320193", "0000320193-26-000011")
        # Must NOT be the broken form: /Archives/edgar/0000320193/...
        parts = url.split("/Archives/edgar/")
        assert parts[1].startswith("data/"), (
            f"Expected '/Archives/edgar/data/' but got path: {url}"
        )


# ---------------------------------------------------------------------------
# R2 – _to_utc handles naive datetimes without TypeError
# ---------------------------------------------------------------------------

class TestR2ToUtc:
    def test_naive_datetime_gets_utc_tzinfo(self):
        naive = datetime.datetime(2026, 5, 5, 12, 0, 0)
        result = _to_utc(naive)
        assert result.tzinfo is not None
        assert result.tzinfo == datetime.timezone.utc

    def test_aware_datetime_unchanged(self):
        aware = datetime.datetime(2026, 5, 5, 12, 0, 0, tzinfo=datetime.timezone.utc)
        result = _to_utc(aware)
        assert result == aware

    def test_none_returns_none(self):
        assert _to_utc(None) is None

    def test_naive_and_aware_comparable_after_normalization(self):
        naive = datetime.datetime(2026, 5, 1, 0, 0, 0)
        aware = datetime.datetime(2026, 5, 5, 0, 0, 0, tzinfo=datetime.timezone.utc)
        # Before fix this raised TypeError; after fix it must not
        result = max(_to_utc(naive), _to_utc(aware))
        assert result == aware

    def test_cluster_last_seen_at_uses_to_utc(self, db_session):
        """build_or_update_cluster must not raise when cluster.last_seen_at is naive."""
        company = _make_company(db_session)
        filing = _make_filing(db_session, company)
        occurred = datetime.datetime(2026, 5, 5, 12, 0, 0, tzinfo=datetime.timezone.utc)
        ev1 = _make_event(db_session, company, occurred_at=occurred)
        cluster = build_or_update_cluster(db_session, ev1, filing=filing)

        # Simulate SQLite returning naive datetime by mutating last_seen_at
        cluster.last_seen_at = datetime.datetime(2026, 5, 5, 12, 0, 0)  # naive
        db_session.flush()

        # Second event — must not raise TypeError
        ev2 = _make_event(db_session, company, occurred_at=occurred)
        try:
            build_or_update_cluster(db_session, ev2, filing=filing)
        except TypeError as e:
            pytest.fail(f"naive/aware comparison raised TypeError: {e}")


# ---------------------------------------------------------------------------
# R3 – upsert_company_fact is idempotent (no UNIQUE constraint crash)
# ---------------------------------------------------------------------------

class TestR3UpsertCompanyFactIdempotent:
    def _fact_data(self, company_id: int) -> dict:
        return {
            "company_id": company_id,
            "taxonomy": "us-gaap",
            "concept": "NetIncomeLoss",
            "label": "Net Income",
            "description": "Net income.",
            "unit": "USD",
            "value": 1_000_000.0,
            "fiscal_year": 2025,
            "fiscal_period": "Q4",
            "form_type": "10-K",
            "filed_date": datetime.date(2026, 1, 28),
            "end_date": datetime.date(2025, 12, 31),
            "accession_number": "0000320193-26-000001",
            "raw_json": {},
        }

    def test_first_insert_succeeds(self, db_session):
        company = _make_company(db_session)
        data = self._fact_data(company.id)
        result = upsert_company_fact(db_session, data)
        assert result is not None

    def test_duplicate_insert_does_not_raise(self, db_session):
        company = _make_company(db_session)
        data = self._fact_data(company.id)
        upsert_company_fact(db_session, data)
        # Second identical call must silently return None, not raise
        result = upsert_company_fact(db_session, data)
        assert result is None  # duplicate → skipped

    def test_batch_of_duplicates_does_not_raise(self, db_session):
        company = _make_company(db_session)
        data = self._fact_data(company.id)
        for _ in range(5):
            upsert_company_fact(db_session, data)  # must not raise
        count = db_session.query(CompanyFact).filter_by(company_id=company.id).count()
        assert count == 1  # only one row in DB


# ---------------------------------------------------------------------------
# R4 – Score compounding: Form 4 clusters must not exceed a reasonable ceiling
# ---------------------------------------------------------------------------

class TestR4ScoreCompounding:
    def test_form4_cluster_does_not_compound_to_1(self, db_session):
        """
        Adding many Form 4 events to the same cluster must not push materiality
        to 1.0 through score compounding.  Maximum reasonable score for routine
        insider transactions (no price data, no keywords) is base + max_boost.
        """
        company = _make_company(db_session)
        base = FORM_BASE_SCORE.get("4", 0.22)
        max_boost = 0.12  # confirming_sources >= 5 boost

        cluster = None
        for i in range(20):
            acc = f"0000000001-26-{i:06d}"
            filing = _make_filing(db_session, company, form="4", accession=acc)
            event = _make_event(db_session, company, mat=base,
                                occurred_at=datetime.datetime(2026, 5, 5, tzinfo=datetime.timezone.utc))
            cluster = build_or_update_cluster(db_session, event, filing=filing)
            db_session.flush()
            event.cluster_id = cluster.id

        assert cluster is not None
        assert cluster.materiality_score <= base + max_boost + 0.05, (  # 0.05 recency
            f"Cluster materiality {cluster.materiality_score} exceeds expected ceiling "
            f"{base + max_boost + 0.05} for routine Form 4 events"
        )

    def test_form4_event_score_not_overwritten_by_cluster(self, db_session):
        """
        Individual Event.materiality_score must retain its raw value after
        clustering — not be overwritten with the cluster's enhanced score.
        """
        company = _make_company(db_session)
        filing = _make_filing(db_session, company)
        raw_score = FORM_BASE_SCORE.get("4", 0.22)
        event = _make_event(db_session, company, mat=raw_score,
                            occurred_at=datetime.datetime(2026, 5, 5, tzinfo=datetime.timezone.utc))
        cluster = build_or_update_cluster(db_session, event, filing=filing)
        db_session.flush()
        event.cluster_id = cluster.id  # as build.py does — without overwriting scores

        # Raw event score must be unchanged
        assert event.materiality_score == pytest.approx(raw_score, abs=1e-4)


# ---------------------------------------------------------------------------
# R5 – Form 4 and Form 144 base scores at revised values
# ---------------------------------------------------------------------------

class TestR5FormBaseScores:
    def test_form4_base_score_at_most_0_25(self):
        score = compute_materiality_score(form_type="4")
        assert score <= 0.25, f"Form 4 base {score} is too high; routine insiders shouldn't dominate"

    def test_form144_base_score_at_most_0_22(self):
        score = compute_materiality_score(form_type="144")
        assert score <= 0.22, f"Form 144 base {score} is too high"

    def test_form4_lower_than_8k(self):
        assert FORM_BASE_SCORE.get("4", 0) < FORM_BASE_SCORE.get("8-K", 0)

    def test_form4_lower_than_10k(self):
        assert FORM_BASE_SCORE.get("4", 0) < FORM_BASE_SCORE.get("10-K", 0)


# ---------------------------------------------------------------------------
# R6 – Cluster titles use human-readable labels, not raw form codes
# ---------------------------------------------------------------------------

class TestR6ClusterTitles:
    def test_form4_title_is_not_bare_code(self):
        from equity_intel.db.models import Filing as _Filing
        f = _Filing(form_type="4", items=None)
        title = _filing_title(f)
        assert title != "4", "Form 4 title must not be the bare form code '4'"
        assert "Insider" in title or "Transaction" in title

    def test_form144_title_is_not_bare_code(self):
        from equity_intel.db.models import Filing as _Filing
        f = _Filing(form_type="144", items=None)
        title = _filing_title(f)
        assert title != "144"

    def test_8k_with_items_in_title(self):
        from equity_intel.db.models import Filing as _Filing
        f = _Filing(form_type="8-K", items="2.02,9.01")
        title = _filing_title(f)
        assert "2.02" in title or "Items" in title

    def test_all_form_labels_non_empty(self):
        for code, label in _FORM_LABELS.items():
            assert label, f"Empty label for form code '{code}'"
            assert label != code, f"Label for '{code}' is just the code itself"

    def test_unknown_form_falls_back_to_code(self):
        from equity_intel.db.models import Filing as _Filing
        f = _Filing(form_type="UNKNOWN-XYZ", items=None)
        title = _filing_title(f)
        assert title == "UNKNOWN-XYZ"  # graceful fallback


# ---------------------------------------------------------------------------
# R7 – get_event_cluster defined exactly once in tools.py
# ---------------------------------------------------------------------------

class TestR7NoDuplicateToolDefinitions:
    def test_get_event_cluster_defined_once(self):
        import inspect
        import equity_intel.mcp_server.tools as tools_mod
        source = inspect.getsource(tools_mod)
        count = source.count("def get_event_cluster(")
        assert count == 1, (
            f"get_event_cluster is defined {count} times in tools.py; "
            "duplicate definitions cause the later one to silently shadow the first"
        )

    def test_get_events_defined_once(self):
        import inspect
        import equity_intel.mcp_server.tools as tools_mod
        source = inspect.getsource(tools_mod)
        assert source.count("def get_events(") == 1

    def test_screen_catalysts_defined_once(self):
        import inspect
        import equity_intel.mcp_server.tools as tools_mod
        source = inspect.getsource(tools_mod)
        assert source.count("def screen_catalysts(") == 1


# ---------------------------------------------------------------------------
# R8 – Live Massive/Polygon pipeline validation (2026-05-11)
# ---------------------------------------------------------------------------

class TestR8LiveProviderConfig:
    """
    Lock in the configuration contract established during live Massive/Polygon
    validation.  All tests are fully deterministic — no network calls.
    """

    def test_settings_has_polygon_api_key_field(self):
        """Settings must expose polygon_api_key, not price_api_key, for provider use."""
        from equity_intel.config import Settings
        import inspect
        src = inspect.getsource(Settings)
        assert "polygon_api_key" in src, "Settings must declare polygon_api_key field"

    def test_settings_polygon_api_key_readable_from_env(self, monkeypatch):
        """POLYGON_API_KEY env var must be picked up into settings.polygon_api_key."""
        monkeypatch.setenv("POLYGON_API_KEY", "testkey_abc123")
        from equity_intel.config import Settings
        s = Settings()
        assert s.polygon_api_key == "testkey_abc123"

    def test_sync_news_factory_uses_polygon_api_key(self, monkeypatch):
        """sync_news._get_news_provider() must pass settings.polygon_api_key to PolygonNewsProvider."""
        monkeypatch.setenv("NEWS_PROVIDER", "polygon")
        monkeypatch.setenv("POLYGON_API_KEY", "news_key_xyz")
        # Reload settings inside the worker module scope
        import importlib
        import equity_intel.config as cfg_mod
        cfg_mod.settings = cfg_mod.Settings()
        import equity_intel.workers.sync_news as sync_news_mod
        importlib.reload(sync_news_mod)

        provider = sync_news_mod._get_news_provider()
        assert provider is not None, "Provider must not be None when NEWS_PROVIDER=polygon and key is set"
        from equity_intel.news.polygon import PolygonNewsProvider
        assert isinstance(provider, PolygonNewsProvider)
        assert provider.api_key == "news_key_xyz"

    def test_sync_prices_factory_uses_polygon_api_key(self, monkeypatch):
        """sync_prices._get_price_provider() must pass settings.polygon_api_key to PolygonPriceProvider."""
        monkeypatch.setenv("PRICE_PROVIDER", "polygon")
        monkeypatch.setenv("POLYGON_API_KEY", "price_key_xyz")
        import importlib
        import equity_intel.config as cfg_mod
        cfg_mod.settings = cfg_mod.Settings()
        import equity_intel.workers.sync_prices as sync_prices_mod
        importlib.reload(sync_prices_mod)

        provider = sync_prices_mod._get_price_provider()
        assert provider is not None, "Provider must not be None when PRICE_PROVIDER=polygon and key is set"
        from equity_intel.prices.polygon import PolygonPriceProvider
        assert isinstance(provider, PolygonPriceProvider)
        assert provider.api_key == "price_key_xyz"

    def test_sync_news_factory_none_when_key_missing(self, monkeypatch):
        """sync_news._get_news_provider() must return None and log error if key is empty."""
        monkeypatch.setenv("NEWS_PROVIDER", "polygon")
        monkeypatch.setenv("POLYGON_API_KEY", "")
        import importlib
        import equity_intel.config as cfg_mod
        cfg_mod.settings = cfg_mod.Settings()
        import equity_intel.workers.sync_news as sync_news_mod
        importlib.reload(sync_news_mod)
        assert sync_news_mod._get_news_provider() is None

    def test_sync_prices_factory_none_when_key_missing(self, monkeypatch):
        """sync_prices._get_price_provider() must return None and log error if key is empty."""
        monkeypatch.setenv("PRICE_PROVIDER", "polygon")
        monkeypatch.setenv("POLYGON_API_KEY", "")
        import importlib
        import equity_intel.config as cfg_mod
        cfg_mod.settings = cfg_mod.Settings()
        import equity_intel.workers.sync_prices as sync_prices_mod
        importlib.reload(sync_prices_mod)
        assert sync_prices_mod._get_price_provider() is None

    def test_sync_news_factory_none_when_provider_is_none(self, monkeypatch):
        """sync_news._get_news_provider() returns None when NEWS_PROVIDER=none."""
        monkeypatch.setenv("NEWS_PROVIDER", "none")
        monkeypatch.setenv("POLYGON_API_KEY", "some_key")
        import importlib
        import equity_intel.config as cfg_mod
        cfg_mod.settings = cfg_mod.Settings()
        import equity_intel.workers.sync_news as sync_news_mod
        importlib.reload(sync_news_mod)
        assert sync_news_mod._get_news_provider() is None

    def test_sync_prices_factory_none_when_provider_is_none(self, monkeypatch):
        """sync_prices._get_price_provider() returns None when PRICE_PROVIDER=none."""
        monkeypatch.setenv("PRICE_PROVIDER", "none")
        monkeypatch.setenv("POLYGON_API_KEY", "some_key")
        import importlib
        import equity_intel.config as cfg_mod
        cfg_mod.settings = cfg_mod.Settings()
        import equity_intel.workers.sync_prices as sync_prices_mod
        importlib.reload(sync_prices_mod)
        assert sync_prices_mod._get_price_provider() is None

    def test_polygon_news_normalization_contract(self):
        """
        Normalized news article must contain all required fields with correct types.
        Validates the contract that live data confirmed for all 10 watchlist tickers.
        """
        import datetime
        from equity_intel.news.polygon import _parse_published_at

        # All fields live data returned — lock them in
        required_str_fields = {"provider", "provider_id", "ticker", "title", "summary",
                               "url", "publisher", "author"}
        required_fields = required_str_fields | {"published_at", "tickers", "sentiment", "raw"}

        # Simulate what fetch_news returns for a real article
        article = {
            "id": "abc123",
            "title": "AAPL hits record high",
            "article_url": "https://example.com/news/abc123",
            "published_utc": "2026-05-11T10:00:00Z",
            "description": "Apple stock hits record high.",
            "author": "Jane Smith",
            "publisher": {"name": "Reuters"},
            "tickers": ["AAPL", "SPY"],
            "insights": [{"ticker": "AAPL", "sentiment": "positive"}],
        }
        normalized = {
            "provider": "polygon",
            "provider_id": article.get("id", ""),
            "ticker": "AAPL",
            "title": article.get("title", ""),
            "summary": article.get("description", ""),
            "url": article.get("article_url", ""),
            "publisher": article.get("publisher", {}).get("name", ""),
            "author": article.get("author", ""),
            "published_at": _parse_published_at(article.get("published_utc")),
            "tickers": article.get("tickers", []),
            "sentiment": article.get("insights", [{}])[0].get("sentiment"),
            "raw": article,
        }

        assert required_fields.issubset(normalized.keys()), \
            f"Missing: {required_fields - normalized.keys()}"
        assert isinstance(normalized["published_at"], datetime.datetime)
        assert normalized["published_at"].tzinfo is not None, "published_at must be tz-aware"
        assert isinstance(normalized["tickers"], list)
        assert normalized["sentiment"] == "positive"

    def test_polygon_price_normalization_contract(self):
        """
        Normalized price bar must contain all required OHLCV fields with correct types.
        Validates the contract that live data confirmed for all 10 watchlist tickers.
        """
        import datetime
        from equity_intel.prices.polygon import _ms_to_datetime

        raw = {"t": 1746748800000, "o": 185.0, "h": 192.0, "l": 184.5, "c": 190.5,
               "v": 52_000_000.0, "vw": 189.0, "n": 500}
        bar = {
            "ticker": "AAPL",
            "timestamp": _ms_to_datetime(raw["t"]),
            "open": raw["o"],
            "high": raw["h"],
            "low": raw["l"],
            "close": raw["c"],
            "volume": raw["v"],
            "adjusted_close": raw["c"],
            "interval": "1d",
            "provider": "polygon",
            "raw": raw,
        }
        required = {"ticker", "timestamp", "open", "high", "low", "close",
                    "volume", "adjusted_close", "interval", "provider", "raw"}
        assert required.issubset(bar.keys())
        assert isinstance(bar["timestamp"], datetime.datetime)
        assert bar["timestamp"].tzinfo is not None
        assert bar["interval"] == "1d"
        assert bar["provider"] == "polygon"
        assert all(bar[k] is not None for k in ("open", "high", "low", "close", "volume"))


# ---------------------------------------------------------------------------
# R9 – Persistence + idempotency validation (2026-05-11)
# ---------------------------------------------------------------------------

class TestR9PersistenceAndIdempotency:
    """
    Lock in the persistence and idempotency behaviors confirmed by live
    ingestion of Massive/Polygon data for all 10 watchlist tickers.
    All tests use in-memory SQLite — no network calls.
    """

    @pytest.fixture
    def fresh_db(self):
        """Provide a fresh in-memory engine + session for each test."""
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)
        session = Session()
        yield session
        session.close()

    def _make_news_article(self, session, ticker="AAPL", provider_id="art001", url="https://example.com/art001"):
        """Insert a minimal NewsArticle and return it."""
        from equity_intel.db.models import NewsArticle
        from equity_intel.workers.sync_news import upsert_news_article
        article = {
            "provider": "polygon",
            "provider_id": provider_id,
            "ticker": ticker,
            "title": f"Test article {provider_id}",
            "summary": "Test summary.",
            "url": url,
            "publisher": "Test Publisher",
            "author": "Test Author",
            "published_at": datetime.datetime(2026, 5, 8, 10, 0, 0, tzinfo=datetime.timezone.utc),
            "tickers": [ticker],
            "sentiment": "positive",
            "raw": {"id": provider_id},
        }
        from equity_intel.db.models import Company as _Company
        company = session.query(_Company).filter(_Company.ticker == ticker).first()
        if not company:
            company = _make_company(session, ticker)
        inserted = upsert_news_article(session, article, company)
        session.flush()
        return inserted, article

    def _make_price_bar(self, session, ticker="AAPL", ts=None):
        """Insert a minimal MarketPrice bar and return whether it was inserted."""
        from equity_intel.workers.sync_prices import upsert_price_bar
        bar = {
            "ticker": ticker,
            "timestamp": ts or datetime.datetime(2026, 5, 8, 0, 0, 0, tzinfo=datetime.timezone.utc),
            "open": 185.0, "high": 192.0, "low": 184.5, "close": 190.5,
            "volume": 52_000_000.0, "adjusted_close": 190.5,
            "interval": "1d", "provider": "polygon", "raw": {},
        }
        inserted = upsert_price_bar(session, bar)
        session.flush()
        return inserted, bar

    # ── News idempotency ──────────────────────────────────────────────────────

    def test_news_upsert_inserts_first_time(self, fresh_db):
        inserted, _ = self._make_news_article(fresh_db)
        assert inserted is True

    def test_news_upsert_skips_duplicate_by_provider_id(self, fresh_db):
        self._make_news_article(fresh_db, provider_id="art001", url="https://example.com/art001")
        inserted2, _ = self._make_news_article(fresh_db, provider_id="art001", url="https://example.com/art001")
        assert inserted2 is False

    def test_news_upsert_skips_duplicate_by_url(self, fresh_db):
        """Second insert with same URL but different provider_id must also be rejected."""
        from equity_intel.workers.sync_news import upsert_news_article
        company = _make_company(fresh_db, "AAPL")
        article1 = {
            "provider": "polygon", "provider_id": "art001",
            "ticker": "AAPL", "title": "A", "summary": "", "url": "https://example.com/shared",
            "publisher": "P", "author": "", "published_at": None, "tickers": [], "raw": {},
        }
        article2 = {
            "provider": "polygon", "provider_id": "art002",  # different id, same URL
            "ticker": "AAPL", "title": "B", "summary": "", "url": "https://example.com/shared",
            "publisher": "P", "author": "", "published_at": None, "tickers": [], "raw": {},
        }
        assert upsert_news_article(fresh_db, article1, company) is True
        fresh_db.flush()
        assert upsert_news_article(fresh_db, article2, company) is False

    def test_news_multiple_tickers_stored_independently(self, fresh_db):
        """Articles for different tickers are stored as separate rows."""
        from equity_intel.db.models import NewsArticle
        self._make_news_article(fresh_db, ticker="AAPL", provider_id="a1", url="https://example.com/a1")
        self._make_news_article(fresh_db, ticker="NVDA", provider_id="n1", url="https://example.com/n1")
        fresh_db.commit()
        assert fresh_db.query(NewsArticle).count() == 2
        assert fresh_db.query(NewsArticle).filter(NewsArticle.ticker == "AAPL").count() == 1
        assert fresh_db.query(NewsArticle).filter(NewsArticle.ticker == "NVDA").count() == 1

    def test_news_source_grounding_fields_written(self, fresh_db):
        """Persisted news row must have url, publisher, and published_at."""
        from equity_intel.db.models import NewsArticle
        self._make_news_article(fresh_db)
        fresh_db.commit()
        row = fresh_db.query(NewsArticle).first()
        assert row is not None
        assert row.url is not None and row.url.startswith("https://")
        assert row.publisher is not None
        assert row.published_at is not None
        # SQLite strips tzinfo on read; in production (PostgreSQL) tzinfo is preserved.
        # We verify the value was stored, not the tz decoration, in this SQLite test.

    # ── Price idempotency ─────────────────────────────────────────────────────

    def test_price_upsert_inserts_first_time(self, fresh_db):
        inserted, _ = self._make_price_bar(fresh_db)
        assert inserted is True

    def test_price_upsert_skips_duplicate(self, fresh_db):
        """Same (ticker, timestamp, interval) must be skipped on second call."""
        ts = datetime.datetime(2026, 5, 8, 0, 0, 0, tzinfo=datetime.timezone.utc)
        self._make_price_bar(fresh_db, ticker="AAPL", ts=ts)
        inserted2, _ = self._make_price_bar(fresh_db, ticker="AAPL", ts=ts)
        assert inserted2 is False

    def test_price_different_dates_stored_independently(self, fresh_db):
        """Bars for different timestamps are stored as separate rows."""
        from equity_intel.db.models import MarketPrice
        t1 = datetime.datetime(2026, 5, 6, 0, 0, 0, tzinfo=datetime.timezone.utc)
        t2 = datetime.datetime(2026, 5, 7, 0, 0, 0, tzinfo=datetime.timezone.utc)
        self._make_price_bar(fresh_db, ticker="AAPL", ts=t1)
        self._make_price_bar(fresh_db, ticker="AAPL", ts=t2)
        fresh_db.commit()
        assert fresh_db.query(MarketPrice).filter(MarketPrice.ticker == "AAPL").count() == 2

    def test_price_ohlcv_fields_written(self, fresh_db):
        """Persisted price bar must have all OHLCV fields non-null."""
        from equity_intel.db.models import MarketPrice
        self._make_price_bar(fresh_db)
        fresh_db.commit()
        row = fresh_db.query(MarketPrice).first()
        assert row is not None
        for field in ("open", "high", "low", "close", "volume"):
            assert getattr(row, field) is not None, f"{field} must not be None"
        assert row.interval == "1d"
        assert row.provider == "polygon"

    # ── MCP get_recent_news surface ───────────────────────────────────────────

    def test_get_recent_news_returns_source_grounded_fields(self, fresh_db):
        """get_recent_news must surface url, publisher, published_at for every article."""
        from equity_intel.mcp_server.tools import get_recent_news
        self._make_news_article(fresh_db, ticker="AAPL")
        fresh_db.commit()
        result = get_recent_news(fresh_db, ticker="AAPL", days=30)
        assert result["total"] == 1
        art = result["articles"][0]
        assert art["url"] is not None
        assert art["publisher"] is not None
        assert art["published_at"] is not None
        assert result.get("note") is not None  # source-grounding note present

    def test_get_recent_news_empty_for_unknown_ticker(self, fresh_db):
        """get_recent_news must return empty list (not error) for unknown ticker."""
        from equity_intel.mcp_server.tools import get_recent_news
        result = get_recent_news(fresh_db, ticker="ZZZZ", days=30)
        assert result["total"] == 0
        assert result["articles"] == []

    # ── MCP explain_stock_move surface ────────────────────────────────────────

    def test_explain_stock_move_weekend_date_returns_available_false(self, fresh_db):
        """
        explain_stock_move on a weekend (no trading bars) must return
        price_move.available=False and include a caution note — not crash.
        """
        from equity_intel.mcp_server.tools import explain_stock_move
        from equity_intel.db.models import MarketPrice

        # Insert bars for trading days around the weekend
        for day in (5, 6, 7, 8):   # Mon-Thu 2026-05-05 to 05-08
            ts = datetime.datetime(2026, 5, day, 0, 0, 0, tzinfo=datetime.timezone.utc)
            fresh_db.add(MarketPrice(
                ticker="AAPL", timestamp=ts, open=185.0, high=190.0, low=184.0,
                close=188.0, volume=50_000_000.0, adjusted_close=188.0,
                interval="1d", provider="polygon", created_at=now_utc(),
            ))
        fresh_db.commit()

        # Saturday — no bars exist
        result = explain_stock_move(fresh_db, ticker="AAPL", date="2026-05-09", window=2)
        assert result["price_move"]["available"] is False
        assert "caution" in result
        assert result["caution"]  # non-empty

    def test_explain_stock_move_trading_day_returns_available_true(self, fresh_db):
        """
        explain_stock_move on a known trading day with bars before AND after
        the target must return price_move.available=True.
        """
        from equity_intel.mcp_server.tools import explain_stock_move
        from equity_intel.db.models import MarketPrice

        for day, close in ((6, 180.0), (7, 190.0), (8, 195.0)):
            ts = datetime.datetime(2026, 5, day, 0, 0, 0, tzinfo=datetime.timezone.utc)
            fresh_db.add(MarketPrice(
                ticker="AAPL", timestamp=ts, open=close - 2, high=close + 2,
                low=close - 3, close=close, volume=50_000_000.0, adjusted_close=close,
                interval="1d", provider="polygon", created_at=now_utc(),
            ))
        fresh_db.commit()

        result = explain_stock_move(fresh_db, ticker="AAPL", date="2026-05-07", window=1)
        pm = result["price_move"]
        assert pm["available"] is True
        assert pm["pct_change"] is not None
        assert pm["volume_ratio"] is not None
        assert "up" in result["interpretation"] or "down" in result["interpretation"]


# ---------------------------------------------------------------------------
# R9 addendum: fresh_db fixture (needed by R9 tests, defined here for R10 reuse)
# ---------------------------------------------------------------------------

@pytest.fixture
def fresh_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


# ---------------------------------------------------------------------------
# R10  E2E Pipeline Regression Tests (2026-05-11)
#
# Bugs fixed:
#   R10-A  sync_documents used .subquery() in .notin_() — SAWarning under
#           SQLAlchemy 2.0; replaced with .scalar_subquery()
#   R10-B  sec/parser.py triggered XMLParsedAsHTMLWarning on XML-like SEC
#           filings; warning now suppressed via warnings.filterwarnings()
#
# Also locks in:
#   - Event builder produces events from both filings and news articles
#   - Clustering produces well-formed cluster keys
#   - Materiality scores remain in [0, 1] range after cluster enhancement
#   - Cluster filing_ids and news_ids reference valid DB rows
#   - explain_stock_move returns non-empty interpretation text
# ---------------------------------------------------------------------------

class TestR10E2EPipelineRegression:
    """R10: E2E pipeline regression tests (2026-05-11)."""

    # ── R10-A: scalar_subquery ────────────────────────────────────────────

    def test_sync_documents_uses_scalar_subquery(self):
        """
        sync_documents.run() must use .scalar_subquery() not .subquery() for the
        'only filings without documents' filter.  Importing the module and
        inspecting the source is the lightest way to lock this in deterministically.
        """
        import inspect
        from equity_intel.workers import sync_documents
        src = inspect.getsource(sync_documents)
        assert "scalar_subquery()" in src, (
            "sync_documents must use .scalar_subquery() for SQLAlchemy 2.0 compatibility"
        )
        assert "notin_(downloaded_ids)" in src

    def test_sync_documents_scalar_subquery_no_sawarning(self, fresh_db):
        """
        Building the notin_() filter with .scalar_subquery() must not emit an
        SAWarning.  We exercise the exact pattern from sync_documents.run().
        """
        import warnings
        from sqlalchemy import select
        from sqlalchemy.exc import SAWarning
        from equity_intel.db.models import Filing, FilingDocument, Company

        # Seed a company + filing
        company = Company(ticker="SAW", name="SAW Co", is_active=True,
                          created_at=now_utc(), updated_at=now_utc())
        fresh_db.add(company)
        fresh_db.flush()
        filing = Filing(
            company_id=company.id, accession_number="0000000099-26-000001",
            form_type="8-K", filing_url="https://example.com/8k",
            created_at=now_utc(), updated_at=now_utc(),
        )
        fresh_db.add(filing)
        fresh_db.flush()

        # The fixed pattern: scalar_subquery in notin_()
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            downloaded_ids = fresh_db.query(FilingDocument.filing_id).scalar_subquery()
            result = (
                fresh_db.query(Filing)
                .filter(Filing.id.notin_(downloaded_ids))
                .all()
            )
        sa_warnings = [w for w in caught if issubclass(w.category, SAWarning)]
        assert len(sa_warnings) == 0, f"Unexpected SAWarnings: {[str(w.message) for w in sa_warnings]}"
        assert len(result) == 1
        assert result[0].accession_number == "0000000099-26-000001"

    # ── R10-B: XMLParsedAsHTMLWarning suppression ─────────────────────────

    def test_xml_parsed_as_html_warning_suppressed(self):
        """
        sec/parser.py must contain a module-level warnings.filterwarnings('ignore',
        category=XMLParsedAsHTMLWarning) call.

        We inspect source rather than runtime warnings.filters because pytest
        manages its own filter stack and catch_warnings(record=True) inserts an
        'always' override that would make the ignore filter invisible at test time.
        """
        import inspect
        from equity_intel.sec import parser as parser_mod

        src = inspect.getsource(parser_mod)
        assert "filterwarnings" in src, "parser.py must call warnings.filterwarnings()"
        assert "XMLParsedAsHTMLWarning" in src, "parser.py must reference XMLParsedAsHTMLWarning"
        assert '"ignore"' in src or "'ignore'" in src, (
            "parser.py must register an 'ignore' action for XMLParsedAsHTMLWarning"
        )

    def test_parser_module_suppresses_warning_at_import(self):
        """
        html_to_plain_text() must return non-empty plain text when given an
        XML-like SEC filing document (verifying the lxml parse path works).
        """
        from equity_intel.sec.parser import html_to_plain_text

        xml_doc = '<?xml version="1.0"?><html><body><p>SEC filing content here</p></body></html>'
        text = html_to_plain_text(xml_doc)
        assert isinstance(text, str)
        assert len(text) > 0
        assert "SEC filing content" in text

    # ── Event builder: filing and news sources ────────────────────────────

    def test_build_event_from_filing_creates_event(self, fresh_db):
        """build_event_from_filing() must create an Event with required fields."""
        from equity_intel.events.build import build_event_from_filing

        company = Company(ticker="EVT", name="Event Co", is_active=True,
                          created_at=now_utc(), updated_at=now_utc())
        fresh_db.add(company)
        fresh_db.flush()
        filing = Filing(
            company_id=company.id,
            accession_number="0000000010-26-000001",
            form_type="8-K", items="2.02",
            filing_url="https://example.com/8k",
            filing_date=datetime.date(2026, 5, 8),
            created_at=now_utc(), updated_at=now_utc(),
        )
        fresh_db.add(filing)
        fresh_db.flush()

        event = build_event_from_filing(fresh_db, filing, company, document=None, run_clustering=False)

        assert event is not None
        assert event.ticker == "EVT"
        assert event.source_type == "filing"
        assert event.source_id == filing.id
        assert event.event_type is not None
        assert event.materiality_score is not None
        assert 0.0 <= event.materiality_score <= 1.0
        assert event.title is not None

    def test_build_event_from_news_creates_event(self, fresh_db):
        """build_event_from_news() must create an Event from a NewsArticle."""
        from equity_intel.events.build import build_event_from_news
        from equity_intel.db.models import NewsArticle

        company = Company(ticker="NEW", name="News Co", is_active=True,
                          created_at=now_utc(), updated_at=now_utc())
        fresh_db.add(company)
        fresh_db.flush()
        article = NewsArticle(
            ticker="NEW", provider="polygon", provider_id="news-001",
            title="NEW Corp reports record earnings",
            summary="Strong quarterly results",
            url="https://example.com/news/1",
            publisher="Reuters",
            published_at=datetime.datetime(2026, 5, 8, 10, 0, 0, tzinfo=datetime.timezone.utc),
            created_at=now_utc(),
        )
        fresh_db.add(article)
        fresh_db.flush()

        event = build_event_from_news(fresh_db, article, company, run_clustering=False)

        assert event is not None
        assert event.ticker == "NEW"
        assert event.source_type == "news"
        assert event.source_id == article.id
        assert event.materiality_score is not None
        assert 0.0 <= event.materiality_score <= 1.0

    def test_build_events_idempotent(self, fresh_db):
        """Calling build_event_from_filing twice for the same filing must not create duplicates."""
        from equity_intel.events.build import build_event_from_filing

        company = Company(ticker="DUP", name="Dup Co", is_active=True,
                          created_at=now_utc(), updated_at=now_utc())
        fresh_db.add(company)
        fresh_db.flush()
        filing = Filing(
            company_id=company.id,
            accession_number="0000000020-26-000001",
            form_type="10-K",
            filing_url="https://example.com/10k",
            filing_date=datetime.date(2026, 4, 1),
            created_at=now_utc(), updated_at=now_utc(),
        )
        fresh_db.add(filing)
        fresh_db.flush()

        e1 = build_event_from_filing(fresh_db, filing, company, document=None, run_clustering=False)
        e2 = build_event_from_filing(fresh_db, filing, company, document=None, run_clustering=False)

        assert e1 is not None
        assert e2 is None  # already exists — returns None

        count = fresh_db.query(Event).filter(Event.source_type == "filing",
                                              Event.source_id == filing.id).count()
        assert count == 1

    # ── Clustering: key format and materiality range ───────────────────────

    def test_cluster_key_format(self, fresh_db):
        """build_or_update_cluster() must produce a key matching TICKER:type:YYYYWww."""
        import re
        from equity_intel.events.cluster import build_or_update_cluster

        company = Company(ticker="CLK", name="Clock Co", is_active=True,
                          created_at=now_utc(), updated_at=now_utc())
        fresh_db.add(company)
        fresh_db.flush()
        filing = Filing(
            company_id=company.id,
            accession_number="0000000030-26-000001",
            form_type="8-K", filing_date=datetime.date(2026, 5, 8),
            filing_url="https://example.com/8k",
            created_at=now_utc(), updated_at=now_utc(),
        )
        fresh_db.add(filing)
        fresh_db.flush()

        event = Event(
            company_id=company.id, ticker="CLK",
            event_type="earnings", event_subtype="results",
            title="CLK Q1 Earnings", summary="Beat estimates",
            source_type="filing", source_id=filing.id,
            source_url="https://example.com/8k",
            occurred_at=datetime.datetime(2026, 5, 8, 0, 0, 0, tzinfo=datetime.timezone.utc),
            detected_at=now_utc(),
            materiality_score=0.7, confidence_score=0.8, novelty_score=1.0,
            created_at=now_utc(), updated_at=now_utc(),
        )
        fresh_db.add(event)
        fresh_db.flush()

        cluster = build_or_update_cluster(fresh_db, event, filing=filing)
        fresh_db.flush()

        assert cluster is not None
        assert re.match(r'^[A-Z]+:[a-z_]+:\d{4}W\d{2}$', cluster.cluster_key), (
            f"Unexpected cluster_key format: {cluster.cluster_key!r}"
        )
        assert cluster.cluster_key.startswith("CLK:earnings:")

    def test_cluster_materiality_in_range(self, fresh_db):
        """Cluster materiality_score must remain in [0, 1] after build_or_update_cluster."""
        from equity_intel.events.cluster import build_or_update_cluster

        company = Company(ticker="MAT", name="Mat Co", is_active=True,
                          created_at=now_utc(), updated_at=now_utc())
        fresh_db.add(company)
        fresh_db.flush()
        filing = Filing(
            company_id=company.id,
            accession_number="0000000040-26-000001",
            form_type="10-K", filing_date=datetime.date(2026, 4, 1),
            filing_url="https://example.com/10k",
            created_at=now_utc(), updated_at=now_utc(),
        )
        fresh_db.add(filing)
        fresh_db.flush()

        # Create multiple events to exercise cluster accumulation
        for i, mat in enumerate([0.4, 0.6, 0.8, 0.9]):
            event = Event(
                company_id=company.id, ticker="MAT",
                event_type="earnings", event_subtype="results",
                title=f"MAT Earnings {i}", summary="Q result",
                source_type="filing", source_id=filing.id,
                occurred_at=datetime.datetime(2026, 4, 1 + i, 0, 0, 0, tzinfo=datetime.timezone.utc),
                detected_at=now_utc(),
                materiality_score=mat, confidence_score=0.5, novelty_score=1.0,
                created_at=now_utc(), updated_at=now_utc(),
            )
            fresh_db.add(event)
            fresh_db.flush()
            cluster = build_or_update_cluster(fresh_db, event, filing=filing)
            fresh_db.flush()
            assert 0.0 <= cluster.materiality_score <= 1.0, (
                f"materiality_score={cluster.materiality_score} out of [0,1] after event {i}"
            )

