"""
13F-HR holdings sync worker.

Pulls quarterly institutional-ownership filings (Form 13F-HR) from SEC EDGAR
for a configurable list of manager CIKs, parses the XML information table,
upserts InstitutionalHolding rows, and emits Event records for significant
quarter-over-quarter position changes (new, exit, major increase/decrease)
on companies we already track.

Usage
-----
    # Load all managers from config/manager_watchlist.json (recommended)
    python -m equity_intel.workers.sync_13f --from-config --days 120

    # Or specify CIKs directly
    python -m equity_intel.workers.sync_13f \
        --manager-ciks 0001067983 0000315066 0001364742 \
        --days 120

Or call ``sync_managers()`` directly from a scheduler.

Manager CIK examples
--------------------
  Berkshire Hathaway        : 0001067983
  Vanguard Group            : 0000102909
  BlackRock                 : 0001364742
  State Street              : 0000093751
  Fidelity (FMR)            : 0000315066
  Situational Awareness LP  : 0002045724
  See config/manager_watchlist.json for the full AI-focused watchlist.
"""
from __future__ import annotations

import asyncio
import datetime
import json
import pathlib
import re
from typing import Any, Dict, List, Optional, Set

from sqlalchemy.orm import Session

from equity_intel.db.models import Company, Event, Filing, InstitutionalHolding, now_utc
from equity_intel.db.session import SessionLocal, create_all_tables
from equity_intel.events.classify import classify_filing_event
from equity_intel.events.score import compute_confidence_score, compute_materiality_score
from equity_intel.logging_config import configure_logging, get_logger
from equity_intel.sec.client import SECClient, normalize_cik
from equity_intel.sec.filings import (
    _build_accession_number,
    _parse_date,
    upsert_filing,
)
from equity_intel.sec.parser_13f import (
    compute_holding_changes,
    parse_13f_header,
    parse_13f_information_table,
)

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ACCESSION_NODASH_RE = re.compile(r"[\-]")

SEC_FILING_INDEX_URL = (
    "https://www.sec.gov/Archives/edgar/data/{cik_nodash}/{accession_nodash}/"
    "{accession_nodash}-index.json"
)
SEC_FILING_DOC_URL = (
    "https://www.sec.gov/Archives/edgar/data/{cik_nodash}/{accession_nodash}/{filename}"
)

# Keywords that indicate a 13F information table XML
_INFO_TABLE_KEYWORDS = {"informationtable", "information_table", "13finfotable"}


# ---------------------------------------------------------------------------
# Filing index helpers
# ---------------------------------------------------------------------------

def _accession_nodash(accession_number: str) -> str:
    """Convert '0001067983-24-000001' → '0001067983240000001'."""
    return _ACCESSION_NODASH_RE.sub("", accession_number)


def _find_info_table_filename(index_data: Dict[str, Any]) -> Optional[str]:
    """
    Search the filing index JSON for the information table XML document.

    The index JSON structure (from SEC EDGAR) looks like:
      {
        "directory": {
          "item": [
            {"name": "informationTable.xml", "type": "INFORMATION TABLE"},
            ...
          ]
        }
      }
    """
    items = (
        index_data.get("directory", {}).get("item", [])
        or index_data.get("items", [])
    )
    if not items:
        return None

    for item in items:
        name = (item.get("name") or "").strip()
        doc_type = (item.get("type") or "").strip().upper()

        # Prefer explicit type match
        if "INFORMATION TABLE" in doc_type:
            return name

        # Fall back to filename heuristics
        name_lower = name.lower()
        if name_lower.endswith(".xml"):
            name_stripped = name_lower.replace(".", "").replace("_", "").replace("-", "")
            if any(kw in name_stripped for kw in _INFO_TABLE_KEYWORDS):
                return name

    # Last resort: any XML file
    for item in items:
        name = (item.get("name") or "").strip()
        if name.lower().endswith(".xml"):
            return name

    return None


