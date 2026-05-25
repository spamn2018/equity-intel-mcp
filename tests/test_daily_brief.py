"""
Tests for the daily brief worker (workers/run_daily_brief.py).

Design principles:
  - No live network calls.
  - No PostgreSQL required — in-memory SQLite.
  - The worker is a thin orchestration layer; tests verify that it calls
    get_watchlist_brief correctly and handles output files correctly.
  - Where the DB is irrelevant, we mock get_watchlist_brief to return a
    canned brief dict so tests remain fast and deterministic.

Coverage:
  - Correct parameters passed to get_watchlist_brief (days, min_materiality, max_items)
  - Empty watchlist → graceful result, no file written or empty file
  - JSON output file: created, named correctly, valid JSON, contains brief keys
  - Markdown output file: created, named correctly, contains Markdown headers
  - File naming convention: brief_{YYYYMMDD}.json / brief_{YYYYMMDD}.md
  - Dry-run mode: no file written
  - Re-run same date overwrites (idempotent)
  - Config-driven defaults (daily_brief_* settings)
  - Disclaimer / advice text not present as actionable advice
"""
from __future__ import annotations

import datetime
import json
import os
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from equity_intel.db.models import Base, Company, EventCluster, now_utc
from equity_intel.workers.run_daily_brief import (
    _brief_filename,
    _write_brief,
    run_daily_brief,
    ADVICE_DISCLAIMER,
)


# ------------------------------------------------------------------ #
# Shared fixtures                                                      #
# ------------------------------------------------------------------ #


@pytest.fixture(scope="module")
def engine():
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture(scope="module")
def session_factory(engine):
    return sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture
def session(session_factory):
    sess = session_factory()
    yield sess
    sess.rollback()
    sess.close()


@pytest.fixture
def tmp_output_dir(tmp_path) -> Path:
    """A fresh temp directory for brief output files."""
    d = tmp_path / "briefs"
    d.mkdir()
    return d


# ── Canned brief dict ─────────────────────────────────────────────────

def _make_brief(
    tickers=None,
    total_catalysts=2,
    days=1,
) -> Dict[str, Any]:
    """Return a minimal valid brief dict for use in mocked tests."""
    return {
        "generated_at": "2024-01-15T07:00:00+00:00",
        "watchlist": tickers or ["AAPL", "MSFT"],
        "time_window_days": days,
        "filters_applied": {
            "min_materiality": 0.3,
            "include_low_confidence": False,
            "max_items": 30,
            "event_types": None,
            "include_price_context": True,
            "include_news": True,
            "include_filings": True,
        },
        "brief_summary": "Over the past 1 day(s), 2 catalyst(s) were identified...",
        "total_catalysts": total_catalysts,
        "catalysts": [
            {
                "ticker": "AAPL",
                "company_name": "Apple Inc.",
                "sector": "Technology",
                "title": "AAPL Earnings Event",
                "event_type": "earnings",
                "event_subtype": "results_of_operations",
                "why_it_matters": "This high-materiality event may reflect a material change in revenue or profitability expectations.",
                "materiality_score": 0.85,
                "confidence_score": 0.75,
                "novelty_score": 0.6,
                "first_seen_at": "2024-01-14T20:00:00+00:00",
                "last_seen_at": "2024-01-14T20:00:00+00:00",
                "event_count": 2,
                "filing_count": 1,
                "news_count": 1,
                "price_move": {"pct_change": 3.2, "volume_ratio": 2.1,
                               "price_before": 182.0, "price_after": 187.8,
                               "date_before": "2024-01-13", "date_after": "2024-01-14"},
                "volume_context": "Volume was approximately 2.1× normal",
                "source_links": ["https://sec.gov/filing/aapl-8k"],
                "related_filing_ids": [1],
                "related_news_ids": [1],
                "related_filings": [{"accession_number": "0001234567-24-000001",
                                     "form_type": "8-K", "filing_date": "2024-01-14",
                                     "items": "2.02", "url": "https://sec.gov/filing/aapl-8k"}],
                "related_news": [{"title": "Apple beats Q1 estimates",
                                  "publisher": "Reuters", "published_at": "2024-01-14T18:00:00+00:00",
                                  "url": "https://reuters.com/aapl", "summary": "Apple..."}],
                "caution": "This may reflect market-moving information. Verify with primary sources.",
                "data_source": "event_clusters",
                "cluster_id": 1,
                "cluster_key": "AAPL:earnings:2024W03",
            }
        ] * total_catalysts,
        "note": "Source URLs are provided for all results. This is not investment advice.",
        "caution": "This brief shows correlation, not causation.",
    }


