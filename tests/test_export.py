"""
Tests for Phase 3: Email / Slack / Markdown Export.

Coverage:
  Markdown rendering:
    - required sections present (header, generated_at, watchlist, window,
      summary, caution, catalysts header, query parameters, footer)
    - materiality and confidence scores rendered
    - first_seen / last_seen dates rendered
    - price context rendered
    - related filings rendered
    - related news rendered
    - source links rendered
    - event types filter shown when set
    - no investment advice language (output says "not investment advice")
    - cautious causality language ("likely related" / "may reflect")
    - no catalysts renders gracefully
    - note footer present

  LocalFileDelivery:
    - creates JSON file
    - creates Markdown file
    - returns status="ok"
    - returns destination path
    - creates parent directories
    - bytes_written > 0
    - idempotent (second write overwrites first)
    - JSON content is valid and round-trips
    - Markdown content contains header

  DeliveryAdapter:
    - is abstract (cannot be instantiated)

  Export module:
    - DeliveryAdapter and LocalFileDelivery importable from equity_intel.export

  No network calls:
    - LocalFileDelivery makes zero HTTP calls
"""
from __future__ import annotations

import json
from abc import ABC
from pathlib import Path
from typing import Any, Dict
from unittest.mock import patch

import httpx
import pytest

from equity_intel.export import DeliveryAdapter, LocalFileDelivery
from equity_intel.workers.generate_watchlist_brief import _render_markdown


# ------------------------------------------------------------------ #
# Shared fixtures / helpers                                            #
# ------------------------------------------------------------------ #


def _make_brief(
    tickers=None,
    total_catalysts=1,
    days=7,
    with_price=True,
    with_filings=True,
    with_news=True,
    event_types_filter=None,
) -> Dict[str, Any]:
    """Return a fully populated brief dict for test use."""
    catalysts = [
        {
            "ticker": "NVDA",
            "company_name": "NVIDIA Corporation",
            "sector": "Technology",
            "title": "NVDA Q4 Earnings Beat",
            "event_type": "earnings",
            "event_subtype": "results_of_operations",
            "why_it_matters": (
                "This high-materiality event may reflect a material change "
                "in revenue or profitability expectations. The available evidence "
                "suggests the stock moved up 8.50% around this event, though this "
                "may reflect broader market factors rather than this catalyst alone."
            ),
            "materiality_score": 0.91,
            "confidence_score": 0.82,
            "novelty_score": 0.70,
            "first_seen_at": "2024-02-21T21:00:00+00:00",
            "last_seen_at": "2024-02-22T09:00:00+00:00",
            "event_count": 3,
            "filing_count": 1,
            "news_count": 2,
            "price_move": (
                {
                    "pct_change": 8.5,
                    "volume_ratio": 3.2,
                    "price_before": 612.0,
                    "price_after": 664.0,
                    "date_before": "2024-02-21",
                    "date_after": "2024-02-22",
                }
                if with_price
                else None
            ),
            "volume_context": "Volume was approximately 3.2× normal" if with_price else None,
            "source_links": ["https://sec.gov/nvda-8k-2024", "https://nvda.com/ir"],
            "related_filing_ids": [42],
            "related_news_ids": [7, 8],
            "related_filings": (
                [
                    {
                        "accession_number": "0001045810-24-000005",
                        "form_type": "8-K",
                        "filing_date": "2024-02-21",
                        "items": "2.02",
                        "url": "https://sec.gov/nvda-8k-2024",
                    }
                ]
                if with_filings
                else []
            ),
            "related_news": (
                [
                    {
                        "title": "Nvidia crushes Q4 earnings estimates",
                        "publisher": "Reuters",
                        "published_at": "2024-02-21T22:00:00+00:00",
                        "url": "https://reuters.com/nvda-q4",
                        "summary": "Nvidia posted record revenue.",
                    }
                ]
                if with_news
                else []
            ),
            "caution": (
                "This brief shows correlation, not causation. "
                "Always verify with primary sources before acting."
            ),
            "data_source": "event_clusters",
            "cluster_id": 10,
            "cluster_key": "NVDA:earnings:2024W08",
        }
    ] * total_catalysts

    return {
        "generated_at": "2024-02-22T07:00:00+00:00",
        "watchlist": tickers or ["NVDA", "AAPL"],
        "time_window_days": days,
        "filters_applied": {
            "min_materiality": 0.3,
            "include_low_confidence": False,
            "max_items": 20,
            "event_types": event_types_filter,
            "include_price_context": with_price,
            "include_news": with_news,
            "include_filings": with_filings,
        },
        "brief_summary": (
            f"Over the past {days} day(s), {total_catalysts} catalyst(s) were identified "
            "across 1 ticker(s) (NVDA). 1 are rated high-materiality (score >= 0.7). "
            "The top-ranked catalyst is 'NVDA Q4 Earnings Beat' for NVDA "
            "(materiality 0.91). The available evidence suggests these events may be "
            "material; always verify with primary sources."
        ),
        "total_catalysts": total_catalysts,
        "catalysts": catalysts,
        "note": (
            "Source URLs are provided for all results. "
            "Dates are in UTC. Summaries are AI-generated from filing text. "
            "This is not investment advice."
        ),
        "caution": (
            "This brief shows correlation, not causation. "
            "Events are described as 'likely related to' or 'may reflect' market moves — "
            "not as confirmed causes. Always verify with primary sources before acting."
        ),
    }


