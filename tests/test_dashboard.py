"""
Dashboard smoke tests.

Tests cover:
- Dashboard HTML is served (index route)
- /api/tickers returns configured tickers
- /api/event_types returns the known event type list
- /api/bias returns a response (empty or populated)
- /api/brief returns a valid brief structure (empty state when DB is empty)
- Filters are accepted without crashing (min_mat, days, event_types, etc.)
- /api/intelligence/latest returns correct structure and excludes gemini_news files
- Source links are present in the response schema
- No live network calls are made (all DB interactions are mocked)
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from equity_intel.dashboard.app import KNOWN_EVENT_TYPES, create_app


# ------------------------------------------------------------------ #
# Fixtures                                                             #
# ------------------------------------------------------------------ #


@pytest.fixture()
def client():
    """
    Flask test client with a mocked SessionLocal so tests never touch
    a real database or make any live network calls.
    """
    app = create_app()
    app.config["TESTING"] = True

    # Patch SessionLocal used inside the /api/brief route.
    with patch("equity_intel.dashboard.app.SessionLocal") as MockSession:
        mock_session = MagicMock()
        MockSession.return_value = mock_session

        # get_watchlist_brief is called inside the route — patch it at
        # the module level so no DB queries run.
        with patch("equity_intel.dashboard.app.get_watchlist_brief") as mock_brief:
            mock_brief.return_value = {
                "generated_at": "2026-05-11T10:00:00+00:00",
                "watchlist": ["AAPL", "MSFT"],
                "time_window_days": 7,
                "filters_applied": {
                    "min_materiality": 0.3,
                    "include_low_confidence": False,
                    "max_items": 30,
                    "event_types": None,
                    "include_price_context": True,
                    "include_news": True,
                    "include_filings": True,
                },
                "brief_summary": "2 tickers tracked. 0 catalysts found.",
                "total_catalysts": 0,
                "catalysts": [],
                "note": "Source URLs are provided for all results.",
                "caution": "This brief shows correlation, not causation.",
            }

            with app.test_client() as c:
                yield c, mock_brief


@pytest.fixture()
def client_with_catalysts():
    """
    Flask test client that returns a brief with two sample catalysts.
    """
    app = create_app()
    app.config["TESTING"] = True

    sample_brief = {
        "generated_at": "2026-05-11T10:00:00+00:00",
        "watchlist": ["AAPL", "NVDA"],
        "time_window_days": 14,
        "filters_applied": {
            "min_materiality": 0.3,
            "include_low_confidence": False,
            "max_items": 30,
            "event_types": None,
        },
        "brief_summary": "2 catalysts found.",
        "total_catalysts": 2,
        "catalysts": [
            {
                "ticker": "NVDA",
                "company_name": "NVIDIA Corporation",
                "title": "Q1 earnings beat guidance",
                "event_type": "earnings",
                "event_subtype": "beat",
                "materiality_score": 0.82,
                "confidence_score": 0.75,
                "why_it_matters": "Revenue significantly exceeded analyst consensus.",
                "first_seen_at": "2026-05-08T21:00:00+00:00",
                "last_seen_at": "2026-05-09T08:00:00+00:00",
                "price_move": {
                    "pct_change": 8.5,
                    "date_before": "2026-05-08",
                    "date_after": "2026-05-09",
                },
                "volume_context": "3.2x average volume",
                "source_quality_summary": "high",
                "source_links": [
                    "https://www.sec.gov/Archives/edgar/data/1045810/000104581026000001/nvda-20260208.htm"
                ],
                "related_filings": [
                    {
                        "accession_number": "0001045810-26-000001",
                        "form_type": "8-K",
                        "filing_date": "2026-05-08",
                        "items": "2.02",
                        "url": "https://www.sec.gov/Archives/edgar/data/1045810/000104581026000001",
                    }
                ],
                "related_news": [
                    {
                        "title": "NVIDIA beats Q1 estimates",
                        "publisher": "Reuters",
                        "published_at": "2026-05-09T06:00:00+00:00",
                        "url": "https://www.reuters.com/technology/nvidia-q1-2026",
                    }
                ],
                "caution": "Price move may reflect broader market conditions as well.",
            },
            {
                "ticker": "AAPL",
                "company_name": "Apple Inc.",
                "title": "Management change: CFO departure",
                "event_type": "management_change",
                "event_subtype": None,
                "materiality_score": 0.55,
                "confidence_score": 0.68,
                "why_it_matters": "CFO departures historically trigger short-term uncertainty.",
                "first_seen_at": "2026-05-10T12:00:00+00:00",
                "last_seen_at": "2026-05-10T12:00:00+00:00",
                "price_move": None,
                "volume_context": None,
                "source_quality_summary": "medium",
                "source_links": [],
                "related_filings": [],
                "related_news": [],
                "caution": None,
            },
        ],
        "note": "Source URLs are provided for all results.",
        "caution": "This brief shows correlation, not causation.",
    }

    with patch("equity_intel.dashboard.app.SessionLocal"):
        with patch("equity_intel.dashboard.app.get_watchlist_brief", return_value=sample_brief):
            with app.test_client() as c:
                yield c, sample_brief


# ------------------------------------------------------------------ #
# Index route                                                          #
# ------------------------------------------------------------------ #


def test_index_serves_html(client):
    c, _ = client
    resp = c.get("/")
    assert resp.status_code == 200
    assert b"text/html" in resp.content_type.encode()
    body = resp.data.decode()
    # Key UI landmarks should be present
    assert "EquityIntel" in body or "Equity" in body
    assert "not investment advice" in body.lower() or "not-advice" in body


def test_index_contains_filter_controls(client):
    c, _ = client
    body = c.get("/").data.decode()
    # Filter inputs should be present
    assert 'id="fTickers"'  in body
    assert 'id="fDays"'     in body
    assert 'id="fMinMat"'   in body
    assert 'id="fLowConf"'  in body
    assert 'id="fMaxItems"' in body


def test_index_contains_bias_section_markup(client):
    c, _ = client
    body = c.get("/").data.decode()
    # Bias layer section must be present but clearly labelled
    assert "bias" in body.lower()
    assert "personal" in body.lower() or "Personal" in body


# ------------------------------------------------------------------ #
# /api/tickers                                                         #
# ------------------------------------------------------------------ #


def test_api_tickers_returns_list(client):
    c, _ = client
    resp = c.get("/api/tickers")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "tickers" in data
    assert isinstance(data["tickers"], list)
    assert len(data["tickers"]) > 0


# ------------------------------------------------------------------ #
# /api/event_types                                                     #
# ------------------------------------------------------------------ #


def test_api_event_types_returns_known_list(client):
    c, _ = client
    resp = c.get("/api/event_types")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "event_types" in data
    assert "earnings" in data["event_types"]
    assert "merger_acquisition" in data["event_types"]
    assert set(data["event_types"]) == set(KNOWN_EVENT_TYPES)


# ------------------------------------------------------------------ #
# /api/bias                                                            #
# ------------------------------------------------------------------ #


def test_api_bias_returns_response_always(client):
    """The bias endpoint must always return 200 even if no file is configured."""
    c, _ = client
    with patch("equity_intel.dashboard.app._load_bias_layer", return_value={}):
        resp = c.get("/api/bias")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "bias_layer" in data
    assert "disclaimer" in data
    # Disclaimer must make clear it is personal opinion
    assert "personal" in data["disclaimer"].lower() or "not" in data["disclaimer"].lower()


def test_api_bias_with_configured_layer(client):
    """If a bias layer is configured it is returned intact."""
    c, _ = client
    fake_bias = {
        "author": "TestUser",
        "updated_at": "2026-05-11",
        "views": [
            {
                "title": "Tariff thesis",
                "body": "US-China tariffs weigh on semis.",
                "tickers": ["NVDA", "INTC"],
            }
        ],
    }
    with patch("equity_intel.dashboard.app._load_bias_layer", return_value=fake_bias):
        resp = c.get("/api/bias")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["bias_layer"]["author"] == "TestUser"
    assert len(data["bias_layer"]["views"]) == 1


# ------------------------------------------------------------------ #
# /api/brief — empty state                                             #
# ------------------------------------------------------------------ #


def test_api_brief_empty_state(client):
    """Brief with no catalysts returns valid schema, not an error."""
    c, _ = client
    resp = c.get("/api/brief")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "catalysts" in data
    assert isinstance(data["catalysts"], list)
    assert "watchlist" in data
    assert "total_catalysts" in data
    assert data["total_catalysts"] == 0
    assert "caution" in data
    assert "note" in data


def test_api_brief_empty_state_has_required_fields(client):
    c, _ = client
    data = c.get("/api/brief").get_json()
    required = ["generated_at", "watchlist", "time_window_days",
                "filters_applied", "brief_summary", "total_catalysts",
                "catalysts", "note", "caution"]
    for field in required:
        assert field in data, f"Missing field: {field}"


# ------------------------------------------------------------------ #
# /api/brief — with catalysts                                          #
# ------------------------------------------------------------------ #


def test_api_brief_with_catalysts(client_with_catalysts):
    c, sample = client_with_catalysts
    resp = c.get("/api/brief?tickers=AAPL,NVDA&days=14")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["total_catalysts"] == 2
    assert len(data["catalysts"]) == 2


def test_catalyst_schema_completeness(client_with_catalysts):
    c, sample = client_with_catalysts
    data = c.get("/api/brief").get_json()
    cat = data["catalysts"][0]
    for field in ["ticker", "company_name", "title", "event_type",
                  "materiality_score", "confidence_score"]:
        assert field in cat, f"Missing catalyst field: {field}"


def test_source_links_in_catalyst(client_with_catalysts):
    c, _ = client_with_catalysts
    data = c.get("/api/brief").get_json()
    nvda = next(cat for cat in data["catalysts"] if cat["ticker"] == "NVDA")
    assert "source_links" in nvda
    assert len(nvda["source_links"]) > 0
    assert nvda["source_links"][0].startswith("https://")


def test_related_filings_schema(client_with_catalysts):
    c, _ = client_with_catalysts
    data = c.get("/api/brief").get_json()
    nvda = next(cat for cat in data["catalysts"] if cat["ticker"] == "NVDA")
    filing = nvda["related_filings"][0]
    assert "accession_number" in filing
    assert "form_type" in filing
    assert "filing_date" in filing
    assert "url" in filing


def test_related_news_schema(client_with_catalysts):
    c, _ = client_with_catalysts
    data = c.get("/api/brief").get_json()
    nvda = next(cat for cat in data["catalysts"] if cat["ticker"] == "NVDA")
    article = nvda["related_news"][0]
    assert "title" in article
    assert "publisher" in article
    assert "url" in article


# ------------------------------------------------------------------ #
# /api/brief — filter parameters                                       #
# ------------------------------------------------------------------ #


def test_filter_tickers_param(client):
    c, mock_brief = client
    c.get("/api/brief?tickers=TSLA,AMZN")
    call_kwargs = mock_brief.call_args[1]
    assert "TSLA" in call_kwargs["tickers"]
    assert "AMZN" in call_kwargs["tickers"]


def test_filter_days_param(client):
    c, mock_brief = client
    c.get("/api/brief?days=30")
    assert mock_brief.call_args[1]["days"] == 30


def test_filter_min_mat_param(client):
    c, mock_brief = client
    c.get("/api/brief?min_mat=0.7")
    assert abs(mock_brief.call_args[1]["min_materiality"] - 0.7) < 0.001


def test_filter_event_types_param(client):
    c, mock_brief = client
    c.get("/api/brief?event_types=earnings,guidance")
    assert mock_brief.call_args[1]["event_types"] == ["earnings", "guidance"]


def test_filter_low_conf_param(client):
    c, mock_brief = client
    c.get("/api/brief?low_conf=1")
    assert mock_brief.call_args[1]["include_low_confidence"] is True


def test_filter_max_items_param(client):
    c, mock_brief = client
    c.get("/api/brief?max_items=50")
    assert mock_brief.call_args[1]["max_items"] == 50


def test_filter_max_items_clamped(client):
    """max_items should be clamped to [1, 100]."""
    c, mock_brief = client
    c.get("/api/brief?max_items=999")
    assert mock_brief.call_args[1]["max_items"] == 100


def test_filter_min_mat_clamped(client):
    """min_mat should be clamped to [0, 1]."""
    c, mock_brief = client
    c.get("/api/brief?min_mat=5.0")
    assert mock_brief.call_args[1]["min_materiality"] == 1.0


def test_filter_invalid_days_defaults(client):
    """Non-numeric days should fall back to default (7)."""
    c, mock_brief = client
    c.get("/api/brief?days=notanumber")
    assert mock_brief.call_args[1]["days"] == 7


def test_no_event_types_param_passes_none(client):
    """Omitting event_types should pass None (all types)."""
    c, mock_brief = client
    c.get("/api/brief")
    assert mock_brief.call_args[1]["event_types"] is None


# ------------------------------------------------------------------ #
# Caution / disclaimer presence                                        #
# ------------------------------------------------------------------ #


def test_brief_always_includes_caution(client):
    c, _ = client
    data = c.get("/api/brief").get_json()
    assert data.get("caution"), "Brief must always include a caution field"


def test_bias_disclaimer_does_not_claim_source_grounding(client):
    c, _ = client
    with patch("equity_intel.dashboard.app._load_bias_layer", return_value={}):
        data = c.get("/api/bias").get_json()
    d = data["disclaimer"].lower()
    assert "not" in d or "personal" in d, \
        "Bias disclaimer must make clear this is NOT system-derived"


# ------------------------------------------------------------------ #
# No live network calls                                                #
# ------------------------------------------------------------------ #


def test_brief_makes_no_network_calls(client):
    """
    The dashboard /api/brief endpoint must never make outbound HTTP calls.
    All data comes from the (mocked) database session.
    """
    import socket
    original_getaddrinfo = socket.getaddrinfo

    calls = []

    def _intercept(*args, **kwargs):
        calls.append(args)
        return original_getaddrinfo(*args, **kwargs)

    # Only patch if a real resolve were attempted; mock_brief short-circuits DB anyway.
    c, _ = client
    with patch("socket.getaddrinfo", side_effect=_intercept):
        c.get("/api/brief")

    # Loopback calls for Flask's test server are fine; no external hosts
    external = [a for a in calls if a and str(a[0]) not in ("localhost", "127.0.0.1", "::1")]
    assert external == [], f"Unexpected external DNS lookups: {external}"


# ------------------------------------------------------------------ #
# /api/intelligence/latest                                             #
# ------------------------------------------------------------------ #


def _intel_client(tmp_path):
    """Return a Flask test client with _intelligence_dir patched to tmp_path."""
    from equity_intel.dashboard.app import create_app
    app = create_app()
    app.config["TESTING"] = True
    with patch("equity_intel.dashboard.app._intelligence_dir", return_value=tmp_path):
        with app.test_client() as c:
            yield c


def test_intelligence_no_files(tmp_path):
    """`available: false` when intelligence/ folder is empty."""
    for c in _intel_client(tmp_path):
        resp = c.get("/api/intelligence/latest")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["available"] is False
    assert "message" in data


def test_intelligence_missing_dir(tmp_path):
    """`available: false` when intelligence/ folder does not exist at all."""
    missing = tmp_path / "nonexistent"
    for c in _intel_client(missing):
        resp = c.get("/api/intelligence/latest")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["available"] is False


def test_intelligence_ignores_gemini_news(tmp_path):
    """gemini_news_*.json must never be returned as the final synthesis."""
    (tmp_path / "gemini_news_20260525_060000.json").write_text(
        json.dumps({"generated_at": "2026-05-25T06:00:00", "news": {"NVDA": {}}}),
        encoding="utf-8",
    )
    for c in _intel_client(tmp_path):
        resp = c.get("/api/intelligence/latest")
    data = resp.get_json()
    assert data["available"] is False, (
        "gemini_news_*.json must not be treated as the final synthesis report"
    )


def test_intelligence_selects_newest_stocks_json(tmp_path):
    """Selects the most recently modified stocks_*.json, not an older one."""
    import time

    older = tmp_path / "stocks_20260524_070000.json"
    newer = tmp_path / "stocks_20260525_080000.json"
    older.write_text(
        json.dumps({"generated_at": "2026-05-24T07:00:00", "one_sentence_takeaway": "older"}),
        encoding="utf-8",
    )
    time.sleep(0.05)  # ensure mtime differs
    newer.write_text(
        json.dumps({"generated_at": "2026-05-25T08:00:00", "one_sentence_takeaway": "newer"}),
        encoding="utf-8",
    )
    for c in _intel_client(tmp_path):
        resp = c.get("/api/intelligence/latest")
    data = resp.get_json()
    assert data["available"] is True
    assert data["report"]["one_sentence_takeaway"] == "newer"
    assert "stocks_20260525" in data["json_file"]


def test_intelligence_response_shape(tmp_path):
    """Response includes all required top-level keys when a report exists."""
    (tmp_path / "stocks_20260525_090000.json").write_text(
        json.dumps({
            "generated_at": "2026-05-25T09:00:00",
            "one_sentence_takeaway": "NVDA leads",
            "summary": "Strong earnings season.",
            "top_signals": [{"asset": "NVDA", "signal": "bullish", "conviction": "high", "why": "Beat estimates"}],
            "key_risks": [{"risk": "Macro headwinds", "severity": "medium", "frequency": "2x"}],
            "actionable_intelligence": [{"action": "Watch NVDA", "urgency": "high", "rationale": "Catalyst confirmed"}],
            "brief_count": 3,
            "model_used": "qwen/qwen3-14b",
        }),
        encoding="utf-8",
    )
    for c in _intel_client(tmp_path):
        data = c.get("/api/intelligence/latest").get_json()
    assert data["available"] is True
    assert data["generated_at"] == "2026-05-25T09:00:00"
    assert "json_file" in data
    report = data["report"]
    for key in ["one_sentence_takeaway", "summary", "top_signals", "key_risks",
                "actionable_intelligence", "brief_count", "model_used"]:
        assert key in report, f"Missing report key: {key}"
    assert report["top_signals"][0]["asset"] == "NVDA"


def test_intelligence_includes_markdown_when_present(tmp_path):
    """markdown field is populated when a matching .md file exists."""
    stem = "stocks_20260525_100000"
    (tmp_path / f"{stem}.json").write_text(
        json.dumps({"generated_at": "2026-05-25T10:00:00"}), encoding="utf-8"
    )
    (tmp_path / f"{stem}.md").write_text("# Report\nSynthesis content here.", encoding="utf-8")
    for c in _intel_client(tmp_path):
        data = c.get("/api/intelligence/latest").get_json()
    assert data["available"] is True
    assert "# Report" in data["markdown"]
    assert data["md_file"] is not None


def test_intelligence_no_markdown_when_md_absent(tmp_path):
    """markdown is empty string when no .md file exists."""
    (tmp_path / "stocks_20260525_110000.json").write_text(
        json.dumps({"generated_at": "2026-05-25T11:00:00"}), encoding="utf-8"
    )
    for c in _intel_client(tmp_path):
        data = c.get("/api/intelligence/latest").get_json()
    assert data["available"] is True
    assert data["markdown"] == ""
    assert data["md_file"] is None


def test_intelligence_malformed_json_returns_unavailable(tmp_path):
    """Malformed stocks_*.json returns available: false — no crash or 500."""
    (tmp_path / "stocks_20260525_120000.json").write_text(
        "not valid json {{{", encoding="utf-8"
    )
    for c in _intel_client(tmp_path):
        resp = c.get("/api/intelligence/latest")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["available"] is False
    assert "message" in data


def test_intelligence_gemini_plus_stocks_returns_stocks(tmp_path):
    """When both gemini_news_* and stocks_* exist, only stocks_* is returned."""
    import time
    (tmp_path / "gemini_news_20260525_060000.json").write_text(
        json.dumps({"generated_at": "2026-05-25T06:00:00", "news": {}}), encoding="utf-8"
    )
    time.sleep(0.05)
    (tmp_path / "stocks_20260525_080000.json").write_text(
        json.dumps({"generated_at": "2026-05-25T08:00:00", "one_sentence_takeaway": "correct"}),
        encoding="utf-8",
    )
    for c in _intel_client(tmp_path):
        data = c.get("/api/intelligence/latest").get_json()
    assert data["available"] is True
    assert data["report"]["one_sentence_takeaway"] == "correct"


def test_news_blocks_latest_no_file_uses_db_diagnostic(tmp_path):
    """My Views endpoint returns a useful diagnostic when no block file exists."""
    for c in _intel_client(tmp_path):
        with patch(
            "equity_intel.dashboard.app._news_blocks_diagnostic",
            return_value={
                "recent_article_count": 3,
                "latest_article_published_at": "2026-05-25T11:58:37",
                "message": "3 news article(s) found in the last 24 hours.",
            },
        ):
            resp = c.get("/api/news-blocks/latest")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["available"] is False
    assert data["recent_article_count"] == 3
    assert "last 24 hours" in data["message"]
