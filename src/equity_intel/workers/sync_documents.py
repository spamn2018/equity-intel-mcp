"""
Worker: download and parse primary filing documents.

For each filing that has a primary_document_url but no parsed FilingDocument,
downloads the HTML, converts to plain text, extracts 8-K sections.

Usage:
    python -m equity_intel.workers.sync_documents
    python -m equity_intel.workers.sync_documents --tickers AAPL --limit 20
"""
from __future__ import annotations

import asyncio
from typing import List, Optional

import click

from equity_intel.config import settings
from equity_intel.db.models import Company, Filing, FilingDocument, now_utc
from equity_intel.db.session import get_session
from equity_intel.logging_config import configure_logging, get_logger
from equity_intel.sec.client import SECClient
from equity_intel.sec.parser import parse_filing_document

logger = get_logger(__name__)


def _has_document(session, filing_id: int) -> bool:
    return (
        session.query(FilingDocument)
        .filter(FilingDocument.filing_id == filing_id)
        .first()
        is not None
    )


async def download_and_parse_filing(
    session,
    client: SECClient,
    filing: Filing,
) -> Optional[FilingDocument]:
    """Download primary document for a filing, parse it, and store the result."""
    if not filing.primary_document_url:
        return None

    if _has_document(session, filing.id):
        logger.debug("document_already_parsed", filing_id=filing.id)
        return None

    logger.info(
        "downloading_document",
        accession=filing.accession_number,
        form=filing.form_type,
        url=filing.primary_document_url,
    )

    try:
        html = await client.get_filing_document(filing.primary_document_url)
    except Exception as exc:
        logger.error(
            "document_download_failed",
            accession=filing.accession_number,
            error=str(exc),
        )
        return None

    parsed = parse_filing_document(html, form_type=filing.form_type or "")

    now = now_utc()
    doc = FilingDocument(
        filing_id=filing.id,
        document_url=filing.primary_document_url,
        document_type=filing.form_type,
        filename=filing.primary_document,
        html_text=html[:500_000],  # cap at 500KB
        plain_text=parsed["plain_text"][:200_000],  # cap at 200KB
        parsed_sections_json={
            "sections": parsed["sections"],
            "detected_items": parsed["detected_items"],
            "keywords": parsed["keywords"],
            "char_count": parsed["char_count"],
        },
        created_at=now,
        updated_at=now,
    )
    session.add(doc)

    # Update filing items from parsed content if SEC didn't provide them
    if parsed["detected_items"] and not filing.items:
        filing.items = ",".join(parsed["detected_items"])
        filing.updated_at = now

    return doc


async def run(
    tickers: Optional[List[str]] = None,
    limit: int = 50,
    form_filter: Optional[List[str]] = None,
) -> None:
    configure_logging(settings.log_level)

    async with SECClient() as client:
        with get_session() as session:
            query = (
                session.query(Filing)
                .join(Company)
                .filter(Filing.primary_document_url.isnot(None))
            )
            if tickers:
                query = query.filter(Company.ticker.in_([t.upper() for t in tickers]))
            if form_filter:
                query = query.filter(Filing.form_type.in_(form_filter))

            # Only get filings without documents
            # Use scalar_subquery() for SQLAlchemy 2.0 compatibility (.subquery() triggers SAWarning)
            downloaded_ids = session.query(FilingDocument.filing_id).scalar_subquery()
            query = query.filter(Filing.id.notin_(downloaded_ids))
            query = query.order_by(Filing.filing_date.desc()).limit(limit)

            filings = query.all()
            logger.info("documents_to_download", count=len(filings))

            downloaded = 0
            for filing in filings:
                doc = await download_and_parse_filing(session, client, filing)
                if doc:
                    downloaded += 1
                session.flush()

            logger.info("documents_synced", downloaded=downloaded)


@click.command()
@click.option("--tickers", default=None, help="Comma-separated tickers")
@click.option("--limit", default=50, show_default=True, help="Max documents to download")
@click.option("--forms", default=None, help="Comma-separated form types")
def main(tickers: Optional[str], limit: int, forms: Optional[str]) -> None:
    """Download and parse filing documents."""
    ticker_list = [t.strip().upper() for t in tickers.split(",")] if tickers else None
    form_list = [f.strip() for f in forms.split(",")] if forms else None
    asyncio.run(run(ticker_list, limit=limit, form_filter=form_list))


if __name__ == "__main__":
    main()
