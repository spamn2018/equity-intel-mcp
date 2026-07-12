"""
Worker: generate trade signals from watchlist catalyst brief.

Reads the pipeline's watchlist brief and converts qualifying catalysts
into TradeSignal records.  Never submits broker orders.

Usage
-----
# Dry-run: show what would be generated without writing to the database
equity-generate-trade-signals --days 7 --dry-run

# Live run: persist signals
equity-generate-trade-signals --days 7

# Override thresholds
equity-generate-trade-signals --days 14 --min-materiality 0.5 --min-confidence 0.4

# Specific tickers
equity-generate-trade-signals --tickers NVDA,AMAT --days 7

Signal status after generation: generated
To execute, first approve signals (set status=approved) then run:
  equity-execute-trade-signals
"""
from __future__ import annotations

import datetime
import json
import sys
from typing import List, Optional

import click

from equity_intel.briefs.watchlist import get_watchlist_brief
from equity_intel.config import settings
from equity_intel.db.session import SessionLocal
from equity_intel.logging_config import configure_logging, get_logger
from equity_intel.trading.signals import generate_trade_signals_from_brief
from equity_intel.trading.strategy_policy import get_signal_policy_block
from equity_intel.workers.backtest_same_day_signals import compute_same_day_outcomes
from equity_intel.workers.review_same_day_strategy import run_review_workflow

logger = get_logger(__name__)

ADVICE_DISCLAIMER = (
    "Trade signals are research workflow output — not investment advice. "
    "All signals require human review before execution. "
    "Verify with primary sources before making any decisions."
)


def _maybe_refresh_strategy_review(session, cfg, *, dry_run: bool) -> Optional[dict]:
    if dry_run or not getattr(cfg, "strategy_review_run_before_signal_generation_enabled", False):
        return None

    interval = getattr(cfg, "strategy_review_backtest_interval", "5m")
    window_sessions = getattr(cfg, "strategy_review_window_sessions", 20)
    output_dir = getattr(cfg, "strategy_review_artifact_output_dir", "strategy_review_artifacts")

    backtest_summary = compute_same_day_outcomes(session, cfg, interval=interval)
    session.commit()
    review_result, artifact_path, markdown_path = run_review_workflow(
        session,
        window_sessions=window_sessions,
        output_dir=output_dir,
        cfg=cfg,
    )
    return {
        "backtest_summary": backtest_summary,
        "review_status": review_result.get("status"),
        "artifact_path": str(artifact_path),
        "markdown_path": str(markdown_path),
        "survived": len(review_result.get("survived", [])),
        "auto_apply": review_result.get("auto_apply"),
    }


def run(
    tickers: List[str],
    days: int,
    min_materiality: float,
    min_confidence: float,
    min_signal_strength: float,
    dry_run: bool = False,
    cfg=None,
) -> dict:
    """
    Generate trade signals from watchlist brief. Returns a summary dict.

    If dry_run=True, builds the brief and scores signals but does NOT
    write anything to the database.
    """
    cfg = cfg or settings

    session = SessionLocal()
    try:
        strategy_review_summary = _maybe_refresh_strategy_review(session, cfg, dry_run=dry_run)
        brief = get_watchlist_brief(
            session=session,
            tickers=tickers,
            days=days,
            min_materiality=0.0,   # fetch all — signal generator applies its own threshold
            max_items=200,
            include_price_context=True,
            include_news=True,
            include_filings=True,
        )

        if dry_run:
            # Score signals in memory without persisting
            from equity_intel.trading.signals import (
                _resolve_side, _signal_strength, _risk_flags, _reason_codes, _build_rationale
            )
            catalysts = brief.get("catalysts", [])
            trad_hedge = set(cfg.trad_hedge_list)
            preview = []
            preview_now = datetime.datetime.now(datetime.timezone.utc)
            for c in catalysts:
                ticker = (c.get("ticker") or "").upper()
                if ticker in trad_hedge:
                    continue
                mat = float(c.get("materiality_score") or 0)
                conf = float(c.get("confidence_score") or 0)
                nov = float(c.get("novelty_score") or 0)
                has_primary = bool(c.get("has_primary_source"))
                if mat < min_materiality or conf < min_confidence:
                    continue
                side = _resolve_side(c.get("event_type"), c.get("event_subtype"))
                strength = _signal_strength(mat, conf, nov, has_primary)
                if strength < min_signal_strength:
                    side = "monitor"
                policy_block = get_signal_policy_block(ticker, c.get("event_type"), preview_now, cfg)
                if policy_block and side in ("buy", "sell", "reduce"):
                    side = "monitor"
                preview.append({
                    "ticker": ticker,
                    "side": side,
                    "strength": round(strength, 4),
                    "materiality": mat,
                    "event_type": c.get("event_type"),
                    "title": c.get("title"),
                })
            return {"dry_run": True, "signals": preview, "brief_catalysts": len(catalysts), "strategy_review": strategy_review_summary}

        # Live run: persist signals
        signals = generate_trade_signals_from_brief(
            session=session,
            brief=brief,
            min_materiality=min_materiality,
            min_confidence=min_confidence,
            min_signal_strength=min_signal_strength,
            allow_probe_stage=cfg.trading_allow_probe_stage_signals,
            cfg=cfg,
        )
        session.commit()

        buy_signals = [s for s in signals if s.signal_side == "buy"]
        sell_signals = [s for s in signals if s.signal_side in ("sell", "reduce")]
        monitor_signals = [s for s in signals if s.signal_side == "monitor"]
        avoid_signals = [s for s in signals if s.signal_side == "avoid"]

        return {
            "dry_run": False,
            "strategy_review": strategy_review_summary,
            "total_generated": len(signals),
            "buy_count": len(buy_signals),
            "sell_reduce_count": len(sell_signals),
            "monitor_count": len(monitor_signals),
            "avoid_count": len(avoid_signals),
            "executable_candidates": len(buy_signals) + len(sell_signals),
            "top_tickers": sorted(
                {s.ticker for s in signals if s.signal_side in ("buy", "sell", "reduce")}
            ),
            "all_tickers": sorted({s.ticker for s in signals}),
        }

    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@click.command("equity-generate-trade-signals")
