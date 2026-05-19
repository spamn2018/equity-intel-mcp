"""
13F-HR information-table XML parser.

SEC Form 13F-HR filings contain two parts:
  1. A cover-page document (HTML/text) with manager identity and report period.
  2. An information table (XML) listing every equity position held at quarter-end.

This module parses the XML information table and returns structured dicts ready
to upsert into the institutional_holdings table.

The SEC XML namespace has changed over the years; we strip it before parsing so
the same code handles all variants.

Reference schema:
  https://www.sec.gov/info/edgar/edgarfm-vol2-v59.pdf  (section 6.5)
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Namespace stripping
# ---------------------------------------------------------------------------

_NS_RE = re.compile(r'\s+xmlns(?::\w+)?="[^"]*"')


def _strip_namespaces(xml_text: str) -> str:
    """Remove all xmlns declarations so we can parse tag names directly."""
    # Also strip the namespace prefix from tags (e.g. ns0:infoTable → infoTable)
    cleaned = _NS_RE.sub("", xml_text)
    # Strip namespace prefixes from element names: <ns0:tag> → <tag>
    cleaned = re.sub(r"<(/?)[\w]+:([\w]+)", r"<\1\2", cleaned)
    return cleaned


# ---------------------------------------------------------------------------
# Helper: safe text extraction
# ---------------------------------------------------------------------------

def _text(element: Optional[ET.Element], tag: str) -> Optional[str]:
    if element is None:
        return None
    child = element.find(tag)
    if child is None or child.text is None:
        return None
    return child.text.strip() or None


def _int(element: Optional[ET.Element], tag: str) -> Optional[int]:
    raw = _text(element, tag)
    if raw is None:
        return None
    # Remove commas (some filers include them)
    raw = raw.replace(",", "").strip()
    try:
        return int(raw)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Cover-page parsing (extract report period and manager name from header text)
# ---------------------------------------------------------------------------

_PERIOD_RE = re.compile(
    r"(?:PERIOD\s+OF\s+REPORT|CONFORMED\s+PERIOD\s+OF\s+REPORT)\s*:\s*(\d{8}|\d{4}-\d{2}-\d{2})",
    re.IGNORECASE,
)
_COMPANY_RE = re.compile(
    r"COMPANY\s+CONFORMED\s+NAME\s*:\s*(.+)",
    re.IGNORECASE,
)
_CIK_RE = re.compile(
    r"CENTRAL\s+INDEX\s+KEY\s*:\s*(\d+)",
    re.IGNORECASE,
)


def parse_13f_header(header_text: str) -> Dict[str, Optional[str]]:
    """
    Extract manager name, CIK, and report period from the 13F cover-page text.

    Returns a dict with keys: manager_name, manager_cik, report_period (YYYY-MM-DD).
    Values are None if not found.
    """
    result: Dict[str, Optional[str]] = {
        "manager_name": None,
        "manager_cik": None,
        "report_period": None,
    }

    m = _COMPANY_RE.search(header_text)
    if m:
        result["manager_name"] = m.group(1).strip()

    m = _CIK_RE.search(header_text)
    if m:
        cik_raw = m.group(1).strip()
        result["manager_cik"] = cik_raw.zfill(10)

    m = _PERIOD_RE.search(header_text)
    if m:
        raw = m.group(1).strip()
        if len(raw) == 8:
            result["report_period"] = f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
        else:
            result["report_period"] = raw

    return result


# ---------------------------------------------------------------------------
# Information table XML parsing
# ---------------------------------------------------------------------------

def parse_13f_information_table(xml_text: str) -> List[Dict[str, Any]]:
    """
    Parse the 13F-HR information table XML and return a list of holding dicts.

    Each dict has:
      issuer_name       : str   – name of the held issuer (e.g. "APPLE INC")
      cusip             : str   – 9-character CUSIP
      title_of_class    : str   – e.g. "COM" (common stock)
      value_usd         : int   – market value in thousands of USD
      shares            : int   – number of shares or principal amount
      share_type        : str   – "SH" (shares) or "PRN" (principal)
      put_call          : str|None – "Put", "Call", or None
      investment_discretion : str – "SOLE", "SHARED", or "OTHER"
      voting_sole       : int|None
      voting_shared     : int|None
      voting_none       : int|None
      raw_json          : dict  – all parsed fields for storage

    Raises ValueError if the XML cannot be parsed at all.
    """
    if not xml_text or not xml_text.strip():
        return []

    cleaned = _strip_namespaces(xml_text)
    try:
        root = ET.fromstring(cleaned)
    except ET.ParseError as exc:
        raise ValueError(f"Failed to parse 13F information table XML: {exc}") from exc

    # The root may be <informationTable> (wrapping <infoTable> children)
    # or the root itself may be an <infoTable> in some edge cases.
    # Collect all <infoTable> elements at any depth.
    info_tables = root.findall(".//infoTable")
    if not info_tables:
        # Fallback: some filers use <InfoTable> (capitalized)
        info_tables = root.findall(".//InfoTable")

    holdings: List[Dict[str, Any]] = []
    for row in info_tables:
        issuer_name = _text(row, "nameOfIssuer")
        cusip = _text(row, "cusip")
        if not issuer_name and not cusip:
            continue  # skip empty rows

        title_of_class = _text(row, "titleOfClass")
        value_usd = _int(row, "value")

        # shrsOrPrnAmt block
        shr_block = row.find("shrsOrPrnAmt")
        if shr_block is None:
            shr_block = row.find("shrsOrPrnAmt")
        shares = _int(shr_block, "sshPrnamt") if shr_block is not None else None
        share_type = _text(shr_block, "sshPrnamtType") if shr_block is not None else None

        # putCall
        put_call_raw = _text(row, "putCall")
        put_call = put_call_raw if put_call_raw and put_call_raw.lower() not in ("", "none") else None

        investment_discretion = _text(row, "investmentDiscretion")

        # votingAuthority block
        voting_block = row.find("votingAuthority")
        voting_sole = _int(voting_block, "Sole") if voting_block is not None else None
        voting_shared = _int(voting_block, "Shared") if voting_block is not None else None
        voting_none = _int(voting_block, "None") if voting_block is not None else None

        # Normalize CUSIP (strip whitespace, uppercase)
        if cusip:
            cusip = cusip.strip().upper().replace("-", "")

        raw = {
            "issuer_name": issuer_name,
            "cusip": cusip,
            "title_of_class": title_of_class,
            "value_usd": value_usd,
            "shares": shares,
            "share_type": share_type,
            "put_call": put_call,
            "investment_discretion": investment_discretion,
            "voting_sole": voting_sole,
            "voting_shared": voting_shared,
            "voting_none": voting_none,
        }

        holdings.append({
            "issuer_name": issuer_name,
            "cusip": cusip,
            "title_of_class": title_of_class,
            "value_usd": value_usd,
            "shares": shares,
            "share_type": share_type or "SH",
            "put_call": put_call,
            "investment_discretion": investment_discretion or "SOLE",
            "raw_json": raw,
        })

    return holdings


# ---------------------------------------------------------------------------
# Quarter-over-quarter change detection
# ---------------------------------------------------------------------------

def compute_holding_changes(
    prev_holdings: List[Dict[str, Any]],
    curr_holdings: List[Dict[str, Any]],
    change_threshold_pct: float = 10.0,
) -> List[Dict[str, Any]]:
    """
    Compare two quarters of holdings and return a list of significant changes.

    Each change dict has:
      cusip             : str
      issuer_name       : str
      change_type       : "new_position" | "exit_position" | "major_increase" | "major_decrease"
      prev_shares       : int|None
      curr_shares       : int|None
      prev_value_usd    : int|None  (thousands)
      curr_value_usd    : int|None  (thousands)
      pct_change        : float|None
    """
    prev_map: Dict[str, Dict[str, Any]] = {}
    for h in prev_holdings:
        key = (h.get("cusip") or "").upper()
        if key:
            prev_map[key] = h

    curr_map: Dict[str, Dict[str, Any]] = {}
    for h in curr_holdings:
        key = (h.get("cusip") or "").upper()
        if key:
            curr_map[key] = h

    all_cusips = set(prev_map) | set(curr_map)
    changes: List[Dict[str, Any]] = []

    for cusip in all_cusips:
        prev = prev_map.get(cusip)
        curr = curr_map.get(cusip)

        prev_shares = prev.get("shares") if prev else None
        curr_shares = curr.get("shares") if curr else None
        prev_val = prev.get("value_usd") if prev else None
        curr_val = curr.get("value_usd") if curr else None
        issuer_name = (curr or prev or {}).get("issuer_name", "Unknown")

        if prev is None and curr is not None:
            change_type = "new_position"
            pct_change = None
        elif curr is None and prev is not None:
            change_type = "exit_position"
            pct_change = None
        else:
            # Both exist — check magnitude of change
            if not prev_shares or not curr_shares:
                continue
            pct_change = (curr_shares - prev_shares) / prev_shares * 100
            if abs(pct_change) < change_threshold_pct:
                continue
            change_type = "major_increase" if pct_change > 0 else "major_decrease"

        changes.append({
            "cusip": cusip,
            "issuer_name": issuer_name,
            "change_type": change_type,
            "prev_shares": prev_shares,
            "curr_shares": curr_shares,
            "prev_value_usd": prev_val,
            "curr_value_usd": curr_val,
            "pct_change": round(pct_change, 2) if pct_change is not None else None,
        })

    return changes
