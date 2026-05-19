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