def _make_empty_brief() -> Dict[str, Any]:
    """Brief with no catalysts."""
    return {
        "generated_at": "2024-02-22T07:00:00+00:00",
        "watchlist": ["TSLA"],
        "time_window_days": 1,
        "filters_applied": {
            "min_materiality": 0.5,
            "include_low_confidence": False,
            "max_items": 20,
            "event_types": None,
            "include_price_context": True,
            "include_news": True,
            "include_filings": True,
        },
        "brief_summary": "No catalysts meeting the specified criteria were found for TSLA over the past 1 day(s).",
        "total_catalysts": 0,
        "catalysts": [],
        "note": "Source URLs are provided for all results. This is not investment advice.",
        "caution": "This brief shows correlation, not causation.",
    }


# ------------------------------------------------------------------ #
# 1. Markdown: required sections                                       #
# ------------------------------------------------------------------ #


def test_markdown_has_title_header():
    out = _render_markdown(_make_brief())
    assert "# Watchlist Catalyst Brief" in out


def test_markdown_has_generated_at():
    out = _render_markdown(_make_brief())
    assert "2024-02-22T07:00:00" in out


def test_markdown_has_watchlist_tickers():
    out = _render_markdown(_make_brief(tickers=["NVDA", "AAPL"]))
    assert "NVDA" in out
    assert "AAPL" in out


def test_markdown_has_time_window():
    out = _render_markdown(_make_brief(days=14))
    assert "14 day" in out


def test_markdown_has_query_parameters_section():
    out = _render_markdown(_make_brief())
    assert "## Query Parameters" in out


def test_markdown_query_parameters_shows_min_materiality():
    out = _render_markdown(_make_brief())
    assert "Min materiality" in out
    assert "0.3" in out


def test_markdown_query_parameters_shows_all_event_types():
    out = _render_markdown(_make_brief(event_types_filter=None))
    assert "Event types: all" in out or "Event types:** all" in out


def test_markdown_query_parameters_shows_specific_event_types():
    out = _render_markdown(_make_brief(event_types_filter=["earnings", "guidance"]))
    assert "earnings" in out
    assert "guidance" in out


def test_markdown_has_summary_section():
    out = _render_markdown(_make_brief())
    assert "## Summary" in out


