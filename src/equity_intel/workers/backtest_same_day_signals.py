"""
Worker: same-day (intraday entry -> 3:55pm ET close-out) signal backtest.

This is the day-trading-shaped measurement the project actually needs --
distinct from workers/backtest_signals.py, which scores multi-day (1/5/10
"trading day") forward returns and is left completely untouched by this
worker. The two tables (signal_outcomes vs same_day_signal_outcomes) are
never read together as if they measured the same thing.

For every directional (buy/sell) TradeSignal, this worker:
  1. Resolves the ET trading session the signal was generated on.
  2. Restricts market_prices to that ticker, an explicit interval (default
     "5m"), and the regular-session window 9:30 AM - close_cutoff ET on
     that session date only -- pre-market and after-hours bars are
     deliberately excluded, even though Polygon's aggregates endpoint
     returns them (confirmed via a real sync in this project: 5m bars span
     04:00-19:55 ET, not just 09:30-16:00).
  3. Rolls to the next session when needed: weekend, holiday, and
     after-cutoff signals enter at the next session's open IF that open
     falls inside the execution retry window (the same 24h constant
     execution.py uses); otherwise the outcome is expired_before_session,
     matching what live execution would have done with the signal.
     Entry = earliest in-window bar with timestamp >= max(generated_at,
     session open).
  4. Exit = latest in-window bar (i.e. the close_cutoff-side bar) --
     reuses close_day_trade_positions.py's exact close-time setting
     (trading_day_trade_close_time_et, default "15:55") and the same
     zoneinfo("America/New_York") approach, rather than inventing a new
     timezone scheme.
  5. Writes one upserted row per trade_signal_id to same_day_signal_outcomes,
     with an explicit outcome_status for every way this can fail to resolve
     (no_intraday_data, intraday_coverage_gap, expired_before_session)
     instead of silently dropping
     or substituting a proxy price.

Idempotent: safe to rerun. Unlike backtest_signals.py (which skips a
(signal, horizon) pair once computed), this worker recomputes and
overwrites the existing row for a trade_signal_id every run, since a
same-day session is always already "mature" once it's run after that
day's close -- there's no reason to freeze a stale status_at_eval the way
the multi-day worker does.

Usage
-----
equity-backtest-same-day-signals                          # compute outcomes for all directional signals
equity-backtest-same-day-signals --tickers AMD,ORCL        # restrict to specific tickers
equity-backtest-same-day-signals --interval 1m             # use 1-minute bars instead of 5-minute
equity-backtest-same-day-signals --limit 50                # cap signals processed this run
equity-backtest-same-day-signals --report                  # print the efficacy report only
"""
from __future__ import annotations

import datetime
import statistics
from typing import Any, Dict, List, Optional

import click
import requests
from sqlalchemy import and_

from equity_intel.config import settings as _default_settings
from equity_intel.db.models import MarketPrice, SameDaySignalOutcome, TradeSignal
# Same 24h retry window execution.py grants a signal before expiring it --
# imported (not duplicated) so backtest and live execution can't drift apart.
from equity_intel.trading.execution import _RETRY_WINDOW_HOURS as _EXECUTION_RETRY_WINDOW_HOURS
from equity_intel.db.session import SessionLocal
from equity_intel.logging_config import configure_logging, get_logger

logger = get_logger(__name__)

_DIRECTIONAL_SIDES = ("buy", "sell")
_DEFAULT_INTERVAL = "5m"
_SUPPORTED_INTERVALS = ("1m", "5m")
_INTERVAL_MINUTES = {"1m": 1, "5m": 5}
_MARKET_OPEN_ET = (9, 30)
_HALT_GAP_MINUTES = 15.0
# Deterministic, documented placeholder assumption -- not a settings field,
# kept as a simple constant per "keep the scope tight." Revisit if/when the
# project wants per-ticker or per-broker-fee-tier slippage modeling.
_ASSUMED_ROUND_TRIP_COST_PCT = 0.10


def _zoneinfo_et():
    """Same stdlib-only zoneinfo approach as close_day_trade_positions.py."""
    from zoneinfo import ZoneInfo
    return ZoneInfo("America/New_York")


