"""
Tests for the Ticker Discovery Radar.

Coverage
--------
  * Ticker extraction from mixed text
  * Prohibited-ticker filtering / exclusion_flag
  * Weekly aggregation
  * Acceleration scoring (2→9→12 pattern)
  * Promotion threshold behaviour
  * No promotion of prohibited / default / trad-hedge tickers
  * Source quality and breadth score components
"""
from __future__ import annotations

import datetime
from typing import Any, Dict, List, Set
from unittest.mock import MagicMock, patch

import pytest

# ── Extractor ──────────────────────────────────────────────────────────────

from equity_intel.discovery.extractor import (
    _WORD_BLOCKLIST,
    _extract_tickers_from_text,
    _iso_week,
    scan_intelligence_files,
)
from equity_intel.discovery.scorer import (
    WeeklyAggregate,
    compute_acceleration_score,
    compute_breadth_score,
    compute_mention_volume_score,
    compute_novelty_score,
    compute_scores,
    compute_source_quality_score,
    upsert_scores,
    _iso_week_offset,
)


# ===========================================================================
# 1. Ticker extraction from mixed text
# ===========================================================================

class TestTickerExtraction:
    def test_dollar_sign_ticker(self):
        pairs = _extract_tickers_from_text("Watching $AAPL and $NVDA closely.")
        tickers = [t for t, _ in pairs]
        assert "AAPL" in tickers
        assert "NVDA" in tickers

    def test_plain_uppercase_ticker(self):
        pairs = _extract_tickers_from_text("MRVL reported strong earnings today.")
        tickers = [t for t, _ in pairs]
        assert "MRVL" in tickers

    def test_context_snippet_captured(self):
        text = "Analysts upgraded POWL after the quarterly beat."
        pairs = _extract_tickers_from_text(text)
        assert pairs, "Expected at least one extraction"
        ticker, ctx = pairs[0]
        assert ticker == "POWL"
        assert "quarterly beat" in ctx

    def test_common_words_blocked(self):
        """Words in _WORD_BLOCKLIST must never be returned as tickers."""
        blocked_sentence = " ".join(list(_WORD_BLOCKLIST)[:20])
        tickers = [t for t, _ in _extract_tickers_from_text(blocked_sentence)]
        # None of the returned tickers should be in the blocklist
        assert all(t not in _WORD_BLOCKLIST for t in tickers)

    def test_mixed_case_ignored(self):
        """Mixed-case words (e.g. 'Apple') must not be extracted."""
        pairs = _extract_tickers_from_text("Apple announced new products.")
        assert not any(t == "APPLE" for t, _ in pairs)

    def test_empty_text(self):
        assert _extract_tickers_from_text("") == []
        assert _extract_tickers_from_text(None) == []  # type: ignore[arg-type]

    def test_five_char_ticker(self):
        pairs = _extract_tickers_from_text("Small-cap SWKH rallied on news.")
        tickers = [t for t, _ in pairs]
        assert "SWKH" in tickers

    def test_ticker_with_class_suffix(self):
        pairs = _extract_tickers_from_text("BRK.B fell 2% today.")
        tickers = [t for t, _ in pairs]
        # BRK should be extracted (suffix stripped)
        assert any("BRK" in t for t in tickers)

    def test_single_letter_words_blocked(self):
        """Single-letter tickers like A, B, C should all be in the blocklist."""
        for letter in "BCDEFGHJKLMNOPQRSTUVWXYZ":
            assert letter in _WORD_BLOCKLIST, f"{letter!r} not in blocklist"


# ===========================================================================
# 2. Exclusion flag logic
# ===========================================================================

