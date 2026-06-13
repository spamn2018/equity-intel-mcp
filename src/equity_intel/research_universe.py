"""
Research Universe Loader.

Loads ``config/ai_tickers.json`` and exposes helpers for the broader thesis-driven
research universe.  Separates the full universe (all ideas, all stages) from the
active watchlist defined in .env (DEFAULT_TICKERS / DAILY_BRIEF_WATCHLIST).

Design choices
--------------
* Ignores top-level keys that begin with ``_`` (e.g. ``_comment``, ``_theme``).
* Accepts both the minimal ticker shape::

    {"ticker": "NVDA", "name": "...", "why": "..."}

  and the richer metadata shape::

    {"ticker": "POWL", "name": "...", "why": "...",
     "stage": "watch", "conviction": "medium",
     "thesis_tags": ["power", "data_centers"],
     "risk_tags": ["cyclical", "small_cap"],
     "source": "manual_research",
     "added_at": "2026-05-25",
     "review_after": "2026-08-25",
     "max_position_pct": 3}

* Normalises ticker symbols to uppercase.
* Deduplicates tickers: first category encountered wins for the canonical
  record; all category memberships are stored in the metadata under
  ``all_categories``.
* Thread-safe module-level cache keyed by resolved file path.
* Never raises on missing optional fields — the richer fields are just absent
  when not present in the source JSON.

Stage vocabulary
----------------
probe    – early idea; collect data, do not treat as high-conviction
watch    – credible thesis fit; monitor catalysts
active   – part of the active research watchlist
core     – established high-conviction name
archived – no longer actively relevant, kept for history
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from equity_intel.config import settings

# ---------------------------------------------------------------------------
# Config path resolution
# ---------------------------------------------------------------------------

_RICHER_FIELDS = (
    "stage", "conviction", "thesis_tags", "risk_tags",
    "source", "added_at", "review_after", "max_position_pct",
)


def _default_config_path() -> Path:
    """Locate ``config/ai_tickers.json`` relative to the installed package."""
    here = Path(__file__).resolve().parent
    for ancestor in [here, here.parent, here.parent.parent, here.parent.parent.parent]:
        candidate = ancestor / "config" / "ai_tickers.json"
        if candidate.exists():
            return candidate
    # Fallback — callers will get a clear FileNotFoundError
    return here.parent.parent / "config" / "ai_tickers.json"


# ---------------------------------------------------------------------------
# Thread-safe module-level cache
# ---------------------------------------------------------------------------

_cache_lock = threading.Lock()
_cache: Dict[str, Any] = {}


def _clear_cache() -> None:
    """Flush the module-level cache.  Intended for use in tests only."""
    with _cache_lock:
        _cache.clear()


# ---------------------------------------------------------------------------
# Label helper
# ---------------------------------------------------------------------------

def _category_to_label(key: str) -> str:
    """Convert a snake_case category key to a human-readable label.

    Examples
    --------
    ``'semiconductors_compute'``  →  ``'Semiconductors Compute'``
    ``'ai_software_platforms'``   →  ``'Ai Software Platforms'``
    """
    return key.replace("_", " ").title()


# ---------------------------------------------------------------------------
# Core loader
# ---------------------------------------------------------------------------

def load_research_universe(path: Optional[Path] = None) -> Dict[str, Any]:
    """Load, parse, and cache the research universe from ``ai_tickers.json``.

    Returns a dict with the shape::

        {
            "categories": {
                "semiconductors_compute": {
                    "note": "...",
                    "label": "Semiconductors Compute",
                    "tickers": [ <enriched metadata dicts> ]
                },
                ...
            },
            "ticker_metadata": {
                "NVDA": {
                    "ticker": "NVDA",
                    "name": "...",
                    "why": "...",
                    "category": "semiconductors_compute",
                    "category_label": "Semiconductors Compute",
                    "all_categories": ["semiconductors_compute"],
                    # optional richer fields if present in source JSON:
                    "stage": "core",
                    "conviction": "high",
                    "thesis_tags": [...],
                    "risk_tags": [...],
                    ...
                },
                ...
            }
        }

    Results are cached per resolved file path.  Call ``_clear_cache()`` in
    tests that need a fresh load.

    Parameters
    ----------
    path:
        Explicit path to the JSON file.  If omitted, the default
        ``config/ai_tickers.json`` is located automatically.
    """
    resolved = (path or _default_config_path()).resolve()
    cache_key = str(resolved)

    with _cache_lock:
        if cache_key in _cache:
            return _cache[cache_key]

    raw: Dict[str, Any] = json.loads(resolved.read_text(encoding="utf-8"))
    prohibited = set(settings.prohibited_tickers_list)

    categories: Dict[str, Any] = {}
    ticker_metadata: Dict[str, Any] = {}
    seen: set[str] = set()

    for cat_key, cat_value in raw.items():
        # Skip metadata-only top-level keys
        if cat_key.startswith("_"):
            continue
        if not isinstance(cat_value, dict):
            continue
        if "tickers" not in cat_value:
            continue

        label = _category_to_label(cat_key)
        categories[cat_key] = {
            "note": cat_value.get("_note", ""),
            "label": label,
            "tickers": [],
        }

        for entry in cat_value.get("tickers", []):
            if not isinstance(entry, dict):
                continue
            ticker = (entry.get("ticker") or "").strip().upper()
            if not ticker:
                continue
            if ticker in prohibited:
                continue

            # Build enriched metadata for this entry
            meta: Dict[str, Any] = {
                "ticker": ticker,
                "name": entry.get("name", ""),
                "why": entry.get("why", ""),
                "category": cat_key,
                "category_label": label,
            }

            # Carry over any richer optional fields
            for field in _RICHER_FIELDS:
                if field in entry:
                    meta[field] = entry[field]

            # Deduplication: first category wins for the canonical record.
            # Track all category memberships regardless.
            if ticker not in seen:
                seen.add(ticker)
                meta["all_categories"] = [cat_key]
                ticker_metadata[ticker] = meta
            else:
                # Append the category to the existing record's all_categories
                ticker_metadata[ticker].setdefault("all_categories", [])
                if cat_key not in ticker_metadata[ticker]["all_categories"]:
                    ticker_metadata[ticker]["all_categories"].append(cat_key)

            # The per-category list always includes this entry
            categories[cat_key]["tickers"].append(meta)

    result: Dict[str, Any] = {
        "categories": categories,
        "ticker_metadata": ticker_metadata,
    }

    with _cache_lock:
        _cache[cache_key] = result

    return result


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def get_all_research_tickers(path: Optional[Path] = None) -> List[str]:
    """Return a deduplicated, uppercased list of all tickers in the universe.

    The order reflects the order in which tickers first appear across categories.
    """
    universe = load_research_universe(path)
    return list(universe["ticker_metadata"].keys())


def get_ticker_metadata(path: Optional[Path] = None) -> Dict[str, Dict[str, Any]]:
    """Return the full metadata dict keyed by uppercase ticker symbol.

    Every entry includes at minimum: ``ticker``, ``name``, ``why``,
    ``category``, ``category_label``, ``all_categories``.

    Richer entries additionally include any of: ``stage``, ``conviction``,
    ``thesis_tags``, ``risk_tags``, ``source``, ``added_at``,
    ``review_after``, ``max_position_pct``.
    """
    return load_research_universe(path)["ticker_metadata"]


def get_ticker_category_map(path: Optional[Path] = None) -> Dict[str, str]:
    """Return a ticker → human-readable category label map.

    Example::

        {
            "NVDA": "Semiconductors Compute",
            "MSFT": "Cloud Hyperscalers",
            "POWL": "Power And Energy",
            ...
        }

    If a ticker appears in multiple categories, the label of the first
    category is returned (consistent with deduplication order).
    """
    meta = get_ticker_metadata(path)
    return {ticker: info["category_label"] for ticker, info in meta.items()}


def get_tickers_by_stage(stage: str, path: Optional[Path] = None) -> List[str]:
    """Return all tickers whose explicit ``stage`` field matches ``stage``.

    Tickers without a ``stage`` field (minimal shape) are never returned.
    Case-sensitive comparison — use lowercase stage names as documented.

    Parameters
    ----------
    stage:
        One of ``probe``, ``watch``, ``active``, ``core``, ``archived``.
    """
    meta = get_ticker_metadata(path)
    return [
        ticker
        for ticker, info in meta.items()
        if info.get("stage") == stage
    ]