def _close_cutoff_hh_mm(cfg) -> tuple:
    """Reuses trading_day_trade_close_time_et (default '15:55') -- the same
    setting close_day_trade_positions.py gates its EOD liquidation on."""
    try:
        hh, mm = (int(x) for x in cfg.trading_day_trade_close_time_et.split(":"))
        return hh, mm
    except Exception:
        return 15, 55


def _session_window_utc_for_date(session_date: datetime.date, cfg) -> tuple:
    """Return (session_date_label, window_open_utc, window_close_utc) for one
    ET calendar date: tz-aware UTC datetimes bounding 9:30 ET to the
    configured close-cutoff ET on that date."""
    et = _zoneinfo_et()
    open_hh, open_mm = _MARKET_OPEN_ET
    close_hh, close_mm = _close_cutoff_hh_mm(cfg)
    window_open_et = datetime.datetime(
        session_date.year, session_date.month, session_date.day,
        open_hh, open_mm, 0, tzinfo=et,
    )
    window_close_et = datetime.datetime(
        session_date.year, session_date.month, session_date.day,
        close_hh, close_mm, 0, tzinfo=et,
    )
    return (
        session_date.isoformat(),
        window_open_et.astimezone(datetime.timezone.utc),
        window_close_et.astimezone(datetime.timezone.utc),
    )


def _resolve_entry_session(db, ticker: str, interval: str,
                           generated_at_utc: datetime.datetime, cfg,
                           carry_forward: bool = True) -> tuple:
    """Find the session this signal would actually have traded in.

    When carry_forward=True (the backtest default), signals are always
    carried to the next available trading session open -- the 24h execution
    retry window is NOT applied. A signal triggered by a filing or news item
    is still valid at the next open regardless of how long the gap is (e.g.
    Friday after-close signals trade at Monday open). Set carry_forward=False
    to mirror live execution behavior (signals expire after 24h).

    Returns (status, session_date_label, window_open_utc, window_close_utc,
    bars). status is None when a tradable session with bars was found;
    otherwise "no_intraday_data" (a plausible session existed inside the
    window but had no bars synced) or "expired_before_session" (no session
    opened inside the retry window at all).
    """
    et = _zoneinfo_et()
    retry_deadline_utc = generated_at_utc + datetime.timedelta(hours=_EXECUTION_RETRY_WINDOW_HOURS)
    candidate_date = generated_at_utc.astimezone(et).date()
    saw_empty_session = False
    for _ in range(7):  # never scan more than a week of candidate dates
        if candidate_date.weekday() < 5:
            session_date, window_open_utc, window_close_utc = _session_window_utc_for_date(candidate_date, cfg)
            if window_close_utc > generated_at_utc:  # session not already over at signal time
                if not carry_forward and window_open_utc > retry_deadline_utc:
                    break  # signal would have expired before this session opened
                # Strip tzinfo before the SQLAlchemy query: SQLite stores
                # timestamps as naive strings, and a tz-aware datetime
                # serialises as '...+00:00' which sorts lexicographically
                # after all naive values, making the >= filter return nothing.
                _q_open = window_open_utc.replace(tzinfo=None) if window_open_utc.tzinfo else window_open_utc
                _q_close = window_close_utc.replace(tzinfo=None) if window_close_utc.tzinfo else window_close_utc
                bars = (
                    db.query(MarketPrice)
                    .filter(
                        MarketPrice.ticker == ticker,
                        MarketPrice.interval == interval,
                        MarketPrice.timestamp >= _q_open,
                        MarketPrice.timestamp <= _q_close,
                    )
                    .order_by(MarketPrice.timestamp.asc())
                    .all()
                )
                if bars:
                    return None, session_date, window_open_utc, window_close_utc, bars
                saw_empty_session = True
        candidate_date = candidate_date + datetime.timedelta(days=1)
    status = "no_intraday_data" if saw_empty_session else "expired_before_session"
    return status, generated_at_utc.astimezone(et).date().isoformat(), None, None, []


