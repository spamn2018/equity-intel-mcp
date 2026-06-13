"""
Tests for trading/signals.py — signal generation logic.

All tests use in-memory SQLite so no real database is needed.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from equity_intel.db.models import Base, TradeSignal, TradingDecisionLog
from equity_intel.trading.signals import (
    _resolve_side,
    _signal_strength,
    generate_trade_signals_from_brief,
)


# ── In-memory DB fixture ───────────────────────────────────────────────────────

@pytest.fixture(scope="function")
def db_session():
    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    Base.metadata.drop_all(engine)


# ── Settings stub ──────────────────────────────────────────────────────────────

class _Cfg:
    trad_hedge_list = ["BNY", "BAC", "FLS"]
    trading_require_primary_source = True
    trading_allow_news_only_signals = False
    trading_allow_probe_stage_signals = False
    trading_min_materiality = 0.60
    trading_min_confidence = 0.50
    trading_min_signal_strength = 0.70


_cfg = _Cfg()


# ── Catalyst helpers ───────────────────────────────────────────────────────────

def _make_brief(catalysts):
    return {"catalysts": catalysts, "total_catalysts": len(catalysts)}


def _catalyst(
    ticker="NVDA",
    event_type="earnings",
    event_subtype=None,
    materiality=0.85,
    confidence=0.80,
    novelty=0.70,
    has_primary=True,
    filing_count=1,
    news_count=2,
    research_stage="active",
    cluster_id=1,
):
    return {
        "ticker": ticker,
        "event_type": event_type,
        "event_subtype": event_subtype,
        "materiality_score": materiality,
        "confidence_score": confidence,
        "novelty_score": novelty,
        "has_primary_source": has_primary,
        "filing_count": filing_count,
        "news_count": news_count,
        "research_stage": research_stage,
        "cluster_id": cluster_id,
        "cluster_key": f"cluster_{ticker}_{cluster_id}",
        "title": f"{ticker} {event_type}",
        "source_links": ["https://www.sec.gov/test"],
        "related_filings": [],
        "related_news": [],
        "source_summary": "1 SEC filing",
        "price_move": None,
        "why_it_matters": "test",
        "caution": None,
    }


# ── Tests: _resolve_side ───────────────────────────────────────────────────────

def test_resolve_side_earnings_is_buy():
    assert _resolve_side("earnings", None) == "buy"


def test_resolve_side_guidance_raised_is_buy():
    assert _resolve_side("guidance_raised", None) == "buy"


def test_resolve_side_offering_is_sell():
    assert _resolve_side("offering_or_dilution", None) == "sell"


def test_resolve_side_going_concern_is_sell():
    assert _resolve_side("bankruptcy_or_going_concern", None) == "sell"


def test_resolve_side_subtype_override():
    assert _resolve_side("management_change", "guidance_lowered") == "sell"
    assert _resolve_side("other", "guidance_raised") == "buy"


def test_resolve_side_neutral_monitor():
    assert _resolve_side("management_change", None) == "monitor"
    assert _resolve_side("other", None) == "monitor"


# ── Tests: _signal_strength ────────────────────────────────────────────────────

def test_signal_strength_formula_primary():
    s = _signal_strength(1.0, 1.0, 1.0, True)
    assert abs(s - 1.0) < 1e-6


def test_signal_strength_formula_no_primary():
    s = _signal_strength(1.0, 1.0, 1.0, False)
    # bonus is 0.5 instead of 1.0 -> drops by 0.10 * 0.5 = 0.05
    assert abs(s - 0.95) < 1e-6


def test_signal_strength_clamped():
    # Minimum: all scores 0, no primary -> 0.10 * 0.5 (bonus) = 0.05
    assert _signal_strength(0.0, 0.0, 0.0, False) == pytest.approx(0.05)
    # Maximum: all scores 1.0, primary confirmed -> clamped to 1.0
    assert _signal_strength(1.0, 1.0, 1.0, True) == 1.0
    # Negative inputs are clamped to 0 floor
    assert _signal_strength(-1.0, -1.0, -1.0, False) == pytest.approx(0.05)


# ── Test 1: High-materiality/high-confidence -> buy candidate ──────────────────

def test_high_materiality_creates_buy(db_session):
    brief = _make_brief([_catalyst(materiality=0.90, confidence=0.85, event_type="earnings")])
    signals = generate_trade_signals_from_brief(
        db_session, brief,
        min_materiality=0.60, min_confidence=0.50, min_signal_strength=0.70,
        require_primary_source=False, allow_news_only=True, allow_probe_stage=True,
        cfg=_cfg,
    )
    assert len(signals) == 1
    assert signals[0].signal_side == "buy"
    assert signals[0].status == "generated"
    assert signals[0].signal_strength >= 0.70


# ── Test 2: Low-materiality -> skipped or monitor ──────────────────────────────

def test_low_materiality_skipped(db_session):
    brief = _make_brief([_catalyst(materiality=0.20, confidence=0.80)])
    signals = generate_trade_signals_from_brief(
        db_session, brief,
        min_materiality=0.60, min_confidence=0.50, min_signal_strength=0.70,
        require_primary_source=False, allow_news_only=True, allow_probe_stage=True,
        cfg=_cfg,
    )
    # Should be skipped entirely -- under materiality gate
    assert len(signals) == 0
    # Decision log should record the skip
    log = db_session.query(TradingDecisionLog).filter_by(decision="skipped").first()
    assert log is not None


# ── Test 3: Low-confidence -> skipped ─────────────────────────────────────────

def test_low_confidence_skipped(db_session):
    brief = _make_brief([_catalyst(materiality=0.80, confidence=0.10)])
    signals = generate_trade_signals_from_brief(
        db_session, brief,
        min_materiality=0.60, min_confidence=0.50, min_signal_strength=0.70,
        require_primary_source=False, allow_news_only=True, allow_probe_stage=True,
        cfg=_cfg,
    )
    assert len(signals) == 0


# ── Test 4: News-only blocked when allow_news_only=False ─────────────────────

def test_news_only_blocked(db_session):
    cat = _catalyst(has_primary=False, filing_count=0, news_count=3)
    brief = _make_brief([cat])
    signals = generate_trade_signals_from_brief(
        db_session, brief,
        min_materiality=0.60, min_confidence=0.50, min_signal_strength=0.70,
        require_primary_source=False,  # not requiring primary; testing news-only gate
        allow_news_only=False,
        allow_probe_stage=True,
        cfg=_cfg,
    )
    assert len(signals) == 0


# ── Test 5: Probe-stage -> monitor-only ────────────────────────────────────────

def test_probe_stage_downgrades_to_monitor(db_session):
    cat = _catalyst(research_stage="probe", event_type="earnings", materiality=0.85)
    brief = _make_brief([cat])
    signals = generate_trade_signals_from_brief(
        db_session, brief,
        min_materiality=0.60, min_confidence=0.50, min_signal_strength=0.00,  # low threshold so strength passes
        require_primary_source=False, allow_news_only=True, allow_probe_stage=False,
        cfg=_cfg,
    )
    assert len(signals) == 1
    assert signals[0].signal_side == "monitor"


# ── Test 6: Negative event -> sell/avoid ──────────────────────────────────────

def test_negative_event_maps_to_sell(db_session):
    cat = _catalyst(event_type="offering_or_dilution", materiality=0.75, confidence=0.70)
    brief = _make_brief([cat])
    signals = generate_trade_signals_from_brief(
        db_session, brief,
        min_materiality=0.60, min_confidence=0.50, min_signal_strength=0.00,
        require_primary_source=False, allow_news_only=True, allow_probe_stage=True,
        cfg=_cfg,
    )
    assert len(signals) == 1
    assert signals[0].signal_side == "sell"


# ── Test 7: TradHedge tickers never receive signals ───────────────────────────

def test_tradhedge_ticker_blocked(db_session):
    cat = _catalyst(ticker="BAC", materiality=0.95, confidence=0.95)
    brief = _make_brief([cat])
    signals = generate_trade_signals_from_brief(
        db_session, brief,
        min_materiality=0.0, min_confidence=0.0, min_signal_strength=0.0,
        require_primary_source=False, allow_news_only=True, allow_probe_stage=True,
        cfg=_cfg,
    )
    assert len(signals) == 0


# ── Test 8: Duplicate prevention ─────────────────────────────────────────────

def test_duplicate_signal_is_updated_not_duplicated(db_session):
    cat = _catalyst(materiality=0.85, cluster_id=99)
    brief = _make_brief([cat])
    gen_kwargs = dict(
        min_materiality=0.60, min_confidence=0.50, min_signal_strength=0.00,
        require_primary_source=False, allow_news_only=True, allow_probe_stage=True,
        cfg=_cfg,
    )
    signals1 = generate_trade_signals_from_brief(db_session, brief, **gen_kwargs)
    db_session.flush()
    signals2 = generate_trade_signals_from_brief(db_session, brief, **gen_kwargs)
    db_session.flush()

    assert len(signals1) == 1
    assert len(signals2) == 1
    # Same DB row -- not a new record
    assert signals1[0].id == signals2[0].id
    total = db_session.query(TradeSignal).count()
    assert total == 1


# ── Test 9: Evidence and rationale always populated ───────────────────────────

def test_signal_has_rationale_and_evidence(db_session):
    cat = _catalyst(materiality=0.80, confidence=0.75)
    brief = _make_brief([cat])
    signals = generate_trade_signals_from_brief(
        db_session, brief,
        min_materiality=0.60, min_confidence=0.50, min_signal_strength=0.00,
        require_primary_source=False, allow_news_only=True, allow_probe_stage=True,
        cfg=_cfg,
    )
    sig = signals[0]
    assert sig.rationale and len(sig.rationale) > 10
    assert sig.reason_codes_json and sig.reason_codes_json.get("codes")
    assert sig.evidence_json is not None