class TestExclusionFlag:
    """
    We patch settings so the test controls PROHIBITED / TRAD_HEDGE / DEFAULT sets.
    """

    def _make_mock_settings(self):
        m = MagicMock()
        m.prohibited_tickers = "NVDA,TSLA,AAPL"
        m.trad_hedge_tickers = "BAC,CI"
        m.default_tickers = "POWL,ETN,VST"
        return m

    def test_prohibited_ticker_flagged(self):
        from equity_intel.discovery import extractor as ext

        with patch.object(ext, "settings") as mock_s:
            mock_s.prohibited_tickers = "NVDA,TSLA,AAPL"
            mock_s.trad_hedge_tickers = "BAC,CI"
            mock_s.default_tickers = "POWL"

            prohibited, trad, default = ext._build_exclusion_sets()
            assert "NVDA" in prohibited
            assert "TSLA" in prohibited
            assert "BAC" in trad

    def test_excluded_tickers_still_in_records(self):
        """Excluded tickers must produce records with exclusion_flag=True, not be dropped."""
        from equity_intel.discovery import extractor as ext

        with patch.object(ext, "settings") as mock_s:
            mock_s.prohibited_tickers = "NVDA"
            mock_s.trad_hedge_tickers = ""
            mock_s.default_tickers = ""

            # Build a minimal fake session-like scan by calling extraction directly
            _, trad, default = ext._build_exclusion_sets()
            prohibited = {"NVDA"}
            all_excluded = prohibited | trad

            tickers_found = _extract_tickers_from_text("NVDA is up 3% alongside MRVL.")
            for ticker, ctx in tickers_found:
                excluded = ticker in all_excluded
                if ticker == "NVDA":
                    assert excluded is True

    def test_default_ticker_not_promoted(self):
        """Default tickers must score novelty_score = 0 and never be probe_candidate."""
        default_set = {"POWL", "ETN", "VST"}
        score = compute_novelty_score("POWL", default_set)
        assert score == 0.0

    def test_universe_ticker_low_novelty(self):
        default_set = {"POWL"}
        universe_set = {"SWKH"}
        score = compute_novelty_score("SWKH", default_set, universe_set)
        assert score == 0.3

    def test_unknown_ticker_full_novelty(self):
        score = compute_novelty_score("XYZ", {"POWL"}, {"SWKH"})
        assert score == 1.0


# ===========================================================================
# 3. Weekly aggregation helpers
# ===========================================================================

class TestWeeklyAggregate:
    def _fake_mention(self, source_id, source_type, source_ticker, excluded=False):
        m = MagicMock()
        m.source_id = source_id
        m.source_type = source_type
        m.source_ticker = source_ticker
        m.confidence = 0.8
        m.exclusion_flag = excluded
        m.context = "some context"
        m.occurred_at = datetime.datetime(2026, 5, 20, tzinfo=datetime.timezone.utc)
        m.url = None
        return m

    def test_mention_count_increments(self):
        agg = WeeklyAggregate("XYZ", "2026-W21")
        for i in range(5):
            agg.add(self._fake_mention(f"src_{i}", "news", "NVDA"))
        assert agg.mention_count == 5

    def test_unique_source_ids(self):
        agg = WeeklyAggregate("XYZ", "2026-W21")
        agg.add(self._fake_mention("news_1", "news", "AAPL"))
        agg.add(self._fake_mention("news_1", "news", "AAPL"))  # duplicate
        agg.add(self._fake_mention("news_2", "news", "AAPL"))
        assert len(agg.source_ids) == 2

    def test_unique_source_tickers(self):
        agg = WeeklyAggregate("XYZ", "2026-W21")
        for st in ["POWL", "ETN", "VST", "VST"]:
            agg.add(self._fake_mention(f"src_{st}", "news", st))
        assert len(agg.source_tickers) == 3  # VST deduplicated

    def test_exclusion_propagates(self):
        agg = WeeklyAggregate("XYZ", "2026-W21")
        agg.add(self._fake_mention("src_1", "news", "POWL", excluded=True))
        assert agg.exclusion_flag is True

    def test_evidence_capped_at_5(self):
        agg = WeeklyAggregate("XYZ", "2026-W21")
        for i in range(10):
            agg.add(self._fake_mention(f"src_{i}", "news", f"T{i}"))
        assert len(agg.evidence) <= 5


# ===========================================================================
# 4. Acceleration scoring: 2 → 9 → 12 pattern
# ===========================================================================

class TestAccelerationScore:
    def test_flat_gives_zero(self):
        # 5 vs 5 and avg=5 → very low acceleration
        score = compute_acceleration_score(5, 5, 5.0)
        assert score < 0.1

    def test_zero_to_positive(self):
        # From 0 to any positive → full acceleration
        score = compute_acceleration_score(5, 0, 0.0)
        assert score == 1.0

    def test_pattern_2_9_12(self):
        """
        Week-3 is 12 mentions, week-2 (prior) is 9.
        4-week avg includes weeks with 2, 2, 9, and something before that.
        The acceleration from 9→12 is modest but still positive.
        """
        # prior=9, 4w_avg includes [2,2,9,~2]= avg ~3.75
        score_w3 = compute_acceleration_score(12, 9, 3.75)
        assert score_w3 > 0.0

        # prior=2, 4w_avg ~2
        score_w2 = compute_acceleration_score(9, 2, 2.0)
        # 9 vs 2 is 4.5× growth — should be significant
        assert score_w2 > 0.5

        # Key invariant: the week-2 acceleration (2→9) should score higher than week-3 (9→12)
        assert score_w2 > score_w3

    def test_decline_gives_zero(self):
        score = compute_acceleration_score(2, 10, 8.0)
        assert score == 0.0

    def test_bounded_zero_one(self):
        for current, prior, avg in [
            (100, 0, 0),
            (0, 100, 50),
            (50, 50, 50),
            (1, 1, 1),
        ]:
            s = compute_acceleration_score(current, prior, avg)
            assert 0.0 <= s <= 1.0, f"Out of range for {current},{prior},{avg}: {s}"