def _time_of_day_bucket(entry_utc: datetime.datetime, cfg) -> str:
    et = _zoneinfo_et()
    # MarketPrice.timestamp comes back from sqlite as naive (even though the
    # column is declared DateTime(timezone=True) -- confirmed by a real run:
    # SQLAlchemy/sqlite does not round-trip tzinfo). It is always semantically
    # UTC (that's what every provider writes), so attach UTC explicitly before
    # converting to ET rather than calling astimezone() on a naive value,
    # which would wrongly assume local system time.
    if entry_utc.tzinfo is None:
        entry_utc = entry_utc.replace(tzinfo=datetime.timezone.utc)
    entry_et = entry_utc.astimezone(et)
    open_hh, open_mm = _MARKET_OPEN_ET
    close_hh, close_mm = _close_cutoff_hh_mm(cfg)
    minutes_since_open = (entry_et.hour * 60 + entry_et.minute) - (open_hh * 60 + open_mm)
    total_window_minutes = (close_hh * 60 + close_mm) - (open_hh * 60 + open_mm)
    if minutes_since_open <= 60:
        return "open"
    if minutes_since_open >= total_window_minutes - 60:
        return "late"
    return "mid"


def _max_bar_gap_minutes(bars: List[Any]) -> float:
    if len(bars) < 2:
        return 0.0
    gaps = [
        (bars[i + 1].timestamp - bars[i].timestamp).total_seconds() / 60.0
        for i in range(len(bars) - 1)
    ]
    return max(gaps) if gaps else 0.0


def _has_close_cutoff_coverage(
    bars: List[Any],
    *,
    window_close_utc: datetime.datetime,
    interval: str,
) -> bool:
    if not bars:
        return False
    tolerance = datetime.timedelta(minutes=_INTERVAL_MINUTES[interval])
    last_timestamp = bars[-1].timestamp
    close_naive = window_close_utc.replace(tzinfo=None) if window_close_utc.tzinfo else window_close_utc
    return last_timestamp >= close_naive - tolerance


