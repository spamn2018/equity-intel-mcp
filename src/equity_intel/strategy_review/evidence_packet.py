"""
Deterministic evidence-packet builder for the same-day strategy-review loop.

Reads same_day_signal_outcomes (populated by
workers/backtest_same_day_signals.py) and produces the compact,
JSON-serializable packet defined in same_day_evidence_packet_spec.md.

Pure aggregation. No model calls, no network calls, no side effects on the
database -- safe to call at any time, as often as needed.
"""
from __future__ import annotations

import datetime
import statistics
from typing import Any, Dict, List, Optional

from equity_intel.db.models import SameDaySignalOutcome

_DEFAULT_WINDOW_SESSIONS = 20
_MIN_N_FOR_FAILURE_PATTERN = 3
_TOP_N_SUMMARY_ROWS = 10
_TOP_N_WINNERS_LOSERS = 5
_FAILURE_PATTERN_WIN_RATE_MARGIN = 0.15
# Mirrors the global gate in hypothesis_and_critique_spec.md Section 3.
GLOBAL_MIN_SAMPLE_FOR_HYPOTHESIS_GENERATION = 27


def _win_rate(vals: List[float]) -> float:
    return sum(1 for x in vals if x > 0) / len(vals)


def build_same_day_evidence_packet(
    session, window_sessions: int = _DEFAULT_WINDOW_SESSIONS
) -> Dict[str, Any]:
    """
    Build the same-day evidence packet from real same_day_signal_outcomes rows.

    Every number in the returned dict traces back to a real column value --
    nothing here is synthesized or assumed.
    """
    all_rows = session.query(SameDaySignalOutcome).all()

    all_session_dates = sorted({r.session_date for r in all_rows if r.session_date}, reverse=True)
    window_dates = set(all_session_dates[:window_sessions])
    window_rows = [r for r in all_rows if r.session_date in window_dates]
    ok_rows = [r for r in window_rows if r.outcome_status == "ok"]

    status_counts: Dict[str, int] = {}
    for r in window_rows:
        status_counts[r.outcome_status] = status_counts.get(r.outcome_status, 0) + 1

    # Best-effort split of no_intraday_data rows into "weekend, no session
    # ever existed" (a simple, cheap weekday check -- covers Saturday/Sunday
    # but not exchange holidays, since there's no market-calendar lookup
    # here yet) vs "other missing data" (could be a genuine provider gap or
    # data that just hasn't been synced yet -- this script cannot tell those
    # two apart without a per-row marker, which is a bigger schema change
    # deliberately deferred; see same_day_evidence_packet_spec.md).
    no_data_rows = [r for r in window_rows if r.outcome_status == "no_intraday_data"]
    weekend_no_session = 0
    other_missing_data = 0
    for r in no_data_rows:
        try:
            is_weekend = datetime.date.fromisoformat(r.session_date).weekday() >= 5
        except (TypeError, ValueError):
            is_weekend = False
        if is_weekend:
            weekend_no_session += 1
        else:
            other_missing_data += 1

    packet: Dict[str, Any] = {
        "window": {
            "start_session_date": min(window_dates) if window_dates else None,
            "end_session_date": max(window_dates) if window_dates else None,
            "sessions_with_data": len(window_dates),
            "sessions_requested": window_sessions,
        },
        "signal_counts": {
            "total_directional_signals_in_window": len(window_rows),
            "by_outcome_status": status_counts,
            "buy": sum(1 for r in window_rows if r.signal_side == "buy"),
            "sell": sum(1 for r in window_rows if r.signal_side == "sell"),
            "no_intraday_data_breakdown": {
                "weekend_no_session": weekend_no_session,
                "other_missing_data": other_missing_data,
            } if no_data_rows else None,
        },
    }

    rets = [r.net_return_pct for r in ok_rows if r.net_return_pct is not None]
    mfes = [r.mfe_pct for r in ok_rows if r.mfe_pct is not None]
    maes = [r.mae_pct for r in ok_rows if r.mae_pct is not None]

    packet["overall"] = {
        "n": len(rets),
        "win_rate": _win_rate(rets) if rets else None,
        "avg_net_return_pct": statistics.mean(rets) if rets else None,
        "median_net_return_pct": statistics.median(rets) if rets else None,
        "return_stddev_pct": statistics.pstdev(rets) if len(rets) > 1 else (0.0 if rets else None),
        "avg_mfe_pct": statistics.mean(mfes) if mfes else None,
        "avg_mae_pct": statistics.mean(maes) if maes else None,
    }

    buckets: Dict[str, List[float]] = {}
    for r in ok_rows:
        buckets.setdefault(r.entry_time_of_day_bucket or "unknown", []).append(r.net_return_pct)
    packet["time_of_day_buckets"] = [
        {"bucket": b, "n": len(vals), "win_rate": _win_rate(vals), "avg_net_return_pct": statistics.mean(vals)}
        for b, vals in sorted(buckets.items())
    ]

    ticker_groups: Dict[str, List[float]] = {}
    for r in ok_rows:
        ticker_groups.setdefault(r.ticker, []).append(r.net_return_pct)
    ticker_summary = [
        {"ticker": t, "n": len(vals), "win_rate": _win_rate(vals), "avg_net_return_pct": statistics.mean(vals)}
        for t, vals in ticker_groups.items()
    ]
    ticker_summary.sort(key=lambda d: abs(d["avg_net_return_pct"]), reverse=True)
    packet["ticker_summary"] = ticker_summary[:_TOP_N_SUMMARY_ROWS]

    event_groups: Dict[str, List[float]] = {}
    for r in ok_rows:
        if r.event_type:
            event_groups.setdefault(r.event_type, []).append(r.net_return_pct)
    event_summary = [
        {"event_type": e, "n": len(vals), "win_rate": _win_rate(vals), "avg_net_return_pct": statistics.mean(vals)}
        for e, vals in event_groups.items()
    ]
    event_summary.sort(key=lambda d: abs(d["avg_net_return_pct"]), reverse=True)
    packet["event_type_summary"] = event_summary[:_TOP_N_SUMMARY_ROWS]

    mat_buckets = []
    for lo, hi in [(0.0, 0.4), (0.4, 0.6), (0.6, 1.01)]:
        vals = [
            r.net_return_pct for r in ok_rows
            if r.materiality_score is not None and lo <= r.materiality_score < hi
        ]
        if vals:
            mat_buckets.append({
                "bucket_label": f"[{lo:.1f}-{hi:.1f})",
                "n": len(vals),
                "win_rate": _win_rate(vals),
                "avg_net_return_pct": statistics.mean(vals),
            })
    packet["materiality_confidence_buckets"] = mat_buckets

    exceed_2x = sum(
        1 for r in ok_rows
        if r.mfe_pct is not None and r.net_return_pct is not None
        and r.mfe_pct > 0 and r.net_return_pct <= r.mfe_pct and r.mfe_pct >= 2 * abs(r.net_return_pct)
    )
    packet["mfe_mae_summary"] = {
        "avg_mfe_pct": statistics.mean(mfes) if mfes else None,
        "avg_mae_pct": statistics.mean(maes) if maes else None,
        "pct_of_trades_with_mfe_exceeding_final_return_by_2x": (exceed_2x / len(ok_rows)) if ok_rows else None,
    }

    def _row_to_dict(r) -> Dict[str, Any]:
        return {
            "trade_signal_id": r.trade_signal_id,
            "ticker": r.ticker,
            "session_date": r.session_date,
            "event_type": r.event_type,
            "net_return_pct": r.net_return_pct,
            "entry_time_of_day_bucket": r.entry_time_of_day_bucket,
        }

    ranked = sorted(ok_rows, key=lambda r: r.net_return_pct if r.net_return_pct is not None else 0.0)
    packet["worst_losers"] = [_row_to_dict(r) for r in ranked[:_TOP_N_WINNERS_LOSERS]]
    packet["strongest_winners"] = [_row_to_dict(r) for r in list(reversed(ranked))[:_TOP_N_WINNERS_LOSERS]]

    overall_win_rate = packet["overall"]["win_rate"] or 0.0
    pattern_groups: Dict[Any, List[float]] = {}
    for r in ok_rows:
        key = (r.event_type or "unknown", r.entry_time_of_day_bucket or "unknown")
        pattern_groups.setdefault(key, []).append(r.net_return_pct)
    patterns = []
    for (event_type, bucket), vals in pattern_groups.items():
        if len(vals) < _MIN_N_FOR_FAILURE_PATTERN:
            continue
        wr = _win_rate(vals)
        if wr <= overall_win_rate - _FAILURE_PATTERN_WIN_RATE_MARGIN:
            patterns.append({
                "description": f"{event_type} signals entered '{bucket}': {sum(1 for x in vals if x > 0)}/{len(vals)} winners",
                "n": len(vals),
                "win_rate": wr,
                "avg_net_return_pct": statistics.mean(vals),
            })
    packet["repeated_failure_patterns"] = patterns[:5]

    caveats: List[str] = []
    no_data_count = status_counts.get("no_intraday_data", 0)
    if no_data_count:
        caveats.append(
            f"{no_data_count} of {len(window_rows)} directional signals in this window have "
            f"outcome_status=no_intraday_data -- either intraday price data has not been synced for those "
            f"sessions yet, or (for the most recent session) the price provider had not backfilled that day's "
            f"intraday bars at compute time. These two cases are not currently distinguished. Do not read this "
            f"count as 'the strategy did not fire.'"
        )
    coverage_gap_count = status_counts.get("intraday_coverage_gap", 0)
    if coverage_gap_count:
        caveats.append(
            f"{coverage_gap_count} directional signal(s) have outcome_status=intraday_coverage_gap -- "
            f"some intraday rows existed for those sessions, but the regular-session bar set did not reach "
            f"the close cutoff cleanly enough to score a trustworthy same-day exit. Treat this as a data "
            f"quality blocker for strategy tuning, not as a trading result."
        )
    expired_count = status_counts.get("expired_before_session", 0)
    if expired_count:
        caveats.append(
            f"{expired_count} signal(s) had no tradable session inside the execution retry window "
            f"(weekend/holiday/after-hours generation) and were excluded from same-day scoring -- "
            f"live execution would have expired them unfilled."
        )
    after_hours_count = status_counts.get("after_hours_no_entry", 0)
    if after_hours_count:
        caveats.append(
            f"{after_hours_count} signal(s) were generated after the same-day entry window closed and were "
            f"correctly excluded from same-day scoring, not scored against a substitute price."
        )
    halt_flagged = sum(1 for r in ok_rows if r.flag == "possible_halt_or_gap")
    if halt_flagged:
        caveats.append(
            f"{halt_flagged} of {len(ok_rows)} resolved outcomes are flagged possible_halt_or_gap "
            f"(a >15 minute gap between consecutive intraday bars) -- treat as lower-confidence data points."
        )
    if len(ok_rows) < GLOBAL_MIN_SAMPLE_FOR_HYPOTHESIS_GENERATION:
        caveats.append(
            f"Only {len(ok_rows)} resolved outcomes exist in this window -- below the "
            f"{GLOBAL_MIN_SAMPLE_FOR_HYPOTHESIS_GENERATION}-outcome global minimum sample threshold for any "
            f"hypothesis-generation pass. This packet should not be used to generate or act on strategy-change "
            f"proposals yet."
        )
    caveats.append(
        "Signals in this window may include news-only catalysts without a primary SEC filing -- source-type "
        "gating was intentionally removed from signal generation (see trading/signals.py); this packet does "
        "not distinguish signal provenance."
    )
    packet["caveats"] = caveats

    return packet
