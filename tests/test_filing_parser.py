"""Tests for the filing document parser."""
from __future__ import annotations

import pytest

from equity_intel.sec.parser import (
    EIGHT_K_ITEMS,
    detect_keywords,
    extract_8k_items,
    html_to_plain_text,
    parse_filing_document,
    parse_items_field,
    truncate_text,
)


# ------------------------------------------------------------------ #
# html_to_plain_text                                                   #
# ------------------------------------------------------------------ #


def test_html_to_plain_text_basic():
    html = "<html><body><p>Hello world</p></body></html>"
    result = html_to_plain_text(html)
    assert "Hello world" in result


def test_html_to_plain_text_removes_scripts():
    html = "<html><body><script>alert('xss')</script><p>Clean</p></body></html>"
    result = html_to_plain_text(html)
    assert "alert" not in result
    assert "Clean" in result


def test_html_to_plain_text_removes_style():
    html = "<html><head><style>body{color:red}</style></head><body><p>Text</p></body></html>"
    result = html_to_plain_text(html)
    assert "color" not in result
    assert "Text" in result


def test_html_to_plain_text_handles_tables():
    html = "<table><tr><td>Cell 1</td><td>Cell 2</td></tr></table>"
    result = html_to_plain_text(html)
    assert "Cell 1" in result
    assert "Cell 2" in result


def test_html_to_plain_text_empty():
    result = html_to_plain_text("")
    assert result == ""


def test_html_to_plain_text_plain_string():
    result = html_to_plain_text("No HTML here")
    assert "No HTML here" in result


# ------------------------------------------------------------------ #
# extract_8k_items                                                     #
# ------------------------------------------------------------------ #

SAMPLE_8K_TEXT = """
UNITED STATES SECURITIES AND EXCHANGE COMMISSION

Item 2.02 Results of Operations and Financial Condition

On January 15, 2024, Acme Corp reported Q4 earnings exceeding analyst estimates.
Revenue was $10 billion, up 15% year-over-year.

Item 5.02 Departure of Directors or Principal Officers

John Smith has resigned as Chief Financial Officer effective immediately.
The board has begun a search for a replacement.

Item 9.01 Financial Statements and Exhibits

Exhibit 99.1 - Press Release
"""


def test_extract_8k_items_finds_items():
    sections = extract_8k_items(SAMPLE_8K_TEXT)
    assert "2.02" in sections
    assert "5.02" in sections
    assert "9.01" in sections


def test_extract_8k_items_content():
    sections = extract_8k_items(SAMPLE_8K_TEXT)
    assert "earnings" in sections["2.02"].lower()
    assert "resigned" in sections["5.02"].lower()


def test_extract_8k_items_empty_text():
    sections = extract_8k_items("")
    assert sections == {}


def test_extract_8k_items_no_items():
    text = "This is a 10-K annual report with no items."
    sections = extract_8k_items(text)
    assert sections == {}


def test_extract_8k_items_caps_section_length():
    long_content = "x" * 10000
    text = f"\nItem 1.01 Some heading\n{long_content}"
    sections = extract_8k_items(text)
    if "1.01" in sections:
        assert len(sections["1.01"]) <= 8000


# ------------------------------------------------------------------ #
# detect_keywords                                                      #
# ------------------------------------------------------------------ #


def test_detect_keywords_finds_bankruptcy():
    text = "The company has filed for bankruptcy protection under Chapter 11."
    kws = detect_keywords(text)
    assert "bankruptcy" in kws


def test_detect_keywords_finds_going_concern():
    text = "There is substantial doubt about the company's ability to continue as a going concern."
    kws = detect_keywords(text)
    assert "going concern" in kws


def test_detect_keywords_finds_merger():
    text = "The board approved a merger with XYZ Corp at a 25% premium."
    kws = detect_keywords(text)
    assert "merger" in kws


def test_detect_keywords_case_insensitive():
    text = "The SEC INVESTIGATION has been resolved."
    kws = detect_keywords(text)
    assert "sec investigation" in kws


def test_detect_keywords_empty():
    kws = detect_keywords("")
    assert kws == []


def test_detect_keywords_no_matches():
    kws = detect_keywords("Quarterly revenue increased by 10%.")
    assert kws == []


# ------------------------------------------------------------------ #
# parse_items_field                                                     #
# ------------------------------------------------------------------ #


def test_parse_items_field_standard():
    items = parse_items_field("2.02, 9.01")
    assert items == ["2.02", "9.01"]


def test_parse_items_field_none():
    assert parse_items_field(None) == []


def test_parse_items_field_empty():
    assert parse_items_field("") == []


def test_parse_items_field_single():
    assert parse_items_field("5.02") == ["5.02"]


# ------------------------------------------------------------------ #
# parse_filing_document                                                #
# ------------------------------------------------------------------ #


def test_parse_filing_document_8k():
    html = f"<html><body>{SAMPLE_8K_TEXT.replace(chr(10), '<br/>')}</body></html>"
    result = parse_filing_document(html, form_type="8-K")

    assert "plain_text" in result
    assert "sections" in result
    assert "detected_items" in result
    assert "keywords" in result
    assert result["char_count"] > 0


def test_parse_filing_document_detects_items():
    html = f"<html><body>{SAMPLE_8K_TEXT}</body></html>"
    result = parse_filing_document(html, form_type="8-K")
    assert len(result["detected_items"]) >= 2


# ------------------------------------------------------------------ #
# truncate_text                                                        #
# ------------------------------------------------------------------ #


def test_truncate_text_short():
    assert truncate_text("hello", 100) == "hello"


def test_truncate_text_long():
    long = "a" * 5000
    result = truncate_text(long, 100)
    assert len(result) == 101  # 100 + ellipsis char
    assert result.endswith("…")


def test_truncate_text_exact():
    text = "x" * 100
    assert truncate_text(text, 100) == text


# ------------------------------------------------------------------ #
# 8-K item registry sanity                                             #
# ------------------------------------------------------------------ #


def test_eight_k_items_contains_key_items():
    assert "2.02" in EIGHT_K_ITEMS
    assert "5.02" in EIGHT_K_ITEMS
    assert "4.02" in EIGHT_K_ITEMS
    assert "1.03" in EIGHT_K_ITEMS
