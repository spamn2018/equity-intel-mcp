"""
Worker: execute approved trade signals through Alpaca.

Queries approved (or generated, depending on settings) TradeSignal rows,
evaluates each through the risk policy, and submits broker orders for
signals that pass all checks.

Safety
------
- Never submits broker orders when TRADING_EXECUTION_ENABLED=False.
- Never submits broker orders in --dry-run mode.
- Every decision is persisted to trading_decision_log.
- Every order attempt creates a trade_orders row.

Approval workflow
-----------------
By default TRADING_REQUIRE_APPROVAL=True: only signals with
status='approved' are considered.  Approve a signal via:

    UPDATE trade_signals SET status='approved', approved_at=now(), approved_by='manual'
    WHERE id = <signal_id>;

Or set TRADING_REQUIRE_APPROVAL=FALSE to auto-execute generated signals.

Usage
-----
equity-execute-trade-signals --dry-run        # evaluate without submitting
equity-execute-trade-signals --limit 5        # process up to 5 signals
equity-execute-trade-signals                  # live execution (requires TRADING_EXECUTION_ENABLED=True)
"""
from __future__ import annotations

import sys
from typing import Optional

import click

from equity_intel.config import settings
from equity_intel.db.session import SessionLocal
from equity_intel.logging_config import configure_logging, get_logger
from equity_intel.trading.execution import execute_approved_signals

logger = get_logger(__name__)


def run(limit: Optional[int] = None, dry_run: bool = False, cfg=None) -> dict:
    """Execute approved signals. Returns a summary dict."""
    cfg = cfg or settings

    session = SessionLocal()
    try:
        orders = execute_approved_signals(session, cfg, limit=limit, dry_run=dry_run)
        if not dry_run:
            session.commit()

        from equity_intel.db.models import TradeSignal
        # Count outcomes from the DB for reporting
        total_considered = (
            session.query(TradeSignal)
            .filter(TradeSignal.status.in_(["executed", "blocked", "failed"]))
            .count()
        )

        return {
            "orders_created": len(orders),
            "dry_run": dry_run,
        }
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@click.command("equity-execute-trade-signals")
@click.option("--limit", default=None, type=int, help="Maximum number of signals to process.")
@click.option("--dry-run", is_flag=True, default=False,
              help="Evaluate risk but never submit broker orders.")
@click.option("--log-level", default="info", show_default=True, help="Logging level.")
def main(limit: Optional[int], dry_run: bool, log_level: str) -> None:
    """Execute approved trade signals through Alpaca broker."""
    configure_logging(log_level)

    click.echo(
        f"\n  Execution config:\n"
        f"    TRADING_EXECUTION_ENABLED : {settings.trading_execution_enabled}\n"
        f"    TRADING_REQUIRE_APPROVAL  : {settings.trading_require_approval}\n"
        f"    ALPACA_PAPER              : {settings.alpaca_paper}\n"
        f"    Dry run                   : {dry_run}\n"
        f"    Limit                     : {limit or 'none'}"
    )

    if not dry_run and not settings.trading_execution_enabled:
        click.echo(
            "\n  TRADING_EXECUTION_ENABLED=False — no broker orders will be submitted.\n"
            "  Set TRADING_EXECUTION_ENABLED=TRUE in .env to enable live execution.\n"
            "  Use --dry-run to simulate execution without this flag.",
            err=True,
        )
        sys.exit(0)

    if not dry_run and not settings.alpaca_api_key:
        click.echo(
            "\n  ERROR: ALPACA_API_KEY is not set in .env.\n"
            "  Set ALPACA_API_KEY and ALPACA_SECRET_KEY before running execution.",
            err=True,
        )
        sys.exit(1)

    result = run(limit=limit, dry_run=dry_run)

    if dry_run:
        click.echo(
            f"\n  DRY RUN complete — {result['orders_created']} order(s) would have been submitted."
        )
    else:
        click.echo(
            f"\n  Execution complete.\n"
            f"    Orders submitted: {result['orders_created']}\n"
            f"\n  Check trade_orders and trading_decision_log tables for full details."
        )


if __name__ == "__main__":
    main()