def compute_same_day_outcomes(
    session,
    cfg=None,
    *,
    interval: str = _DEFAULT_INTERVAL,
    tickers: Optional[List[str]] = None,
    limit: Optional[int] = None,
) -> Dict[str, int]:
    """
    Compute and upsert same_day_signal_outcomes for directional signals.

    Returns a summary dict: {signals_considered, outcomes_ok,
    outcomes_no_intraday_data, outcomes_intraday_coverage_gap,
    outcomes_expired_before_session, outcomes_written}.
    """
    cfg = cfg or _default_settings
    if interval not in _SUPPORTED_INTERVALS:
        raise ValueError(f"Unsupported interval {interval!r}. Supported: {_SUPPORTED_INTERVALS}")

    summary = {
        "signals_considered": 0,
        "outcomes_ok": 0,
        "outcomes_no_intraday_data": 0,
        "outcomes_intraday_coverage_gap": 0,
        "outcomes_expired_before_session": 0,
        "outcomes_written": 0,
    }

    q = session.query(TradeSignal).filter(TradeSignal.signal_side.in_(_DIRECTIONAL_SIDES))
    if tickers:
        q = q.filter(TradeSignal.ticker.in_([t.upper() for t in tickers]))
    if limit:
        q = q.limit(limit)
    signals = q.all()

    existing_by_signal_id = {
        row.trade_signal_id: row
        for row in session.query(SameDaySignalOutcome).all()
    }

    for signal in signals:
        summary["signals_considered"] += 1
        generated_at = signal.generated_at
        if generated_at is None:
            continue
        if generated_at.tzinfo is None:
            generated_at = generated_at.replace(tzinfo=datetime.timezone.utc)

        (resolve_status, session_date, window_open_utc,
         window_close_utc, window_bars) = _resolve_entry_session(
            session, signal.ticker, interval, generated_at, cfg,
        )

        outcome_kwargs: Dict[str, Any] = dict(
            trade_signal_id=signal.id,
            ticker=signal.ticker,
            signal_side=signal.signal_side,
            session_date=session_date,
            interval_used=interval,
            status_at_eval=signal.status,
            event_type=signal.event_type,
            materiality_score=signal.materiality_score,
            confidence_score=signal.confidence_score,
            research_stage=signal.research_stage,
        )

        if resolve_status is not None:
            outcome_kwargs["outcome_status"] = resolve_status
            if resolve_status == "no_intraday_data":
                summary["outcomes_no_intraday_data"] += 1
            else:
                summary["outcomes_expired_before_session"] += 1
            _upsert_outcome(session, existing_by_signal_id, outcome_kwargs)
            summary["outcomes_written"] += 1
            continue

        if not _has_close_cutoff_coverage(
            window_bars,
            window_close_utc=window_close_utc,
            interval=interval,
        ):
            outcome_kwargs["outcome_status"] = "intraday_coverage_gap"
            summary["outcomes_intraday_coverage_gap"] += 1
            _upsert_outcome(session, existing_by_signal_id, outcome_kwargs)
            summary["outcomes_written"] += 1
            continue

        # window_bars come back from sqlite as naive datetimes (semantically
        # UTC) even though generated_at is tz-aware -- compare naive-to-naive
        # rather than mixing aware/naive (confirmed crash on a real run
        # otherwise: "can't compare offset-naive and offset-aware datetimes").
        # Entry cutoff = max(signal time, session open): for rolled sessions
        # (weekend/holiday/after-cutoff signals) the signal predates the open,
        # so entry is the first bar of the session.
        generated_at_naive = generated_at.replace(tzinfo=None) if generated_at.tzinfo else generated_at
        window_open_naive = window_open_utc.replace(tzinfo=None) if window_open_utc.tzinfo else window_open_utc
        entry_cutoff = max(generated_at_naive, window_open_naive)
        entry_candidates = [b for b in window_bars if b.timestamp >= entry_cutoff]
        if not entry_candidates:
            # Bars exist for the session but none at/after the entry cutoff:
            # the synced data ends before the entry point. A data problem,
            # never a trading result.
            outcome_kwargs["outcome_status"] = "intraday_coverage_gap"
            summary["outcomes_intraday_coverage_gap"] += 1
            _upsert_outcome(session, existing_by_signal_id, outcome_kwargs)
            summary["outcomes_written"] += 1
            continue

        entry_row = entry_candidates[0]
        exit_row = window_bars[-1]
        path_bars = [b for b in window_bars if entry_row.timestamp <= b.timestamp <= exit_row.timestamp]

        entry_price = entry_row.close
        exit_price = exit_row.close
        if not entry_price or not exit_price:
            outcome_kwargs["outcome_status"] = "intraday_coverage_gap"
            summary["outcomes_intraday_coverage_gap"] += 1
            _upsert_outcome(session, existing_by_signal_id, outcome_kwargs)
            summary["outcomes_written"] += 1
            continue

        raw_return = (exit_price / entry_price - 1.0) * 100.0
        is_sell = signal.signal_side == "sell"
        gross_return_pct = -raw_return if is_sell else raw_return
        # "sell" signals close an existing long -- this system never shorts.
        # gross_return_pct for a sell therefore measures the further loss
        # AVOIDED by exiting at the entry bar instead of holding to the
        # close, and only a one-way transaction cost is attributable to the
        # signal (the position's round trip was already paid by its buy).
        cost_pct = (_ASSUMED_ROUND_TRIP_COST_PCT / 2.0) if is_sell else _ASSUMED_ROUND_TRIP_COST_PCT
        net_return_pct = gross_return_pct - cost_pct

        # Entry happens at the entry bar's CLOSE, so that bar's own high/low
        # occurred before the position existed -- exclude it from MFE/MAE.
        post_entry_bars = [b for b in path_bars if b.timestamp > entry_row.timestamp]
        highs = [b.high for b in post_entry_bars if b.high is not None]
        lows = [b.low for b in post_entry_bars if b.low is not None]
        raw_max_pct = ((max(highs) / entry_price) - 1.0) * 100.0 if highs else 0.0
        raw_min_pct = ((min(lows) / entry_price) - 1.0) * 100.0 if lows else 0.0
        if is_sell:
            mfe_pct = -raw_min_pct
            mae_pct = -raw_max_pct
        else:
            mfe_pct = raw_max_pct
            mae_pct = raw_min_pct

        win_loss = "win" if net_return_pct > 0 else ("loss" if net_return_pct < 0 else "flat")
        gap_minutes = _max_bar_gap_minutes(path_bars)

        outcome_kwargs.update(
            entry_timestamp=entry_row.timestamp,
            entry_price=entry_price,
            exit_timestamp=exit_row.timestamp,
            exit_price=exit_price,
            gross_return_pct=gross_return_pct,
            net_return_pct=net_return_pct,
            mfe_pct=mfe_pct,
            mae_pct=mae_pct,
            win_loss=win_loss,
            entry_time_of_day_bucket=_time_of_day_bucket(entry_row.timestamp, cfg),
            outcome_status="ok",
            flag="possible_halt_or_gap" if gap_minutes > _HALT_GAP_MINUTES else None,
        )
        summary["outcomes_ok"] += 1
        _upsert_outcome(session, existing_by_signal_id, outcome_kwargs)
        summary["outcomes_written"] += 1

    session.flush()
    logger.info("backtest_same_day_signals_run_complete", **summary)
    return summary


