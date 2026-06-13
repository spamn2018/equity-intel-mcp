"""
Worker: clean up old news articles beyond the retention window.

Deletes rows from news_articles where published_at (or created_at as fallback)
is older than the configured retention period. Safe to run repeatedly -
idempotent, rolls back on any error.

Usage:
    equity-cleanup-news                  # delete articles older than 60 days
    equity-cleanup-news --days 30        # tighter 30-day window
    equity-cleanup-news --dry-run        # show count, delete nothing
    equity-cleanup-news --days 90 --dry-run
"""
from __future__ import annotations

import datetime
import sys
from typing import Optional

import click

from equity_intel.db.models import NewsArticle
from equity_intel.db.session import get_session
from equity_intel.logging_config import configure_logging, get_logger

logger = get_logger(__name__)

DEFAULT_RETENTION_DAYS = 60


def _cutoff_utc(days: int) -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)


def cleanup_news(days: int = DEFAULT_RETENTION_DAYS, dry_run: bool = False) -> int:
    """
    Delete news_articles older than `days`.

    Uses published_at when available; falls back to created_at for rows
    where the publisher timestamp was not set.

    Returns the number of rows deleted (or that would be deleted in dry-run mode).
    Raises on DB error after rolling back.
    """
    cutoff = _cutoff_utc(days)

    with get_session() as session:
        # Count rows that fall outside the retention window.
        # A row is stale if published_at < cutoff OR (published_at IS NULL AND created_at < cutoff).
        stale_query = session.query(NewsArticle).filter(
            (
                (NewsArticle.published_at.isnot(None)) &
                (NewsArticle.published_at < cutoff)
            ) | (
                (NewsArticle.published_at.is_(None)) &
                (NewsArticle.created_at < cutoff)
            )
        )

        count = stale_query.count()

        if dry_run or count == 0:
            return count

        # Real delete - session.commit() is called by get_session() context manager
        stale_query.delete(synchronize_session=False)

    return count


@click.command("equity-cleanup-news")
@click.option(
    "--days",
    default=DEFAULT_RETENTION_DAYS,
    show_default=True,
    type=int,
    help="Delete news articles older than this many days.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Show how many rows would be deleted without deleting them.",
)
@click.option(
    "--log-level",
    default="warning",
    show_default=True,
    help="Logging level (debug, info, warning, error).",
)
def main(days: int, dry_run: bool, log_level: str) -> None:
    """
    Delete news_articles older than --days (default 60).

    Run this as a periodic maintenance step - it does not affect the active
    synthesis window (7 days) or any other table. Safe to run repeatedly.
    """
    configure_logging(log_level)

    cutoff = _cutoff_utc(days)
    cutoff_str = cutoff.strftime("%Y-%m-%d %H:%M UTC")

    mode = "DRY RUN - " if dry_run else ""
    click.echo(
        f"\n  {mode}News article cleanup\n"
        f"    Retention : {days} days\n"
        f"    Cutoff    : {cutoff_str}\n"
        f"    Mode      : {'dry-run (no changes)' if dry_run else 'live delete'}\n"
    )

    try:
        deleted = cleanup_news(days=days, dry_run=dry_run)
    except Exception as exc:
        click.echo(f"\n  ERROR: cleanup failed - {exc}", err=True)
        logger.error("news_cleanup_failed", error=str(exc))
        sys.exit(1)

    if dry_run:
        click.echo(f"  Would delete : {deleted} row(s)")
        click.echo("  No changes made (--dry-run).\n")
    elif deleted == 0:
        click.echo("  Nothing to delete - all articles are within the retention window.\n")
    else:
        click.echo(f"  Deleted      : {deleted} row(s) older than {cutoff_str}\n")
        logger.info("news_cleanup_complete", deleted=deleted, cutoff=cutoff_str)


if __name__ == "__main__":
    main()
