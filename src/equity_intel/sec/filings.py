"""
SEC EDGAR filings sync.

Fetches recent filings from the submissions API for each company and upserts
them into the filings table. Also builds document URLs and queues documents
for download.
"""
from __future__ import annotations

import datetime
from typing import Any, Dict, List, Optional, Set

from sqlalchemy.orm import Session

from equity_intel.db.models import Company, Filing, now_utc
from equity_intel.logging_config import get_logger
from equity_intel.sec.client import (
    SECClient,
    build_filing_document_url,
    build_filing_index_url,
    normalize_cik,
)

logger = get_logger(__name__)

# Filing forms we prioritize
PRIORITY_FORMS = {
    "8-K", "10-Q", "10-K", "S-1", "S-3",
    "424B1", "424B2", "424B3", "424B4", "424B5",
    "DEF 14A", "13D", "13G", "SC 13D", "SC 13G",
    "4", "144",
    # Institutional ownership disclosures
    "13F-HR", "13F-HR/A",
}


def _parse_date(date_str: Optional[str]) -> Optional[datetime.datetime]:
    if not date_str:
        return None
    try:
        return datetime.datetime.strptime(date_str[:10], "%Y-%m-%d")
    except (ValueError, TypeError):
        return None


def _parse_acceptance(dt_str: Optional[str]) -> Optional[datetime.datetime]:
    if not dt_str:
        return None
    try:
        # Format: "2024-01-15T12:34:56.000000"
        return datetime.datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        try:
            return datetime.datetime.strptime(dt_str[:19], "%Y-%m-%dT%H:%M:%S")
        except (ValueError, TypeError):
            return None


def _build_accession_number(raw: str) -> str:
    """Normalize accession number to dashed format (XXXXXXXXXX-YY-ZZZZZZ)."""
    raw = raw.strip().replace(" ", "")
    if len(raw) == 18 and "-" not in raw:
        return f"{raw[:10]}-{raw[10:12]}-{raw[12:]}"
    return raw


def extract_filings_from_submissions(
    submissions: Dict[str, Any],
    company: Company,
    days: int = 90,
    form_filter: Optional[Set[str]] = None,
) -> List[Dict[str, Any]]:
    """
    Parse recent filings from a submissions JSON blob.

    Returns a list of dicts ready to upsert into the filings table.
    """
    recent = submissions.get("filings", {}).get("recent", {})
    if not recent:
        return []

    accession_numbers = recent.get("accessionNumber", [])
    form_types = recent.get("form", [])
    filing_dates = recent.get("filingDate", [])
    report_dates = recent.get("reportDate", [])
    primary_docs = recent.get("primaryDocument", [])
    acceptance_datetimes = recent.get("acceptanceDateTime", [])
    items_list = recent.get("items", [])

    cik = company.cik or normalize_cik(submissions.get("cik", "0"))
    cutoff = datetime.datetime.now() - datetime.timedelta(days=days)

    results: List[Dict[str, Any]] = []
    for i, acc in enumerate(accession_numbers):
        form_type = form_types[i] if i < len(form_types) else ""
        filing_date_str = filing_dates[i] if i < len(filing_dates) else ""
        filing_date = _parse_date(filing_date_str)

        if filing_date and filing_date < cutoff:
            # Stop early — filings are newest-first
            break

        if form_filter and form_type not in form_filter:
            continue

        acc_norm = _build_accession_number(acc)
        primary_doc = primary_docs[i] if i < len(primary_docs) else ""
        items = items_list[i] if i < len(items_list) else ""

        filing_url = build_filing_index_url(cik, acc_norm)
        primary_doc_url = (
            build_filing_document_url(cik, acc_norm, primary_doc) if primary_doc else None
        )

        results.append(
            {
                "company_id": company.id,
                "accession_number": acc_norm,
                "form_type": form_type,
                "filing_date": filing_date,
                "report_date": _parse_date(report_dates[i] if i < len(report_dates) else ""),
                "acceptance_datetime": _parse_acceptance(
                    acceptance_datetimes[i] if i < len(acceptance_datetimes) else ""
                ),
                "primary_document": primary_doc,
                "filing_url": filing_url,
                "primary_document_url": primary_doc_url,
                "sec_index_url": filing_url,
                "items": str(items) if items else None,
                "raw_metadata_json": {
                    "cik": cik,
                    "accession_number": acc_norm,
                    "form_type": form_type,
                    "filing_date": filing_date_str,
                    "primary_document": primary_doc,
                    "items": items,
                },
            }
        )

    return results


def upsert_filing(session: Session, data: Dict[str, Any]) -> Filing:
    """Insert or update a filing record. Returns the Filing ORM object."""
    acc = data["accession_number"]
    existing = session.query(Filing).filter(Filing.accession_number == acc).first()
    now = now_utc()

    if existing:
        for k, v in data.items():
            if v is not None and hasattr(existing, k):
                setattr(existing, k, v)
        existing.updated_at = now
        return existing

    filing = Filing(**data, created_at=now, updated_at=now)
    session.add(filing)
    return filing


async def sync_company_filings(
    session: Session,
    client: SECClient,
    company: Company,
    days: int = 90,
    form_filter: Optional[Set[str]] = None,
    force_refresh: bool = False,
) -> List[Filing]:
    """
    Fetch and store recent filings for a single company.

    Returns list of upserted Filing objects.
    """
    if not company.cik:
        logger.debug("no_cik_skipping_filings_sync", ticker=company.ticker)
        return []

    logger.info("sync_filings_start", ticker=company.ticker, cik=company.cik)

    if force_refresh:
        client.invalidate(
            f"https://data.sec.gov/submissions/CIK{company.cik}.json"
        )

    try:
        submissions = await client.get_submissions(company.cik)
    except Exception as exc:
        logger.error("submissions_fetch_failed", ticker=company.ticker, error=str(exc))
        return []

    filing_dicts = extract_filings_from_submissions(
        submissions, company, days=days, form_filter=form_filter
    )

    filings: List[Filing] = []
    for fd in filing_dicts:
        f = upsert_filing(session, fd)
        filings.append(f)

    logger.info(
        "sync_filings_done",
        ticker=company.ticker,
        total=len(filings),
    )
    return filings
