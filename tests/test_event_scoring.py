"""Tests for event classification and scoring."""
from __future__ import annotations

import datetime

import pytest

from equity_intel.events.classify import (
    ITEM_EVENT_MAP,
    classify_filing_event,
)
from equity_intel.events.score import (
    compute_confidence_score,
    compute_materiality_score,
)


# ------------------------------------------------------------------ #
# classify_filing_event                                                #
# ------------------------------------------------------------------ #


def test_classify_8k_item_202():
    event_type, subtype = classify_filing_event("8-K", items="2.02")
    assert event_type == "earnings"
    assert subtype == "results_of_operations"


def test_classify_8k_item_502():
    event_type, subtype = classify_filing_event("8-K", items="5.02")
    assert event_type == "management_change"
    assert subtype == "director_officer_departure"


def test_classify_8k_item_403():
    event_type, subtype = classify_filing_event("8-K", items="4.02")
    assert event_type == "restatement"
    assert subtype == "non_reliance"


def test_classify_keyword_bankruptcy_overrides_form():
    event_type, subtype = classify_filing_event("8-K", items="9.01", keywords=["bankruptcy"])
    assert event_type == "bankruptcy_or_going_concern"


def test_classify_keyword_merger():
    event_type, subtype = classify_filing_event("8-K", keywords=["merger"])
    assert event_type == "merger_acquisition"


def test_classify_form_s1():
    event_type, subtype = classify_filing_event("S-1")
    assert event_type == "offering_or_dilution"


def test_classify_form_13d():
    event_type, subtype = classify_filing_event("13D")
    assert event_type == "activist_stake"


def test_classify_form_4():
    event_type, subtype = classify_filing_event("4")
    assert event_type == "insider_transaction"


def test_classify_unknown_form_fallback():
    event_type, subtype = classify_filing_event("UNKNOWN_FORM")
    assert event_type == "other"


def test_classify_multiple_items_picks_first_match():
    # 1.03 = bankruptcy; 9.01 = financial_statements
    event_type, subtype = classify_filing_event("8-K", items="1.03,9.01")
    assert event_type == "bankruptcy_or_going_concern"


# ------------------------------------------------------------------ #
# compute_materiality_score                                            #
# ------------------------------------------------------------------ #


def test_materiality_8k_base():
    score = compute_materiality_score(form_type="8-K")
    assert 0.5 <= score <= 0.75


def test_materiality_bankruptcy_keyword():
    score_no_kw = compute_materiality_score(form_type="8-K")
    score_kw = compute_materiality_score(form_type="8-K", keywords=["bankruptcy"])
    assert score_kw > score_no_kw


def test_materiality_item_202_adds_delta():
    base = compute_materiality_score(form_type="8-K")
    with_item = compute_materiality_score(form_type="8-K", items="2.02")
    assert with_item > base


def test_materiality_item_103_high():
    score = compute_materiality_score(form_type="8-K", items="1.03")
    assert score >= 0.8


def test_materiality_capped_at_1():
    # Pile on all signals
    score = compute_materiality_score(
        form_type="8-K",
        items="1.03,4.02",
        keywords=["bankruptcy", "going concern", "restatement", "sec investigation"],
        occurred_at=datetime.datetime.now(datetime.timezone.utc),
    )
    assert score <= 1.0


def test_materiality_news_lower_than_filing():
    filing_score = compute_materiality_score(form_type="8-K", source_type="filing")
    news_score = compute_materiality_score(form_type="8-K", source_type="news")
    assert news_score < filing_score


def test_materiality_recency_boost():
    recent = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=1)
    old = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=30)

    score_recent = compute_materiality_score(form_type="8-K", occurred_at=recent)
    score_old = compute_materiality_score(form_type="8-K", occurred_at=old)
    assert score_recent > score_old


def test_materiality_144_low():
    score = compute_materiality_score(form_type="144")
    assert score < 0.4


def test_materiality_always_non_negative():
    score = compute_materiality_score(form_type=None)
    assert score >= 0.0


# ------------------------------------------------------------------ #
# compute_confidence_score                                             #
# ------------------------------------------------------------------ #


def test_confidence_baseline():
    score = compute_confidence_score()
    assert score == 0.5


def test_confidence_with_parsed_text():
    score = compute_confidence_score(has_parsed_text=True)
    assert score > 0.5


def test_confidence_with_price_reaction():
    score = compute_confidence_score(has_price_reaction=True)
    assert score > 0.5


def test_confidence_full_evidence():
    score = compute_confidence_score(
        has_parsed_text=True, has_price_reaction=True, keyword_count=5
    )
    assert score == 1.0


def test_confidence_capped_at_1():
    score = compute_confidence_score(
        has_parsed_text=True, has_price_reaction=True, keyword_count=100
    )
    assert score <= 1.0