def test_markdown_summary_contains_brief_summary_text():
    brief = _make_brief()
    out = _render_markdown(brief)
    # The summary text should appear verbatim (or at least the key phrase)
    assert "catalyst(s) were identified" in out


def test_markdown_has_caution_block():
    out = _render_markdown(_make_brief())
    assert "Caution" in out or "caution" in out.lower()


def test_markdown_has_catalysts_section():
    out = _render_markdown(_make_brief())
    assert "## Catalysts" in out


def test_markdown_has_note_footer():
    brief = _make_brief()
    out = _render_markdown(brief)
    assert "Source URLs are provided" in out


# ------------------------------------------------------------------ #
# 2. Markdown: per-catalyst fields                                     #
# ------------------------------------------------------------------ #


def test_markdown_shows_materiality_score():
    out = _render_markdown(_make_brief())
    assert "0.91" in out or "Materiality" in out


def test_markdown_shows_confidence_score():
    out = _render_markdown(_make_brief())
    assert "0.82" in out or "Confidence" in out


def test_markdown_shows_first_seen_date():
    out = _render_markdown(_make_brief())
    assert "first seen" in out.lower()
    assert "2024-02-21" in out


def test_markdown_shows_price_context():
    out = _render_markdown(_make_brief(with_price=True))
    assert "8.50" in out or "8.5" in out
    assert "Price move" in out or "price move" in out.lower()


def test_markdown_shows_source_links():
    out = _render_markdown(_make_brief())
    assert "https://sec.gov/nvda-8k-2024" in out


def test_markdown_shows_related_filings():
    out = _render_markdown(_make_brief(with_filings=True))
    assert "Related filings" in out
    assert "0001045810-24-000005" in out or "8-K" in out


def test_markdown_shows_related_news():
    out = _render_markdown(_make_brief(with_news=True))
    assert "Related news" in out
    assert "Nvidia crushes" in out


# ------------------------------------------------------------------ #
# 3. Markdown: safety / language requirements                          #
# ------------------------------------------------------------------ #


def test_markdown_not_investment_advice_in_footer():
    out = _render_markdown(_make_brief())
    assert "not investment advice" in out.lower()


def test_markdown_not_investment_advice_in_empty_brief():
    out = _render_markdown(_make_empty_brief())
    assert "not investment advice" in out.lower()


def test_markdown_cautious_causality_language():
    """Renderer must preserve 'may reflect' / 'likely related to' hedging."""
    out = _render_markdown(_make_brief())
    assert "may reflect" in out or "likely related" in out


def test_markdown_no_catalysts_renders_gracefully():
    out = _render_markdown(_make_empty_brief())
    assert "# Watchlist Catalyst Brief" in out
    assert "No catalysts found" in out
    # Should still have footer
    assert "not investment advice" in out.lower()


# ------------------------------------------------------------------ #
# 4. LocalFileDelivery: file creation                                  #
# ------------------------------------------------------------------ #


def test_local_delivery_creates_json_file(tmp_path):
    brief = _make_brief()
    path = tmp_path / "brief.json"
    adapter = LocalFileDelivery()
    adapter.deliver(brief, path, "json")
    assert path.exists()


def test_local_delivery_creates_markdown_file(tmp_path):
    brief = _make_brief()
    path = tmp_path / "brief.md"
    adapter = LocalFileDelivery()
    adapter.deliver(brief, path, "markdown")
    assert path.exists()


def test_local_delivery_json_is_valid(tmp_path):
    brief = _make_brief()
    path = tmp_path / "brief.json"
    LocalFileDelivery().deliver(brief, path, "json")
    parsed = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)
    assert "catalysts" in parsed
    assert "watchlist" in parsed


def test_local_delivery_markdown_has_header(tmp_path):
    brief = _make_brief()
    path = tmp_path / "brief.md"
    LocalFileDelivery().deliver(brief, path, "markdown")
    content = path.read_text(encoding="utf-8")
    assert "# Watchlist Catalyst Brief" in content