# ------------------------------------------------------------------ #
# 1. File naming convention                                            #
# ------------------------------------------------------------------ #


def test_brief_filename_json(tmp_output_dir):
    date = datetime.date(2024, 1, 15)
    path = _brief_filename(tmp_output_dir, "json", date)
    assert path.name == "brief_20240115.json"
    assert path.parent == tmp_output_dir


def test_brief_filename_markdown(tmp_output_dir):
    date = datetime.date(2024, 3, 5)
    path = _brief_filename(tmp_output_dir, "markdown", date)
    assert path.name == "brief_20240305.md"


def test_brief_filename_different_dates_different_files(tmp_output_dir):
    d1 = _brief_filename(tmp_output_dir, "json", datetime.date(2024, 1, 15))
    d2 = _brief_filename(tmp_output_dir, "json", datetime.date(2024, 1, 16))
    assert d1 != d2


def test_brief_filename_same_date_same_file(tmp_output_dir):
    d1 = _brief_filename(tmp_output_dir, "json", datetime.date(2024, 1, 15))
    d2 = _brief_filename(tmp_output_dir, "json", datetime.date(2024, 1, 15))
    assert d1 == d2


# ------------------------------------------------------------------ #
# 2. _write_brief: JSON output                                         #
# ------------------------------------------------------------------ #


def test_write_brief_json_creates_file(tmp_output_dir):
    brief = _make_brief()
    path = tmp_output_dir / "brief_test.json"
    _write_brief(brief, path, "json")
    assert path.exists()


def test_write_brief_json_is_valid_json(tmp_output_dir):
    brief = _make_brief()
    path = tmp_output_dir / "brief_test_valid.json"
    _write_brief(brief, path, "json")
    parsed = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)


def test_write_brief_json_contains_brief_keys(tmp_output_dir):
    brief = _make_brief()
    path = tmp_output_dir / "brief_keys.json"
    _write_brief(brief, path, "json")
    parsed = json.loads(path.read_text(encoding="utf-8"))
    for key in ("generated_at", "watchlist", "brief_summary", "total_catalysts", "catalysts"):
        assert key in parsed, f"Missing key: {key}"


def test_write_brief_json_round_trip(tmp_output_dir):
    brief = _make_brief(tickers=["NVDA", "AMD"])
    path = tmp_output_dir / "brief_roundtrip.json"
    _write_brief(brief, path, "json")
    parsed = json.loads(path.read_text(encoding="utf-8"))
    assert parsed["watchlist"] == ["NVDA", "AMD"]
    assert parsed["total_catalysts"] == 2


# ------------------------------------------------------------------ #
# 3. _write_brief: Markdown output                                     #
# ------------------------------------------------------------------ #


def test_write_brief_markdown_creates_file(tmp_output_dir):
    brief = _make_brief()
    path = tmp_output_dir / "brief_test.md"
    _write_brief(brief, path, "markdown")
    assert path.exists()


def test_write_brief_markdown_contains_header(tmp_output_dir):
    brief = _make_brief()
    path = tmp_output_dir / "brief_md_header.md"
    _write_brief(brief, path, "markdown")
    content = path.read_text(encoding="utf-8")
    assert "# Watchlist Catalyst Brief" in content


def test_write_brief_markdown_contains_ticker(tmp_output_dir):
    brief = _make_brief(tickers=["TSLA"])
    path = tmp_output_dir / "brief_md_ticker.md"
    _write_brief(brief, path, "markdown")
    content = path.read_text(encoding="utf-8")
    assert "TSLA" in content


