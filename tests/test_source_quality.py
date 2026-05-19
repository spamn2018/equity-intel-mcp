"""
Tests for src/equity_intel/events/source_quality.py

Coverage
--------
- SourceTier enum values
- SOURCE_TIER_SCORES mapping
- tier_for_source: all source types, publisher matching, URL fallback
- source_quality_score: scores map to expected tier
- source_quality_label: human-readable, non-empty
- source_quality_metadata: dict has required keys, correct types
- Integration: confidence boosted by high-quality source (via score.py)
- Edge cases: None inputs, unknown source type, empty strings
- No network calls

"""
from __future__ import annotations

import pytest

from equity_intel.events.source_quality import (
    SOURCE_TIER_LABELS,
    SOURCE_TIER_SCORES,
    SourceTier,
    source_quality_label,
    source_quality_metadata,
    source_quality_score,
    tier_for_source,
)
from equity_intel.events.score import compute_confidence_score, compute_cluster_confidence


# ===========================================================================
# SourceTier enum
# ===========================================================================

class TestSourceTierEnum:
    def test_all_five_tiers_exist(self):
        names = {t.name for t in SourceTier}
        assert names == {
            "SEC_FILING",
            "COMPANY_IR",
            "REPUTABLE_FINANCIAL",
            "SYNDICATED",
            "UNKNOWN",
        }

    def test_tier_values_are_strings(self):
        for tier in SourceTier:
            assert isinstance(tier.value, str)

    def test_sec_filing_tier_value(self):
        assert SourceTier.SEC_FILING.value == "sec_filing"

    def test_unknown_tier_value(self):
        assert SourceTier.UNKNOWN.value == "unknown"


# ===========================================================================
# SOURCE_TIER_SCORES mapping
# ===========================================================================

class TestSourceTierScores:
    def test_all_tiers_have_score(self):
        for tier in SourceTier:
            assert tier in SOURCE_TIER_SCORES

    def test_sec_filing_scores_1_0(self):
        assert SOURCE_TIER_SCORES[SourceTier.SEC_FILING] == 1.0

    def test_company_ir_scores_0_8(self):
        assert SOURCE_TIER_SCORES[SourceTier.COMPANY_IR] == 0.80

    def test_reputable_financial_scores_0_7(self):
        assert SOURCE_TIER_SCORES[SourceTier.REPUTABLE_FINANCIAL] == 0.70

    def test_syndicated_scores_0_5(self):
        assert SOURCE_TIER_SCORES[SourceTier.SYNDICATED] == 0.50

    def test_unknown_scores_0_3(self):
        assert SOURCE_TIER_SCORES[SourceTier.UNKNOWN] == 0.30

    def test_scores_ordered_descending(self):
        ordered = [
            SOURCE_TIER_SCORES[SourceTier.SEC_FILING],
            SOURCE_TIER_SCORES[SourceTier.COMPANY_IR],
            SOURCE_TIER_SCORES[SourceTier.REPUTABLE_FINANCIAL],
            SOURCE_TIER_SCORES[SourceTier.SYNDICATED],
            SOURCE_TIER_SCORES[SourceTier.UNKNOWN],
        ]
        assert ordered == sorted(ordered, reverse=True)


# ===========================================================================
# tier_for_source
# ===========================================================================

