from __future__ import annotations

import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from equity_intel.db.models import Base, MarketPrice, SameDaySignalOutcome, TradeSignal
from equity_intel.workers.backtest_same_day_signals import compute_same_day_outcomes
from equity_intel.workers.sync_prices import summarize_intraday_coverage


class _Cfg:
    trading_day_trade_close_time_et = "15:55"


def _utc(year: int, month: int, day: int, hour: int, minute: int) -> datetime.datetime:
    return datetime.datetime(year, month, day, hour, minute, tzinfo=datetime.timezone.utc)


def test_same_day_backtest_marks_intraday_coverage_gap_when_close_cutoff_missing():
    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        signal = TradeSignal(
            ticker="NVDA",
            signal_side="buy",
            generated_at=_utc(2026, 6, 10, 14, 0),
            status="approved",
            event_type="earnings",
            materiality_score=0.8,
            confidence_score=0.7,
            research_stage="active",
        )
        session.add(signal)
        session.flush()

        bars = [
            MarketPrice(
                ticker="NVDA",
                interval="5m",
                provider="polygon",
                timestamp=_utc(2026, 6, 10, 14, 0).replace(tzinfo=None),
                open=100.0,
                high=100.5,
                low=99.8,
                close=100.2,
                volume=1000,
                adjusted_close=100.2,
            ),
            MarketPrice(
                ticker="NVDA",
                interval="5m",
                provider="polygon",
                timestamp=_utc(2026, 6, 10, 16, 0).replace(tzinfo=None),
                open=101.0,
                high=101.5,
                low=100.9,
                close=101.1,
                volume=900,
                adjusted_close=101.1,
            ),
        ]
        for bar in bars:
            session.add(bar)
        session.commit()

        result = compute_same_day_outcomes(session, _Cfg(), interval="5m")

        assert result["outcomes_ok"] == 0
        assert result["outcomes_intraday_coverage_gap"] == 1
        outcome = session.query(SameDaySignalOutcome).filter_by(trade_signal_id=signal.id).one()
        assert outcome is not None
        assert outcome.outcome_status == "intraday_coverage_gap"

    Base.metadata.drop_all(engine)


def test_intraday_coverage_summary_marks_partial_session_when_close_missing():
    bars = [
        {"timestamp": _utc(2026, 6, 10, 13, 30)},
        {"timestamp": _utc(2026, 6, 10, 17, 0)},
        {"timestamp": _utc(2026, 6, 10, 18, 0)},
    ]
    summary = summarize_intraday_coverage(
        bars,
        start=datetime.date(2026, 6, 10),
        end=datetime.date(2026, 6, 10),
        interval="5m",
    )
    assert summary is not None
    assert summary["expected_session_count"] == 1
    assert summary["covered_session_count"] == 1
    assert summary["partial_session_count"] == 1
    assert summary["partial_session_dates"] == ["2026-06-10"]


# ---------------------------------------------------------------------------
# Regression tests added 2026-07-07: session rolling, sell semantics, MFE/MAE
# ---------------------------------------------------------------------------

def _bar(ticker, ts, close, high=None, low=None):
    return MarketPrice(
        ticker=ticker, interval="5m", provider="polygon",
        timestamp=ts.replace(tzinfo=None),
        open=close, high=high if high is not None else close,
        low=low if low is not None else close,
        close=close, volume=1000, adjusted_close=close,
    )


def _full_session_bars(ticker, year, month, day, closes):
    """Bars from 13:30 UTC (9:30 ET) to 19:55 UTC (15:55 ET) with coverage."""
    bars = []
    times = [(13, 30), (15, 0), (17, 0), (19, 55)]
    for (hh, mm), spec in zip(times, closes):
        if isinstance(spec, tuple):
            close, high, low = spec
        else:
            close, high, low = spec, spec, spec
        bars.append(_bar(ticker, _utc(year, month, day, hh, mm), close, high, low))
    return bars


def _session_for(engine_url="sqlite:///:memory:"):
    engine = create_engine(engine_url, echo=False)
    Base.metadata.create_all(engine)
    return engine, Session(engine)


