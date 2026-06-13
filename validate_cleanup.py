"""Validation script: dry-run then real cleanup of old news articles."""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from sqlalchemy import func
from equity_intel.db.models import NewsArticle
from equity_intel.db.session import SessionLocal
from equity_intel.workers.cleanup_news import cleanup_news, _cutoff_utc, DEFAULT_RETENTION_DAYS

s = SessionLocal()
try:
    total = s.query(NewsArticle).count()
    oldest = s.query(func.min(NewsArticle.published_at)).scalar()
    newest = s.query(func.max(NewsArticle.published_at)).scalar()
    print(f"Total news_articles: {total}")
    print(f"Oldest published_at: {oldest}")
    print(f"Newest published_at: {newest}")
    print()
finally:
    s.close()

# Dry-run
print("--- DRY RUN (--days 60) ---")
cutoff = _cutoff_utc(DEFAULT_RETENTION_DAYS)
print(f"  Retention : {DEFAULT_RETENTION_DAYS} days")
print(f"  Cutoff    : {cutoff.strftime('%Y-%m-%d %H:%M UTC')}")
would_delete = cleanup_news(days=DEFAULT_RETENTION_DAYS, dry_run=True)
print(f"  Would delete: {would_delete} row(s)")
print()

if would_delete > 0:
    print("--- REAL DELETE (--days 60) ---")
    deleted = cleanup_news(days=DEFAULT_RETENTION_DAYS, dry_run=False)
    print(f"  Deleted: {deleted} row(s)")
    print()

    s2 = SessionLocal()
    try:
        remaining = s2.query(NewsArticle).count()
        print(f"  Remaining rows: {remaining}")
    finally:
        s2.close()
else:
    print("  No rows older than 60 days — nothing to delete.")