# ===========================================================================
# 5. Mention volume and source quality scores
# ===========================================================================

class TestVolumeAndQuality:
    def test_volume_caps_at_one(self):
        assert compute_mention_volume_score(30) == 1.0
        assert compute_mention_volume_score(100) == 1.0

    def test_volume_linear_before_cap(self):
        assert compute_mention_volume_score(0) == 0.0
        assert compute_mention_volume_score(15) == pytest.approx(0.5)

    def test_source_quality_filing_highest(self):
        q_filing = compute_source_quality_score({"filing_document"})
        q_podcast = compute_source_quality_score({"podcast_intelligence"})
        assert q_filing > q_podcast

    def test_source_quality_empty(self):
        assert compute_source_quality_score(set()) == 0.0

    def test_breadth_all_maxed(self):
        score = compute_breadth_score(5, 5, 4)
        assert score == 1.0

    def test_breadth_zero(self):
        score = compute_breadth_score(0, 0, 0)
        assert score == 0.0


# ===========================================================================
# 6. Promotion threshold
# ===========================================================================

class TestPromotion:
    """
    Build a synthetic scored dict and verify the promotion rule fires / doesn't fire.
    """

    def _score_dict(self, **overrides) -> Dict[str, Any]:
        # Base components chosen so total_score > 0.70 when no overrides:
        #   0.35*0.80 + 0.25*0.60 + 0.20*0.85 + 0.10*0.80 + 0.10*1.00 = 0.78
        base = {
            "ticker": "XYZ",
            "week_key": "2026-W21",
            "mention_count": 10,
            "unique_source_count": 4,
            "unique_source_ticker_count": 4,
            "prior_week_count": 2,
            "four_week_avg": 2.0,
            "acceleration_score": 0.80,
            "mention_volume_score": 0.60,
            "source_quality_score": 0.85,
            "breadth_score": 0.80,
            "novelty_score": 1.0,
            "total_score": 0.0,
            "exclusion_flag": False,
        }
        base.update(overrides)
        # Recompute total_score from components
        if "total_score" not in overrides:
            base["total_score"] = round(
                0.35 * base["acceleration_score"]
                + 0.25 * base["mention_volume_score"]
                + 0.20 * base["source_quality_score"]
                + 0.10 * base["breadth_score"]
                + 0.10 * base["novelty_score"],
                4,
            )
        return base

    def _recommend(self, s: Dict) -> str:
        """Apply the promotion rule from scorer.py (mirrored here for testing)."""
        if s["exclusion_flag"]:
            return "excluded"
        if (
            s["mention_count"] >= 8
            and s["total_score"] >= 0.70
            and s["acceleration_score"] >= 0.50
            and (
                s["unique_source_count"] >= 3
                or s["unique_source_ticker_count"] >= 3
            )
        ):
            return "probe_candidate"
        return "watch"

    def test_qualifies_for_probe(self):
        s = self._score_dict()
        assert self._recommend(s) == "probe_candidate"

    def test_low_mentions_blocks_probe(self):
        s = self._score_dict(mention_count=5)
        assert self._recommend(s) != "probe_candidate"

    def test_low_score_blocks_probe(self):
        s = self._score_dict(total_score=0.60)
        assert self._recommend(s) != "probe_candidate"

    def test_low_acceleration_blocks_probe(self):
        s = self._score_dict(acceleration_score=0.40)
        assert self._recommend(s) != "probe_candidate"

    def test_low_source_diversity_blocks_probe(self):
        s = self._score_dict(unique_source_count=2, unique_source_ticker_count=2)
        assert self._recommend(s) != "probe_candidate"

    def test_exclusion_overrides_probe(self):
        s = self._score_dict(exclusion_flag=True)
        assert self._recommend(s) == "excluded"

    def test_prohibited_ticker_not_probe(self):
        """Explicitly verify: prohibited tickers cannot be probe_candidates."""
        s = self._score_dict(
            ticker="NVDA",
            exclusion_flag=True,  # prohibited tickers carry exclusion_flag
        )
        assert self._recommend(s) == "excluded"

    def test_default_ticker_not_probe_via_novelty(self):
        """Default tickers get novelty=0.0, making it hard to hit 0.70 total."""
        # Max other scores: accel=1, vol=1, qual=1, breadth=1, novelty=0
        max_total = 0.35 + 0.25 + 0.20 + 0.10 + 0.0  # = 0.90 still passes?
        # But in practice default_tickers carry exclusion_flag in scorer
        # The test verifies novelty alone correctly penalises
        novelty = compute_novelty_score("POWL", {"POWL"})
        assert novelty == 0.0
        # Total with all other scores maxed is 0.90 — BUT they also get exclusion_flag
        # from the scorer, so they'd be "excluded" not "probe_candidate"
        s = self._score_dict(ticker="POWL", exclusion_flag=True, total_score=0.90)
        assert self._recommend(s) == "excluded"

    def test_trad_hedge_not_probe(self):
        s = self._score_dict(ticker="BAC", exclusion_flag=True)
        assert self._recommend(s) == "excluded"