async def _fetch_info_table_xml(
    client: SECClient,
    cik: str,
    accession_number: str,
) -> Optional[str]:
    """
    Given a manager CIK and accession number, find and download the
    information table XML from the filing index.

    Returns the XML text, or None on failure.
    """
    cik_nodash = cik.lstrip("0") or "0"
    acc_nodash = _accession_nodash(accession_number)

    index_url = SEC_FILING_INDEX_URL.format(
        cik_nodash=cik_nodash,
        accession_nodash=acc_nodash,
    )

    try:
        index_data = await client.get_json(index_url, cache=True)
    except Exception as exc:
        logger.warning("13f_index_fetch_failed", url=index_url, error=str(exc))
        return None

    filename = _find_info_table_filename(index_data)
    if not filename:
        logger.warning("13f_no_info_table_found", accession=accession_number)
        return None

    doc_url = SEC_FILING_DOC_URL.format(
        cik_nodash=cik_nodash,
        accession_nodash=acc_nodash,
        filename=filename,
    )

    try:
        xml_text = await client.get_text(doc_url, cache=True)
        return xml_text
    except Exception as exc:
        logger.warning("13f_xml_fetch_failed", url=doc_url, error=str(exc))
        return None


# ---------------------------------------------------------------------------
# Company name → company_id resolver
# ---------------------------------------------------------------------------

def _build_name_index(session: Session) -> Dict[str, int]:
    """Return a dict mapping normalized company name → company.id."""
    rows = session.query(Company.id, Company.name, Company.ticker).all()
    index: Dict[str, int] = {}
    for company_id, name, ticker in rows:
        if name:
            index[name.upper().strip()] = company_id
        if ticker:
            index[ticker.upper().strip()] = company_id
    return index


def _resolve_company(
    issuer_name: Optional[str],
    name_index: Dict[str, int],
    session: Session,
) -> tuple[Optional[int], Optional[str]]:
    """
    Try to resolve issuer_name to (company_id, ticker) using our companies table.

    Matching strategy (in order):
    1. Exact name match (upper)
    2. Issuer name starts with known company name (handles "APPLE INC" matching "APPLE")
    3. No match → (None, None)
    """
    if not issuer_name:
        return None, None

    upper = issuer_name.upper().strip()

    # Exact match
    if upper in name_index:
        company_id = name_index[upper]
        company = session.get(Company, company_id)
        return company_id, company.ticker if company else None

    # Prefix match: "APPLE INC" ↔ "APPLE"
    for known_name, company_id in name_index.items():
        if len(known_name) >= 3 and (
            upper.startswith(known_name) or known_name.startswith(upper)
        ):
            company = session.get(Company, company_id)
            return company_id, company.ticker if company else None

    return None, None


# ---------------------------------------------------------------------------
# Upsert helpers
# ---------------------------------------------------------------------------

def _upsert_holding(
    session: Session,
    data: Dict[str, Any],
) -> InstitutionalHolding:
    """Insert or update an InstitutionalHolding row."""
    existing = (
        session.query(InstitutionalHolding)
        .filter(
            InstitutionalHolding.filing_id == data["filing_id"],
            InstitutionalHolding.cusip == data.get("cusip"),
            InstitutionalHolding.share_type == data.get("share_type"),
        )
        .first()
    )
    now = now_utc()
    if existing:
        for k, v in data.items():
            if hasattr(existing, k) and v is not None:
                setattr(existing, k, v)
        existing.updated_at = now
        return existing

    holding = InstitutionalHolding(**data, created_at=now, updated_at=now)
    session.add(holding)
    return holding