@click.option("--tickers", default=None, help="Comma-separated tickers. Defaults to daily_brief_tickers from .env.")
@click.option("--days", default=7, show_default=True, help="Look-back window in calendar days.")
@click.option("--min-materiality", default=None, type=float, help="Override TRADING_MIN_MATERIALITY.")
@click.option("--min-confidence", default=None, type=float, help="Override TRADING_MIN_CONFIDENCE.")
@click.option("--min-signal-strength", default=None, type=float, help="Override TRADING_MIN_SIGNAL_STRENGTH.")
@click.option("--dry-run", is_flag=True, default=False, help="Score signals but do not write to database.")
@click.option("--log-level", default="warning", show_default=True, help="Logging level.")
def main(
    tickers: Optional[str],
    days: int,
    min_materiality: Optional[float],
    min_confidence: Optional[float],
    min_signal_strength: Optional[float],
    dry_run: bool,
    log_level: str,
) -> None:
    """Generate trade signals from watchlist catalyst brief."""
    configure_logging(log_level)

    resolved_tickers = (
        [t.strip().upper() for t in tickers.split(",") if t.strip()]
        if tickers
        else settings.daily_brief_tickers
    )
    if not resolved_tickers:
        try:
            from equity_intel.research_universe import load_research_universe
            universe = load_research_universe()
            prohibited = set(settings.prohibited_tickers_list)
            seen: set = set()
            resolved_tickers = []
            for cat_data in universe.get("categories", {}).values():
                for entry in cat_data.get("tickers", []):
                    if not isinstance(entry, dict):
                        continue
                    ticker = (entry.get("ticker") or "").strip().upper()
                    if ticker and ticker not in prohibited and ticker not in seen:
                        seen.add(ticker)
                        resolved_tickers.append(ticker)
        except Exception:
            pass
    if not resolved_tickers:
        resolved_tickers = settings.tickers_list
    if not resolved_tickers:
        click.echo("Error: no tickers configured.", err=True)
        sys.exit(1)

    resolved_min_mat = min_materiality if min_materiality is not None else settings.trading_min_materiality
    resolved_min_conf = min_confidence if min_confidence is not None else settings.trading_min_confidence
    resolved_min_str = min_signal_strength if min_signal_strength is not None else settings.trading_min_signal_strength

    click.echo(
        f"\n  Signal generation config:\n"
        f"    Tickers            : {', '.join(resolved_tickers)}\n"
        f"    Days               : {days}\n"
        f"    Min materiality    : {resolved_min_mat}\n"
        f"    Min confidence     : {resolved_min_conf}\n"
        f"    Min signal strength: {resolved_min_str}\n"
        f"    Dry run            : {dry_run}\n"
        f"    Execution enabled  : {settings.trading_execution_enabled}\n"
        f"    Require approval   : {settings.trading_require_approval}"
    )

    result = run(
        tickers=resolved_tickers,
        days=days,
        min_materiality=resolved_min_mat,
        min_confidence=resolved_min_conf,
        min_signal_strength=resolved_min_str,
        dry_run=dry_run,
    )

    if dry_run:
        signals = result.get("signals", [])
        click.echo(f"\n  DRY RUN — {len(signals)} signal(s) would be generated (nothing written)")
        if signals:
            click.echo(f"\n  {'Ticker':<8} {'Side':<8} {'Strength':>8}  Event type")
            click.echo("  " + "-" * 50)
            for s in sorted(signals, key=lambda x: -x["strength"])[:20]:
                click.echo(
                    f"  {s['ticker']:<8} {s['side']:<8} {s['strength']:>8.4f}  {s.get('event_type', '')}"
                )
            if len(signals) > 20:
                click.echo(f"  ... and {len(signals) - 20} more")
    else:
        click.echo(
            f"\n  Results:\n"
            f"    Total generated    : {result['total_generated']}\n"
            f"    Buy candidates     : {result['buy_count']}\n"
            f"    Sell/reduce        : {result['sell_reduce_count']}\n"
            f"    Monitor-only       : {result['monitor_count']}\n"
            f"    Avoid              : {result['avoid_count']}\n"
            f"    Executable         : {result['executable_candidates']}\n"
            f"    Top tickers        : {', '.join(result['top_tickers'][:10]) or '(none)'}"
        )
        if result["total_generated"] == 0:
            click.echo(
                "\n  No signals generated. Check:\n"
                "    - min_materiality / min_confidence thresholds\n"
                "    - whether events exist (run equity-build-events, equity-cluster-events)",
                err=True,
            )
        click.echo(f"\n  {ADVICE_DISCLAIMER}")


if __name__ == "__main__":
    main()