def _upsert_outcome(session, existing_by_signal_id: Dict[int, Any], kwargs: Dict[str, Any]) -> None:
    existing = existing_by_signal_id.get(kwargs["trade_signal_id"])
    if existing:
        for k, v in kwargs.items():
            setattr(existing, k, v)
        existing.computed_at = datetime.datetime.now(datetime.timezone.utc)
    else:
        row = SameDaySignalOutcome(**kwargs)
        session.add(row)
        existing_by_signal_id[kwargs["trade_signal_id"]] = row



def _fetch_spy_benchmark(session_dates):
    """Fetch SPY daily closes over the signal date range from Polygon and
    compute the buy-and-hold return as an S&P 500 proxy benchmark."""
    if not session_dates:
        return None
    try:
        from equity_intel.config import settings as cfg
        api_key = cfg.polygon_api_key
        if not api_key:
            return None
        min_date = min(session_dates)
        max_date = max(session_dates)
        url = (
            f"https://api.polygon.io/v2/aggs/ticker/SPY/range/1/day/"
            f"{min_date}/{max_date}"
        )
        resp = requests.get(
            url,
            params={"adjusted": "true", "sort": "asc", "apiKey": api_key},
            timeout=10,
        )
        data = resp.json()
        results = data.get("results", [])
        if len(results) < 2:
            return None
        first_close = results[0]["c"]
        last_close = results[-1]["c"]
        import datetime as _dt
        first_dt = _dt.datetime.fromtimestamp(results[0]["t"] / 1000).strftime("%Y-%m-%d")
        last_dt = _dt.datetime.fromtimestamp(results[-1]["t"] / 1000).strftime("%Y-%m-%d")
        pct = (last_close - first_close) / first_close * 100
        return {
            "start_date": first_dt,
            "end_date": last_dt,
            "start_price": first_close,
            "end_price": last_close,
            "return_pct": pct,
            "trading_days": len(results),
        }
    except Exception:
        return None