def _emit_event(
    session: Session,
    ticker: Optional[str],
    company_id: Optional[int],
    change: Dict[str, Any],
    manager_name: Optional[str],
    filing: Filing,
) -> None:
    """Create an Event row for a significant 13F position change."""
    change_type = change["change_type"]
    issuer_name = change.get("issuer_name", "Unknown")
    pct_change = change.get("pct_change")

    # Build readable title / summary
    manager_label = manager_name or f"CIK {filing.company_id}"
    if change_type == "new_position":
        title = f"{manager_label} opened new position in {issuer_name}"
        summary = (
            f"{manager_label} initiated a new position in {issuer_name} "
            f"({change.get('curr_shares', 0):,} shares, "
            f"${(change.get('curr_value_usd') or 0) * 1000:,.0f}) "
            f"as of {filing.report_date or 'N/A'}."
        )
    elif change_type == "exit_position":
        title = f"{manager_label} exited position in {issuer_name}"
        summary = (
            f"{manager_label} fully exited its position in {issuer_name} "
            f"(had {change.get('prev_shares', 0):,} shares). "
            f"Reported in 13F-HR filed {filing.filing_date or 'N/A'}."
        )
    elif change_type == "major_increase":
        title = f"{manager_label} increased {issuer_name} position by {pct_change:.0f}%"
        summary = (
            f"{manager_label} increased its {issuer_name} position by {pct_change:.1f}% "
            f"to {change.get('curr_shares', 0):,} shares "
            f"(from {change.get('prev_shares', 0):,})."
        )
    else:  # major_decrease
        title = f"{manager_label} reduced {issuer_name} position by {abs(pct_change or 0):.0f}%"
        summary = (
            f"{manager_label} reduced its {issuer_name} position by {abs(pct_change or 0):.1f}% "
            f"to {change.get('curr_shares', 0):,} shares "
            f"(from {change.get('prev_shares', 0):,})."
        )

    # Score
    mat = compute_materiality_score(
        form_type="13F-HR",
        occurred_at=filing.filing_date,
        source_type="filing",
    )
    # Boost for exits and new positions from large managers
    if change_type in ("new_position", "exit_position"):
        mat = min(1.0, mat + 0.10)
    conf = compute_confidence_score(has_parsed_text=True, source_quality=0.9)

    event_type = "institutional_holding"
    event_subtype = change_type

    # Dedup: skip if we already have this event (same source filing + subtype)
    existing = (
        session.query(Event)
        .filter(
            Event.source_type == "filing",
            Event.source_id == filing.id,
            Event.event_subtype == event_subtype,
            Event.ticker == ticker,
        )
        .first()
    )
    # For new/exit events on the same filing+ticker the check above is sufficient.
    # For increase/decrease, we also check CUSIP via evidence_json — skip for MVP.
    if existing:
        return

    occurred_at = (
        datetime.datetime.combine(filing.filing_date, datetime.time.min)
        .replace(tzinfo=datetime.timezone.utc)
        if isinstance(filing.filing_date, datetime.date)
        else filing.filing_date
    )

    now = now_utc()
    event = Event(
        company_id=company_id,
        ticker=ticker,
        event_type=event_type,
        event_subtype=event_subtype,
        title=title,
        summary=summary,
        source_type="filing",
        source_id=filing.id,
        source_url=filing.filing_url,
        occurred_at=occurred_at,
        detected_at=now,
        materiality_score=mat,
        confidence_score=conf,
        novelty_score=0.8,
        evidence_json={
            "change": change,
            "manager_name": manager_name,
            "accession_number": filing.accession_number,
            "filing_url": filing.filing_url,
        },
        created_at=now,
        updated_at=now,
    )
    session.add(event)


# ---------------------------------------------------------------------------
# Per-filing sync
# ---------------------------------------------------------------------------

async def sync_13f_filing(
    session: Session,
    client: SECClient,
    filing: Filing,
    manager_cik: str,
    manager_name: Optional[str],
    tracked_company_ids: Set[int],
    name_index: Dict[str, int],
) -> int:
    """
    Download, parse, and store holdings for a single 13F-HR filing.

    Returns the number of holding rows upserted.
    """
    xml_text = await _fetch_info_table_xml(client, manager_cik, filing.accession_number)
    if not xml_text:
        return 0

    try:
        raw_holdings = parse_13f_information_table(xml_text)
    except ValueError as exc:
        logger.warning(
            "13f_parse_failed",
            accession=filing.accession_number,
            error=str(exc),
        )
        return 0

    report_date = filing.report_date
    filing_date = filing.filing_date
    upserted = 0

    for h in raw_holdings:
        company_id, resolved_ticker = _resolve_company(
            h.get("issuer_name"), name_index, session
        )

        data: Dict[str, Any] = {
            "filing_id": filing.id,
            "manager_cik": manager_cik,
            "manager_name": manager_name,
            "issuer_name": h.get("issuer_name"),
            "cusip": h.get("cusip"),
            "title_of_class": h.get("title_of_class"),
            "value_usd": h.get("value_usd"),
            "shares": h.get("shares"),
            "share_type": h.get("share_type"),
            "put_call": h.get("put_call"),
            "investment_discretion": h.get("investment_discretion"),
            "report_date": report_date,
            "filing_date": filing_date,
            "ticker": resolved_ticker,
            "company_id": company_id,
            "raw_json": h.get("raw_json"),
        }

        _upsert_holding(session, data)
        upserted += 1

    session.flush()
    logger.info(
        "13f_holdings_upserted",
        accession=filing.accession_number,
        count=upserted,
    )

    # ------------------------------------------------------------------ #
    # Quarter-over-quarter change detection                               #
    # ------------------------------------------------------------------ #

    # Fetch the immediately preceding 13F-HR for this manager (if any)
    prev_filing = (
        session.query(Filing)
        .filter(
            Filing.company_id == filing.company_id,
            Filing.form_type.in_(["13F-HR", "13F-HR/A"]),
            Filing.filing_date < filing.filing_date,
        )
        .order_by(Filing.filing_date.desc())
        .first()
    )

    if prev_filing:
        prev_holdings_orm = (
            session.query(InstitutionalHolding)
            .filter(InstitutionalHolding.filing_id == prev_filing.id)
            .all()
        )
        prev_dicts = [
            {
                "cusip": h.cusip,
                "issuer_name": h.issuer_name,
                "shares": h.shares,
                "value_usd": h.value_usd,
                "share_type": h.share_type,
            }
            for h in prev_holdings_orm
        ]
        curr_dicts = [
            {
                "cusip": h.get("cusip"),
                "issuer_name": h.get("issuer_name"),
                "shares": h.get("shares"),
                "value_usd": h.get("value_usd"),
                "share_type": h.get("share_type"),
            }
            for h in raw_holdings
        ]

        changes = compute_holding_changes(prev_dicts, curr_dicts)

        for change in changes:
            # Only emit events for companies we track
            cusip = (change.get("cusip") or "").upper()
            # Look up company for this CUSIP via the holding we just stored
            held = (
                session.query(InstitutionalHolding)
                .filter(
                    InstitutionalHolding.filing_id == filing.id,
                    InstitutionalHolding.cusip == cusip,
                )
                .first()
            )
            if held and held.company_id in tracked_company_ids:
                _emit_event(
                    session, held.ticker, held.company_id, change, manager_name, filing
                )

    return upserted


