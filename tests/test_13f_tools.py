"""
Tests for the get_institutional_holders and get_manager_holdings MCP tools.

Uses an in-memory SQLite database — no external services required.
"""
from __future__ import annotations

import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from equity_intel.db.models import Base, Company, Filing, InstitutionalHolding, now_utc
from equity_intel.mcp_server.tools import (
    get_institutional_holders,
    get_manager_holdings,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Helper factories
# ---------------------------------------------------------------------------

def _make_company(session, ticker, name, cik=None) -> Company:
    c = Company(
        ticker=ticker,
        name=name,
        cik=cik,
        is_active=True,
        created_at=now_utc(),
        updated_at=now_utc(),
    )
    session.add(c)
    session.flush()
    return c


def _make_filing(session, company, accession, form_type="13F-HR",
                 filing_date=None, report_date=None) -> Filing:
    filing_date = filing_date or datetime.date(2024, 2, 14)
    report_date = report_date or datetime.date(2023, 12, 31)
    f = Filing(
        company_id=company.id,
        accession_number=accession,
        form_type=form_type,
        filing_date=filing_date,
        report_date=report_date,
        filing_url=f"https://www.sec.gov/Archives/edgar/data/1/{accession}/",
        created_at=now_utc(),
        updated_at=now_utc(),
    )
    session.add(f)
    session.flush()
    return f


def _make_holding(
    session,
    filing,
    held_company,
    manager_cik,
    manager_name,
    shares,
    value_usd,
    cusip,
    report_date=None,
    filing_date=None,
) -> InstitutionalHolding:
    h = InstitutionalHolding(
        filing_id=filing.id,
        manager_cik=manager_cik,
        manager_name=manager_name,
        issuer_name=held_company.name,
        cusip=cusip,
        title_of_class="COM",
        value_usd=value_usd,
        shares=shares,
        share_type="SH",
        investment_discretion="SOLE",
        report_date=report_date or datetime.date(2023, 12, 31),
        filing_date=filing_date or datetime.date(2024, 2, 14),
        ticker=held_company.ticker,
        company_id=held_company.id,
        raw_json={},
        created_at=now_utc(),
        updated_at=now_utc(),
    )
    session.add(h)
    session.flush()
    return h


# ---------------------------------------------------------------------------
# get_institutional_holders tests
# ---------------------------------------------------------------------------

class TestGetInstitutionalHolders:

    def test_unknown_ticker_returns_error(self, session):
        result = get_institutional_holders(session, ticker="ZZZZ")
        assert "error" in result

    def test_no_holdings_returns_message(self, session):
        _make_company(session, "NOHLD", "No Holdings Corp")
        result = get_institutional_holders(session, ticker="NOHLD")
        assert result["holders"] == [] or "message" in result

    def test_returns_holders_for_known_ticker(self, session):
        # Create held company
        held = _make_company(session, "AAPL2", "Apple Inc Test", cik="0000320193")

        # Create the manager company (the filer)
        mgr = _make_company(session, "_MGR_BERK", "Berkshire Hathaway", cik="0001067983")

        # Create a 13F-HR filing for the manager
        filing = _make_filing(session, mgr, "0001067983-24-000001")

        # Add a holding record linking the manager → held company
        _make_holding(
            session, filing, held, "0001067983", "Berkshire Hathaway",
            shares=200_000_000, value_usd=30_000_000, cusip="037833100",
        )
        session.commit()

        result = get_institutional_holders(session, ticker="AAPL2")
        assert result["ticker"] == "AAPL2"
        assert len(result["quarters"]) >= 1
        q0 = result["quarters"][0]
        assert q0["holder_count"] >= 1
        holder = q0["holders"][0]
        assert holder["shares"] == 200_000_000
        assert holder["value_usd"] == 30_000_000 * 1000

    def test_value_usd_is_multiplied_by_1000(self, session):
        """value_usd_thousands * 1000 == value_usd in the response."""
        held = _make_company(session, "MSFT2", "Microsoft Test")
        mgr = _make_company(session, "_MGR_VAN", "Vanguard", cik="0000315066")
        filing = _make_filing(session, mgr, "0000315066-24-000002")
        _make_holding(
            session, filing, held, "0000315066", "Vanguard",
            shares=500_000, value_usd=99_000, cusip="594918104",
        )
        session.commit()

        result = get_institutional_holders(session, ticker="MSFT2")
        holder = result["quarters"][0]["holders"][0]
        assert holder["value_usd_thousands"] == 99_000
        assert holder["value_usd"] == 99_000_000

    def test_multiple_quarters_returned(self, session):
        held = _make_company(session, "NVDA2", "NVIDIA Test")
        mgr = _make_company(session, "_MGR_BLK", "BlackRock", cik="0001364742")

        filing_q4 = _make_filing(
            session, mgr, "0001364742-24-000001",
            filing_date=datetime.date(2024, 2, 14),
            report_date=datetime.date(2023, 12, 31),
        )
        filing_q3 = _make_filing(
            session, mgr, "0001364742-23-000999",
            filing_date=datetime.date(2023, 11, 14),
            report_date=datetime.date(2023, 9, 30),
        )

        _make_holding(
            session, filing_q4, held, "0001364742", "BlackRock",
            shares=10_000_000, value_usd=5_000_000, cusip="67066G104",
            report_date=datetime.date(2023, 12, 31),
        )
        _make_holding(
            session, filing_q3, held, "0001364742", "BlackRock",
            shares=8_000_000, value_usd=3_000_000, cusip="67066G104",
            report_date=datetime.date(2023, 9, 30),
        )
        session.commit()

        result = get_institutional_holders(session, ticker="NVDA2", quarters=4)
        assert len(result["quarters"]) == 2
        # Most recent quarter should be first
        dates = [q["report_date"][:7] for q in result["quarters"]]
        assert dates[0] >= dates[1]

    def test_source_is_sec_edgar(self, session):
        held = _make_company(session, "TST1", "Test Co 1")
        mgr = _make_company(session, "_MGR_TST", "Test Manager", cik="0099999999")
        filing = _make_filing(session, mgr, "0099999999-24-000001")
        _make_holding(
            session, filing, held, "0099999999", "Test Manager",
            shares=1_000, value_usd=100, cusip="999999999",
        )
        session.commit()

        result = get_institutional_holders(session, ticker="TST1")
        assert "SEC EDGAR" in result.get("source", "")


# ---------------------------------------------------------------------------
# get_manager_holdings tests
# ---------------------------------------------------------------------------

class TestGetManagerHoldings:

    def test_error_when_no_identifier_given(self, session):
        result = get_manager_holdings(session)
        assert "error" in result

    def test_no_holdings_returns_message(self, session):
        result = get_manager_holdings(session, manager_cik="0000000000")
        assert "message" in result or "holdings" in result

    def test_returns_holdings_by_cik(self, session):
        held1 = _make_company(session, "GGE1", "Alphabet Test")
        held2 = _make_company(session, "AMZ1", "Amazon Test")

        mgr = _make_company(session, "_MGR_ST8", "State Street", cik="0000093751")
        filing = _make_filing(session, mgr, "0000093751-24-000001")

        _make_holding(
            session, filing, held1, "0000093751", "State Street",
            shares=5_000_000, value_usd=7_000_000, cusip="02079K305",
        )
        _make_holding(
            session, filing, held2, "0000093751", "State Street",
            shares=3_000_000, value_usd=4_000_000, cusip="023135106",
        )
        session.commit()

        result = get_manager_holdings(session, manager_cik="0000093751")
        assert result["manager_cik"] == "0000093751"
        assert result["total_positions"] == 2
        assert result["total_value_usd"] == (7_000_000 + 4_000_000) * 1000

    def test_returns_holdings_by_name(self, session):
        held = _make_company(session, "INTC2", "Intel Test")
        mgr = _make_company(session, "_MGR_FID", "Fidelity Management", cik="0000315066X")
        filing = _make_filing(session, mgr, "0000315066X-24-000001")
        _make_holding(
            session, filing, held, "0000315066X", "Fidelity Management",
            shares=9_000_000, value_usd=2_000_000, cusip="458140100",
        )
        session.commit()

        result = get_manager_holdings(session, manager_name="Fidelity")
        assert result["manager_name"] == "Fidelity Management"
        assert len(result["holdings"]) >= 1

    def test_holdings_sorted_by_value_desc(self, session):
        held_a = _make_company(session, "SRT1", "SortA Corp")
        held_b = _make_company(session, "SRT2", "SortB Corp")
        held_c = _make_company(session, "SRT3", "SortC Corp")

        mgr = _make_company(session, "_MGR_SRT", "Sort Manager", cik="0000111111")
        filing = _make_filing(session, mgr, "0000111111-24-000001")

        _make_holding(session, filing, held_a, "0000111111", "Sort Manager",
                      shares=100, value_usd=300, cusip="AAA000001")
        _make_holding(session, filing, held_b, "0000111111", "Sort Manager",
                      shares=100, value_usd=100, cusip="BBB000002")
        _make_holding(session, filing, held_c, "0000111111", "Sort Manager",
                      shares=100, value_usd=200, cusip="CCC000003")
        session.commit()

        result = get_manager_holdings(session, manager_cik="0000111111")
        values = [h["value_usd_thousands"] for h in result["holdings"]]
        assert values == sorted(values, reverse=True)

    def test_report_date_filter(self, session):
        held = _make_company(session, "RDF1", "ReportDate Corp")
        mgr = _make_company(session, "_MGR_RDF", "RDF Manager", cik="0000222222")

        filing_q4 = _make_filing(
            session, mgr, "0000222222-24-000001",
            report_date=datetime.date(2023, 12, 31),
        )
        filing_q3 = _make_filing(
            session, mgr, "0000222222-23-000001",
            filing_date=datetime.date(2023, 11, 1),
            report_date=datetime.date(2023, 9, 30),
        )

        _make_holding(
            session, filing_q4, held, "0000222222", "RDF Manager",
            shares=1_000_000, value_usd=500_000, cusip="RDF000001",
            report_date=datetime.date(2023, 12, 31),
        )
        _make_holding(
            session, filing_q3, held, "0000222222", "RDF Manager",
            shares=800_000, value_usd=400_000, cusip="RDF000001",
            report_date=datetime.date(2023, 9, 30),
        )
        session.commit()

        result = get_manager_holdings(
            session, manager_cik="0000222222",
            report_date="2023-09-30",
        )
        assert result["total_positions"] == 1
        assert result["holdings"][0]["shares"] == 800_000

    def test_value_usd_multiplied(self, session):
        held = _make_company(session, "VMU1", "ValueMul Corp")
        mgr = _make_company(session, "_MGR_VMU", "ValueMul Mgr", cik="0000333333")
        filing = _make_filing(session, mgr, "0000333333-24-000001")
        _make_holding(
            session, filing, held, "0000333333", "ValueMul Mgr",
            shares=1, value_usd=42, cusip="VMU000001",
        )
        session.commit()

        result = get_manager_holdings(session, manager_cik="0000333333")
        h = result["holdings"][0]
        assert h["value_usd_thousands"] == 42
        assert h["value_usd"] == 42_000

    def test_source_is_sec_edgar(self, session):
        held = _make_company(session, "SRC1", "Source Corp")
        mgr = _make_company(session, "_MGR_SRC", "Source Manager", cik="0000444444")
        filing = _make_filing(session, mgr, "0000444444-24-000001")
        _make_holding(
            session, filing, held, "0000444444", "Source Manager",
            shares=1, value_usd=1, cusip="SRC000001",
        )
        session.commit()

        result = get_manager_holdings(session, manager_cik="0000444444")
        assert "SEC EDGAR" in result.get("source", "")
