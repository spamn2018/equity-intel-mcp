"""
Tests for trading/risk.py -- risk policy evaluation.

All broker interactions are mocked. No real Alpaca calls are made.
"""
from __future__ import annotations

import datetime

import pytest
from unittest.mock import MagicMock, patch
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from equity_intel.db.models import Base, TradeSignal, TradingDecisionLog
from equity_intel.trading.risk import evaluate_signal_for_execution


# ── Fixtures ───────────────────────────────────────────────────────────────────

@pytest.fixture(scope="function")
def db_session():
    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    Base.metadata.drop_all(engine)


class _Cfg:
    trading_execution_enabled = True
    trading_require_approval = False       # default: no approval needed
    trading_min_signal_strength = 0.70
    trading_max_position_pct = 5.0
    trading_max_order_notional = 500.0
    trading_allow_shorts = False
    trading_regular_hours_only = False
    trading_regular_hours_open_et = "09:30"
    trading_regular_hours_close_et = "15:55"
    # orders are always limit+day+notional (fractional enabled by default)


def _make_signal(
    ticker="NVDA",
    side="buy",
    strength=0.80,
    status="generated",
    max_position_pct=None,
):
    sig = TradeSignal(
        id=1,
        ticker=ticker,
        signal_side=side,
        signal_strength=strength,
        status=status,
        max_position_pct=max_position_pct,
    )
    return sig


def _make_broker(
    trading_blocked=False,
    buying_power=10000.0,
    portfolio_value=50000.0,
    quote_mid=100.0,
    spread_pct=0.1,
    has_open_order=False,
    position=None,
    quote_raises=False,
):
    broker = MagicMock()
    broker.get_account.return_value = {
        "trading_blocked": trading_blocked,
        "buying_power": buying_power,
        "portfolio_value": portfolio_value,
        "equity": portfolio_value,
    }
    if quote_raises:
        broker.get_quote.side_effect = ValueError("no quote")
    else:
        broker.get_quote.return_value = {
            "symbol": "NVDA",
            "bid": quote_mid - 0.05,
            "ask": quote_mid + 0.05,
            "mid": quote_mid,
            "spread": 0.10,
            "spread_pct": spread_pct,
        }
    broker.has_open_order.return_value = has_open_order
    broker.get_positions.return_value = [position] if position else []
    broker.get_position.return_value = position
    return broker


# ── Test 1: TRADING_EXECUTION_ENABLED=False blocks everything ─────────────────

def test_execution_disabled_blocks(db_session):
    cfg = _Cfg()
    cfg.trading_execution_enabled = False
    sig = _make_signal()
    db_session.add(sig); db_session.flush()
    result = evaluate_signal_for_execution(db_session, sig, MagicMock(), cfg)
    assert not result["allowed"]
    assert any("TRADING_EXECUTION_ENABLED" in r for r in result["reasons"])


def test_regular_hours_gate_blocks_weekend_or_after_hours(db_session):
    cfg = _Cfg()
    cfg.trading_regular_hours_only = True
    sig = _make_signal(status="approved")
    db_session.add(sig); db_session.flush()
    broker = _make_broker()
    saturday_utc = datetime.datetime(2026, 7, 4, 15, 0, tzinfo=datetime.timezone.utc)
    result = evaluate_signal_for_execution(db_session, sig, broker, cfg, now_utc=saturday_utc)
    assert not result["allowed"]
    assert result["retriable"] is True
    assert any("regular-hours gate" in r for r in result["reasons"])


def test_regular_hours_gate_can_be_disabled(db_session):
    cfg = _Cfg()
    cfg.trading_regular_hours_only = False
    sig = _make_signal(status="approved", strength=0.80)
    db_session.add(sig); db_session.flush()
    broker = _make_broker()
    saturday_utc = datetime.datetime(2026, 7, 4, 15, 0, tzinfo=datetime.timezone.utc)
    result = evaluate_signal_for_execution(db_session, sig, broker, cfg, now_utc=saturday_utc)
    assert result["allowed"]


# ── Test 2: TRADING_REQUIRE_APPROVAL=True blocks unapproved signals ───────────

def test_require_approval_blocks_generated(db_session):
    cfg = _Cfg()
    cfg.trading_require_approval = True
    sig = _make_signal(status="generated")
    db_session.add(sig); db_session.flush()
    result = evaluate_signal_for_execution(db_session, sig, MagicMock(), cfg)
    assert not result["allowed"]
    assert any("approval" in r.lower() for r in result["reasons"])


def test_approved_signal_passes_approval_gate(db_session):
    cfg = _Cfg()
    cfg.trading_require_approval = True
    sig = _make_signal(status="approved", strength=0.80)
    db_session.add(sig); db_session.flush()
    broker = _make_broker()
    result = evaluate_signal_for_execution(db_session, sig, broker, cfg)
    assert result["allowed"]


