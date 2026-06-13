"""
auto_rebalance.py — Automatic portfolio rebalance worker.

Loads the research universe, computes the rebalance plan using the
cash/proceeds-directed, loss-protected policy, and submits orders to
Alpaca (paper by default; live when ALPACA_PAPER=false).

Safety
------
- TRADING_EXECUTION_ENABLED must be True in .env, otherwise the script
  exits without touching the broker.
- ALPACA_PAPER=true (default) routes all orders to Alpaca paper trading.
  Switch to ALPACA_PAPER=false only when you are ready for live money.
- Pass --dry-run to compute and print the plan without submitting anything,
  regardless of TRADING_EXECUTION_ENABLED.

Usage
-----
# Inspect the plan — never touches the broker
python -m equity_intel.workers.auto_rebalance --dry-run

# Execute on paper (requires TRADING_EXECUTION_ENABLED=true, ALPACA_PAPER=true)
python -m equity_intel.workers.auto_rebalance

# Override thresholds
python -m equity_intel.workers.auto_rebalance --buy-threshold 5 --sell-threshold 10

# Pause sell-side (e.g. during broad market decline)
python -m equity_intel.workers.auto_rebalance --pause-sells
"""
from __future__ import annotations

import json
import sys

import click

from equity_intel.config import settings
from equity_intel.logging_config import configure_logging, get_logger

logger = get_logger(__name__)


# ── Universe loader (mirrors app.py logic) ────────────────────────────────────

_CAT_LABELS: dict = {
    "semiconductors_compute":           "Chips",
    "semiconductor_equipment":          "Chip Equip",
    "cloud_hyperscalers":               "Hyperscalers",
    "ai_software_platforms":            "AI Software",
    "data_centers_reits":               "Data Centers",
    "power_and_energy":                 "Power & Energy",
    "networking_and_interconnect":      "Networking",
    "memory_and_storage":               "Memory",
    "critical_minerals_rare_earth":     "Critical Minerals",
    "ai_infrastructure_replacements":   "AI Infra Replacements",
    "trad_hedge":                       "Trad Hedge",
}
_SKIP_CATS = {"bitcoin_miners_data_center_angle"}
_STAGE_W   = {"core": 15, "established": 10, "probe": 5, "watch": 5}


def _load_portfolio_tickers(cfg=None):
    """Return the portfolio_tickers list and normalized category weights."""
    cfg = cfg or settings
    from equity_intel.research_universe import load_research_universe

    universe  = load_research_universe()
    prohibited = set(cfg.prohibited_tickers_list)

    portfolio_tickers = []
    for cat_key, cat_data in universe.get("categories", {}).items():
        if cat_key in _SKIP_CATS:
            continue
        label = _CAT_LABELS.get(cat_key, cat_key.replace("_", " ").title())
        for entry in cat_data.get("tickers", []):
            if not isinstance(entry, dict):
                continue
            ticker = (entry.get("ticker") or "").strip().upper()
            if not ticker or ticker in prohibited:
                continue
            portfolio_tickers.append({
                "ticker":   ticker,
                "name":     entry.get("name", ticker),
                "category": label,
                "stage":    entry.get("stage", "probe"),
                "weight":   _STAGE_W.get(entry.get("stage", "probe"), 5),
            })
    return portfolio_tickers


def _normalize_cat_weights(portfolio_tickers):
    """Return {category: pct} normalized to 100."""
    from collections import defaultdict
    raw: dict = defaultdict(float)
    for t in portfolio_tickers:
        raw[t["category"]] += t["weight"]
    total = sum(raw.values()) or 1
    return {cat: w / total * 100 for cat, w in raw.items()}


# ── Main runner ───────────────────────────────────────────────────────────────

def run(
    dry_run: bool = True,
    buy_threshold_pct: float = 5.0,
    sell_threshold_pct: float = 10.0,
    pause_sell_side: bool = False,
    cfg=None,
) -> dict:
    """
    Build and (optionally) execute a rebalance plan.

    Parameters
    ----------
    dry_run           : compute plan only — never submit broker orders
    buy_threshold_pct : min underweight gap to queue a buy (pp)
    sell_threshold_pct: min overweight gap to trigger a profitable trim (pp)
    pause_sell_side   : suppress all sell-side orders (broad market decline)
    cfg               : settings object (defaults to module-level singleton)

    Returns
    -------
    plan dict as returned by build_rebalance_plan
    """
    cfg = cfg or settings

    # Guard: require execution to be enabled (unless dry-run)
    if not dry_run and not cfg.trading_execution_enabled:
        return {
            "error": (
                "TRADING_EXECUTION_ENABLED=False in .env. "
                "Set it to True to allow automatic execution, "
                "or pass --dry-run to preview the plan."
            ),
            "dry_run": dry_run,
        }

    # Build adapter
    if not cfg.alpaca_api_key or not cfg.alpaca_secret_key:
        return {"error": "ALPACA_API_KEY / ALPACA_SECRET_KEY not set in .env", "dry_run": dry_run}

    from equity_intel.trading.alpaca_adapter import AlpacaBrokerAdapter
    adapter = AlpacaBrokerAdapter(
        api_key=cfg.alpaca_api_key,
        secret_key=cfg.alpaca_secret_key,
        paper=cfg.alpaca_paper,
    )

    # Load universe
    portfolio_tickers = _load_portfolio_tickers(cfg)
    if not portfolio_tickers:
        return {"error": "No portfolio tickers loaded from research universe.", "dry_run": dry_run}

    cat_weights = _normalize_cat_weights(portfolio_tickers)

    # Build and (maybe) execute plan
    from equity_intel.trading.rebalance import build_rebalance_plan
    trad_hedge_syms = set(cfg.trad_hedge_list)
    plan = build_rebalance_plan(
        portfolio_tickers=portfolio_tickers,
        category_weights_pct=cat_weights,
        adapter=adapter,
        buy_threshold_pct=buy_threshold_pct,
        sell_threshold_pct=sell_threshold_pct,
        pause_sell_side=pause_sell_side,
        dry_run=dry_run,
        trad_hedge_syms=trad_hedge_syms,
    )
    return plan


