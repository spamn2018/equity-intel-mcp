"""
XBRL company facts sync.

Fetches company facts from the SEC EDGAR XBRL API and normalizes
selected financial concepts into the company_facts table.
"""
from __future__ import annotations

import datetime
from typing import Any, Dict, List, Optional, Set

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from equity_intel.db.models import Company, CompanyFact, now_utc
from equity_intel.logging_config import get_logger
from equity_intel.sec.client import SECClient

logger = get_logger(__name__)

# Concepts to prioritize (taxonomy -> set of concept names)
PRIORITY_CONCEPTS: Dict[str, Set[str]] = {
    "us-gaap": {
        "Revenues",
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "SalesRevenueNet",
        "NetIncomeLoss",
        "GrossProfit",
        "OperatingIncomeLoss",
        "EarningsPerShareBasic",
        "EarningsPerShareDiluted",
        "CashAndCashEquivalentsAtCarryingValue",
        "Assets",
        "Liabilities",
        "LongTermDebt",
        "ShortTermBorrowings",
        "NetCashProvidedByUsedInOperatingActivities",
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "CommonStockSharesOutstanding",
        "StockholdersEquity",
        "RetainedEarningsAccumulatedDeficit",
    },
    "dei": {
        "EntityCommonStockSharesOutstanding",
        "EntityPublicFloat",
    },
}


def _parse_fact_date(date_str: Optional[str]) -> Optional[datetime.datetime]:
    if not date_str:
        return None
    try:
        return datetime.datetime.strptime(date_str[:10], "%Y-%m-%d")
    except (ValueError, TypeError):
        return None


def _extract_facts_from_json(
    company: Company,
    data: Dict[str, Any],
    priority_only: bool = True,
) -> List[Dict[str, Any]]:
    """
    Parse the company facts JSON and return a list of normalized fact dicts.
    """
    facts_section = data.get("facts", {})
    results: List[Dict[str, Any]] = []

    for taxonomy, concepts in facts_section.items():
        allowed_concepts = PRIORITY_CONCEPTS.get(taxonomy) if priority_only else None

        for concept, concept_data in concepts.items():
            if allowed_concepts and concept not in allowed_concepts:
                continue

            label = concept_data.get("label", concept)
            description = concept_data.get("description", "")
            units_data = concept_data.get("units", {})

            for unit, entries in units_data.items():
                for entry in entries:
                    # Skip entries without end dates
                    end_date = entry.get("end")
                    if not end_date:
                        continue

                    # Prefer annual/quarterly data
                    form = entry.get("form", "")
                    fp = entry.get("fp", "")
                    fy = entry.get("fy")
                    accession = entry.get("accn", "")
                    filed = entry.get("filed")
                    val = entry.get("val")

                    if val is None:
                        continue

                    results.append(
                        {
                            "company_id": company.id,
                            "taxonomy": taxonomy,
                            "concept": concept,
                            "label": label[:512] if label else None,
                            "description": description[:1000] if description else None,
                            "unit": unit,
                            "value": float(val),
                            "fiscal_year": int(fy) if fy else None,
                            "fiscal_period": fp or None,
                            "form_type": form or None,
                            "filed_date": _parse_fact_date(filed),
                            "end_date": _parse_fact_date(end_date),
                            "accession_number": accession or None,
                            "raw_json": entry,
                        }
                    )

    return results


def upsert_company_fact(session: Session, data: Dict[str, Any]) -> Optional[CompanyFact]:
    """Insert or skip a company fact (unique on company+taxonomy+concept+end_date+period+accession).

    Uses a nested transaction (savepoint) so that a duplicate-key violation on a single
    fact does not roll back the entire session — important when bulk-inserting XBRL data
    where the same data point can appear under multiple frames.
    """
    fact = CompanyFact(**data, created_at=now_utc())
    try:
        with session.begin_nested():
            session.add(fact)
        return fact
    except IntegrityError:
        # Already present — skip silently
        return None


async def sync_company_facts(
    session: Session,
    client: SECClient,
    company: Company,
    priority_only: bool = True,
) -> int:
    """
    Fetch and store XBRL facts for a single company.

    Returns the number of facts upserted.
    """
    if not company.cik:
        logger.warning("no_cik_for_company_facts", ticker=company.ticker)
        return 0

    logger.info("sync_facts_start", ticker=company.ticker, cik=company.cik)

    try:
        data = await client.get_company_facts(company.cik)
    except Exception as exc:
        logger.error("facts_fetch_failed", ticker=company.ticker, error=str(exc))
        return 0

    fact_dicts = _extract_facts_from_json(company, data, priority_only=priority_only)

    count = 0
    for fd in fact_dicts:
        upsert_company_fact(session, fd)
        count += 1
        if count % 500 == 0:
            session.flush()

    logger.info("sync_facts_done", ticker=company.ticker, count=count)
    return count