def test_saturday_signal_rolls_forward_to_monday_open():
    """The backtest now carries weekend signals forward to the next tradable
    session so we can score the catalyst rather than dropping it."""
    engine, session = _session_for()
    with session:
        sig = TradeSignal(ticker="NVDA", signal_side="buy", status="approved",
                          generated_at=_utc(2026, 6, 13, 14, 0))
        session.add(sig)
        for b in _full_session_bars("NVDA", 2026, 6, 15, [100.0, 101.0, 102.0, 103.0]):
            session.add(b)
        session.commit()

        result = compute_same_day_outcomes(session, _Cfg(), interval="5m")
        assert result["outcomes_expired_before_session"] == 0
        assert result["outcomes_ok"] == 1
        row = session.query(SameDaySignalOutcome).filter_by(trade_signal_id=sig.id).one()
        assert row.outcome_status == "ok"
        assert row.session_date == "2026-06-15"
        assert row.entry_price == 100.0
        assert row.exit_price == 103.0
    Base.metadata.drop_all(engine)


def test_sunday_evening_signal_rolls_to_monday_open():
    """Sun 2026-06-14 22:00 UTC signal: Monday 13:30 UTC open is within 24h,
    so the backtest enters at Monday's first bar -- like live execution."""
    engine, session = _session_for()
    with session:
        sig = TradeSignal(ticker="NVDA", signal_side="buy", status="approved",
                          generated_at=_utc(2026, 6, 14, 22, 0))
        session.add(sig)
        for b in _full_session_bars("NVDA", 2026, 6, 15, [100.0, 101.0, 102.0, 103.0]):
            session.add(b)
        session.commit()

        result = compute_same_day_outcomes(session, _Cfg(), interval="5m")
        assert result["outcomes_ok"] == 1
        row = session.query(SameDaySignalOutcome).filter_by(trade_signal_id=sig.id).one()
        assert row.outcome_status == "ok"
        assert row.session_date == "2026-06-15"
        assert row.entry_price == 100.0   # Monday's first bar close
        assert row.exit_price == 103.0
    Base.metadata.drop_all(engine)


def test_sell_signal_scored_as_exit_avoidance_with_one_way_cost():
    """Sell closes an existing long: gross = loss avoided (price fell 2%
    after exit), net = gross - one-way cost (0.05), not full round trip."""
    engine, session = _session_for()
    with session:
        sig = TradeSignal(ticker="NVDA", signal_side="sell", status="approved",
                          generated_at=_utc(2026, 6, 10, 14, 0))
        session.add(sig)
        for b in _full_session_bars("NVDA", 2026, 6, 10, [100.0, 99.5, 99.0, 98.0]):
            session.add(b)
        session.commit()

        result = compute_same_day_outcomes(session, _Cfg(), interval="5m")
        assert result["outcomes_ok"] == 1
        row = session.query(SameDaySignalOutcome).filter_by(trade_signal_id=sig.id).one()
        # entry bar is 15:00 UTC (first bar >= 14:00 generated_at) @ 99.5
        assert row.entry_price == 99.5
        expected_gross = -((98.0 / 99.5) - 1.0) * 100.0
        assert abs(row.gross_return_pct - expected_gross) < 1e-9
        assert abs(row.net_return_pct - (expected_gross - 0.05)) < 1e-9
    Base.metadata.drop_all(engine)


def test_mfe_mae_exclude_entry_bar_high_low():
    """Entry fills at the entry bar's close; that bar's own high/low predate
    the position and must not count toward MFE/MAE."""
    engine, session = _session_for()
    with session:
        sig = TradeSignal(ticker="NVDA", signal_side="buy", status="approved",
                          generated_at=_utc(2026, 6, 10, 13, 0))
        session.add(sig)
        # Entry bar has an extreme high/low that must be ignored.
        bars = _full_session_bars("NVDA", 2026, 6, 10, [
            (100.0, 150.0, 50.0),   # entry bar: fake spike high/low
            (101.0, 102.0, 100.5),
            (102.0, 103.0, 101.0),
            (103.0, 103.5, 102.5),
        ])
        for b in bars:
            session.add(b)
        session.commit()

        result = compute_same_day_outcomes(session, _Cfg(), interval="5m")
        assert result["outcomes_ok"] == 1
        row = session.query(SameDaySignalOutcome).filter_by(trade_signal_id=sig.id).one()
        assert row.entry_price == 100.0
        expected_mfe = ((103.5 / 100.0) - 1.0) * 100.0   # not 150-based
        expected_mae = ((100.5 / 100.0) - 1.0) * 100.0   # not 50-based
        assert abs(row.mfe_pct - expected_mfe) < 1e-9
        assert abs(row.mae_pct - expected_mae) < 1e-9
    Base.metadata.drop_all(engine)
