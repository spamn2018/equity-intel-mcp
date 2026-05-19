"""
Event classification logic.

Maps filing form types and 8-K items to structured event types.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

# Form type → broad event type
FORM_EVENT_MAP: Dict[str, str] = {
    # Institutional ownership disclosures
    "13F-HR": "institutional_holding",
    "13F-HR/A": "institutional_holding",
    "8-K": "other",           # refined by item
    "10-Q": "earnings",
    "10-K": "earnings",
    "S-1": "offering_or_dilution",
    "S-3": "offering_or_dilution",
    "424B1": "offering_or_dilution",
    "424B2": "offering_or_dilution",
    "424B3": "offering_or_dilution",
    "424B4": "offering_or_dilution",
    "424B5": "offering_or_dilution",
    "SC 13D": "activist_stake",
    "13D": "activist_stake",
    "SC 13G": "activist_stake",
    "13G": "activist_stake",
    "4": "insider_transaction",
    "144": "insider_transaction",
    "DEF 14A": "other",
}

# 8-K item → event type + subtype
ITEM_EVENT_MAP: Dict[str, Tuple[str, str]] = {
    "1.01": ("merger_acquisition", "material_agreement"),
    "1.02": ("merger_acquisition", "agreement_termination"),
    "1.03": ("bankruptcy_or_going_concern", "bankruptcy"),
    "2.01": ("merger_acquisition", "acquisition_completion"),
    "2.02": ("earnings", "results_of_operations"),
    "2.03": ("other", "debt_obligation"),
    "2.04": ("other", "triggering_event"),
    "2.05": ("other", "exit_costs"),
    "2.06": ("other", "material_impairment"),
    "3.01": ("other", "delisting_notice"),
    "3.02": ("offering_or_dilution", "unregistered_sale"),
    "4.01": ("restatement", "auditor_change"),
    "4.02": ("restatement", "non_reliance"),
    "5.01": ("management_change", "change_of_control"),
    "5.02": ("management_change", "director_officer_departure"),
    "5.03": ("other", "charter_amendment"),
    "5.07": ("other", "shareholder_vote"),
    "7.01": ("other", "reg_fd_disclosure"),
    "8.01": ("other", "other_events"),
    "9.01": ("other", "financial_statements"),
}

# Keywords that bump event type
KEYWORD_OVERRIDE_MAP: Dict[str, Tuple[str, str]] = {
    "bankruptcy": ("bankruptcy_or_going_concern", "bankruptcy"),
    "going concern": ("bankruptcy_or_going_concern", "going_concern"),
    "restatement": ("restatement", "financial_restatement"),
    "restated": ("restatement", "financial_restatement"),
    "material weakness": ("restatement", "material_weakness"),
    "sec investigation": ("regulatory", "sec_investigation"),
    "subpoena": ("litigation", "subpoena"),
    "class action": ("litigation", "class_action"),
    "securities fraud": ("litigation", "securities_fraud"),
    "doj": ("regulatory", "doj_investigation"),
    "fda approval": ("regulatory", "fda_approval"),
    "fda rejection": ("regulatory", "fda_rejection"),
    "complete response letter": ("regulatory", "fda_crl"),
    "merger": ("merger_acquisition", "merger"),
    "acquisition": ("merger_acquisition", "acquisition"),
    "tender offer": ("merger_acquisition", "tender_offer"),
    "offering": ("offering_or_dilution", "offering"),
    "dilution": ("offering_or_dilution", "dilution"),
    "reverse split": ("offering_or_dilution", "reverse_split"),
    "delisting": ("other", "delisting"),
    "strategic alternatives": ("merger_acquisition", "strategic_alternatives"),
    "guidance raised": ("earnings", "guidance_raised"),
    "guidance lowered": ("earnings", "guidance_lowered"),
    "guidance cut": ("earnings", "guidance_lowered"),
    "write-down": ("other", "write_down"),
    "impairment": ("other", "impairment"),
}


def classify_filing_event(
    form_type: str,
    items: Optional[str] = None,
    keywords: Optional[List[str]] = None,
    text_snippet: Optional[str] = None,
) -> Tuple[str, str]:
    """
    Classify a filing into (event_type, event_subtype).

    Priority:
    1. Keyword override (highest signal)
    2. 8-K item mapping
    3. Form type mapping
    4. Fallback to "other"
    """
    # Check keywords first
    if keywords:
        for kw in keywords:
            if kw in KEYWORD_OVERRIDE_MAP:
                return KEYWORD_OVERRIDE_MAP[kw]

    # Check 8-K items
    if items:
        item_list = [i.strip() for i in str(items).split(",") if i.strip()]
        for item in item_list:
            if item in ITEM_EVENT_MAP:
                return ITEM_EVENT_MAP[item]

    # Form type fallback
    event_type = FORM_EVENT_MAP.get(form_type.upper() if form_type else "", "other")
    return (event_type, form_type.lower().replace(" ", "_") if form_type else "unknown")