def test_write_brief_markdown_contains_caution(tmp_output_dir):
    brief = _make_brief()
    path = tmp_output_dir / "brief_md_caution.md"
    _write_brief(brief, path, "markdown")
    content = path.read_text(encoding="utf-8")
    # Caution block or the word itself must appear
    assert "caution" in content.lower() or "⚠" in content


# ------------------------------------------------------------------ #
# 4. _write_brief: creates output directory if absent                  #
# ------------------------------------------------------------------ #


def test_write_brief_creates_parent_dir(tmp_path):
    nested = tmp_path / "a" / "b" / "c"
    assert not nested.exists()
    brief = _make_brief()
    _write_brief(brief, nested / "brief.json", "json")
    assert (nested / "brief.json").exists()


# ------------------------------------------------------------------ #
# 5. run_daily_brief: correct parameters passed to service             #
# ------------------------------------------------------------------ #


def test_run_daily_brief_passes_tickers_to_service(tmp_output_dir):
    captured = {}
    canned = _make_brief(tickers=["NVDA"])

    def mock_service(session, tickers, days, min_materiality, max_items, **kwargs):
        captured["tickers"] = tickers
        captured["days"] = days
        captured["min_materiality"] = min_materiality
        captured["max_items"] = max_items
        return canned

    with patch("equity_intel.workers.run_daily_brief.get_watchlist_brief", mock_service):
        with patch("equity_intel.workers.run_daily_brief.SessionLocal") as mock_sl:
            mock_sl.return_value.__enter__ = lambda s: MagicMock()
            mock_sl.return_value.__exit__ = MagicMock(return_value=False)
            mock_sl.return_value = MagicMock()

            run_daily_brief(
                tickers=["NVDA", "AMD"],
                days=3,
                min_materiality=0.5,
                max_items=15,
                fmt="json",
                output_dir=tmp_output_dir,
            )

    assert captured["tickers"] == ["NVDA", "AMD"]
    assert captured["days"] == 3
    assert captured["min_materiality"] == 0.5
    assert captured["max_items"] == 15


def test_run_daily_brief_passes_days_to_service(tmp_output_dir):
    captured = {}
    canned = _make_brief()

    def mock_service(session, tickers, days, min_materiality, max_items, **kwargs):
        captured["days"] = days
        return canned

    with patch("equity_intel.workers.run_daily_brief.get_watchlist_brief", mock_service):
        with patch("equity_intel.workers.run_daily_brief.SessionLocal", return_value=MagicMock()):
            run_daily_brief(
                tickers=["AAPL"],
                days=7,
                min_materiality=0.3,
                max_items=20,
                fmt="json",
                output_dir=tmp_output_dir,
            )

    assert captured["days"] == 7


# ------------------------------------------------------------------ #
# 6. run_daily_brief: dry-run mode                                     #
# ------------------------------------------------------------------ #


def test_dry_run_does_not_write_file(tmp_output_dir):
    canned = _make_brief()

    with patch("equity_intel.workers.run_daily_brief.get_watchlist_brief", return_value=canned):
        with patch("equity_intel.workers.run_daily_brief.SessionLocal", return_value=MagicMock()):
            result = run_daily_brief(
                tickers=["AAPL"],
                days=1,
                min_materiality=0.3,
                max_items=20,
                fmt="json",
                output_dir=tmp_output_dir,
                dry_run=True,
            )

    # No files should have been created
    files = list(tmp_output_dir.glob("*.json"))
    assert not files, f"Expected no files in dry-run, found: {files}"
    assert result.get("_output_path") is None


def test_dry_run_returns_brief_dict(tmp_output_dir):
    canned = _make_brief(total_catalysts=3)

    with patch("equity_intel.workers.run_daily_brief.get_watchlist_brief", return_value=canned):
        with patch("equity_intel.workers.run_daily_brief.SessionLocal", return_value=MagicMock()):
            result = run_daily_brief(
                tickers=["AAPL"],
                days=1,
                min_materiality=0.3,
                max_items=20,
                fmt="json",
                output_dir=tmp_output_dir,
                dry_run=True,
            )

    assert result["total_catalysts"] == 3
    assert "catalysts" in result


# ------------------------------------------------------------------ #
# 7. run_daily_brief: JSON file output                                 #
# ------------------------------------------------------------------ #