class TestTierForSource:
    # --- filing source type -------------------------------------------------

    def test_filing_source_type_returns_sec_filing(self):
        assert tier_for_source("filing") == SourceTier.SEC_FILING

    def test_filing_source_type_case_insensitive(self):
        assert tier_for_source("FILING") == SourceTier.SEC_FILING

    def test_filing_ignores_publisher(self):
        # Even if publisher is Reuters, a filing is still SEC_FILING
        assert tier_for_source("filing", publisher="Reuters") == SourceTier.SEC_FILING

    # --- press_release source type ------------------------------------------

    def test_press_release_returns_company_ir(self):
        assert tier_for_source("press_release") == SourceTier.COMPANY_IR

    # --- news with wire service publishers ----------------------------------

    def test_news_pr_newswire_returns_company_ir(self):
        assert tier_for_source("news", publisher="PR Newswire") == SourceTier.COMPANY_IR

    def test_news_business_wire_returns_company_ir(self):
        assert tier_for_source("news", publisher="Business Wire") == SourceTier.COMPANY_IR

    def test_news_globe_newswire_returns_company_ir(self):
        assert tier_for_source("news", publisher="GlobeNewswire") == SourceTier.COMPANY_IR

    # --- news with reputable financial publishers ---------------------------

    def test_news_reuters_returns_reputable(self):
        assert tier_for_source("news", publisher="Reuters") == SourceTier.REPUTABLE_FINANCIAL

    def test_news_bloomberg_returns_reputable(self):
        assert tier_for_source("news", publisher="Bloomberg") == SourceTier.REPUTABLE_FINANCIAL

    def test_news_wsj_returns_reputable(self):
        assert tier_for_source("news", publisher="Wall Street Journal") == SourceTier.REPUTABLE_FINANCIAL

    def test_news_cnbc_returns_reputable(self):
        assert tier_for_source("news", publisher="CNBC") == SourceTier.REPUTABLE_FINANCIAL

    def test_news_marketwatch_returns_reputable(self):
        assert tier_for_source("news", publisher="MarketWatch") == SourceTier.REPUTABLE_FINANCIAL

    def test_news_partial_publisher_match(self):
        # "Reuters Health" should still match Reuters
        assert tier_for_source("news", publisher="Reuters Health") == SourceTier.REPUTABLE_FINANCIAL

    # --- news with unrecognized publisher -----------------------------------

    def test_news_unknown_publisher_name_returns_syndicated(self):
        assert tier_for_source("news", publisher="Some News Blog") == SourceTier.SYNDICATED

    def test_news_no_publisher_returns_unknown(self):
        assert tier_for_source("news") == SourceTier.UNKNOWN

    def test_news_empty_publisher_returns_unknown(self):
        assert tier_for_source("news", publisher="") == SourceTier.UNKNOWN

    # --- URL fallback for sec.gov -------------------------------------------

    def test_news_sec_gov_url_returns_sec_filing(self):
        assert tier_for_source(
            "news",
            url="https://www.sec.gov/Archives/edgar/data/1234/0001.htm"
        ) == SourceTier.SEC_FILING

    def test_unknown_source_type_sec_url_returns_sec_filing(self):
        assert tier_for_source(
            "other",
            url="https://data.sec.gov/submissions/CIK0001234.json"
        ) == SourceTier.SEC_FILING

    # --- completely unknown source ------------------------------------------

    def test_empty_source_type_no_url_returns_unknown(self):
        assert tier_for_source("") == SourceTier.UNKNOWN

    def test_none_like_source_type_returns_unknown(self):
        assert tier_for_source("xyz_unknown_type") == SourceTier.UNKNOWN


# ===========================================================================
# source_quality_score
# ===========================================================================

class TestSourceQualityScore:
    def test_filing_score_is_1_0(self):
        assert source_quality_score("filing") == 1.0

    def test_press_release_score_is_0_8(self):
        assert source_quality_score("press_release") == 0.80

    def test_reuters_news_score_is_0_7(self):
        assert source_quality_score("news", publisher="Reuters") == 0.70

    def test_unknown_publisher_news_score_is_0_5(self):
        assert source_quality_score("news", publisher="Some Blog") == 0.50

    def test_no_publisher_news_score_is_0_3(self):
        assert source_quality_score("news") == 0.30

    def test_sec_outranks_news_only(self):
        filing_sq = source_quality_score("filing")
        news_sq = source_quality_score("news", publisher="Bloomberg")
        assert filing_sq > news_sq

    def test_syndicated_does_not_inflate_above_0_5(self):
        sq = source_quality_score("news", publisher="Unknown Outlet")
        assert sq <= 0.50

    def test_unknown_source_does_not_crash(self):
        sq = source_quality_score("totally_made_up_source_type")
        assert isinstance(sq, float)
        assert 0.0 <= sq <= 1.0

    def test_all_scores_in_range(self):
        for source_type in ["filing", "news", "press_release", "", "other"]:
            sq = source_quality_score(source_type)
            assert 0.0 <= sq <= 1.0, f"score out of range for source_type={source_type!r}"


# ===========================================================================
# source_quality_label
# ===========================================================================

