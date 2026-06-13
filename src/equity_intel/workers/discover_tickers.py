"""
Worker: Ticker Discovery Radar
==============================

Scans all ingested project data for cross-ticker mentions, persists raw
mention records, computes weekly discovery scores, and optionally promotes
high-confidence candidates into the research universe.

Usage
-----
    equity-discover-tickers [OPTIONS]

Options
-------
    --days INT          Look-back window in days (default: 30)
    --min-score FLOAT   Minimum total_score to include in output (default: 0.0)
    --promote           Promote probe_candidates into ai_tickers.json
    --dry-run           Print what would happen without writing to DB or files
    --week TEXT         Target ISO week key e.g. "2026-W21" (default: current)
    --top INT           How many candidates to print (default: 10)
"""
from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import click

from equity_intel.config import settings
from equity_intel.db.models import TickerMention, now_utc
from equity_intel.db.session import get_session
from equity_intel.discovery.extractor import (
    scan_brief_files,
    scan_event_clusters,
    scan_events,
    scan_filing_documents,
    scan_intelligence_files,
    scan_news_articles,
)
from equity_intel.discovery.scorer import (
    compute_scores,
    upsert_scores,
)
from equity_intel.logging_config import configure_logging, get_logger
from equity_intel.research_universe import load_universe

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _current_week_key() -> str:
    now = datetime.datetime.now(datetime.timezone.utc)
    iso = now.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def _repo_root() -> Path:
    """Locate the project root by climbing from this file."""
    here = Path(__file__).resolve().parent
    for ancestor in [here, here.parent, here.parent.parent, here.parent.parent.parent]:
        if (ancestor / "pyproject.toml").exists():
            return ancestor
    return here.parent.parent.parent


def _default_tickers_set() -> Set[str]:
    return {t.strip().upper() for t in settings.default_tickers.split(",") if t.strip()}


def _prohibited_set() -> Set[str]:
    return {t.strip().upper() for t in settings.prohibited_tickers.split(",") if t.strip()}


def _trad_hedge_set() -> Set[str]:
    return {t.strip().upper() for t in settings.trad_hedge_tickers.split(",") if t.strip()}


def _universe_tickers_set() -> Set[str]:
    """Load all tickers from ai_tickers.json (any stage)."""
    try:
        uni = load_universe()
        return {e["ticker"].upper() for e in uni.get("all", [])}
    except Exception:
        return set()


# ---------------------------------------------------------------------------
# Mention persistence
# ---------------------------------------------------------------------------

def _save_mentions(session, records: List[Dict], dry_run: bool) -> int:
    """
    Dedupe and upsert raw mention records.
    Returns number of new rows inserted.
    """
    inserted = 0
    for rec in records:
        if dry_run:
            inserted += 1
            continue
        existing = (
            session.query(TickerMention)
            .filter(
                TickerMention.mentioned_ticker == rec["mentioned_ticker"],
                TickerMention.source_type == rec["source_type"],
                TickerMention.source_id == rec["source_id"],
            )
            .first()
        )
        if existing:
            continue
        row = TickerMention(
            mentioned_ticker=rec["mentioned_ticker"],
            source_ticker=rec.get("source_ticker"),
            source_type=rec["source_type"],
            source_id=rec["source_id"],
            occurred_at=rec.get("occurred_at"),
            week_key=rec.get("week_key"),
            context=rec.get("context"),
            url=rec.get("url"),
            confidence=rec.get("confidence", 1.0),
            exclusion_flag=rec.get("exclusion_flag", False),
            created_at=now_utc(),
        )
        session.add(row)
        inserted += 1
    return inserted


# ---------------------------------------------------------------------------
# Promotion
# ---------------------------------------------------------------------------

