"""
Tests for MCP tool functions using an in-memory SQLite database.

These tests do not require PostgreSQL or any external services.
They verify tool logic and response shapes.
"""
from __future__ import annotations

import datetime
import json
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from equity_intel.db.models import Base, Company, Event, Filing, FilingDocument, now_utc
from equity_intel.mcp_server.tools import (
    get_company,
    get_events,
    get_filing,
    get_recent_filings,
    get_company_facts,
    explain_stock_move,
    screen_catalysts,
    search_filings_tool,
)


# ------------------------------------------------------------------ #
# SQLite in-memory test fixtures                                       #
# ------------------------------------------------------------------ #


@pytest.fixture(scope="module")
def engine():
    """In-memory SQLite engine for testing."""
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


@pytest.fixture
def sample_company(session) -> Company:
    """Insert a sample company into the test DB."""
    existing = session.query(Company).filter(Company.ticker == "TSLA").first()
    if existing:
        return existing
    now = now_utc()
    company = Company(
        ticker="TSLA",
        cik="0001318605",
        name="Tesla, Inc.",
        exchange="NASDAQ",
        sic="3711",
        sector="Consumer Discretionary",
        industry="Auto Manufacturers",
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    session.add(company)
    session.flush()
    return company


@pytest.fixture
def sample_filing(session, sample_company) -> Filing:
    """Insert a sample 8-K filing."""
    now = now_utc()
    acc = "0001318605-24-000001"
    existing = session.query(Filing).filter(Filing.accession_number == acc).first()
    if existing:
        return existing
    filing = Filing(
        company_id=sample_company.id,
        accession_number=acc,
        form_type="8-K",
        filing_date=datetime.datetime(2024, 1, 15),
        report_date=datetime.datetime(2024, 1, 15),
        items="2.02,9.01",
        filing_url="https://www.sec.gov/Archives/edgar/0001318605/000131860524000001/",
        primary_document_url="https://www.sec.gov/Archives/edgar/0001318605/000131860524000001/form8k.htm",
        sec_index_url="https://www.sec.gov/Archives/edgar/0001318605/000131860524000001/",
        raw_metadata_json={"test": True},
        created_at=now,
        updated_at=now,
    )
    session.add(filing)
    session.flush()
    return filing


@pytest.fixture
def sample_document(session, sample_filing) -> FilingDocument:
    """Insert a parsed filing document."""
    now = now_utc()
    existing = session.query(FilingDocument).filter(
        FilingDocument.filing_id == sample_filing.id
    ).first()
    if existing:
        return existing
    doc = FilingDocument(
        filing_id=sample_filing.id,
        document_url=sample_filing.primary_document_url,
        document_type="8-K",
        filename="form8k.htm",
        plain_text="Tesla Q4 2023 earnings exceeded expectations. Revenue was $25B, up 19% YoY.",
        parsed_sections_json={
            "sections": {"2.02": "Results of operations text here."},
            "detected_items": ["2.02", "9.01"],
            "keywords": ["earnings"],
            "char_count": 100,
        },
        created_at=now,
        updated_at=now,
    )
    session.add(doc)
    session.flush()
    return doc


@pytest.fixture
def sample_event(session, sample_company, sample_filing) -> Event:
    """Insert a sample event."""
    now = now_utc()
    event = Event(
        company_id=sample_company.id,
        ticker="TSLA",
        event_type="earnings",
        event_subtype="results_of_operations",
        title="8-K — Items: 2.02,9.01",
        summary="Tesla 8-K filed. Items: 2.02. Filed: 2024-01-15",
        source_type="filing",
        source_id=sample_filing.id,
        source_url=sample_filing.filing_url,
        occurred_at=datetime.datetime(2024, 1, 15, tzinfo=datetime.timezone.utc),
        detected_at=now,
        materiality_score=0.75,
        novelty_score=0.5,
        confidence_score=0.7,
        evidence_json={"accession_number": sample_filing.accession_number},
        created_at=now,
        updated_at=now,
    )
    session.add(event)
    session.flush()
    return event


# ------------------------------------------------------------------ #
# get_company tests                                                    #
# ------------------------------------------------------------------ #


def test_get_company_found(session, sample_company):
    result = get_company(session, ticker="TSLA")
    assert result["ticker"] == "TSLA"
    assert result["cik"] == "0001318605"
    assert result["name"] == "Tesla, Inc."
    assert "source" in result
    assert "note" in result


def test_get_company_not_found(session):
    result = get_company(session, ticker="XXXX")
    assert "error" in result
    assert result["ticker"] == "XXXX"


def test_get_company_normalizes_ticker(session, sample_company):
    result = get_company(session, ticker="tsla")
    assert result["ticker"] == "TSLA"


def test_get_company_returns_sec_url(session, sample_company):
    result = get_company(session, ticker="TSLA")
    assert result.get("sec_url") and "sec.gov" in result["sec_url"]


# ------------------------------------------------------------------ #
# get_recent_filings tests                                             #
# ------------------------------------------------------------------ #


def test_get_recent_filings_no_company(session):
    result = get_recent_filings(session, ticker="NOCOMP")
    assert "filings" in result
    # Either empty filings or message
    assert result.get("filings") == [] or "message" in result


def test_get_recent_filings_returns_structure(session, sample_company, sample_filing):
    result = get_recent_filings(session, ticker="TSLA", days=365 * 5)
    assert "filings" in result
    assert "ticker" in result
    assert result["ticker"] == "TSLA"


# ------------------------------------------------------------------ #
# get_filing tests                                                     #
# ------------------------------------------------------------------ #


def test_get_filing_found(session, sample_filing, sample_document):
    result = get_filing(session, accession_number="0001318605-24-000001")
    assert result["accession_number"] == "0001318605-24-000001"
    assert result["form_type"] == "8-K"
    assert result["ticker"] == "TSLA"
    assert result["has_parsed_document"] is True
    assert "source" in result


def test_get_filing_not_found(session):
    result = get_filing(session, accession_number="0000000000-00-000000")
    assert "error" in result


def test_get_filing_includes_items(session, sample_filing, sample_document):
    result = get_filing(session, accession_number="0001318605-24-000001")
    assert result["items"]
    item_nums = [i["item"] for i in result["items"]]
    assert "2.02" in item_nums


def test_get_filing_sections_capped(session, sample_filing, sample_document):
    result = get_filing(session, accession_number="0001318605-24-000001")
    for section_text in result["sections"].values():
        assert len(section_text) <= 2000


# ------------------------------------------------------------------ #
# search_filings_tool tests                                           #
# ------------------------------------------------------------------ #


def test_search_filings_returns_structure(session, sample_company, sample_filing, sample_document):
    # SQLite doesn't support tsvector; tool falls back gracefully
    result = search_filings_tool(session, query="earnings revenue")
    assert "query" in result
    assert "results" in result
    assert isinstance(result["results"], list)
    assert "source" in result


def test_search_filings_with_date_range(session, sample_company, sample_filing, sample_document):
    result = search_filings_tool(
        session,
        query="earnings",
        start_date="2024-01-01",
        end_date="2024-12-31",
    )
    assert "results" in result


# ------------------------------------------------------------------ #
# get_events tests                                                     #
# ------------------------------------------------------------------ #


def test_get_events_returns_structure(session, sample_event):
    result = get_events(session, ticker="TSLA", days=365 * 5)
    assert "events" in result
    assert "ticker" in result
    assert result["ticker"] == "TSLA"


def test_get_events_each_event_has_required_fields(session, sample_event):
    result = get_events(session, ticker="TSLA", days=365 * 5)
    for event in result["events"]:
        assert "event_type" in event
        assert "materiality_score" in event
        assert "source_url" in event or event.get("source_url") is None


# ------------------------------------------------------------------ #
# explain_stock_move tests                                             #
# ------------------------------------------------------------------ #


def test_explain_stock_move_returns_structure(session, sample_company):
    result = explain_stock_move(session, ticker="TSLA", date="2024-01-15", window=3)
    assert "ticker" in result
    assert "evidence" in result
    assert "confidence_score" in result
    assert "caution" in result
    assert "note" in result


def test_explain_stock_move_caution_language(session, sample_company):
    result = explain_stock_move(session, ticker="TSLA")
    # Must use cautious language
    assert "caution" in result
    combined = result.get("caution", "") + result.get("interpretation", "")
    # Should NOT claim to know cause with certainty
    assert "caused" not in combined.lower() or "likely" in combined.lower()


def test_explain_stock_move_no_data_graceful(session, sample_company):
    result = explain_stock_move(session, ticker="TSLA", date="2020-01-01", window=1)
    assert result["evidence_count"] == 0 or "evidence" in result


# ------------------------------------------------------------------ #
# screen_catalysts tests                                               #
# ------------------------------------------------------------------ #


def test_screen_catalysts_returns_structure(session, sample_event):
    result = screen_catalysts(session, min_materiality=0.0, days=365 * 5)
    assert "catalysts" in result
    assert "filters" in result
    assert "total" in result


def test_screen_catalysts_respects_min_materiality(session, sample_event):
    high = screen_catalysts(session, min_materiality=0.9, days=365 * 5)
    low = screen_catalysts(session, min_materiality=0.0, days=365 * 5)
    assert low["total"] >= high["total"]


# ------------------------------------------------------------------ #
# get_company_facts tests                                              #
# ------------------------------------------------------------------ #


def test_get_company_facts_no_company(session):
    result = get_company_facts(session, ticker="XXXX")
    assert "error" in result


def test_get_company_facts_returns_structure(session, sample_company):
    result = get_company_facts(session, ticker="TSLA")
    assert "facts" in result
    assert "ticker" in result
    assert result["ticker"] == "TSLA"
