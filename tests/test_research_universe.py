"""
Tests for the research_universe loader (src/equity_intel/research_universe.py).

Coverage
--------
* Loading the real config/ai_tickers.json
* Ignoring top-level ``_comment`` / ``_theme`` keys
* Returning all tickers deduplicated and uppercased
* Returning the category map (ticker → readable label)
* Returning metadata for both minimal and richer ticker entries
* Filtering by ``stage``
* MU deduplication (appears in two categories in the real config)
* /api/research_universe endpoint returns the expected shape
* New tickers added to ai_tickers.json are picked up by the dashboard
  AI-suggest context without editing _CAT_MAP in Python code
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from equity_intel.research_universe import (
    _clear_cache,
    _category_to_label,
    get_all_research_tickers,
    get_ticker_category_map,
    get_ticker_metadata,
    get_tickers_by_stage,
    load_research_universe,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def clear_universe_cache():
    """Flush the module cache before every test so results are always fresh."""
    _clear_cache()
    yield
    _clear_cache()


def _make_universe_file(tmp_path: Path, data: dict) -> Path:
    """Write a JSON universe file and return its path."""
    p = tmp_path / "ai_tickers.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# 1. Real config/ai_tickers.json integration
# ---------------------------------------------------------------------------


def test_loads_real_config():
    """load_research_universe() succeeds without error on the real config file."""
    universe = load_research_universe()
    assert "categories" in universe
    assert "ticker_metadata" in universe
    assert len(universe["ticker_metadata"]) > 0


def test_real_config_has_expected_categories():
    """Known categories from the real config must be present."""
    universe = load_research_universe()
    expected_cats = {
        "semiconductors_compute",
        "cloud_hyperscalers",
        "power_and_energy",
        "critical_minerals_rare_earth",
    }
    assert expected_cats.issubset(universe["categories"].keys())


def test_all_tickers_returns_nonempty_list():
    tickers = get_all_research_tickers()
    assert isinstance(tickers, list)
    assert len(tickers) > 10  # conservatively: real file has many tickers


def test_tickers_are_uppercase():
    tickers = get_all_research_tickers()
    for t in tickers:
        assert t == t.upper(), f"Ticker {t!r} is not uppercase"


def test_tickers_are_deduplicated():
    tickers = get_all_research_tickers()
    assert len(tickers) == len(set(tickers)), "Tickers list must be deduplicated"


def test_nvda_in_universe():
    tickers = get_all_research_tickers()
    assert "NVDA" in tickers


def test_nvda_category_map():
    cat_map = get_ticker_category_map()
    assert "NVDA" in cat_map
    # Category label should be a non-empty human-readable string
    assert cat_map["NVDA"]
    assert isinstance(cat_map["NVDA"], str)


# ---------------------------------------------------------------------------
# 2. Ignoring top-level _comment / _theme keys
# ---------------------------------------------------------------------------


def test_ignores_comment_key(tmp_path):
    data = {
        "_comment": "This is a comment — must be ignored.",
        "_theme": "Test theme.",
        "chips": {
            "_note": "chip note",
            "tickers": [
                {"ticker": "NVDA", "name": "NVIDIA", "why": "GPUs"}
            ],
        },
    }
    p = _make_universe_file(tmp_path, data)
    universe = load_research_universe(p)

    assert "_comment" not in universe["categories"]
    assert "_theme" not in universe["categories"]
    assert "chips" in universe["categories"]
    assert "NVDA" in universe["ticker_metadata"]


def test_ignores_all_underscore_prefixed_keys(tmp_path):
    data = {
        "_anything": "ignored",
        "_also_ignored": {"tickers": [{"ticker": "FAKE", "name": "Fake"}]},
        "real_category": {
            "tickers": [{"ticker": "REAL", "name": "Real Corp", "why": "reason"}]
        },
    }
    p = _make_universe_file(tmp_path, data)
    universe = load_research_universe(p)

    assert "_anything" not in universe["categories"]
    assert "_also_ignored" not in universe["categories"]
    assert "FAKE" not in universe["ticker_metadata"]
    assert "REAL" in universe["ticker_metadata"]


# ---------------------------------------------------------------------------
# 3. Ticker deduplication
# ---------------------------------------------------------------------------


def test_deduplication_first_category_wins(tmp_path):
    """When a ticker appears in two categories, first category is canonical."""
    data = {
        "category_a": {
            "tickers": [{"ticker": "DUAL", "name": "Dual Corp", "why": "first"}]
        },
        "category_b": {
            "tickers": [{"ticker": "DUAL", "name": "Dual Corp", "why": "second"}]
        },
    }
    p = _make_universe_file(tmp_path, data)
    universe = load_research_universe(p)

    tickers = get_all_research_tickers(p)
    assert tickers.count("DUAL") == 1, "DUAL must appear only once in the tickers list"

    meta = universe["ticker_metadata"]["DUAL"]
    assert meta["category"] == "category_a", "First category must win"


def test_deduplication_all_categories_tracked(tmp_path):
    """all_categories must list every category the ticker appears in."""
    data = {
        "category_a": {
            "tickers": [{"ticker": "MULTI", "name": "Multi Corp", "why": "a"}]
        },
        "category_b": {
            "tickers": [{"ticker": "MULTI", "name": "Multi Corp", "why": "b"}]
        },
        "category_c": {
            "tickers": [{"ticker": "MULTI", "name": "Multi Corp", "why": "c"}]
        },
    }
    p = _make_universe_file(tmp_path, data)
    universe = load_research_universe(p)

    meta = universe["ticker_metadata"]["MULTI"]
    assert set(meta["all_categories"]) == {"category_a", "category_b", "category_c"}


def test_mu_deduplicated_in_real_config():
    """MU appears in both semiconductors_compute and memory_and_storage in the real config."""
    tickers = get_all_research_tickers()
    assert tickers.count("MU") == 1, "MU must be deduplicated across categories"
    meta = get_ticker_metadata()["MU"]
    # Must list both categories
    assert len(meta["all_categories"]) == 2


# ---------------------------------------------------------------------------
# 4. Category map
# ---------------------------------------------------------------------------


def test_category_map_returns_string_values(tmp_path):
    data = {
        "my_category": {
            "tickers": [{"ticker": "X", "name": "X Corp", "why": "reason"}]
        }
    }
    p = _make_universe_file(tmp_path, data)
    cat_map = get_ticker_category_map(p)
    assert cat_map["X"] == "My Category"


def test_category_to_label_helper():
    assert _category_to_label("semiconductors_compute") == "Semiconductors Compute"
    assert _category_to_label("ai_software_platforms") == "Ai Software Platforms"
    assert _category_to_label("power_and_energy") == "Power And Energy"
    assert _category_to_label("single") == "Single"


def test_category_map_covers_all_tickers(tmp_path):
    """Every ticker returned by get_all_research_tickers must be in the cat map."""
    data = {
        "cat1": {
            "tickers": [
                {"ticker": "A", "name": "A Corp"},
                {"ticker": "B", "name": "B Corp"},
            ]
        },
        "cat2": {
            "tickers": [{"ticker": "C", "name": "C Corp"}]
        },
    }
    p = _make_universe_file(tmp_path, data)
    tickers = get_all_research_tickers(p)
    cat_map = get_ticker_category_map(p)
    for t in tickers:
        assert t in cat_map, f"Ticker {t} not in category map"


# ---------------------------------------------------------------------------
# 5. Metadata — minimal shape
# ---------------------------------------------------------------------------


def test_minimal_ticker_has_required_fields(tmp_path):
    data = {
        "chips": {
            "tickers": [{"ticker": "MINI", "name": "Mini Corp", "why": "A reason."}]
        }
    }
    p = _make_universe_file(tmp_path, data)
    meta = get_ticker_metadata(p)

    assert "MINI" in meta
    m = meta["MINI"]
    assert m["ticker"] == "MINI"
    assert m["name"] == "Mini Corp"
    assert m["why"] == "A reason."
    assert m["category"] == "chips"
    assert m["category_label"] == "Chips"
    assert "all_categories" in m


def test_minimal_ticker_has_no_stage_field(tmp_path):
    data = {
        "chips": {
            "tickers": [{"ticker": "MINI2", "name": "Mini Two", "why": "reason"}]
        }
    }
    p = _make_universe_file(tmp_path, data)
    meta = get_ticker_metadata(p)["MINI2"]
    assert "stage" not in meta


# ---------------------------------------------------------------------------
# 6. Metadata — richer shape
# ---------------------------------------------------------------------------


def test_rich_ticker_metadata_fields(tmp_path):
    data = {
        "power": {
            "tickers": [
                {
                    "ticker": "POWL",
                    "name": "Powell Industries Inc",
                    "why": "Electrical switchgear.",
                    "stage": "watch",
                    "conviction": "medium",
                    "thesis_tags": ["power", "data_centers", "electrification"],
                    "risk_tags": ["cyclical", "small_cap", "execution"],
                    "source": "manual_research",
                    "added_at": "2026-05-25",
                    "review_after": "2026-08-25",
                    "max_position_pct": 3,
                }
            ]
        }
    }
    p = _make_universe_file(tmp_path, data)
    meta = get_ticker_metadata(p)["POWL"]

    assert meta["stage"] == "watch"
    assert meta["conviction"] == "medium"
    assert "power" in meta["thesis_tags"]
    assert "small_cap" in meta["risk_tags"]
    assert meta["source"] == "manual_research"
    assert meta["added_at"] == "2026-05-25"
    assert meta["review_after"] == "2026-08-25"
    assert meta["max_position_pct"] == 3


def test_real_config_powl_has_metadata():
    """POWL in the real config must have richer metadata."""
    meta = get_ticker_metadata()
    assert "POWL" in meta
    m = meta["POWL"]
    assert m.get("stage") == "watch"
    assert "thesis_tags" in m
    assert "risk_tags" in m


def test_real_config_critical_minerals_are_probe():
    """All critical_minerals_rare_earth tickers must be probe-stage in the real config."""
    universe = load_research_universe()
    cat_tickers = universe["categories"]["critical_minerals_rare_earth"]["tickers"]
    for entry in cat_tickers:
        ticker = entry["ticker"]
        assert entry.get("stage") == "probe", (
            f"{ticker} in critical_minerals_rare_earth should be stage=probe"
        )


# ---------------------------------------------------------------------------
# 7. Filtering by stage
# ---------------------------------------------------------------------------


def test_get_tickers_by_stage_probe(tmp_path):
    data = {
        "early": {
            "tickers": [
                {"ticker": "P1", "name": "Probe One", "stage": "probe"},
                {"ticker": "P2", "name": "Probe Two", "stage": "probe"},
                {"ticker": "W1", "name": "Watch One", "stage": "watch"},
                {"ticker": "C1", "name": "Core One",  "stage": "core"},
                {"ticker": "NO", "name": "No Stage"},
            ]
        }
    }
    p = _make_universe_file(tmp_path, data)
    probes = get_tickers_by_stage("probe", p)
    assert set(probes) == {"P1", "P2"}


def test_get_tickers_by_stage_core(tmp_path):
    data = {
        "chips": {
            "tickers": [
                {"ticker": "NVDA", "name": "NVIDIA", "stage": "core"},
                {"ticker": "AMD",  "name": "AMD",    "stage": "core"},
                {"ticker": "MINI", "name": "Mini"},
            ]
        }
    }
    p = _make_universe_file(tmp_path, data)
    core = get_tickers_by_stage("core", p)
    assert "NVDA" in core
    assert "AMD" in core
    assert "MINI" not in core


def test_get_tickers_by_stage_nonexistent_stage(tmp_path):
    data = {
        "chips": {
            "tickers": [{"ticker": "NVDA", "name": "NVIDIA", "stage": "core"}]
        }
    }
    p = _make_universe_file(tmp_path, data)
    result = get_tickers_by_stage("archived", p)
    assert result == []


def test_real_config_stage_filtering():
    """get_tickers_by_stage must work on the real config without error."""
    core = get_tickers_by_stage("core")
    watch = get_tickers_by_stage("watch")
    probe = get_tickers_by_stage("probe")
    active = get_tickers_by_stage("active")

    # From the real config: NVDA, AMD, MSFT, GOOGL, AMZN, META are core
    assert "NVDA" in core
    assert "MSFT" in core

    # POWL is watch
    assert "POWL" in watch

    # Critical minerals are probe
    assert "MP" in probe
    assert "USAR" in probe

    # PLTR is active
    assert "PLTR" in active

    # No overlap between core and probe
    assert not (set(core) & set(probe))


# ---------------------------------------------------------------------------
# 8. Caching behaviour
# ---------------------------------------------------------------------------


def test_cache_returns_same_object(tmp_path):
    data = {
        "chips": {"tickers": [{"ticker": "NVDA", "name": "NVIDIA", "why": "GPUs"}]}
    }
    p = _make_universe_file(tmp_path, data)
    first = load_research_universe(p)
    second = load_research_universe(p)
    assert first is second, "Cached result should be the exact same object"


def test_clear_cache_forces_reload(tmp_path):
    data1 = {"chips": {"tickers": [{"ticker": "A", "name": "A Corp"}]}}
    data2 = {"chips": {"tickers": [{"ticker": "B", "name": "B Corp"}]}}
    p = _make_universe_file(tmp_path, data1)

    first = load_research_universe(p)
    assert "A" in first["ticker_metadata"]

    # Write new content and clear cache
    p.write_text(json.dumps(data2), encoding="utf-8")
    _clear_cache()

    second = load_research_universe(p)
    assert "B" in second["ticker_metadata"]
    assert "A" not in second["ticker_metadata"]


# ---------------------------------------------------------------------------
# 9. Ticker normalisation
# ---------------------------------------------------------------------------


def test_tickers_uppercased_from_lowercase_source(tmp_path):
    data = {
        "chips": {
            "tickers": [
                {"ticker": "nvda", "name": "NVIDIA"},
                {"ticker": "Amd",  "name": "AMD"},
            ]
        }
    }
    p = _make_universe_file(tmp_path, data)
    tickers = get_all_research_tickers(p)
    assert "NVDA" in tickers
    assert "AMD" in tickers
    assert "nvda" not in tickers


def test_empty_ticker_skipped(tmp_path):
    data = {
        "chips": {
            "tickers": [
                {"ticker": "",    "name": "Empty"},
                {"name": "No ticker key"},
                {"ticker": "OK",  "name": "Valid"},
            ]
        }
    }
    p = _make_universe_file(tmp_path, data)
    tickers = get_all_research_tickers(p)
    assert "OK" in tickers
    assert "" not in tickers


# ---------------------------------------------------------------------------
# 10. /api/research_universe endpoint
# ---------------------------------------------------------------------------


def test_api_research_universe_returns_200():
    """The endpoint returns 200 and correct top-level shape."""
    from equity_intel.dashboard.app import create_app

    app = create_app()
    app.config["TESTING"] = True

    with app.test_client() as client:
        resp = client.get("/api/research_universe")

    assert resp.status_code == 200
    data = resp.get_json()
    assert "categories" in data
    assert "ticker_metadata" in data
    assert "total_tickers" in data
    assert isinstance(data["total_tickers"], int)
    assert data["total_tickers"] > 0


def test_api_research_universe_includes_nvda():
    """NVDA must appear in both categories and ticker_metadata."""
    from equity_intel.dashboard.app import create_app

    app = create_app()
    app.config["TESTING"] = True

    with app.test_client() as client:
        data = client.get("/api/research_universe").get_json()

    assert "NVDA" in data["ticker_metadata"]
    assert "semiconductors_compute" in data["categories"]


def test_api_research_universe_note_field():
    """A note must be present to distinguish the universe from the active watchlist."""
    from equity_intel.dashboard.app import create_app

    app = create_app()
    app.config["TESTING"] = True

    with app.test_client() as client:
        data = client.get("/api/research_universe").get_json()

    assert "note" in data
    assert "watchlist" in data["note"].lower() or "DEFAULT_TICKERS" in data["note"]


# ---------------------------------------------------------------------------
# 11. Dashboard _CAT_MAP integration — new ticker picks up category
# ---------------------------------------------------------------------------


def test_new_ticker_in_config_appears_in_cat_map(tmp_path):
    """
    A ticker added to config/ai_tickers.json should show a category in the
    AI suggestion context without any edits to _CAT_MAP in Python code.
    """
    from equity_intel.research_universe import _clear_cache, get_ticker_category_map

    fake_universe = {
        "brand_new_sector": {
            "tickers": [
                {"ticker": "BRANDNEW", "name": "Brand New Corp", "why": "thesis"}
            ]
        }
    }
    p = _make_universe_file(tmp_path, fake_universe)

    _clear_cache()
    cat_map = get_ticker_category_map(p)
    assert "BRANDNEW" in cat_map
    assert cat_map["BRANDNEW"] == "Brand New Sector"