# ── Test 3: monitor/avoid are not executable ──────────────────────────────────

def test_monitor_side_not_executable(db_session):
    cfg = _Cfg()
    sig = _make_signal(side="monitor")
    db_session.add(sig); db_session.flush()
    result = evaluate_signal_for_execution(db_session, sig, MagicMock(), cfg)
    assert not result["allowed"]
    assert any("not executable" in r for r in result["reasons"])


# ── Test 4: Wide spread does NOT block ──────────────────────────────────────

def test_wide_spread_does_not_block(db_session):
    """Spread is irrelevant for limit orders -- the old spread gate is gone."""
    cfg = _Cfg()
    sig = _make_signal()
    db_session.add(sig); db_session.flush()
    broker = _make_broker(spread_pct=2.5)   # would have tripped the old 1.0 gate
    result = evaluate_signal_for_execution(db_session, sig, broker, cfg)
    assert result["allowed"]
    assert not any("spread" in r for r in result["reasons"])


# ── Test 5: Existing open order blocks ────────────────────────────────────────

def test_open_order_blocks(db_session):
    cfg = _Cfg()
    sig = _make_signal()
    db_session.add(sig); db_session.flush()
    broker = _make_broker(has_open_order=True)
    result = evaluate_signal_for_execution(db_session, sig, broker, cfg)
    assert not result["allowed"]
    assert any("Open order" in r for r in result["reasons"])


# ── Test 6: Insufficient buying power blocks ──────────────────────────────────

def test_insufficient_buying_power_blocks(db_session):
    cfg = _Cfg()
    cfg.trading_max_order_notional = 500.0
    sig = _make_signal()
    db_session.add(sig); db_session.flush()
    # buying_power=0.0 -> order_notional=min(500, capacity, 0) = 0 -> qty=0 -> blocked
    broker = _make_broker(buying_power=0.0, portfolio_value=50000.0)
    result = evaluate_signal_for_execution(db_session, sig, broker, cfg)
    assert not result["allowed"]


# ── Test 7: No position blocks sell ──────────────────────────────────────────

def test_sell_without_position_blocks(db_session):
    cfg = _Cfg()
    sig = _make_signal(side="sell")
    db_session.add(sig); db_session.flush()
    broker = _make_broker(position=None)
    result = evaluate_signal_for_execution(db_session, sig, broker, cfg)
    assert not result["allowed"]
    assert any("TRADING_ALLOW_SHORTS=False" in r for r in result["reasons"])


# ── Test 8: Buy order uses notional (fractional) ─────────────────────────────

def test_buy_order_uses_notional(db_session):
    cfg = _Cfg()
    cfg.trading_max_order_notional = 150.0
    sig = _make_signal(strength=0.80)
    db_session.add(sig); db_session.flush()
    broker = _make_broker(quote_mid=100.0, buying_power=10000.0, portfolio_value=50000.0)
    result = evaluate_signal_for_execution(db_session, sig, broker, cfg)
    if result["allowed"]:
        assert result["order"]["notional"] is not None, "Buy order must use notional"
        assert result["order"]["qty"] is None, "Buy order must not set qty"
        assert result["order"]["order_type"] == "limit"
        assert result["order"]["time_in_force"] == "day"


# ── Test 9: Trading-blocked account blocks ────────────────────────────────────

def test_account_trading_blocked(db_session):
    cfg = _Cfg()
    sig = _make_signal()
    db_session.add(sig); db_session.flush()
    broker = _make_broker(trading_blocked=True)
    result = evaluate_signal_for_execution(db_session, sig, broker, cfg)
    assert not result["allowed"]
    assert any("trading_blocked" in r for r in result["reasons"])


# ── Test 10: Successful buy returns order spec ────────────────────────────────

def test_successful_buy_returns_order_spec(db_session):
    cfg = _Cfg()
    sig = _make_signal(strength=0.85)
    db_session.add(sig); db_session.flush()
    broker = _make_broker(quote_mid=50.0, buying_power=5000.0, portfolio_value=50000.0)
    result = evaluate_signal_for_execution(db_session, sig, broker, cfg)
    assert result["allowed"]
    order = result["order"]
    assert order["symbol"] == "NVDA"
    assert order["side"] == "buy"
    assert order["qty"] is None, "Buy order must not set qty (uses notional for fractional)"
    assert order["notional"] > 0
    # Decisions logged
    log = db_session.query(TradingDecisionLog).filter_by(decision="allowed").first()
    assert log is not None


# ── Test 11: Quote fetch failure blocks ───────────────────────────────────────

def test_quote_failure_blocks(db_session):
    cfg = _Cfg()
    sig = _make_signal()
    db_session.add(sig); db_session.flush()
    broker = _make_broker(quote_raises=True)
    result = evaluate_signal_for_execution(db_session, sig, broker, cfg)
    assert not result["allowed"]
    assert any("quote" in r.lower() for r in result["reasons"])