# ------------------------------------------------------------------ #
# 5. LocalFileDelivery: return value                                   #
# ------------------------------------------------------------------ #


def test_local_delivery_returns_ok_status(tmp_path):
    result = LocalFileDelivery().deliver(_make_brief(), tmp_path / "b.json", "json")
    assert result["status"] == "ok"


def test_local_delivery_returns_destination(tmp_path):
    path = tmp_path / "b.json"
    result = LocalFileDelivery().deliver(_make_brief(), path, "json")
    assert "destination" in result
    assert str(path) in result["destination"] or result["destination"] == str(path)


def test_local_delivery_returns_bytes_written(tmp_path):
    result = LocalFileDelivery().deliver(_make_brief(), tmp_path / "b.json", "json")
    assert result.get("bytes_written", 0) > 0


def test_local_delivery_returns_fmt(tmp_path):
    result = LocalFileDelivery().deliver(_make_brief(), tmp_path / "b.md", "markdown")
    assert result.get("fmt") == "markdown"


# ------------------------------------------------------------------ #
# 6. LocalFileDelivery: parent directories                             #
# ------------------------------------------------------------------ #


def test_local_delivery_creates_parent_dirs(tmp_path):
    nested = tmp_path / "a" / "b" / "c" / "brief.json"
    assert not nested.parent.exists()
    LocalFileDelivery().deliver(_make_brief(), nested, "json")
    assert nested.exists()


# ------------------------------------------------------------------ #
# 7. LocalFileDelivery: idempotency                                    #
# ------------------------------------------------------------------ #


def test_local_delivery_idempotent_json(tmp_path):
    path = tmp_path / "brief.json"
    adapter = LocalFileDelivery()

    brief_v1 = _make_brief(tickers=["NVDA"], total_catalysts=1)
    brief_v2 = _make_brief(tickers=["AAPL", "MSFT"], total_catalysts=0)
    brief_v2["catalysts"] = []

    adapter.deliver(brief_v1, path, "json")
    adapter.deliver(brief_v2, path, "json")

    parsed = json.loads(path.read_text(encoding="utf-8"))
    assert parsed["watchlist"] == ["AAPL", "MSFT"]
    assert parsed["total_catalysts"] == 0


# ------------------------------------------------------------------ #
# 8. DeliveryAdapter: abstract base                                    #
# ------------------------------------------------------------------ #


def test_delivery_adapter_is_abstract():
    assert issubclass(DeliveryAdapter, ABC)
    with pytest.raises(TypeError):
        DeliveryAdapter()  # type: ignore[abstract]


def test_delivery_adapter_has_deliver_method():
    import inspect
    assert hasattr(DeliveryAdapter, "deliver")
    assert inspect.isabstract(DeliveryAdapter)


# ------------------------------------------------------------------ #
# 9. Export module imports                                             #
# ------------------------------------------------------------------ #


def test_export_module_exposes_delivery_adapter():
    from equity_intel.export import DeliveryAdapter as DA
    assert DA is DeliveryAdapter


def test_export_module_exposes_local_file_delivery():
    from equity_intel.export import LocalFileDelivery as LFD
    assert LFD is LocalFileDelivery


def test_local_file_delivery_is_subclass_of_adapter():
    assert issubclass(LocalFileDelivery, DeliveryAdapter)


# ------------------------------------------------------------------ #
# 10. No network calls                                                 #
# ------------------------------------------------------------------ #


def test_local_delivery_makes_no_network_calls(tmp_path):
    """LocalFileDelivery must never touch the network."""

    def explode(*args, **kwargs):
        raise AssertionError("LocalFileDelivery must not make HTTP calls")

    brief = _make_brief()
    with patch.object(httpx.Client, "get", explode):
        with patch.object(httpx.AsyncClient, "get", explode):
            LocalFileDelivery().deliver(brief, tmp_path / "brief.json", "json")
    # reaching here = no network calls were made