def test_run_daily_brief_writes_json_file(tmp_output_dir):
    canned = _make_brief()
    fixed_date = datetime.date(2024, 1, 15)

    with patch("equity_intel.workers.run_daily_brief.get_watchlist_brief", return_value=canned):
        with patch("equity_intel.workers.run_daily_brief.SessionLocal", return_value=MagicMock()):
            run_daily_brief(
                tickers=["AAPL"],
                days=1,
                min_materiality=0.3,
                max_items=20,
                fmt="json",
                output_dir=tmp_output_dir,
                run_date=fixed_date,
            )

    expected = tmp_output_dir / "brief_20240115.json"
    assert expected.exists()
    parsed = json.loads(expected.read_text(encoding="utf-8"))
    assert "total_catalysts" in parsed


def test_run_daily_brief_output_path_in_result(tmp_output_dir):
    canned = _make_brief()
    fixed_date = datetime.date(2024, 2, 20)

    with patch("equity_intel.workers.run_daily_brief.get_watchlist_brief", return_value=canned):
        with patch("equity_intel.workers.run_daily_brief.SessionLocal", return_value=MagicMock()):
            result = run_daily_brief(
                tickers=["MSFT"],
                days=1,
                min_materiality=0.3,
                max_items=20,
                fmt="json",
                output_dir=tmp_output_dir,
                run_date=fixed_date,
            )

    assert "_output_path" in result
    assert "brief_20240220.json" in result["_output_path"]


# ------------------------------------------------------------------ #
# 8. run_daily_brief: Markdown file output                             #
# ------------------------------------------------------------------ #


def test_run_daily_brief_writes_markdown_file(tmp_output_dir):
    canned = _make_brief()
    fixed_date = datetime.date(2024, 3, 10)

    with patch("equity_intel.workers.run_daily_brief.get_watchlist_brief", return_value=canned):
        with patch("equity_intel.workers.run_daily_brief.SessionLocal", return_value=MagicMock()):
            run_daily_brief(
                tickers=["NVDA"],
                days=1,
                min_materiality=0.3,
                max_items=20,
                fmt="markdown",
                output_dir=tmp_output_dir,
                run_date=fixed_date,
            )

    expected = tmp_output_dir / "brief_20240310.md"
    assert expected.exists()
    content = expected.read_text(encoding="utf-8")
    assert "# Watchlist Catalyst Brief" in content


# ------------------------------------------------------------------ #
# 9. run_daily_brief: idempotency (re-run overwrites)                  #
# ------------------------------------------------------------------ #


def test_rerun_same_date_overwrites_file(tmp_output_dir):
    fixed_date = datetime.date(2024, 1, 20)

    canned_first = _make_brief(total_catalysts=1)
    canned_second = _make_brief(total_catalysts=5)

    with patch("equity_intel.workers.run_daily_brief.get_watchlist_brief", return_value=canned_first):
        with patch("equity_intel.workers.run_daily_brief.SessionLocal", return_value=MagicMock()):
            run_daily_brief(
                tickers=["AAPL"], days=1, min_materiality=0.3, max_items=20,
                fmt="json", output_dir=tmp_output_dir, run_date=fixed_date,
            )

    with patch("equity_intel.workers.run_daily_brief.get_watchlist_brief", return_value=canned_second):
        with patch("equity_intel.workers.run_daily_brief.SessionLocal", return_value=MagicMock()):
            run_daily_brief(
                tickers=["AAPL"], days=1, min_materiality=0.3, max_items=20,
                fmt="json", output_dir=tmp_output_dir, run_date=fixed_date,
            )

    expected = tmp_output_dir / "brief_20240120.json"
    assert expected.exists()
    parsed = json.loads(expected.read_text(encoding="utf-8"))
    # Second run should have overwritten — total_catalysts is 5
    assert parsed["total_catalysts"] == 5


# ------------------------------------------------------------------ #
# 10. run_daily_brief: empty watchlist                                 #
# ------------------------------------------------------------------ #


