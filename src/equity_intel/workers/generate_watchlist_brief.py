"""
CLI worker: generate_watchlist_brief

Generates a ranked catalyst brief for a watchlist of tickers and prints
it as formatted JSON (or Markdown if --markdown is set).

Usage examples
--------------
# Use tickers from .env DEFAULT_TICKERS
equity-generate-watchlist-brief

# Specify tickers explicitly
equity-generate-watchlist-brief --tickers AAPL,MSFT,NVDA,TSLA

# Restrict to high-materiality events in the last 14 days
equity-generate-watchlist-brief --tickers AAPL,MSFT --days 14 --min-materiality 0.6

# Earnings only, Markdown output
equity-generate-watchlist-brief --tickers NVDA,AMD --event-types earnings,guidance --markdown

# Save to a file
equity-generate-watchlist-brief --tickers AAPL,MSFT,GOOGL --output brief.json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

import click

from equity_intel.briefs.watchlist import get_watchlist_brief
from equity_intel.config import settings
from equity_intel.db.session import SessionLocal
from equity_intel.logging_config import configure_logging, get_logger

logger = get_logger(__name__)


# ------------------------------------------------------------------ #
# Footer helper                                                        #
# ------------------------------------------------------------------ #


def _footer(brief: dict) -> str:
    """Return a consistent footer block for Markdown briefs."""
    note = brief.get("note", "")
    return (
        "\n---\n"
        f"_{note}_\n\n"
        "_⚠️ This output is **not investment advice**. "
        "Events are described as 'likely related to' or 'may reflect' market moves — "
        "not as confirmed causes. Always verify with primary sources before making "
        "any financial decisions._"
    )


# ------------------------------------------------------------------ #
# Markdown renderer                                                    #
# ------------------------------------------------------------------ #


def _render_markdown(brief: dict) -> str:
    """
    Convert a get_watchlist_brief result dict into a human-readable
    Markdown report.

    Sections
    --------
    - Header with generated timestamp, watchlist, window, total catalysts
    - Query Parameters (filters applied)
    - Summary prose
    - Caution block
    - Ranked catalyst entries (materiality, confidence, dates, price context,
      source links, related filings, related news)
    - Footer with source note and explicit not-investment-advice statement
    """
    lines: list[str] = []

    # -- Header -------------------------------------------------------
    lines.append("# Watchlist Catalyst Brief")
    lines.append(f"\n**Generated:** {brief.get('generated_at', 'unknown')}")
    watchlist = brief.get("watchlist", [])
    lines.append(f"**Watchlist:** {', '.join(watchlist) if watchlist else '(none)'}")
    lines.append(f"**Window:** {brief.get('time_window_days', '?')} day(s)")
    lines.append(f"**Total catalysts:** {brief.get('total_catalysts', 0)}")

    # -- Query Parameters ---------------------------------------------
    filters = brief.get("filters_applied", {})
    if filters:
        lines.append("\n## Query Parameters")
        lines.append(f"- **Min materiality:** {filters.get('min_materiality', 0.3)}")
        et = filters.get("event_types")
        if et:
            lines.append(f"- **Event types:** {', '.join(et)}")
        else:
            lines.append("- **Event types:** all")
        lines.append(f"- **Max items:** {filters.get('max_items', 20)}")
        lines.append(
            f"- **Include low confidence:** {filters.get('include_low_confidence', False)}"
        )

    # -- Summary ------------------------------------------------------
    lines.append(f"\n## Summary\n\n{brief.get('brief_summary', '')}")

    # -- Top-level caution --------------------------------------------
    lines.append(f"\n> **Caution:** {brief.get('caution', '')}")

    catalysts = brief.get("catalysts", [])
    if not catalysts:
        lines.append("\n_No catalysts found for the specified criteria._")
        lines.append(_footer(brief))
        return "\n".join(lines)

    # -- Catalyst entries ---------------------------------------------
    lines.append("\n## Catalysts")

    for i, cat in enumerate(catalysts, 1):
        ticker = cat.get("ticker", "?")
        company = cat.get("company_name") or ticker
        title = cat.get("title") or "Untitled event"
        mat = cat.get("materiality_score")
        conf = cat.get("confidence_score")
        evt_type = cat.get("event_type", "unknown")
        evt_sub = cat.get("event_subtype", "")

        lines.append(f"\n### {i}. [{ticker}] {title}")
        lines.append(f"**Company:** {company}")
        lines.append(
            f"**Event:** {evt_type}"
            + (f" / {evt_sub}" if evt_sub else "")
        )

        if mat is not None:
            score_line = f"**Materiality:** {mat:.2f}"
            if conf is not None:
                score_line += f"  |  **Confidence:** {conf:.2f}"
            lines.append(score_line)

        # Dates
        first_seen = cat.get("first_seen_at")
        last_seen = cat.get("last_seen_at")
        if first_seen or last_seen:
            date_parts = []
            if first_seen:
                date_parts.append(f"first seen {str(first_seen)[:10]}")
            if last_seen and last_seen != first_seen:
                date_parts.append(f"last seen {str(last_seen)[:10]}")
            lines.append(f"**When:** {', '.join(date_parts)}")

        lines.append(f"\n_{cat.get('why_it_matters', '')}_")

        # Price move
        price = cat.get("price_move")
        if price and price.get("pct_change") is not None:
            pct = price["pct_change"]
            direction = "▲" if pct >= 0 else "▼"
            lines.append(
                f"\n**Price move:** {direction} {abs(pct):.2f}% "
                f"({price.get('date_before')} → {price.get('date_after')})"
            )
        vol_ctx = cat.get("volume_context")
        if vol_ctx:
            lines.append(f"**Volume:** {vol_ctx}")

        # Source links
        links = cat.get("source_links", [])
        if links:
            lines.append("\n**Sources:**")
            for url in links[:3]:
                lines.append(f"- {url}")

        # Related filings
        filings = cat.get("related_filings", [])
        if filings:
            lines.append("\n**Related filings:**")
            for f in filings[:3]:
                acc = f.get("accession_number", "?")
                form = f.get("form_type", "?")
                date = f.get("filing_date", "?")
                url = f.get("url", "")
                line = f"- {form} filed {date} ({acc})"
                if url:
                    line += f" — [{url}]({url})"
                lines.append(line)

        # Related news
        news = cat.get("related_news", [])
        if news:
            lines.append("\n**Related news:**")
            for n in news[:3]:
                pub = n.get("publisher", "?")
                ndate = (n.get("published_at") or "?")[:10]
                ntitle = n.get("title", "?")
                nurl = n.get("url", "")
                line = (
                    f"- [{ntitle}]({nurl}) — {pub}, {ndate}"
                    if nurl
                    else f"- {ntitle} — {pub}, {ndate}"
                )
                lines.append(line)

        caution = cat.get("caution")
        if caution:
            lines.append(f"\n> ⚠️ {caution}")

    lines.append(_footer(brief))
    return "\n".join(lines)


# ------------------------------------------------------------------ #
# CLI                                                                  #
# ------------------------------------------------------------------ #


@click.command("equity-generate-watchlist-brief")
@click.option(
    "--tickers",
    default=None,
    help=(
        "Comma-separated ticker list (e.g. AAPL,MSFT,NVDA). "
        "Defaults to DEFAULT_TICKERS from .env."
    ),
)
@click.option(
    "--days",
    default=7,
    show_default=True,
    type=int,
    help="Look-back window in calendar days.",
)
@click.option(
    "--min-materiality",
    default=0.3,
    show_default=True,
    type=float,
    help="Minimum materiality score [0, 1].",
)
@click.option(
    "--include-low-confidence",
    is_flag=True,
    default=False,
    help="Include catalysts with confidence_score < 0.3.",
)
@click.option(
    "--max-items",
    default=20,
    show_default=True,
    type=int,
    help="Maximum number of catalysts to return.",
)
@click.option(
    "--event-types",
    default=None,
    help=(
        "Comma-separated event type filter "
        "(e.g. earnings,guidance,merger_acquisition). "
        "Omit for all types."
    ),
)
@click.option(
    "--no-price",
    is_flag=True,
    default=False,
    help="Omit price move / volume context from output.",
)
@click.option(
    "--no-news",
    is_flag=True,
    default=False,
    help="Omit linked news articles from output.",
)
@click.option(
    "--no-filings",
    is_flag=True,
    default=False,
    help="Omit linked SEC filings from output.",
)
@click.option(
    "--markdown",
    is_flag=True,
    default=False,
    help="Render output as Markdown instead of JSON.",
)
@click.option(
    "--output",
    default=None,
    type=click.Path(),
    help="Write output to this file path instead of stdout.",
)
@click.option(
    "--log-level",
    default="warning",
    show_default=True,
    help="Logging level (debug, info, warning, error).",
)
def main(
    tickers: Optional[str],
    days: int,
    min_materiality: float,
    include_low_confidence: bool,
    max_items: int,
    event_types: Optional[str],
    no_price: bool,
    no_news: bool,
    no_filings: bool,
    markdown: bool,
    output: Optional[str],
    log_level: str,
) -> None:
    """
    Generate a ranked catalyst brief for a watchlist of tickers.

    Reads from the equity_intel database. Run the sync workers first
    to populate data (sync_companies, sync_filings, sync_news, sync_prices,
    build_events, cluster_events).
    """
    configure_logging(log_level)

    # Resolve tickers
    if tickers:
        ticker_list = [t.strip().upper() for t in tickers.split(",") if t.strip()]
    else:
        ticker_list = settings.tickers_list

    if not ticker_list:
        click.echo("Error: no tickers specified and DEFAULT_TICKERS is empty in .env.", err=True)
        sys.exit(1)

    # Resolve event_types
    event_types_list = None
    if event_types:
        event_types_list = [e.strip() for e in event_types.split(",") if e.strip()]

    logger.info(
        "generating_brief",
        tickers=ticker_list,
        days=days,
        min_materiality=min_materiality,
    )

    session = SessionLocal()
    try:
        brief = get_watchlist_brief(
            session=session,
            tickers=ticker_list,
            days=days,
            min_materiality=min_materiality,
            include_low_confidence=include_low_confidence,
            max_items=max_items,
            event_types=event_types_list,
            include_price_context=not no_price,
            include_news=not no_news,
            include_filings=not no_filings,
        )
    finally:
        session.close()

    # Render
    if markdown:
        rendered = _render_markdown(brief)
    else:
        rendered = json.dumps(brief, default=str, indent=2)

    # Output
    if output:
        out_path = Path(output)
        out_path.write_text(rendered, encoding="utf-8")
        click.echo(f"Brief written to {out_path}")
        click.echo(
            f"  {brief['total_catalysts']} catalyst(s) across "
            f"{len(brief['watchlist'])} ticker(s) | "
            f"window: {brief['time_window_days']}d | "
            f"min_materiality: {brief['filters_applied']['min_materiality']}"
        )
    else:
        click.echo(rendered)


if __name__ == "__main__":
    main()
