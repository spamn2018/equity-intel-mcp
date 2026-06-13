"""
Daily Brief Worker — run_daily_brief

Thin orchestration layer over the Watchlist Catalyst Brief service.
Generates a dated brief file by calling get_watchlist_brief() with
settings from .env, then delivers the result via LocalFileDelivery.

This worker contains NO ranking, filtering, or formatting logic of its own.
All of that lives in briefs/watchlist.py and workers/generate_watchlist_brief.py.

Usage examples
--------------
# Run with settings from .env
equity-run-daily-brief

# Override the watchlist for a single run
equity-run-daily-brief --tickers AAPL,MSFT,NVDA

# Longer window (last 3 days instead of today-only)
equity-run-daily-brief --days 3

# Write Markdown instead of JSON
equity-run-daily-brief --format markdown

# Dry-run: show what would be written without touching disk
equity-run-daily-brief --dry-run

# Custom output directory
equity-run-daily-brief --output-dir /tmp/briefs

Scheduling
----------
Run this command daily at 07:00 using the OS scheduler:

  Windows Task Scheduler:
    Action -> Start a program
    Program: python
    Arguments: -m equity_intel.workers.run_daily_brief
    Start in: C:\\path\\to\\your\\project

  Linux / macOS cron (add via `crontab -e`):
    0 7 * * * cd /path/to/project && python -m equity_intel.workers.run_daily_brief

  See README.md -> "Scheduled Daily Brief" for full scheduling instructions.

Output file naming
------------------
Files are named:
  {output_dir}/brief_{YYYYMMDD}.json      (format=json)
  {output_dir}/brief_{YYYYMMDD}.md        (format=markdown)

Re-running on the same calendar date overwrites the existing file (idempotent).
"""
from __future__ import annotations

import datetime
import json
import sys
from pathlib import Path
from typing import List, Optional

import click

from equity_intel.briefs.watchlist import get_watchlist_brief, _render_markdown_from_brief
from equity_intel.config import settings
from equity_intel.db.session import SessionLocal
from equity_intel.export import LocalFileDelivery
from equity_intel.logging_config import configure_logging, get_logger

# Trading signal generation (optional — only imported when TRADING_SIGNALS_ENABLED=True)
def _maybe_generate_signals(session, brief: dict, cfg) -> None:
    """
    Generate trade signals from the brief when TRADING_SIGNALS_ENABLED=True.
    Logs but never raises — brief delivery must not fail because of trading code.
    """
    if not cfg.trading_signals_enabled:
        return
    try:
        from equity_intel.trading.signals import generate_trade_signals_from_brief  # noqa: PLC0415
        signals = generate_trade_signals_from_brief(
            session=session,
            brief=brief,
            min_materiality=cfg.trading_min_materiality,
            min_confidence=cfg.trading_min_confidence,
            min_signal_strength=cfg.trading_min_signal_strength,
            require_primary_source=cfg.trading_require_primary_source,
            allow_news_only=cfg.trading_allow_news_only_signals,
            allow_probe_stage=cfg.trading_allow_probe_stage_signals,
            cfg=cfg,
        )
        session.commit()
        _log = get_logger(__name__)
        _log.info(
            "daily_brief_signals_generated",
            count=len(signals),
            buy=sum(1 for s in signals if s.signal_side == "buy"),
            monitor=sum(1 for s in signals if s.signal_side == "monitor"),
        )
    except Exception as exc:
        get_logger(__name__).warning("daily_brief_signal_generation_failed", error=str(exc))

logger = get_logger(__name__)

ADVICE_DISCLAIMER = (
    "This brief is research workflow output — not investment advice. "
    "Events are described as 'likely related to' or 'may reflect' market moves. "
    "Always verify with primary sources before making any decisions."
)


# ------------------------------------------------------------------ #
# File helpers (kept for backward-compat; tests import these directly) #
# ------------------------------------------------------------------ #


def _brief_filename(output_dir: Path, fmt: str, date: datetime.date) -> Path:
    """Return the dated output path for a brief."""
    ext = "md" if fmt == "markdown" else "json"
    return output_dir / f"brief_{date.strftime('%Y%m%d')}.{ext}"


