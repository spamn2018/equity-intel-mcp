"""
Tests for worker CLIs -- generate_trade_signals and execute_trade_signals.

Tests the worker run() functions directly (not via Click CLI invocation)
to keep them fast and mock-friendly.
"""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from equity_intel.db.models import Base, TradeSignal, TradeOrder, TradingDecisionLog


# In-memory DB fixture

@pytest.fixture(scope="function")
def db_session():
    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    Base.metadata.drop_all(engine)


# Settings stubs

class _CfgNoExec:
    """Execution disabled (default safe config)."""
    trad_hedge_list = ["BNY", "BAC"]
    trading_execution_enabled = False
    trading_require_approval = True
    trading_signals_enabled = False
    trading_min_materiality = 0.60
    trading_min_confidence = 0.50
    trading_min_signal_strength = 0.70
    trading_require_primary_source = True
    trading_allow_news_only_signals = False
    trading_allow_probe_stage_signals = False
    trading_max_position_pct = 5.0
    trading_max_order_notional = 500.0
    trading_max_spread_pct = 1.0
    alpaca_api_key = "test_key"
    alpaca_secret_key = "test_secret"
    alpaca_paper = True
    daily_brief_tickers = ["NVDA", "AMAT"]


class _CfgExec(_CfgNoExec):
    """Execution enabled, no approval required."""
    trading_execution_enabled = True
    trading_require_approval = False


_MOCK_BRIEF = {
    "catalysts": [
        {
            "ticker": "NVDA",
            "event_type": "earnings",
            "event_subtype": None,
            "materiality_score": 0.85,
            "confidence_score": 0.80,
            "novelty_score": 0.70,
            "has_primary_source": True,
            "filing_count": 1,
            "news_count": 2,
            "research_stage": "active",
            "cluster_id": 1,
            "cluster_key": "cluster_NVDA_1",
            "title": "NVDA earnings",
            "source_links": ["https://www.sec.gov/test"],
            "related_filings": [],
            "related_news": [],
            "source_summary": "1 SEC filing",
            "price_move": None,
            "why_it_matters": "test",
            "caution": None,
        }
    ],
    "total_catalysts": 1,
}


def test_dry_run_writes_nothing(db_session):
    """Dry-run must not touch the database."""
    from equity_intel.workers.generate_trade_signals import run

    with patch("equity_intel.workers.generate_trade_signals.SessionLocal") as mock_session_factory,          patch("equity_intel.workers.generate_trade_signals.get_watchlist_brief") as mock_brief:
        mock_brief.return_value = _MOCK_BRIEF
        mock_session_factory.return_value.__enter__ = lambda s: db_session
        mock_session_factory.return_value.__exit__ = MagicMock(return_value=False)
        mock_session_factory.return_value = db_session

        result = run(
            tickers=["NVDA"],
            days=7,
            min_materiality=0.60,
            min_confidence=0.50,
            min_signal_strength=0.70,
            dry_run=True,
            cfg=_CfgNoExec(),
        )

    assert db_session.query(TradeSignal).count() == 0
    assert result["dry_run"] is True


def test_live_run_persists_signal(db_session):
    from equity_intel.workers.generate_trade_signals import run

    with patch("equity_intel.workers.generate_trade_signals.SessionLocal") as mock_sl,          patch("equity_intel.workers.generate_trade_signals.get_watchlist_brief") as mock_brief:
        mock_brief.return_value = _MOCK_BRIEF
        mock_sl.return_value = db_session

        result = run(
            tickers=["NVDA"],
            days=7,
            min_materiality=0.60,
            min_confidence=0.50,
            min_signal_strength=0.70,
            dry_run=False,
            cfg=_CfgNoExec(),
        )

    signals = db_session.query(TradeSignal).all()
    assert len(signals) >= 1
    assert result["total_generated"] >= 1


def test_execution_disabled_submits_no_order(db_session):
    """Master kill-switch: no broker orders when execution is disabled."""
    from equity_intel.trading.execution import execute_approved_signals

    sig = TradeSignal(
        ticker="NVDA",
        signal_side="buy",
        signal_strength=0.85,
        status="approved",
    )
    db_session.add(sig)
    db_session.flush()

    cfg = _CfgNoExec()
    orders = execute_approved_signals(db_session, cfg, dry_run=False)

    assert len(orders) == 0
    db_session.refresh(sig)
    assert sig.status == "approved"


def test_execute_dry_run_submits_nothing(db_session):
    """--dry-run must never submit broker orders."""
    from equity_intel.trading.execution import execute_approved_signals

    sig = TradeSignal(
        ticker="NVDA",
        signal_side="buy",
        signal_strength=0.85,
        status="approved",
    )
    db_session.add(sig)
    db_session.flush()

    cfg = _CfgExec()
    orders = execute_approved_signals(db_session, cfg, dry_run=True)

    assert len(orders) == 0
    assert db_session.query(TradeOrder).count() == 0


def test_require_approval_blocks_generated_in_execution(db_session):
    from equity_intel.trading.execution import execute_approved_signals

    sig = TradeSignal(
        ticker="NVDA",
        signal_side="buy",
        signal_strength=0.85,
        status="generated",
    )
    db_session.add(sig)
    db_session.flush()

    class _CfgApprovalRequired(_CfgExec):
        trading_require_approval = True

    cfg = _CfgApprovalRequired()
    with patch("equity_intel.trading.execution._build_broker", return_value=MagicMock()):
        orders = execute_approved_signals(db_session, cfg, dry_run=False)
    assert len(orders) == 0


def test_execute_submits_order_when_all_pass(db_session):
    from equity_intel.trading.execution import execute_approved_signals

    sig = TradeSignal(
        ticker="NVDA",
        signal_side="buy",
        signal_strength=0.85,
        status="approved",
    )
    db_session.add(sig)
    db_session.flush()

    cfg = _CfgExec()

    mock_broker = MagicMock()
    mock_broker.get_account.return_value = {
        "trading_blocked": False,
        "buying_power": 10000.0,
        "portfolio_value": 50000.0,
        "equity": 50000.0,
    }
    mock_broker.get_quote.return_value = {
        "symbol": "NVDA", "bid": 99.9, "ask": 100.1,
        "mid": 100.0, "spread": 0.2, "spread_pct": 0.2,
    }
    mock_broker.has_open_order.return_value = False
    mock_broker.get_position.return_value = None
    mock_broker.get_positions.return_value = []
    mock_broker.submit_limit_order.return_value = {
        "broker_order_id": "test-order-1",
        "symbol": "NVDA",
        "side": "buy",
        "qty": None,
        "notional": "500.0",
        "limit_price": "100.1",
        "order_type": "limit",
        "time_in_force": "day",
        "status": "pending_new",
        "submitted_at": None,
    }

    with patch("equity_intel.trading.execution._build_broker", return_value=mock_broker):
        orders = execute_approved_signals(db_session, cfg, dry_run=False)

    assert len(orders) == 1
    assert orders[0].ticker == "NVDA"
    assert orders[0].status == "pending_new"
    db_session.refresh(sig)
    assert sig.status == "pending_fill"
    log = db_session.query(TradingDecisionLog).filter_by(decision="submitted").first()
    assert log is not None