def test_run_daily_brief_empty_watchlist(tmp_output_dir):
    """An empty tickers list should produce a brief with 0 catalysts gracefully."""
    empty_brief = {
        "generated_at": "2024-01-15T07:00:00+00:00",
        "watchlist": [],
        "time_window_days": 1,
        "filters_applied": {"min_materiality": 0.3},
        "brief_summary": "No tickers provided.",
        "total_catalysts": 0,
        "catalysts": [],
        "note": "Not investment advice.",
        "caution": "This brief shows correlation, not causation.",
    }

    with patch("equity_intel.workers.run_daily_brief.get_watchlist_brief", return_value=empty_brief):
        with patch("equity_intel.workers.run_daily_brief.SessionLocal", return_value=MagicMock()):
            result = run_daily_brief(
                tickers=[],
                days=1,
                min_materiality=0.3,
                max_items=20,
                fmt="json",
                output_dir=tmp_output_dir,
                run_date=datetime.date(2024, 1, 15),
            )

    assert result["total_catalysts"] == 0
    assert result["catalysts"] == []


# ------------------------------------------------------------------ #
# 11. Disclaimer / advice text                                         #
# ------------------------------------------------------------------ #


def test_advice_disclaimer_is_present():
    assert ADVICE_DISCLAIMER
    assert len(ADVICE_DISCLAIMER) > 20


def test_advice_disclaimer_is_not_advice():
    """The disclaimer must not read as investment advice — it must deny being advice."""
    text = ADVICE_DISCLAIMER.lower()
    assert "not investment advice" in text or "not" in text


def test_advice_disclaimer_mentions_verification():
    """Users should be reminded to verify with primary sources."""
    text = ADVICE_DISCLAIMER.lower()
    assert "verify" in text or "primary source" in text


# ------------------------------------------------------------------ #
# 12. Config-driven defaults                                           #
# ------------------------------------------------------------------ #


def test_config_daily_brief_tickers_falls_back_to_default():
    """If DAILY_BRIEF_WATCHLIST is empty, fall back to DEFAULT_TICKERS."""
    from equity_intel.config import Settings
    s = Settings(
        _env_file=None,
        database_url="sqlite:///:memory:",
        default_tickers="AAPL,MSFT",
        daily_brief_watchlist="",
    )
    assert s.daily_brief_tickers == ["AAPL", "MSFT"]


def test_config_daily_brief_tickers_uses_watchlist_when_set():
    """DAILY_BRIEF_WATCHLIST overrides DEFAULT_TICKERS for the daily brief."""
    from equity_intel.config import Settings
    s = Settings(
        _env_file=None,
        database_url="sqlite:///:memory:",
        default_tickers="AAPL,MSFT",
        daily_brief_watchlist="NVDA,TSLA,AMD",
    )
    assert s.daily_brief_tickers == ["NVDA", "TSLA", "AMD"]


def test_config_daily_brief_tickers_normalizes_uppercase():
    from equity_intel.config import Settings
    s = Settings(
        _env_file=None,
        database_url="sqlite:///:memory:",
        daily_brief_watchlist="nvda,tsla",
    )
    assert s.daily_brief_tickers == ["NVDA", "TSLA"]


def test_config_daily_brief_defaults():
    from equity_intel.config import Settings
    s = Settings(_env_file=None, database_url="sqlite:///:memory:")
    # Default look-back must be 7 days so synthesis has enough catalyst volume.
    # A default of 1 day produces empty briefs on most runs.
    assert s.daily_brief_days == 7
    assert s.daily_brief_min_materiality == 0.3
    assert s.daily_brief_format == "json"
    assert s.daily_brief_max_items == 30
    assert s.daily_brief_output_dir == "briefs"


def test_config_daily_brief_days_default_is_seven():
    """Regression: config default must be 7 not 1.

    DAILY_BRIEF_DAYS=1 silently produces empty briefs on most runs because
    there is rarely a full calendar day of new events at run time. The
    correct default is 7 days so the synthesizer always has material to work with.
    """
    from equity_intel.config import Settings
    s = Settings(_env_file=None, database_url="sqlite:///:memory:")
    assert s.daily_brief_days == 7, (
        "daily_brief_days default must be 7. "
        "A default of 1 causes empty briefs and 'No synthesis data yet' in the UI."
    )