def _write_brief(
    brief: dict,
    output_path: Path,
    fmt: str,
) -> None:
    """
    Serialize brief to disk as JSON or Markdown.

    This is a convenience wrapper kept for backward compatibility and
    direct use in tests.  Production code in run_daily_brief() routes
    through LocalFileDelivery instead.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "markdown":
        content = _render_markdown_from_brief(brief)
    else:
        content = json.dumps(brief, default=str, indent=2)
    output_path.write_text(content, encoding="utf-8")


# ------------------------------------------------------------------ #
# Core runner (importable for testing)                                 #
# ------------------------------------------------------------------ #


def run_daily_brief(
    tickers: List[str],
    days: int,
    min_materiality: float,
    max_items: int,
    fmt: str,
    output_dir: Path,
    dry_run: bool = False,
    run_date: Optional[datetime.date] = None,
) -> dict:
    """
    Generate and (optionally) persist a dated watchlist catalyst brief.

    Parameters
    ----------
    tickers         : ticker symbols to include
    days            : look-back window in calendar days
    min_materiality : minimum materiality score [0, 1]
    max_items       : maximum catalysts to return
    fmt             : "json" or "markdown"
    output_dir      : directory where brief files are written
    dry_run         : if True, skip writing to disk
    run_date        : override the calendar date used for file naming
                      (default: today in UTC)

    Returns
    -------
    The brief dict (same shape as get_watchlist_brief()), with an
    additional ``_output_path`` key indicating where the file was written
    (or None in dry-run mode).
    """
    date = run_date or datetime.datetime.now(datetime.timezone.utc).date()

    session = SessionLocal()
    try:
        brief = get_watchlist_brief(
            session=session,
            tickers=tickers,
            days=days,
            min_materiality=min_materiality,
            max_items=max_items,
            include_price_context=True,
            include_news=True,
            include_filings=True,
        )
        # Optional: generate trade signals from this brief when enabled
        _maybe_generate_signals(session, brief, settings)
    finally:
        session.close()

    if not dry_run:
        out_path = _brief_filename(output_dir, fmt, date)
        adapter = LocalFileDelivery()
        delivery_result = adapter.deliver(brief, out_path, fmt)
        logger.info(
            "daily_brief_delivered",
            delivery_status=delivery_result["status"],
            destination=delivery_result["destination"],
            bytes_written=delivery_result.get("bytes_written"),
            total_catalysts=brief["total_catalysts"],
            tickers=brief["watchlist"],
        )
        brief["_output_path"] = delivery_result["destination"]
    else:
        brief["_output_path"] = None

    return brief


# ------------------------------------------------------------------ #
# CLI                                                                  #
# ------------------------------------------------------------------ #


@click.command("equity-run-daily-brief")
@click.option(
    "--tickers",
    default=None,
    help=(
        "Comma-separated tickers (e.g. AAPL,MSFT,NVDA). "
        "Defaults to DAILY_BRIEF_WATCHLIST -> DEFAULT_TICKERS from .env."
    ),
)
@click.option(
    "--days",
    default=None,
    type=int,
    help="Look-back window in calendar days. Defaults to DAILY_BRIEF_DAYS from .env.",
)
@click.option(
    "--min-materiality",
    default=None,
    type=float,
    help="Minimum materiality score [0, 1]. Defaults to DAILY_BRIEF_MIN_MATERIALITY from .env.",
)
@click.option(
    "--max-items",
    default=None,
    type=int,
    help="Maximum catalysts to include. Defaults to DAILY_BRIEF_MAX_ITEMS from .env.",
)
@click.option(
    "--format",
    "fmt",
    default=None,
    type=click.Choice(["json", "markdown"], case_sensitive=False),
    help="Output format. Defaults to DAILY_BRIEF_FORMAT from .env.",
)
@click.option(
    "--output-dir",
    default=None,
    type=click.Path(),
    help="Output directory for brief files. Defaults to DAILY_BRIEF_OUTPUT_DIR from .env.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Generate the brief but do not write it to disk. Prints to stdout instead.",
)
@click.option(
    "--log-level",
    default="warning",
    show_default=True,
    help="Logging level (debug, info, warning, error).",
)
def main(
    tickers: Optional[str],
    days: Optional[int],
    min_materiality: Optional[float],
    max_items: Optional[int],
    fmt: Optional[str],
    output_dir: Optional[str],
    dry_run: bool,
    log_level: str,
) -> None:
    """
    Generate a dated watchlist catalyst brief and write it to disk.

    All parameters default to values from .env (DAILY_BRIEF_* keys).
    Re-running on the same calendar date overwrites the existing file.

    This is research workflow output — not investment advice.
    """
    configure_logging(log_level)

    # Resolve parameters: CLI flags override .env, which overrides hard defaults
    resolved_tickers = (
        [t.strip().upper() for t in tickers.split(",") if t.strip()]
        if tickers
        else settings.daily_brief_tickers
    )
    # Fall back to research universe → DEFAULT_TICKERS if brief-specific list is empty
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
    resolved_days = days if days is not None else settings.daily_brief_days
    resolved_min_mat = min_materiality if min_materiality is not None else settings.daily_brief_min_materiality
    resolved_max_items = max_items if max_items is not None else settings.daily_brief_max_items
    resolved_fmt = (fmt or settings.daily_brief_format).lower()
    resolved_output_dir = Path(output_dir) if output_dir else Path(settings.daily_brief_output_dir)

    if not resolved_tickers:
        click.echo(
            "Error: no tickers configured. Set DAILY_BRIEF_WATCHLIST or DEFAULT_TICKERS in .env.",
            err=True,
        )
        sys.exit(1)

    logger.info(
        "daily_brief_starting",
        tickers=resolved_tickers,
        days=resolved_days,
        min_materiality=resolved_min_mat,
        fmt=resolved_fmt,
        output_dir=str(resolved_output_dir),
        dry_run=dry_run,
    )

    brief = run_daily_brief(
        tickers=resolved_tickers,
        days=resolved_days,
        min_materiality=resolved_min_mat,
        max_items=resolved_max_items,
        fmt=resolved_fmt,
        output_dir=resolved_output_dir,
        dry_run=dry_run,
    )

    total = brief["total_catalysts"]
    watchlist = brief["watchlist"]

    # Always-print diagnostic block — visible regardless of dry-run mode
    click.echo(
        f"\n  Daily brief config:\n"
        f"    DAILY_BRIEF_DAYS          : {resolved_days}\n"
        f"    Window                    : last {resolved_days} day(s)\n"
        f"    Min materiality           : {resolved_min_mat}\n"
        f"    Max items                 : {resolved_max_items}\n"
        f"    Catalysts found           : {total}"
    )
    if total == 0:
        click.echo(
            "\n  WARNING: Daily brief contains 0 catalysts.\n"
            "  This can be normal for a quiet window, but verify:\n"
            "    - DAILY_BRIEF_DAYS is set correctly in .env\n"
            "    - DAILY_BRIEF_MIN_MATERIALITY is not too high\n"
            "    - Events have been built (run equity-build-events and equity-cluster-events)\n"
            "  synthesize.py will fail if no catalysts are found.",
            err=True,
        )

    if dry_run:
        # Print to stdout and exit — nothing written to disk
        if resolved_fmt == "markdown":
            click.echo(_render_markdown_from_brief(brief))
        else:
            click.echo(json.dumps(brief, default=str, indent=2))
    else:
        out_path = brief.get("_output_path", "unknown")
        click.echo(
            f"Daily brief written -> {out_path}\n"
            f"  Tickers : {', '.join(watchlist)}\n"
            f"  Window  : {resolved_days}d | min_materiality: {resolved_min_mat}\n"
            f"  Catalysts found: {total}\n"
            f"  {ADVICE_DISCLAIMER}"
        )


if __name__ == "__main__":
    main()