# ---------------------------------------------------------------------------
# Manager sync
# ---------------------------------------------------------------------------

async def sync_manager(
    session: Session,
    client: SECClient,
    manager_cik: str,
    days: int = 120,
    tracked_company_ids: Optional[Set[int]] = None,
    name_index: Optional[Dict[str, int]] = None,
) -> Dict[str, Any]:
    """
    Sync all recent 13F-HR filings for a single institutional manager.

    Returns a summary dict with counts.
    """
    manager_cik = normalize_cik(manager_cik)
    logger.info("13f_manager_sync_start", manager_cik=manager_cik)

    if tracked_company_ids is None:
        tracked_company_ids = {c.id for c in session.query(Company.id).all()}

    if name_index is None:
        name_index = _build_name_index(session)

    # Fetch submissions for this manager
    try:
        submissions = await client.get_submissions(manager_cik)
    except Exception as exc:
        logger.error("13f_submissions_failed", manager_cik=manager_cik, error=str(exc))
        return {"manager_cik": manager_cik, "error": str(exc)}

    manager_name = submissions.get("name") or submissions.get("entityType")

    recent = submissions.get("filings", {}).get("recent", {})
    accession_numbers = recent.get("accessionNumber", [])
    form_types = recent.get("form", [])
    filing_dates = recent.get("filingDate", [])
    report_dates = recent.get("reportDate", [])
    primary_docs = recent.get("primaryDocument", [])

    cutoff = datetime.datetime.now() - datetime.timedelta(days=days)

    # Find or create a Company row for the manager itself
    # (13F filer = the manager company, not the held stocks)
    manager_company = (
        session.query(Company)
        .filter(Company.cik == manager_cik)
        .first()
    )
    if not manager_company:
        manager_company = Company(
            ticker=f"_MGR_{manager_cik}",
            cik=manager_cik,
            name=manager_name or manager_cik,
            is_active=True,
            created_at=now_utc(),
            updated_at=now_utc(),
        )
        session.add(manager_company)
        session.flush()

    filings_synced = 0
    holdings_synced = 0

    for i, acc in enumerate(accession_numbers):
        form_type = form_types[i] if i < len(form_types) else ""
        if form_type not in ("13F-HR", "13F-HR/A"):
            continue

        filing_date_str = filing_dates[i] if i < len(filing_dates) else ""
        filing_date = _parse_date(filing_date_str)
        if filing_date and filing_date < cutoff:
            break  # filings are newest-first

        acc_norm = _build_accession_number(acc)
        primary_doc = primary_docs[i] if i < len(primary_docs) else ""
        report_date_str = report_dates[i] if i < len(report_dates) else ""

        from equity_intel.sec.client import build_filing_index_url, build_filing_document_url
        filing_url = build_filing_index_url(manager_cik, acc_norm)
        primary_doc_url = (
            build_filing_document_url(manager_cik, acc_norm, primary_doc)
            if primary_doc else None
        )

        filing_data = {
            "company_id": manager_company.id,
            "accession_number": acc_norm,
            "form_type": form_type,
            "filing_date": filing_date,
            "report_date": _parse_date(report_date_str),
            "primary_document": primary_doc,
            "filing_url": filing_url,
            "primary_document_url": primary_doc_url,
            "sec_index_url": filing_url,
            "raw_metadata_json": {
                "manager_cik": manager_cik,
                "manager_name": manager_name,
                "form_type": form_type,
            },
        }
        filing = upsert_filing(session, filing_data)
        session.flush()

        count = await sync_13f_filing(
            session, client, filing, manager_cik, manager_name,
            tracked_company_ids, name_index,
        )
        filings_synced += 1
        holdings_synced += count

    session.commit()
    logger.info(
        "13f_manager_sync_done",
        manager_cik=manager_cik,
        filings=filings_synced,
        holdings=holdings_synced,
    )
    return {
        "manager_cik": manager_cik,
        "manager_name": manager_name,
        "filings_synced": filings_synced,
        "holdings_synced": holdings_synced,
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def sync_managers(
    manager_ciks: List[str],
    days: int = 120,
) -> List[Dict[str, Any]]:
    """Sync 13F-HR filings for a list of manager CIKs."""
    create_all_tables()
    session = SessionLocal()

    try:
        tracked_ids = {row.id for row in session.query(Company.id).all()}
        name_index = _build_name_index(session)

        results = []
        async with SECClient() as client:
            for cik in manager_ciks:
                result = await sync_manager(
                    session, client, cik,
                    days=days,
                    tracked_company_ids=tracked_ids,
                    name_index=name_index,
                )
                results.append(result)
        return results
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------

_CONFIG_DEFAULT = pathlib.Path(__file__).parent.parent.parent.parent / "config" / "manager_watchlist.json"


def load_ciks_from_config(config_path: Optional[pathlib.Path] = None) -> List[str]:
    """
    Read manager CIKs from config/manager_watchlist.json.

    Returns a flat, deduplicated list of CIK strings.
    The config file groups managers by category — this flattens all of them.
    """
    path = config_path or _CONFIG_DEFAULT
    if not path.exists():
        raise FileNotFoundError(
            f"Manager watchlist not found at {path}. "
            "Either create it or pass --manager-ciks directly."
        )

    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    ciks: List[str] = []
    seen: set = set()
    for group_key, group_val in data.items():
        if group_key.startswith("_"):
            continue
        if not isinstance(group_val, dict):
            continue
        for manager in group_val.get("managers", []):
            cik = manager.get("cik", "").strip()
            if cik and cik not in seen:
                ciks.append(cik)
                seen.add(cik)

    return ciks


def main() -> None:
    import argparse

    configure_logging("info")
    parser = argparse.ArgumentParser(
        description="Sync 13F-HR institutional holdings from SEC EDGAR",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Use the pre-configured AI-focused manager watchlist (recommended):
  python -m equity_intel.workers.sync_13f --from-config

  # Specify managers directly:
  python -m equity_intel.workers.sync_13f --manager-ciks 0001067983 0001364742

  # Load from a custom config file:
  python -m equity_intel.workers.sync_13f --from-config --config-path /path/to/watchlist.json
        """,
    )
    parser.add_argument(
        "--manager-ciks",
        nargs="+",
        default=[],
        help="One or more manager CIK strings (e.g. 0001067983 for Berkshire Hathaway)",
    )
    parser.add_argument(
        "--from-config",
        action="store_true",
        default=False,
        help="Load manager CIKs from config/manager_watchlist.json instead of (or in addition to) --manager-ciks",
    )
    parser.add_argument(
        "--config-path",
        type=pathlib.Path,
        default=None,
        help="Path to a custom manager_watchlist.json (defaults to config/manager_watchlist.json in project root)",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=120,
        help="Look-back window in days (default 120 ≈ one quarter)",
    )
    args = parser.parse_args()

    ciks: List[str] = list(args.manager_ciks)

    if args.from_config:
        config_ciks = load_ciks_from_config(args.config_path)
        existing = set(ciks)
        ciks.extend(c for c in config_ciks if c not in existing)
        print(f"Loaded {len(config_ciks)} manager CIKs from config ({len(ciks)} total after merge).")

    if not ciks:
        parser.error("No manager CIKs provided. Use --manager-ciks or --from-config.")

    results = asyncio.run(sync_managers(ciks, days=args.days))
    print(json.dumps(results, indent=2, default=str))


if __name__ == "__main__":
    main()