# ------------------------------------------------------------------ #
# 13. No live network calls                                            #
# ------------------------------------------------------------------ #


def test_run_daily_brief_makes_no_network_calls(tmp_output_dir):
    """
    Verify the daily brief worker makes zero HTTP calls.
    We mock get_watchlist_brief (the only place that could touch the DB),
    and patch httpx to raise if anything tries to call the network.
    """
    import httpx

    canned = _make_brief()

    def explode(*args, **kwargs):
        raise AssertionError("run_daily_brief must not make HTTP calls")

    with patch("equity_intel.workers.run_daily_brief.get_watchlist_brief", return_value=canned):
        with patch("equity_intel.workers.run_daily_brief.SessionLocal", return_value=MagicMock()):
            with patch.object(httpx.Client, "get", explode):
                with patch.object(httpx.AsyncClient, "get", explode):
                    run_daily_brief(
                        tickers=["AAPL"],
                        days=1,
                        min_materiality=0.3,
                        max_items=20,
                        fmt="json",
                        output_dir=tmp_output_dir,
                        run_date=datetime.date(2024, 1, 15),
                    )
    # If we get here without AssertionError, no network calls were made.


# ------------------------------------------------------------------ #
# 14. CLI diagnostics — resolved config and catalyst count             #
# ------------------------------------------------------------------ #


def test_cli_prints_resolved_days(tmp_output_dir):
    """CLI output must include the resolved DAILY_BRIEF_DAYS window."""
    from click.testing import CliRunner
    from equity_intel.workers.run_daily_brief import main

    canned = _make_brief(total_catalysts=3)

    with patch("equity_intel.workers.run_daily_brief.get_watchlist_brief", return_value=canned):
        with patch("equity_intel.workers.run_daily_brief.SessionLocal", return_value=MagicMock()):
            runner = CliRunner()
            result = runner.invoke(
                main,
                ["--tickers", "AAPL", "--days", "7", "--output-dir", str(tmp_output_dir)],
            )

    assert "7" in result.output, "Resolved DAILY_BRIEF_DAYS not in CLI output"
    assert "Catalysts found" in result.output or "catalysts" in result.output.lower()


def test_cli_prints_catalyst_count(tmp_output_dir):
    """CLI output must include the exact catalyst count."""
    from click.testing import CliRunner
    from equity_intel.workers.run_daily_brief import main

    canned = _make_brief(total_catalysts=5)

    with patch("equity_intel.workers.run_daily_brief.get_watchlist_brief", return_value=canned):
        with patch("equity_intel.workers.run_daily_brief.SessionLocal", return_value=MagicMock()):
            runner = CliRunner()
            result = runner.invoke(
                main,
                ["--tickers", "AAPL,MSFT", "--days", "7", "--output-dir", str(tmp_output_dir)],
            )

    combined = result.output + (result.stderr or "")
    assert "5" in combined, "Catalyst count (5) not in CLI output"


def test_cli_warns_on_zero_catalysts(tmp_output_dir):
    """CLI must emit a visible WARNING to stderr when catalyst count is 0."""
    from click.testing import CliRunner
    from equity_intel.workers.run_daily_brief import main

    zero_brief = _make_brief(total_catalysts=0)
    zero_brief["catalysts"] = []

    with patch("equity_intel.workers.run_daily_brief.get_watchlist_brief", return_value=zero_brief):
        with patch("equity_intel.workers.run_daily_brief.SessionLocal", return_value=MagicMock()):
            runner = CliRunner(mix_stderr=False)
            result = runner.invoke(
                main,
                ["--tickers", "AAPL", "--days", "7", "--output-dir", str(tmp_output_dir)],
            )

    # Warning must appear in stderr (click.echo(..., err=True))
    stderr_text = result.stderr if hasattr(result, "stderr") and result.stderr else ""
    combined = result.output + stderr_text
    assert "WARNING" in combined or "0 catalysts" in combined, (
        "Expected a visible WARNING when brief has 0 catalysts. Got:\n"
        f"stdout: {result.output!r}\nstderr: {stderr_text!r}"
    )