# ── CLI ───────────────────────────────────────────────────────────────────────

@click.command("equity-auto-rebalance")
@click.option("--dry-run", is_flag=True, default=False,
              help="Compute and print the plan — never submit broker orders.")
@click.option("--buy-threshold", default=5.0, show_default=True, type=float,
              help="Min underweight gap (pp) to queue a buy.")
@click.option("--sell-threshold", default=10.0, show_default=True, type=float,
              help="Min overweight gap (pp) to trigger a profitable trim.")
@click.option("--pause-sells", is_flag=True, default=False,
              help="Suppress all sell-side orders (use during broad market declines).")
@click.option("--json-out", is_flag=True, default=False,
              help="Dump the full plan as JSON to stdout.")
@click.option("--log-level", default="info", show_default=True)
def main(
    dry_run: bool,
    buy_threshold: float,
    sell_threshold: float,
    pause_sells: bool,
    json_out: bool,
    log_level: str,
) -> None:
    """Auto-rebalance the portfolio against Alpaca (paper by default)."""
    configure_logging(log_level)

    mode_label = "PAPER" if settings.alpaca_paper else "⚠  LIVE MONEY"
    click.echo(
        f"\n  Auto-rebalance\n"
        f"    Mode               : {mode_label}\n"
        f"    Execution enabled  : {settings.trading_execution_enabled}\n"
        f"    Dry run            : {dry_run}\n"
        f"    Buy threshold      : {buy_threshold}pp\n"
        f"    Sell threshold     : {sell_threshold}pp  (profitable positions only)\n"
        f"    Pause sell-side    : {pause_sells}\n"
    )

    if not dry_run and not settings.trading_execution_enabled:
        click.echo(
            "  TRADING_EXECUTION_ENABLED=False — nothing will be submitted.\n"
            "  Set TRADING_EXECUTION_ENABLED=TRUE in .env or use --dry-run.",
            err=True,
        )
        sys.exit(0)

    plan = run(
        dry_run=dry_run,
        buy_threshold_pct=buy_threshold,
        sell_threshold_pct=sell_threshold,
        pause_sell_side=pause_sells,
    )

    if "error" in plan:
        click.echo(f"\n  ERROR: {plan['error']}", err=True)
        sys.exit(1)

    if json_out:
        click.echo(json.dumps(plan, indent=2, default=str))
        return

    # ── Human-readable summary ────────────────────────────────────────────────
    stats  = plan.get("stats", {})
    orders = plan.get("orders", [])

    click.echo(
        f"  Account value     : ${plan.get('account_value', 0):,.2f}\n"
        f"  Available cash    : ${plan.get('cash', 0):,.2f}\n"
        f"  Available funds   : ${plan.get('available_funds', 0):,.2f}  "
        f"(cash + estimated sell proceeds)\n"
        f"\n"
        f"  Orders            : {stats.get('total_orders', 0)}  "
        f"({stats.get('sells', 0)} sells, {stats.get('buys', 0)} buys)\n"
        f"  Skipped           : {stats.get('skipped', 0)}\n"
        f"  Est. sell value   : ${stats.get('estimated_sell_value', 0):,.2f}\n"
        f"  Est. buy value    : ${stats.get('estimated_buy_value', 0):,.2f}\n"
        f"  Remaining funds   : ${stats.get('remaining_funds_after_buys', 0):,.2f}\n"
    )

    if orders:
        click.echo(f"  {'Ticker':<8} {'Side':<5} {'Shares':>8}  {'Price':>9}  {'Drift':>7}  Rationale")
        click.echo("  " + "─" * 80)
        for o in orders:
            click.echo(
                f"  {o['ticker']:<8} {o['side']:<5} {o['shares']:>8.2f}  "
                f"${o['price']:>8.2f}  {o['drift_pct']:>+6.1f}pp  {o['rationale']}"
            )
    else:
        click.echo("  No orders generated — portfolio is within thresholds or no funds available.")

    warnings = plan.get("warnings", [])
    if warnings:
        click.echo("\n  Warnings:")
        for w in warnings:
            click.echo(f"    ⚠  {w}")

    if dry_run:
        click.echo("\n  [DRY RUN — no orders submitted]")
    else:
        results = plan.get("execution_result") or []
        submitted  = [r for r in results if r.get("status") == "submitted"]
        skipped_r  = [r for r in results if r.get("status") == "skipped"]
        errors     = [r for r in results if r.get("status") == "error"]

        click.echo(
            f"\n  Execution results:\n"
            f"    Submitted : {len(submitted)}\n"
            f"    Skipped   : {len(skipped_r)}  (open order already exists)\n"
            f"    Errors    : {len(errors)}\n"
        )
        if errors:
            click.echo("  Errors:")
            for e in errors:
                click.echo(f"    {e['ticker']}: {e.get('error')}")


if __name__ == "__main__":
    main()
