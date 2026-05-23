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
# AI Suggest helpers                                                   #
# ------------------------------------------------------------------ #

_SUGGEST_SYSTEM = (
    "You are a portfolio research assistant analyzing an AI/robotics research watchlist. "
    "This is a personal research tool — NOT investment advice. "
    "Allocate 100% across the provided tickers based on catalyst data, price momentum, and the user's market views. "
    "Output ONLY valid JSON matching this exact schema (no markdown, no extra keys):\n"
    '{"allocations":[{"ticker":"NVDA","pct":15,"reasoning":"one sentence max 115 chars"}],'
    '"summary":"2-3 sentence overview of the allocation rationale",'
    '"top_conviction":"TICKER","risk_flag":"one sentence main risk"}\n'
    "Rules: allocations must sum to exactly 100. Every ticker in the input must appear. "
    "Min allocation 1%, max 30%. Reasoning must cite a catalyst or price signal."
)

_CAT_MAP: Dict[str, str] = {
    "NVDA": "Chips", "AMD": "Chips", "AVGO": "Chips",
    "MSFT": "Hyperscalers", "GOOGL": "Hyperscalers", "AMZN": "Hyperscalers",
    "TSLA": "Robotics", "ISRG": "Robotics", "SYM": "Robotics",
    "META": "Software", "PLTR": "Software", "AI": "Software",
    "BOTZ": "ETFs", "ROBO": "ETFs",
}


def _build_suggest_context(
    ticker_list: List[str],
    brief: Dict[str, Any],
    quotes: Dict[str, Any],
    bias: Dict[str, Any],
) -> str:
    """Build a compact context string for the gpt-4o-mini prompt."""
    lines: List[str] = []

    lines.append("WATCHLIST:")
    lines.append(", ".join(f"{t}({_CAT_MAP.get(t, '?')})" for t in ticker_list))
    lines.append("")

    lines.append("PRICES (15-min delayed, USD):")
    for t in ticker_list:
        q = quotes.get(t) or {}
        price = q.get("price")
        if price is not None:
            chg = q.get("change_pct")
            hi = q.get("day_high")
            lo = q.get("day_low")
            whi = q.get("fifty_two_wk_high")
            wlo = q.get("fifty_two_wk_low")
            chg_s = f"{'+' if chg and chg > 0 else ''}{chg:.2f}%" if chg is not None else "—"
            hl = f" Day:{hi:.0f}/{lo:.0f}" if hi and lo else ""
            whl = f" 52W:{whi:.0f}/{wlo:.0f}" if whi and wlo else ""
            lines.append(f"  {t}: ${price:.2f} {chg_s}{hl}{whl}")
        else:
            lines.append(f"  {t}: price unavailable")
    lines.append("")

    # Top catalysts per ticker
    cats_by_ticker: Dict[str, List[Any]] = {}
    for c in (brief.get("catalysts") or []):
        tk = c.get("ticker", "")
        cats_by_ticker.setdefault(tk, []).append(c)

    lines.append("TOP CATALYSTS (7-day window):")
    for t in ticker_list:
        evs = sorted(cats_by_ticker.get(t, []), key=lambda e: e.get("materiality_score", 0), reverse=True)
        if evs:
            e = evs[0]
            mat = e.get("materiality_score", 0)
            conf = e.get("confidence_score", 0)
            etype = (e.get("event_type") or "other").replace("_", " ")
            title = (e.get("title") or "")[:80]
            lines.append(f"  {t}: {etype} [MAT:{mat:.2f} CONF:{conf:.2f}] {title}")
        else:
            lines.append(f"  {t}: no catalysts in window")
    lines.append("")

    if bias and bias.get("market_views"):
        lines.append("USER MARKET VIEWS:")
        for v in (bias.get("market_views") or [])[:5]:
            title = v.get("title", "")
            body = (v.get("body") or "")[:180]
            tickers = ",".join(v.get("tickers") or [])
            lines.append(f"  [{title}] {body} (tickers: {tickers})")
        lines.append("")

    return "\n".join(lines)


