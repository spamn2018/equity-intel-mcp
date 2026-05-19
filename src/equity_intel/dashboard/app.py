"""
Flask application factory and API routes for the local research dashboard.

API endpoints (all return JSON):
    GET /api/brief          - generate/return a catalyst brief for the watchlist
    GET /api/tickers        - return the configured default tickers
    GET /api/event_types    - return the known event type list
    GET /api/bias           - return the personal market-bias layer (if configured)
    GET /                   - serve the single-page dashboard HTML

Query parameters for /api/brief:
    tickers      comma-separated (default: settings.tickers_list)
    days         integer look-back window (default: 7)
    min_mat      float minimum materiality [0,1] (default: 0.3)
    event_types  comma-separated event type filter (default: all)
    low_conf     "1" to include low-confidence catalysts (default: omit)
    max_items    integer (default: 30)
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List

from flask import Flask, jsonify, render_template, request

from equity_intel.briefs.watchlist import get_watchlist_brief
from equity_intel.config import settings
from equity_intel.db.session import SessionLocal
from equity_intel.logging_config import get_logger

logger = get_logger(__name__)

# ------------------------------------------------------------------ #
# Known event types                                                    #
# ------------------------------------------------------------------ #

KNOWN_EVENT_TYPES: List[str] = [
    "earnings",
    "guidance",
    "merger_acquisition",
    "offering_or_dilution",
    "insider_transaction",
    "activist_stake",
    "management_change",
    "regulatory",
    "litigation",
    "bankruptcy_or_going_concern",
    "restatement",
    "buyback",
    "dividend",
    "product_announcement",
    "analyst_rating",
    "macro_sensitive_news",
    "unusual_price_volume",
    "other",
]


# ------------------------------------------------------------------ #
# Bias layer loader                                                    #
# ------------------------------------------------------------------ #


def _load_bias_layer() -> Dict[str, Any]:
    """
    Load the personal market-bias layer from ``bias_layer.json`` in the
    project root (next to ``.env``).

    The file is optional.  If absent or unreadable, returns an empty dict
    so the dashboard renders without the bias section.

    The bias layer is kept STRICTLY SEPARATE from source-grounded evidence.
    It is labelled clearly in both the API response and the UI as personal
    political/geopolitical opinion — not a system inference, not a buy/sell
    signal.
    """
    # Resolve relative to the working directory (typically the project root)
    bias_path = Path(os.environ.get("BIAS_LAYER_FILE", "bias_layer.json"))
    if not bias_path.is_absolute():
        # Walk up from this file until we find a pyproject.toml → project root
        here = Path(__file__).resolve().parent
        for parent in [here, here.parent, here.parent.parent, here.parent.parent.parent]:
            candidate = parent / bias_path
            if candidate.exists():
                bias_path = candidate
                break

    if not bias_path.exists():
        return {}

    try:
        raw = bias_path.read_text(encoding="utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict):
            return {}
        return data
    except Exception as exc:  # pragma: no cover
        logger.warning("bias_layer_load_error", path=str(bias_path), error=str(exc))
        return {}


# ------------------------------------------------------------------ #
# Application factory                                                  #
# ------------------------------------------------------------------ #


def create_app() -> Flask:
    """Create and configure the Flask dashboard application."""
    template_dir = Path(__file__).resolve().parent / "templates"
    app = Flask(__name__, template_folder=str(template_dir))
    app.config["JSON_SORT_KEYS"] = False

    # ---------------------------------------------------------------- #
    # Routes                                                             #
    # ---------------------------------------------------------------- #

    @app.route("/")
    def index():  # type: ignore[return]
        """Serve the single-page dashboard."""
        return render_template("index.html")

    @app.route("/api/tickers")
    def api_tickers():  # type: ignore[return]
        return jsonify({"tickers": settings.tickers_list})

    @app.route("/api/event_types")
    def api_event_types():  # type: ignore[return]
        return jsonify({"event_types": KNOWN_EVENT_TYPES})

    @app.route("/api/bias")
    def api_bias():  # type: ignore[return]
        """
        Return the personal market-bias layer.

        This section is ENTIRELY the user's own political/geopolitical
        opinion — it is NOT derived from SEC filings, news ingestion,
        event scoring, or any system inference.  It is labelled as such
        in both this response and the dashboard UI.
        """
        bias = _load_bias_layer()
        return jsonify(
            {
                "bias_layer": bias,
                "disclaimer": (
                    "The market-bias layer below is a personal "
                    "political/geopolitical overlay written by the user. "
                    "It is NOT derived from SEC filings, news, or system "
                    "scoring.  It does not constitute investment advice."
                ),
            }
        )

    @app.route("/api/brief")
    def api_brief():  # type: ignore[return]
        """
        Generate (or retrieve) the current catalyst brief.

        All filtering is done server-side; the client just passes params.
        """
        # -- Parse query params ----------------------------------------
        raw_tickers = request.args.get("tickers", "").strip()
        if raw_tickers:
            ticker_list = [t.strip().upper() for t in raw_tickers.split(",") if t.strip()]
        else:
            ticker_list = settings.tickers_list

        try:
            days = int(request.args.get("days", 7))
        except (ValueError, TypeError):
            days = 7

        try:
            min_mat = float(request.args.get("min_mat", 0.3))
            min_mat = max(0.0, min(1.0, min_mat))
        except (ValueError, TypeError):
            min_mat = 0.3

        raw_et = request.args.get("event_types", "").strip()
        event_types = [e.strip() for e in raw_et.split(",") if e.strip()] if raw_et else None

        low_conf = request.args.get("low_conf", "0") == "1"

        try:
            max_items = int(request.args.get("max_items", 30))
            max_items = max(1, min(100, max_items))
        except (ValueError, TypeError):
            max_items = 30

        # -- Generate brief --------------------------------------------
        session = SessionLocal()
        try:
            brief = get_watchlist_brief(
                session=session,
                tickers=ticker_list,
                days=days,
                min_materiality=min_mat,
                include_low_confidence=low_conf,
                max_items=max_items,
                event_types=event_types,
                include_price_context=True,
                include_news=True,
                include_filings=True,
            )
        except Exception as exc:  # pragma: no cover
            logger.error("brief_api_error", error=str(exc))
            return jsonify({"error": str(exc)}), 500
        finally:
            session.close()

        return jsonify(brief)

    return app