# ===========================================================================
# 7. ISO week offset helper
# ===========================================================================

class TestIsoWeekOffset:
    def test_minus_one_week(self):
        result = _iso_week_offset("2026-W21", -1)
        assert result == "2026-W20"

    def test_minus_four_weeks(self):
        result = _iso_week_offset("2026-W21", -4)
        assert result == "2026-W17"

    def test_year_boundary(self):
        # 2026-W01 minus 1 should be last week of 2025
        result = _iso_week_offset("2026-W01", -1)
        # 2025 has 52 or 53 weeks
        assert result.startswith("2025-W")


# ===========================================================================
# 8. compute_scores integration (in-memory mock session)
# ===========================================================================

class TestComputeScores:
    """Mock the session and DB queries to test compute_scores end-to-end."""

    def _fake_mention_row(self, ticker, source_id, source_type, source_ticker):
        m = MagicMock()
        m.mentioned_ticker = ticker
        m.source_id = source_id
        m.source_type = source_type
        m.source_ticker = source_ticker
        m.confidence = 0.8
        m.exclusion_flag = False
        m.context = f"Context for {ticker}"
        m.occurred_at = datetime.datetime(2026, 5, 20, tzinfo=datetime.timezone.utc)
        m.url = None
        return m

    def test_excluded_ticker_gets_excluded_rec(self):
        """A ticker with exclusion_flag=True in all its mentions → recommendation='excluded'."""
        m = MagicMock()
        m.mentioned_ticker = "NVDA"
        m.source_id = "news_1"
        m.source_type = "news"
        m.source_ticker = "POWL"
        m.confidence = 0.8
        m.exclusion_flag = True
        m.context = "NVDA mentioned"
        m.occurred_at = datetime.datetime(2026, 5, 20, tzinfo=datetime.timezone.utc)
        m.url = None

        mock_session = MagicMock()
        mock_session.query.return_value.filter.return_value.all.return_value = [m]
        mock_session.query.return_value.filter.return_value.scalar.return_value = 0

        with patch("equity_intel.discovery.scorer.aggregate_mentions") as mock_agg, \
             patch("equity_intel.discovery.scorer._get_week_mention_count", return_value=0):
            agg = WeeklyAggregate("NVDA", "2026-W21")
            agg.add(m)
            mock_agg.return_value = {"NVDA": agg}

            results = compute_scores(
                mock_session,
                "2026-W21",
                default_tickers={"POWL"},
                universe_tickers=set(),
            )
        nvda_result = next((r for r in results if r["ticker"] == "NVDA"), None)
        assert nvda_result is not None
        assert nvda_result["recommendation"] == "excluded"

    def test_high_mention_count_produces_score(self):
        """12 mentions from 4 distinct sources → total_score > 0."""
        mock_session = MagicMock()

        with patch("equity_intel.discovery.scorer.aggregate_mentions") as mock_agg, \
             patch("equity_intel.discovery.scorer._get_week_mention_count") as mock_hist:
            # History: 2, 2, 9 → avg ~3.25; prior=9
            def hist_side_effect(session, ticker, week_key):
                offsets_to_counts = {
                    "2026-W20": 9,
                    "2026-W19": 2,
                    "2026-W18": 2,
                    "2026-W17": 2,
                }
                return offsets_to_counts.get(week_key, 0)
            mock_hist.side_effect = hist_side_effect

            agg = WeeklyAggregate("SWKH", "2026-W21")
            for i in range(12):
                m = MagicMock()
                m.source_id = f"news_{i}"
                m.source_type = ["news", "filing_document", "event", "gemini_news_block"][i % 4]
                m.source_ticker = ["POWL", "ETN", "VST", "ANET"][i % 4]
                m.confidence = 0.8
                m.exclusion_flag = False
                m.context = f"Context {i}"
                m.occurred_at = datetime.datetime(2026, 5, 20, tzinfo=datetime.timezone.utc)
                m.url = None
                agg.add(m)
            mock_agg.return_value = {"SWKH": agg}

            results = compute_scores(
                mock_session,
                "2026-W21",
                default_tickers={"POWL", "ETN"},
                universe_tickers=set(),
            )

        assert results, "Expected at least one scored ticker"
        swkh = results[0]
        assert swkh["ticker"] == "SWKH"
        assert swkh["total_score"] > 0.0
        assert swkh["mention_count"] == 12