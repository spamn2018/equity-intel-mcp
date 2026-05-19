"""
Filing document parser.

Converts SEC EDGAR HTML filings to plain text and extracts structured sections,
particularly 8-K item headings.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

import warnings

from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

from equity_intel.logging_config import get_logger

# SEC EDGAR filings often contain XML declarations inside HTML-structured documents.
# Suppress the BeautifulSoup warning — lxml handles them correctly for our purposes.
warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

logger = get_logger(__name__)

# 8-K items we care about
EIGHT_K_ITEMS: Dict[str, str] = {
    "1.01": "Material Definitive Agreement",
    "1.02": "Termination of Material Definitive Agreement",
    "1.03": "Bankruptcy or Receivership",
    "2.01": "Completion of Acquisition or Disposition",
    "2.02": "Results of Operations and Financial Condition",
    "2.03": "Creation of a Direct Financial Obligation",
    "2.04": "Triggering Events",
    "2.05": "Exit or Disposal Costs",
    "2.06": "Material Impairments",
    "3.01": "Notice of Delisting",
    "3.02": "Unregistered Sales of Equity Securities",
    "3.03": "Material Modification to Rights of Security Holders",
    "4.01": "Changes in Registrant's Certifying Accountant",
    "4.02": "Non-Reliance on Previously Issued Financial Statements",
    "5.01": "Changes in Control of Registrant",
    "5.02": "Departure of Directors or Principal Officers",
    "5.03": "Amendments to Articles of Incorporation",
    "5.05": "Amendments to the Registrant's Code of Ethics",
    "5.07": "Submission of Matters to a Vote of Security Holders",
    "5.08": "Shareholder Director Nominations",
    "6.01": "ABS Informational and Computational Material",
    "7.01": "Regulation FD Disclosure",
    "8.01": "Other Events",
    "9.01": "Financial Statements and Exhibits",
}

# Pattern to detect item headings like "Item 2.02", "ITEM 1.01", "Item 5.02."
ITEM_HEADING_RE = re.compile(
    r"(?:^|\n)\s*(?:Item|ITEM)\s+(\d+\.\d+)[\.\s–—-]",
    re.MULTILINE,
)

# High-impact keywords for event scoring
HIGH_IMPACT_KEYWORDS = frozenset(
    [
        "bankruptcy",
        "going concern",
        "restatement",
        "subpoena",
        "investigation",
        "sec investigation",
        "doj",
        "fda approval",
        "fda rejection",
        "complete response letter",
        "merger",
        "acquisition",
        "tender offer",
        "offering",
        "dilution",
        "reverse split",
        "delisting",
        "resignation",
        "termination",
        "guidance lowered",
        "guidance raised",
        "strategic alternatives",
        "material weakness",
        "going private",
        "hostile takeover",
        "poison pill",
        "rights plan",
        "whistleblower",
        "class action",
        "securities fraud",
        "ponzi",
        "restated",
        "write-down",
        "write-off",
        "impairment",
    ]
)


def html_to_plain_text(html: str) -> str:
    """Convert HTML filing document to clean plain text."""
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")

    # Remove script/style tags
    for tag in soup(["script", "style", "head", "meta", "link"]):
        tag.decompose()

    # Extract text preserving block structure
    lines: List[str] = []
    for element in soup.find_all(["p", "div", "span", "td", "th", "li", "h1", "h2", "h3", "h4", "h5", "h6", "br", "tr"]):
        text = element.get_text(" ", strip=True)
        if text:
            lines.append(text)

    if not lines:
        # Fallback: just get all text
        return soup.get_text("\n", strip=True)

    result = "\n".join(lines)
    # Collapse excessive blank lines
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result.strip()


def extract_8k_items(plain_text: str) -> Dict[str, str]:
    """
    Extract 8-K item sections from plain text.

    Returns a dict mapping item number -> text for that section.
    """
    matches = list(ITEM_HEADING_RE.finditer(plain_text))
    if not matches:
        return {}

    sections: Dict[str, str] = {}
    for i, match in enumerate(matches):
        item_num = match.group(1)
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(plain_text)
        section_text = plain_text[start:end].strip()
        if section_text:
            sections[item_num] = section_text[:8000]  # cap per section

    return sections


def detect_item_numbers_from_text(plain_text: str) -> List[str]:
    """Return list of 8-K item numbers found in the text."""
    return list(extract_8k_items(plain_text).keys())


def parse_items_field(items_str: Optional[str]) -> List[str]:
    """Parse the comma-separated items field from SEC submissions JSON."""
    if not items_str:
        return []
    return [i.strip() for i in items_str.split(",") if i.strip()]


def detect_keywords(text: str) -> List[str]:
    """Return list of high-impact keywords found in text (lowercased)."""
    lower = text.lower()
    return [kw for kw in HIGH_IMPACT_KEYWORDS if kw in lower]


def parse_filing_document(html: str, form_type: str = "") -> Dict[str, object]:
    """
    Full parse of a filing document.

    Returns:
        plain_text: full plain text
        sections: dict of section name -> text (for 8-K)
        detected_items: list of 8-K item numbers
        keywords: list of matched high-impact keywords
        char_count: length of plain text
    """
    plain_text = html_to_plain_text(html)
    sections: Dict[str, str] = {}
    detected_items: List[str] = []

    if "8-K" in form_type.upper() or not form_type:
        sections = extract_8k_items(plain_text)
        detected_items = list(sections.keys())

    keywords = detect_keywords(plain_text)

    return {
        "plain_text": plain_text,
        "sections": sections,
        "detected_items": detected_items,
        "keywords": keywords,
        "char_count": len(plain_text),
    }


def truncate_text(text: str, max_chars: int = 4000) -> str:
    """Truncate text to max_chars, adding ellipsis if needed."""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "…"