def _promote_candidates(
    candidates: List[Dict],
    ai_tickers_path: Path,
    dry_run: bool,
    week_key: str,
) -> List[str]:
    """
    Add probe_candidate tickers to ai_tickers.json under the 'discovery_radar'
    category.  Returns list of newly promoted tickers.
    """
    if not candidates:
        return []

    promoted: List[str] = []
    today = datetime.date.today().isoformat()
    review_after = (
        datetime.date.today() + datetime.timedelta(weeks=4)
    ).isoformat()

    # Load existing universe
    try:
        existing_data: Dict[str, Any] = json.loads(
            ai_tickers_path.read_text(encoding="utf-8")
        ) if ai_tickers_path.exists() else {}
    except Exception as exc:
        logger.error("ai_tickers_load_failed", error=str(exc))
        return []

    # Build set of already-present tickers
    existing_tickers: Set[str] = set()
    for category_entries in existing_data.values():
        if isinstance(category_entries, list):
            for entry in category_entries:
                if isinstance(entry, dict) and "ticker" in entry:
                    existing_tickers.add(entry["ticker"].upper())

    radar_list: List[Dict] = existing_data.setdefault("discovery_radar", [])
    # Build quick lookup for radar list
    radar_tickers = {e["ticker"].upper() for e in radar_list if isinstance(e, dict)}

    for cand in candidates:
        ticker = cand["ticker"]
        if ticker in radar_tickers:
            logger.info("already_in_radar", ticker=ticker)
            continue

        entry = {
            "ticker": ticker,
            "name": "",
            "why": (
                f"Discovered by rising co-mention frequency around monitored "
                f"tickers (week {week_key}). "
                f"Score: {cand['total_score']:.3f}, "
                f"Mentions: {cand['mention_count']}, "
                f"Accel: {cand['acceleration_score']:.3f}."
            ),
            "stage": "probe",
            "conviction": "low",
            "thesis_tags": ["discovery_radar"],
            "risk_tags": ["unvalidated", "needs_primary_source_confirmation"],
            "source": "discovery_radar",
            "added_at": today,
            "review_after": review_after,
        }

        if dry_run:
            logger.info(
                "dry_run_would_promote",
                ticker=ticker,
                score=cand["total_score"],
            )
        else:
            radar_list.append(entry)
            logger.info("promoted_to_probe", ticker=ticker, score=cand["total_score"])

        promoted.append(ticker)

    if not dry_run and promoted:
        ai_tickers_path.write_text(
            json.dumps(existing_data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    return promoted


# ---------------------------------------------------------------------------
# Main CLI
# ---------------------------------------------------------------------------

@click.command()
@click.option("--days", default=30, show_default=True, help="Look-back window in days")
@click.option("--min-score", default=0.0, show_default=True, help="Minimum total_score to display")
@click.option("--promote", is_flag=True, default=False, help="Promote probe_candidates into universe")
@click.option("--dry-run", is_flag=True, default=False, help="Simulate without writing to DB/files")
@click.option("--week", default=None, help="Target ISO week key (default: current week)")
@click.option("--top", default=10, show_default=True, help="Number of top candidates to print")
def main(
    days: int,
    min_score: float,
    promote: bool,
    dry_run: bool,
    week: Optional[str],
    top: int,
) -> None:
    """Ticker Discovery Radar — find and score emerging tickers in ingested data."""
    configure_logging(settings.log_level)

    week_key = week or _current_week_key()
    since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)
    root = _repo_root()

    default_tickers = _default_tickers_set()
    prohibited = _prohibited_set()
    trad_hedge = _trad_hedge_set()
    all_excluded = prohibited | trad_hedge
    universe_tickers = _universe_tickers_set()

    click.echo(f"\n{'='*60}")
    click.echo(f"  Ticker Discovery Radar")
    click.echo(f"  Week: {week_key}  |  Look-back: {days} days")
    if dry_run:
        click.echo("  ⚠️  DRY RUN — no DB writes")
    click.echo(f"{'='*60}\n")

    # ------------------------------------------------------------------
    # Phase 1: Collect raw mentions
    # ------------------------------------------------------------------
    click.echo("Phase 1: Scanning ingested data for ticker mentions...")
    all_records: List[Dict] = []

    with get_session() as session:
        # News
        n = 0
        for rec in scan_news_articles(session, since):
            all_records.append(rec)
            n += 1
        click.echo(f"  news_articles:       {n:>6} mentions")

        # Filing documents
        n = 0
        for rec in scan_filing_documents(session, since):
            all_records.append(rec)
            n += 1
        click.echo(f"  filing_documents:    {n:>6} mentions")

        # Events
        n = 0
        for rec in scan_events(session, since):
            all_records.append(rec)
            n += 1
        click.echo(f"  events:              {n:>6} mentions")

        # Event clusters
        n = 0
        for rec in scan_event_clusters(session, since):
            all_records.append(rec)
            n += 1
        click.echo(f"  event_clusters:      {n:>6} mentions")

    # Intelligence files (outside DB session)
    n = 0
    for rec in scan_intelligence_files(root / "intelligence", since):
        all_records.append(rec)
        n += 1
    click.echo(f"  intelligence files:  {n:>6} mentions")

    # Brief files
    n = 0
    for rec in scan_brief_files(root / "briefs", since):
        all_records.append(rec)
        n += 1
    click.echo(f"  brief files:         {n:>6} mentions")

    click.echo(f"\n  Total raw mentions scanned: {len(all_records)}")

    # Override week_key on all records so they land in the target week bucket
    # (the source-derived week_key is still stored as the natural occurred_at week)
    # — keep source week_key for history; only force if explicitly targeting a week.

    # Dedupe within this run (same ticker+source_type+source_id)
    seen: Set[tuple] = set()
    deduped: List[Dict] = []
    for rec in all_records:
        key = (rec["mentioned_ticker"], rec["source_type"], rec["source_id"])
        if key not in seen:
            seen.add(key)
            deduped.append(rec)
    click.echo(f"  After deduplication:        {len(deduped)}")

    excluded_count = sum(1 for r in deduped if r.get("exclusion_flag"))
    click.echo(f"  Excluded (prohibited/hedge): {excluded_count}")

    # ------------------------------------------------------------------
    # Phase 2: Persist mentions
    # ------------------------------------------------------------------
    click.echo("\nPhase 2: Persisting raw mentions to ticker_mentions table...")
    total_saved = 0
    with get_session() as session:
        # Save in batches
        batch: List[Dict] = []
        for rec in deduped:
            batch.append(rec)
            if len(batch) >= 500:
                saved = _save_mentions(session, batch, dry_run)
                total_saved += saved
                if not dry_run:
                    session.commit()
                batch = []
        if batch:
            saved = _save_mentions(session, batch, dry_run)
            total_saved += saved
            if not dry_run:
                session.commit()

    click.echo(f"  {'Would save' if dry_run else 'Saved'} {total_saved} new mention rows")

    # ------------------------------------------------------------------
    # Phase 3: Compute weekly scores
    # ------------------------------------------------------------------
    click.echo(f"\nPhase 3: Computing weekly discovery scores for {week_key}...")

    with get_session() as session:
        scored = compute_scores(
            session=session,
            week_key=week_key,
            default_tickers=default_tickers,
            universe_tickers=universe_tickers,
        )

        score_count = len(scored)
        if not dry_run:
            rows_saved = upsert_scores(session, scored)
            session.commit()
            click.echo(f"  Computed and saved {rows_saved} score rows for {score_count} tickers")
        else:
            click.echo(f"  Computed {score_count} score rows (dry run — not saved)")

    # ------------------------------------------------------------------
    # Phase 4: Print top candidates
    # ------------------------------------------------------------------
    candidates = [
        s for s in scored
        if not s["exclusion_flag"] and s["total_score"] >= min_score
    ]
    candidates.sort(key=lambda x: x["total_score"], reverse=True)
    probe_candidates = [c for c in candidates if c["recommendation"] == "probe_candidate"]

    click.echo(f"\n{'='*60}")
    click.echo(f"  TOP {top} DISCOVERED CANDIDATES (week {week_key})")
    click.echo(f"{'='*60}")
    click.echo(
        f"  {'TICKER':<8} {'SCORE':>6} {'MENTIONS':>8} {'ACCEL':>7} "
        f"{'VOL':>6} {'QUAL':>6} {'BRDTH':>6} {'NOVEL':>6}  REC"
    )
    click.echo("  " + "-" * 68)

    for s in candidates[:top]:
        click.echo(
            f"  {s['ticker']:<8} "
            f"{s['total_score']:>6.3f} "
            f"{s['mention_count']:>8} "
            f"{s['acceleration_score']:>7.3f} "
            f"{s['mention_volume_score']:>6.3f} "
            f"{s['source_quality_score']:>6.3f} "
            f"{s['breadth_score']:>6.3f} "
            f"{s['novelty_score']:>6.3f}  "
            f"{s['recommendation']}"
        )

    click.echo(f"\n  Probe candidates this week: {len(probe_candidates)}")

    # Excluded tickers summary
    excluded_scored = [s for s in scored if s["exclusion_flag"]]
    if excluded_scored:
        click.echo(
            f"\n  Excluded tickers encountered ({len(excluded_scored)}): "
            + ", ".join(s["ticker"] for s in excluded_scored[:15])
            + ("..." if len(excluded_scored) > 15 else "")
        )

    # ------------------------------------------------------------------
    # Phase 5: Optional promotion
    # ------------------------------------------------------------------
    if promote:
        click.echo(f"\nPhase 5: Promoting {len(probe_candidates)} probe_candidate(s)...")
        ai_tickers_path = root / "config" / "ai_tickers.json"
        newly_promoted = _promote_candidates(
            candidates=probe_candidates,
            ai_tickers_path=ai_tickers_path,
            dry_run=dry_run,
            week_key=week_key,
        )
        if newly_promoted:
            click.echo(f"  Promoted: {', '.join(newly_promoted)}")
        else:
            click.echo("  No new tickers promoted (all already in universe or none qualified)")

    click.echo(f"\n{'='*60}")
    click.echo("  Discovery radar run complete.")
    click.echo(f"{'='*60}\n")


if __name__ == "__main__":
    main()
