"""
Tests for retry and fill reconciliation in trading execution.
"""
from __future__ import annotations

import datetime
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from equity_intel.db.models import Base, TradeOrder, TradeSignal
from equity_intel.trading.execution import execute_approved_signals


@pytest.fixture(scope="function")
def db_session():
    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    Base.metadata.drop_all(engine)


class _Cfg:
    trading_execution_enabled = True
    trading_require_approval = False
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


def _seed_buy_signal(session: Session, status: str = "approved") -> TradeSignal:
    sig = TradeSignal(
        ticker="NVDA",
        signal_side="buy",
        signal_strength=0.85,
        status=status,
        created_at=datetime.datetime.now(datetime.timezone.utc),
    )
    session.add(sig)
    session.flush()
    return sig


def _ready_broker() -> MagicMock:
    broker = MagicMock()
    broker.get_account.return_value = {
        "trading_blocked": False,
        "buying_power": 10000.0,
        "portfolio_value": 50000.0,
        "equity": 50000.0,
    }
    broker.get_quote.return_value = {
        "symbol": "NVDA",
        "bid": 99.9,
        "ask": 100.1,
        "mid": 100.0,
        "spread": 0.2,
        "spread_pct": 0.2,
    }
    broker.has_open_order.return_value = False
    broker.get_position.return_value = None
    broker.get_positions.return_value = []
    return broker


def test_submitted_order_stays_pending_until_filled(db_session):
    sig = _seed_buy_signal(db_session, status="pending_fill")
    order = TradeOrder(
        trade_signal_id=sig.id,
        ticker="NVDA",
        side="buy",
        order_type="limit",
        time_in_force="day",
        broker="alpaca",
        broker_order_id="existing-order",
        status="submitted",
        submitted_at=datetime.datetime.now(datetime.timezone.utc),
    )
    db_session.add(order)
    db_session.flush()

    broker = _ready_broker()
    broker.get_order.return_value = {
        "broker_order_id": "existing-order",
        "symbol": "NVDA",
        "status": "pending_new",
        "filled_at": None,
        "filled_avg_price": None,
    }

    with patch("equity_intel.trading.execution._build_broker", return_value=broker):
        orders = execute_approved_signals(db_session, _Cfg(), dry_run=False)

    assert orders == []
    db_session.refresh(sig)
    db_session.refresh(order)
    assert sig.status == "pending_fill"
    assert order.status == "pending_new"
    broker.submit_limit_order.assert_not_called()


def test_canceled_order_is_retried_same_run(db_session):
    sig = _seed_buy_signal(db_session, status="executed")
    old_order = TradeOrder(
        trade_signal_id=sig.id,
        ticker="NVDA",
        side="buy",
        order_type="limit",
        time_in_force="day",
        broker="alpaca",
        broker_order_id="old-order",
        status="submitted",
        submitted_at=datetime.datetime.now(datetime.timezone.utc),
    )
    db_session.add(old_order)
    db_session.flush()

    broker = _ready_broker()
    broker.get_order.return_value = {
        "broker_order_id": "old-order",
        "symbol": "NVDA",
        "status": "canceled",
        "filled_at": None,
        "filled_avg_price": None,
    }
    broker.submit_limit_order.return_value = {
        "broker_order_id": "new-order",
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

    with patch("equity_intel.trading.execution._build_broker", return_value=broker):
        orders = execute_approved_signals(db_session, _Cfg(), dry_run=False)

    assert len(orders) == 1
    db_session.refresh(sig)
    db_session.refresh(old_order)
    assert sig.status == "pending_fill"
    assert old_order.status == "canceled"
    assert old_order.failure_reason == "order closed without fill: canceled"
    assert db_session.query(TradeOrder).count() == 2


def test_filled_order_marks_signal_executed(db_session):
    sig = _seed_buy_signal(db_session, status="pending_fill")
    order = TradeOrder(
        trade_signal_id=sig.id,
        ticker="NVDA",
        side="buy",
        order_type="limit",
        time_in_force="day",
        broker="alpaca",
        broker_order_id="fill-order",
        status="submitted",
        submitted_at=datetime.datetime.now(datetime.timezone.utc),
    )
    db_session.add(order)
    db_session.flush()

    broker = _ready_broker()
    broker.get_order.return_value = {
        "broker_order_id": "fill-order",
        "symbol": "NVDA",
        "status": "filled",
        "filled_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "filled_avg_price": "100.05",
    }

    with patch("equity_intel.trading.execution._build_broker", return_value=broker):
        orders = execute_approved_signals(db_session, _Cfg(), dry_run=False)

    assert orders == []
    db_session.refresh(sig)
    db_session.refresh(order)
    assert sig.status == "executed"
    assert order.status == "filled"
    assert order.filled_at is not None
    assert order.filled_avg_price == 100.05