def _call_openai_suggest(api_key: str, context: str, ticker_list: List[str]) -> Dict[str, Any]:
    """Call gpt-4o-mini and return a parsed allocation dict."""
    import json as _json

    try:
        import openai as _openai  # type: ignore[import]
    except ImportError as exc:
        raise ImportError("openai package not installed. Run: pip install openai") from exc

    client = _openai.OpenAI(api_key=api_key)
    user_msg = f"Here is the current watchlist data. Generate the allocation JSON:\n\n{context}"

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": _SUGGEST_SYSTEM},
            {"role": "user", "content": user_msg},
        ],
        temperature=0.3,
        max_tokens=1400,
        response_format={"type": "json_object"},
    )

    raw = response.choices[0].message.content or "{}"
    data: Dict[str, Any] = _json.loads(raw)

    # Ensure every ticker is present
    allocs: List[Dict[str, Any]] = data.get("allocations") or []
    present = {a["ticker"] for a in allocs if "ticker" in a}
    for t in ticker_list:
        if t not in present:
            allocs.append({"ticker": t, "pct": 1, "reasoning": "Insufficient catalyst data."})

    # Normalise to exactly 100
    total = sum(float(a.get("pct", 0)) for a in allocs)
    if total > 0 and abs(total - 100) > 0.1:
        factor = 100.0 / total
        for a in allocs:
            a["pct"] = round(float(a["pct"]) * factor, 1)
        diff = round(100.0 - sum(a["pct"] for a in allocs), 1)
        if allocs:
            allocs[0]["pct"] = round(allocs[0]["pct"] + diff, 1)

    data["allocations"] = allocs
    return data


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

    @app.route("/api/prices")
    def api_prices():  # type: ignore[return]
        """
        Return live (15-min delayed) Yahoo Finance quotes for requested tickers.

        Query params:
            tickers   comma-separated list (required)

        Returns:
            { "quotes": { "NVDA": { price, change, change_pct, ... }, ... },
              "as_of": "<iso timestamp>" }
        """
        from equity_intel.prices.yahoo import YahooPriceProvider

        raw = request.args.get("tickers", "").strip()
        if not raw:
            return jsonify({"error": "tickers param required"}), 400

        ticker_list = [t.strip().upper() for t in raw.split(",") if t.strip()]
        if not ticker_list:
            return jsonify({"error": "no valid tickers"}), 400

        try:
            provider = YahooPriceProvider()
            quotes = provider.fetch_quotes(ticker_list)
        except ImportError as exc:
            return jsonify({"error": str(exc)}), 503
        except Exception as exc:
            logger.error("prices_api_error", error=str(exc))
            return jsonify({"error": str(exc)}), 500

        import datetime as _dt
        return jsonify({
            "quotes": quotes,
            "as_of": _dt.datetime.now(tz=_dt.timezone.utc).isoformat(),
        })

    @app.route("/api/suggest")
    def api_suggest():  # type: ignore[return]
        """
        Generate a suggested portfolio allocation via OpenAI gpt-4o-mini.

        Combines the current brief, live prices, and the personal bias layer
        into a compact prompt and returns structured allocation percentages
        with per-ticker reasoning.

        Requires OPENAI_API_KEY in the environment (or .env file).
        """
        import datetime as _dt
        import json as _json

        openai_key = os.environ.get("OPENAI_API_KEY") or getattr(settings, "openai_api_key", None)
        if not openai_key:
            return jsonify({"error": "OPENAI_API_KEY not configured. Add it to your .env file."}), 503

        raw_tickers = request.args.get("tickers", "").strip()
        ticker_list = (
            [t.strip().upper() for t in raw_tickers.split(",") if t.strip()]
            if raw_tickers
            else settings.tickers_list
        )
        try:
            days = int(request.args.get("days", 7))
        except (ValueError, TypeError):
            days = 7

        # Fetch brief
        session = SessionLocal()
        try:
            brief = get_watchlist_brief(
                session=session,
                tickers=ticker_list,
                days=days,
                min_materiality=0.2,
                include_low_confidence=True,
                max_items=60,
                include_price_context=True,
                include_news=True,
                include_filings=True,
            )
        except Exception as exc:
            logger.error("suggest_brief_error", error=str(exc))
            brief = {"catalysts": []}
        finally:
            session.close()

        # Fetch prices
        try:
            from equity_intel.prices.yahoo import YahooPriceProvider
            quotes = YahooPriceProvider().fetch_quotes(ticker_list)
        except Exception as exc:
            logger.warning("suggest_prices_error", error=str(exc))
            quotes = {}

        bias = _load_bias_layer()
        context = _build_suggest_context(ticker_list, brief, quotes, bias)

        try:
            result = _call_openai_suggest(openai_key, context, ticker_list)
        except Exception as exc:
            logger.error("suggest_openai_error", error=str(exc))
            return jsonify({"error": str(exc)}), 500

        result["generated_at"] = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()
        result["model"] = "gpt-4o-mini"
        return jsonify(result)

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

    # ---------------------------------------------------------------- #
    # CORS — allow ai_portfolio.html (file:// or any local origin)    #
    # ---------------------------------------------------------------- #

    @app.after_request
    def add_cors_headers(response):  # type: ignore[return]
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
        response.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
        return response

    return app