def _print_report(session) -> None:
    rows = session.query(SameDaySignalOutcome).all()
    if not rows:
        click.echo("\n  No same_day_signal_outcomes rows yet -- run without --report first.")
        return

    by_status: Dict[str, List[Any]] = {}
    for r in rows:
        by_status.setdefault(r.outcome_status, []).append(r)

    click.echo(f"\n  === Same-day backtest report -- n={len(rows)} total ===")
    for status, group in sorted(by_status.items()):
        click.echo(f"    {status:<22} n={len(group)}")

    ok_rows = by_status.get("ok", [])
    # Buys and sells are reported separately because they measure different
    # things: buy = long P&L; sell = further loss avoided by closing an
    # existing long (this system never shorts). Averaging them together
    # would mix units of meaning.
    side_labels = (
        ("buy", "long P&L"),
        ("sell", "exit avoidance -- closes longs, NOT short P&L"),
    )
    for side, label in side_labels:
        side_rows = [r for r in ok_rows if r.signal_side == side]
        rets = [r.net_return_pct for r in side_rows if r.net_return_pct is not None]
        if not rets:
            continue
        click.echo(f"\n  === Resolved 'ok' {side} outcomes ({label}) -- n={len(rets)} ===")
        click.echo(f"    Avg net return  : {statistics.mean(rets):+.3f}%")
        click.echo(f"    Median net return: {statistics.median(rets):+.3f}%")
        win = sum(1 for x in rets if x > 0)
        click.echo(f"    Win rate        : {win}/{len(rets)} = {win/len(rets)*100:.1f}%")
        click.echo("    -- example rows --")
        for r in side_rows[:5]:
            click.echo(
                f"      signal={r.trade_signal_id} {r.ticker} {r.signal_side} "
                f"session={r.session_date} entry={r.entry_timestamp}@{r.entry_price} "
                f"exit={r.exit_timestamp}@{r.exit_price} net_ret={r.net_return_pct:+.3f}% "
                f"{r.win_loss} flag={r.flag}"
            )

    # S&P 500 benchmark comparison (SPY as proxy)
    session_dates = [r.session_date for r in ok_rows if r.session_date is not None]
    bench = _fetch_spy_benchmark(session_dates)
    if bench:
        click.echo(f"\n  === S&P 500 benchmark (SPY proxy) ===")
        click.echo(f"    Period          : {bench['start_date']} -> {bench['end_date']} ({bench['trading_days']} trading days)")
        click.echo(f"    SPY             : ${bench['start_price']:.2f} -> ${bench['end_price']:.2f}")
        click.echo(f"    Buy-and-hold    : {bench['return_pct']:+.3f}%")
        buy_rets = [r.net_return_pct for r in ok_rows if r.signal_side == "buy" and r.net_return_pct is not None]
        if buy_rets:
            avg_buy = statistics.mean(buy_rets)
            click.echo(f"    Signal avg (buy): {avg_buy:+.3f}%")
            click.echo(f"    Alpha per trade : {avg_buy - bench['return_pct']:+.3f}%")
    else:
        click.echo("\n  (S&P 500 benchmark unavailable -- no POLYGON_API_KEY or network error)")


@click.command("equity-backtest-same-day-signals")
@click.option("--tickers", default=None, help="Comma-separated tickers to restrict to.")
@click.option("--interval", default=_DEFAULT_INTERVAL, show_default=True,
              type=click.Choice(_SUPPORTED_INTERVALS), help="Intraday bar interval to resolve entry/exit from.")
@click.option("--limit", default=None, type=int, help="Cap on number of signals scanned this run.")
@click.option("--report", is_flag=True, default=False, help="Print the efficacy report instead of computing.")
@click.option("--log-level", default="info", show_default=True, help="Logging level.")
def main(tickers: Optional[str], interval: str, limit: Optional[int], report: bool, log_level: str) -> None:
    """Same-day (intraday entry -> 3:55pm ET close-out) signal backtest."""
    configure_logging(log_level)
    session = SessionLocal()
    try:
        if report:
            _print_report(session)
            return

        ticker_list = [t.strip().upper() for t in tickers.split(",")] if tickers else None
        result = compute_same_day_outcomes(session, _default_settings, interval=interval, tickers=ticker_list, limit=limit)
        session.commit()

        click.echo(
            f"\n  Same-day backtest complete.\n"
            f"    Signals considered           : {result['signals_considered']}\n"
            f"    Outcomes resolved (ok)       : {result['outcomes_ok']}\n"
            f"    No intraday data             : {result['outcomes_no_intraday_data']}\n"
            f"    Intraday coverage gaps       : {result['outcomes_intraday_coverage_gap']}\n"
            f"    Expired before session       : {result['outcomes_expired_before_session']}\n"
            f"    Total rows written/updated   : {result['outcomes_written']}\n"
            f"\n  Run with --report to see the efficacy breakdown."
        )
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


if __name__ == "__main__":
    main()