class TestSourceQualityLabel:
    def test_filing_label_is_human_readable(self):
        label = source_quality_label("filing")
        assert isinstance(label, str) and len(label) > 3

    def test_filing_label_mentions_sec(self):
        label = source_quality_label("filing")
        assert "SEC" in label or "sec" in label.lower() or "Primary" in label

    def test_label_accepts_tier_enum_directly(self):
        label = source_quality_label(SourceTier.UNKNOWN)
        assert isinstance(label, str)

    def test_all_tiers_have_non_empty_labels(self):
        for tier in SourceTier:
            label = source_quality_label(tier)
            assert label and len(label) > 0

    def test_labels_differ_across_tiers(self):
        labels = [source_quality_label(t) for t in SourceTier]
        assert len(set(labels)) == len(labels), "All tier labels should be unique"


# ===========================================================================
# source_quality_metadata
# ===========================================================================

class TestSourceQualityMetadata:
    def test_returns_dict(self):
        meta = source_quality_metadata("filing")
        assert isinstance(meta, dict)

    def test_has_required_keys(self):
        meta = source_quality_metadata("news", publisher="Reuters")
        assert "source_quality_tier" in meta
        assert "source_quality_score" in meta
        assert "source_quality_label" in meta

    def test_tier_is_string(self):
        meta = source_quality_metadata("filing")
        assert isinstance(meta["source_quality_tier"], str)

    def test_score_is_float(self):
        meta = source_quality_metadata("filing")
        assert isinstance(meta["source_quality_score"], float)

    def test_label_is_string(self):
        meta = source_quality_metadata("filing")
        assert isinstance(meta["source_quality_label"], str)

    def test_filing_metadata_tier_value(self):
        meta = source_quality_metadata("filing")
        assert meta["source_quality_tier"] == SourceTier.SEC_FILING.value

    def test_evidence_json_embed_pattern(self):
        """evidence_json dict can be populated with ** unpacking."""
        base = {"accession_number": "0001234", "form_type": "8-K"}
        sq_meta = source_quality_metadata("filing")
        combined = {**base, **sq_meta}
        assert "source_quality_tier" in combined
        assert combined["accession_number"] == "0001234"


# ===========================================================================
# Integration: confidence scoring reacts to source quality
# ===========================================================================

class TestConfidenceScoreIntegration:
    def test_high_quality_source_boosts_confidence(self):
        low = compute_confidence_score(
            has_parsed_text=True, keyword_count=3, source_quality=0.3
        )
        high = compute_confidence_score(
            has_parsed_text=True, keyword_count=3, source_quality=1.0
        )
        assert high > low

    def test_filing_quality_gives_higher_confidence_than_news_only(self):
        filing_sq = source_quality_score("filing")
        news_sq = source_quality_score("news")
        conf_filing = compute_confidence_score(
            has_parsed_text=True, keyword_count=2, source_quality=filing_sq
        )
        conf_news = compute_confidence_score(
            has_parsed_text=True, keyword_count=2, source_quality=news_sq
        )
        assert conf_filing > conf_news

    def test_cluster_confidence_boosted_by_primary_source_quality(self):
        low = compute_cluster_confidence(
            base_confidence=0.5, has_price_reaction=False,
            filing_count=0, news_count=1, primary_source_quality=0.3
        )
        high = compute_cluster_confidence(
            base_confidence=0.5, has_price_reaction=False,
            filing_count=1, news_count=0, primary_source_quality=1.0
        )
        assert high > low

    def test_quality_adjustment_is_modest(self):
        """Quality adjustment is capped at ±0.10 range for confidence score."""
        low = compute_confidence_score(
            has_parsed_text=False, keyword_count=0, source_quality=0.3
        )
        high = compute_confidence_score(
            has_parsed_text=False, keyword_count=0, source_quality=1.0
        )
        diff = abs(high - low)
        assert diff <= 0.15, f"Quality adjustment too large: {diff}"

    def test_no_network_calls(self):
        """All functions are deterministic; no I/O should occur."""
        import socket
        original = socket.socket
        calls = []

        def mock_socket(*args, **kwargs):
            calls.append(True)
            return original(*args, **kwargs)

        socket.socket = mock_socket
        try:
            _ = source_quality_score("filing")
            _ = tier_for_source("news", publisher="Reuters")
            _ = source_quality_metadata("press_release")
        finally:
            socket.socket = original

        assert not calls, "source_quality functions must not make network calls"
