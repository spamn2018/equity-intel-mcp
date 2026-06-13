"""
Flask application factory and API routes for the local research dashboard.

API endpoints (all return JSON):
    GET /api/brief                  - generate/return a catalyst brief for the watchlist
    GET /api/tickers                - return the configured default tickers
    GET /api/event_types            - return the known event type list
    GET /api/bias                   - return the personal market-bias layer (if configured)
    GET /api/intelligence/latest    - return the newest LM Studio synthesis report
    GET /api/discovery/tickers      - ticker discovery radar results
    GET /                           - serve the single-page dashboard HTML

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
import threading
import time
from pathlib import Path
from typing import Any, Dict, List

from flask import Flask, jsonify, render_template, request

from equity_intel.briefs.watchlist import get_watchlist_brief
from equity_intel.config import settings
from equity_intel.db.session import SessionLocal
from equity_intel.logging_config import get_logger

logger = get_logger(__name__)


def _news_blocks_diagnostic() -> Dict[str, Any]:
    """Return a small DB-backed diagnostic for the My Views news panel."""
    try:
        import datetime as _dt

        from equity_intel.db.models import NewsArticle

        cutoff = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=24)
        with SessionLocal() as session:
            recent_count = (
                session.query(NewsArticle)
                .filter(NewsArticle.published_at >= cutoff)
                .count()
            )
            latest = (
                session.query(NewsArticle)
                .filter(NewsArticle.published_at.isnot(None))
                .order_by(NewsArticle.published_at.desc())
                .first()
            )

        latest_ts = (
            latest.published_at.isoformat()
            if latest is not None and latest.published_at is not None
            else None
        )
        if recent_count == 0:
            message = (
                "No 24-hour news found for the current watchlist. "
                f"Last news article in database: {latest_ts or 'none'}. "
                "Run the news ingestion step, then refresh."
            )
        else:
            message = (
                f"{recent_count} news article(s) found in the last 24 hours, "
                "but no My Views news-block synthesis file exists yet. "
                "Rerun the AI Portfolio launcher or run equity-synthesize-news-blocks manually."
            )
        return {
            "recent_article_count": recent_count,
            "latest_article_published_at": latest_ts,
            "message": message,
        }
    except Exception as exc:
        logger.warning("news_blocks_diagnostic_error", error=str(exc))
        return {
            "message": (
                "No news-blocks synthesis found. Run run.bat (step 11b) "
                "or equity-synthesize-news-blocks manually."
            )
        }

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

def _load_cat_map() -> Dict[str, str]:
    """
    Load the ticker → category label map from config/ai_tickers.json via the
    research_universe module.

    Falls back to an empty dict so the dashboard continues to work even if the
    config file is missing or unreadable.  Unknown tickers will render as '?'
    in the AI suggestion context (unchanged from prior behaviour).
    """
    try:
        from equity_intel.research_universe import get_ticker_category_map
        return get_ticker_category_map()
    except Exception as exc:
        logger.warning("cat_map_load_failed", error=str(exc))
        return {}


# Populated at module load from config/ai_tickers.json; refreshed via
# _load_cat_map() if callers need a fresh copy.  Unknown tickers → '?'.
_CAT_MAP: Dict[str, str] = _load_cat_map()


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
# Intelligence report loader                                           #
# ------------------------------------------------------------------ #


def _intelligence_dir() -> Path:
    """
    Resolve the intelligence/ folder that lives next to the project root
    (the same directory that holds synthesize.py and run.bat).
    Walks up from this file until it finds an intelligence/ folder or
    a pyproject.toml sentinel, then returns the sibling intelligence/ path.
    """
    here = Path(__file__).resolve().parent
    for parent in [here, here.parent, here.parent.parent, here.parent.parent.parent]:
        candidate = parent / "intelligence"
        if candidate.exists():
            return candidate
        if (parent / "pyproject.toml").exists():
            return parent / "intelligence"
    return here.parent.parent.parent / "intelligence"


def _load_latest_intelligence() -> Dict[str, Any]:
    """
    Find the newest ``stocks_*.json`` synthesis file in intelligence/.

    Explicitly excludes ``gemini_news_*.json`` and any other non-synthesis
    files.  Returns a structured response dict suitable for jsonify().
    """
    intel_dir = _intelligence_dir()

    if not intel_dir.exists():
        return {
            "available": False,
            "message": (
                "No synthesized intelligence report found. "
                "Run run.bat or synthesize.py first."
            ),
        }

    # Only final synthesis files — pattern stocks_*.json excludes gemini_news_* etc.
    candidates = sorted(
        intel_dir.glob("stocks_*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )

    if not candidates:
        return {
            "available": False,
            "message": (
                "No synthesized intelligence report found. "
                "Run run.bat or synthesize.py first."
            ),
        }

    json_path = candidates[0]
    md_path = json_path.with_suffix(".md")

    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.error("intelligence_parse_error", path=str(json_path), error=str(exc))
        return {
            "available": False,
            "message": f"Report exists but could not be parsed: {exc}",
        }

    markdown = ""
    if md_path.exists():
        try:
            markdown = md_path.read_text(encoding="utf-8")
        except Exception:
            pass

    return {
        "available": True,
        "generated_at": data.get("generated_at", ""),
        "json_file": str(json_path),
        "md_file": str(md_path) if md_path.exists() else None,
        "markdown": markdown,
        "report": {
            "one_sentence_takeaway": data.get("one_sentence_takeaway", ""),
            "summary":               data.get("summary", ""),
            "top_signals":           data.get("top_signals", []),
            "key_risks":             data.get("key_risks", []),
            "actionable_intelligence": data.get("actionable_intelligence", []),
            "dominant_themes":       data.get("dominant_themes", []),
            "brief_count":           data.get("brief_count", 0),
            "date_range":            data.get("date_range", {}),
            "model_used":            data.get("model_used", ""),
        },
    }


# ------------------------------------------------------------------ #
# Application factory                                                  #
# ------------------------------------------------------------------ #


def create_app(shutdown_on_idle: bool = False, idle_timeout: int = 25) -> Flask:
    """Create and configure the Flask dashboard application.

    Args:
        shutdown_on_idle: If True, shut the process down automatically after
            ``idle_timeout`` seconds with no browser ping.  Intended for
            windowless (pythonw) launches where there is no Ctrl-C.
        idle_timeout: Seconds without a ``/api/ping`` before the process exits.
    """
    template_dir = Path(__file__).resolve().parent / "templates"
    app = Flask(__name__, template_folder=str(template_dir))
    app.config["JSON_SORT_KEYS"] = False

    # ---------------------------------------------------------------- #
    # Idle-shutdown watchdog                                             #
    # ---------------------------------------------------------------- #

    if shutdown_on_idle:
        _last_ping: list[float] = [time.monotonic()]  # mutable cell

        def _watchdog() -> None:
            while True:
                time.sleep(5)
                if time.monotonic() - _last_ping[0] > idle_timeout:
                    logger.info("dashboard_idle_shutdown", timeout=idle_timeout)
                    os._exit(0)

        _wt = threading.Thread(target=_watchdog, daemon=True, name="idle-watchdog")
        _wt.start()
    else:
        _last_ping = None  # type: ignore[assignment]

    # ---------------------------------------------------------------- #
    # Routes                                                             #
    # ---------------------------------------------------------------- #

    @app.route("/")
    def index():  # type: ignore[return]
        """Serve the single-page dashboard."""
        return render_template("index.html")

    @app.route("/api/ping", methods=["POST", "GET"])
    def api_ping():  # type: ignore[return]
        """Heartbeat from the browser — resets the idle-shutdown timer."""
        if _last_ping is not None:
            _last_ping[0] = time.monotonic()
        return jsonify({"ok": True})

    @app.route("/api/tickers")
    def api_tickers():  # type: ignore[return]
        return jsonify({"tickers": settings.tickers_list})

    @app.route("/api/event_types")
    def api_event_types():  # type: ignore[return]
        return jsonify({"event_types": KNOWN_EVENT_TYPES})

    @app.route("/open-portfolio")
    def open_portfolio():  # type: ignore[return]
        """Open ai_portfolio.html in the default browser via OS shell."""
        portfolio_path = Path(r"C:\Users\noleg\Desktop\Claude\Projects\AI Portfolio\ai_portfolio.html")
        if portfolio_path.exists():
            os.startfile(str(portfolio_path))
            return jsonify({"status": "ok"})
        return jsonify({"status": "error", "detail": "Portfolio file not found"}), 404

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


    @app.route("/api/research_universe")
    def api_research_universe():  # type: ignore[return]
        """
        Return the full research universe loaded from config/ai_tickers.json.

        Read-only.  Includes every category, its tickers, and all available
        ticker metadata (stage, conviction, thesis_tags, risk_tags, etc.).

        Response shape::

            {
              "categories": {
                "semiconductors_compute": {
                  "note": "...",
                  "label": "Semiconductors Compute",
                  "tickers": [...]
                },
                ...
              },
              "ticker_metadata": {
                "NVDA": {
                  "ticker": "NVDA",
                  "name": "...",
                  "category": "semiconductors_compute",
                  "category_label": "Semiconductors Compute",
                  "stage": "core",
                  ...
                },
                ...
              },
              "total_tickers": 42,
              "note": "..."
            }
        """
        try:
            from equity_intel.research_universe import load_research_universe
            universe = load_research_universe()
            total = len(universe["ticker_metadata"])
            return jsonify({
                **universe,
                "total_tickers": total,
                "note": (
                    "Research universe loaded from config/ai_tickers.json. "
                    "This is the broad thesis-driven universe — it is NOT the active watchlist. "
                    "The active watchlist is controlled by DEFAULT_TICKERS / DAILY_BRIEF_WATCHLIST in .env."
                ),
            })
        except FileNotFoundError:
            return jsonify({
                "error": "config/ai_tickers.json not found.",
                "categories": {},
                "ticker_metadata": {},
                "total_tickers": 0,
            }), 404
        except Exception as exc:
            logger.error("research_universe_api_error", error=str(exc))
            return jsonify({"error": str(exc)}), 500

    @app.route("/api/portfolio/config")
    def api_portfolio_config():  # type: ignore[return]
        """
        Return the research universe filtered for AI Portfolio consumption.

        Strips prohibited tickers and the bitcoin-miners category (crypto-adjacent).
        Maps category keys to short display labels and assigns weights by conviction stage.

        Response shape:
            {
              "tickers": [
                {"ticker": "AMD", "name": "...", "category": "Chips", "category_key": "...",
                 "stage": "core", "weight": 15},
                ...
              ],
              "categories": ["Chips", "Chip Equip", ...],
              "total": 28
            }
        """
        _CAT_LABELS: dict = {
            "semiconductors_compute":           "Chips",
            "semiconductor_equipment":          "Chip Equip",
            "cloud_hyperscalers":               "Hyperscalers",
            "ai_software_platforms":            "AI Software",
            "data_centers_reits":               "Data Centers",
            "power_and_energy":                 "Power & Energy",
            "networking_and_interconnect":      "Networking",
            "memory_and_storage":               "Memory",
            "critical_minerals_rare_earth":     "Critical Minerals",
            "ai_infrastructure_replacements":   "AI Infra Replacements",
            "trad_hedge":                       "Trad Hedge",
        }
        _SKIP_CATEGORIES: set = {"bitcoin_miners_data_center_angle"}
        # "watch" stage = same weight as probe; normalization handles final %
        _STAGE_WEIGHT: dict = {"core": 15, "established": 10, "probe": 5, "watch": 5}

        # Market cap designation shown on portfolio cards next to category label
        _CAP_MAP: dict = {
            # Large Cap (>$10B)
            "NVDA": "Large", "AMD": "Large", "AVGO": "Large", "INTC": "Large",
            "QCOM": "Large", "MRVL": "Large", "ARM": "Large", "SMCI": "Large",
            "MU": "Large", "ASML": "Large", "AMAT": "Large", "LRCX": "Large",
            "KLAC": "Large", "ON": "Large", "MSFT": "Large", "GOOGL": "Large",
            "AMZN": "Large", "META": "Large", "ORCL": "Large", "PLTR": "Large",
            "SNOW": "Large", "DDOG": "Large", "NET": "Large", "MDB": "Large",
            "PATH": "Large", "EQIX": "Large", "DLR": "Large", "IRM": "Large",
            "CEG": "Large", "VRT": "Large", "VST": "Large", "NEE": "Large",
            "NRG": "Large", "ETN": "Large", "FSLR": "Large", "ANET": "Large",
            "CSCO": "Large", "WDC": "Large", "STX": "Large", "BAC": "Large",
            "CI": "Large", "STT": "Large", "CTVA": "Large", "CL": "Large",
            "HIG": "Large", "C": "Large",
            # Mid Cap ($2B-$10B)
            "POWL": "Mid", "CIEN": "Mid", "CORZ": "Mid", "MARA": "Mid",
            "FLS": "Mid", "WLY": "Mid",
            # Small Cap ($300M-$2B)
            "INFN": "Small", "CLSK": "Small", "RIOT": "Small", "HUT": "Small",
            "MP": "Small", "USAR": "Small", "UUUU": "Small", "WASH": "Small",
            # Micro Cap (<$300M)
            "AREC": "Micro", "UAMY": "Micro",
        }

        prohibited = set(settings.prohibited_tickers_list)

        try:
            from equity_intel.research_universe import load_research_universe
            universe = load_research_universe()
        except Exception as exc:
            logger.error("portfolio_config_universe_error", error=str(exc))
            # Fall back to default_tickers with no category metadata
            fallback = [
                {"ticker": t, "name": t, "category": "Tracked", "category_key": "tracked",
                 "stage": "established", "weight": 10}
                for t in settings.tickers_list if t not in prohibited
            ]
            return jsonify({"tickers": fallback, "categories": ["Tracked"], "total": len(fallback)})

        result = []
        seen_cats: list = []

        for cat_key, cat_data in universe.get("categories", {}).items():
            if cat_key in _SKIP_CATEGORIES:
                continue
            label = _CAT_LABELS.get(cat_key, cat_key.replace("_", " ").title())
            for ticker_obj in cat_data.get("tickers", []):
                if isinstance(ticker_obj, str):
                    ticker, name, stage = ticker_obj, ticker_obj, "probe"
                else:
                    ticker = ticker_obj.get("ticker", "")
                    name  = ticker_obj.get("name", ticker)
                    stage = ticker_obj.get("stage", "probe")
                if not ticker or ticker.upper() in prohibited:
                    continue
                if label not in seen_cats:
                    seen_cats.append(label)
                result.append({
                    "ticker":       ticker.upper(),
                    "name":         name,
                    "category":     label,
                    "category_key": cat_key,
                    "stage":        stage,
                    "weight":       _STAGE_WEIGHT.get(stage, 5),
                    "cap":          _CAP_MAP.get(ticker.upper(), ""),
                })

        return jsonify({"tickers": result, "categories": seen_cats, "total": len(result)})

    # ── Rebalancing ────────────────────────────────────────────────────────────

    def _build_alpaca_adapter():
        """Instantiate AlpacaBrokerAdapter from env settings. Raises on missing keys."""
        from equity_intel.trading.alpaca_adapter import AlpacaBrokerAdapter
        api_key    = getattr(settings, "alpaca_api_key", None) or ""
        secret_key = getattr(settings, "alpaca_secret_key", None) or ""
        paper      = getattr(settings, "alpaca_paper", True)
        if not api_key or not secret_key:
            raise ValueError("ALPACA_API_KEY / ALPACA_SECRET_KEY not set in .env")
        return AlpacaBrokerAdapter(api_key=api_key, secret_key=secret_key, paper=paper)

    def _normalize_cat_weights(portfolio_tickers):
        """
        Replicate the JS catWeights() logic server-side.
        Trad Hedge pinned to 5%; remainder distributed proportionally.
        """
        raw: dict = {}
        for t in portfolio_tickers:
            cat = t.get("category", "")
            raw[cat] = raw.get(cat, 0) + t.get("weight", 5)

        trad_key = next(
            (k for k in raw if "trad" in k.lower() and "hedge" in k.lower()), None
        )
        if not trad_key or len(raw) <= 1:
            total = sum(raw.values()) or 1
            return {k: round(v / total * 100, 2) for k, v in raw.items()}

        TRAD_PCT = 5.0
        others = {k: v for k, v in raw.items() if k != trad_key}
        others_sum = sum(others.values()) or 1
        remaining = 100.0 - TRAD_PCT
        result = {trad_key: TRAD_PCT}
        for k, v in others.items():
            result[k] = round(v / others_sum * remaining, 2)
        return result

    @app.route("/api/rebalance/preview")
    def api_rebalance_preview():  # type: ignore[return]
        """
        Compute a rebalance plan without executing any orders.

        Query params:
            buy_threshold_pct   float  (default 5.0)  — min underweight gap to queue a buy
            sell_threshold_pct  float  (default 10.0) — min overweight gap to trigger a trim
            account_value       float  (optional override of live equity)

        Returns the full rebalance plan dict.
        This endpoint is always safe — it NEVER submits orders to Alpaca.
        """
        try:
            adapter = _build_alpaca_adapter()
        except ValueError as exc:
            return jsonify({"error": str(exc), "dry_run": True}), 400
        except Exception as exc:
            logger.error("rebalance_adapter_error", error=str(exc))
            return jsonify({"error": f"Alpaca adapter init failed: {exc}", "dry_run": True}), 500

        try:
            from equity_intel.research_universe import load_research_universe
            universe = load_research_universe()
        except Exception as exc:
            logger.error("rebalance_universe_error", error=str(exc))
            return jsonify({"error": f"Research universe load failed: {exc}", "dry_run": True}), 500

        # Build the same filtered ticker list as /api/portfolio/config
        _CAT_LABELS: dict = {
            "semiconductors_compute":           "Chips",
            "semiconductor_equipment":          "Chip Equip",
            "cloud_hyperscalers":               "Hyperscalers",
            "ai_software_platforms":            "AI Software",
            "data_centers_reits":               "Data Centers",
            "power_and_energy":                 "Power & Energy",
            "networking_and_interconnect":      "Networking",
            "memory_and_storage":               "Memory",
            "critical_minerals_rare_earth":     "Critical Minerals",
            "ai_infrastructure_replacements":   "AI Infra Replacements",
            "trad_hedge":                       "Trad Hedge",
        }
        _SKIP = {"bitcoin_miners_data_center_angle"}
        _STAGE_W = {"core": 15, "established": 10, "probe": 5, "watch": 5}
        prohibited = set(settings.prohibited_tickers_list)

        portfolio_tickers = []
        for cat_key, cat_data in universe.get("categories", {}).items():
            if cat_key in _SKIP:
                continue
            label = _CAT_LABELS.get(cat_key, cat_key.replace("_", " ").title())
            for entry in cat_data.get("tickers", []):
                if not isinstance(entry, dict):
                    continue
                ticker = (entry.get("ticker") or "").strip().upper()
                if not ticker or ticker in prohibited:
                    continue
                portfolio_tickers.append({
                    "ticker":   ticker,
                    "name":     entry.get("name", ticker),
                    "category": label,
                    "stage":    entry.get("stage", "probe"),
                    "weight":   _STAGE_W.get(entry.get("stage", "probe"), 5),
                })

        cat_weights = _normalize_cat_weights(portfolio_tickers)

        buy_threshold  = float(request.args.get("buy_threshold_pct", 5.0))
        sell_threshold = float(request.args.get("sell_threshold_pct", 10.0))
        pause_sells    = request.args.get("pause_sell_side", "").lower() in ("1", "true", "yes")
        acct_val       = request.args.get("account_value")
        acct_val       = float(acct_val) if acct_val else None

        from equity_intel.trading.rebalance import build_rebalance_plan
        plan = build_rebalance_plan(
            portfolio_tickers=portfolio_tickers,
            category_weights_pct=cat_weights,
            adapter=adapter,
            account_value=acct_val,
            buy_threshold_pct=buy_threshold,
            sell_threshold_pct=sell_threshold,
            pause_sell_side=pause_sells,
            dry_run=True,
        )
        return jsonify(plan)

    @app.route("/api/rebalance/execute", methods=["POST"])
    def api_rebalance_execute():  # type: ignore[return]
        """
        Execute a rebalance plan. Requires TRADING_EXECUTION_ENABLED=True.

        Body (JSON): { "buy_threshold_pct": 5.0, "sell_threshold_pct": 10.0, "pause_sell_side": false, "account_value": null }

        This endpoint submits real orders to Alpaca.
        TRADING_EXECUTION_ENABLED must be True in .env or the request is rejected.
        """
        if not getattr(settings, "trading_execution_enabled", False):
            return jsonify({
                "error": "TRADING_EXECUTION_ENABLED is False. Set it to True in .env to allow order execution.",
                "dry_run": False,
            }), 403

        try:
            adapter = _build_alpaca_adapter()
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:
            return jsonify({"error": f"Alpaca adapter init failed: {exc}"}), 500

        body = request.get_json(silent=True) or {}
        buy_threshold  = float(body.get("buy_threshold_pct", 5.0))
        sell_threshold = float(body.get("sell_threshold_pct", 10.0))
        pause_sells    = bool(body.get("pause_sell_side", False))
        acct_val       = body.get("account_value")
        acct_val       = float(acct_val) if acct_val else None

        try:
            from equity_intel.research_universe import load_research_universe
            universe = load_research_universe()
        except Exception as exc:
            return jsonify({"error": f"Research universe load failed: {exc}"}), 500

        _CAT_LABELS: dict = {
            "semiconductors_compute":           "Chips",
            "semiconductor_equipment":          "Chip Equip",
            "cloud_hyperscalers":               "Hyperscalers",
            "ai_software_platforms":            "AI Software",
            "data_centers_reits":               "Data Centers",
            "power_and_energy":                 "Power & Energy",
            "networking_and_interconnect":      "Networking",
            "memory_and_storage":               "Memory",
            "critical_minerals_rare_earth":     "Critical Minerals",
            "ai_infrastructure_replacements":   "AI Infra Replacements",
            "trad_hedge":                       "Trad Hedge",
        }
        _SKIP = {"bitcoin_miners_data_center_angle"}
        _STAGE_W = {"core": 15, "established": 10, "probe": 5, "watch": 5}
        prohibited = set(settings.prohibited_tickers_list)

        portfolio_tickers = []
        for cat_key, cat_data in universe.get("categories", {}).items():
            if cat_key in _SKIP:
                continue
            label = _CAT_LABELS.get(cat_key, cat_key.replace("_", " ").title())
            for entry in cat_data.get("tickers", []):
                if not isinstance(entry, dict):
                    continue
                ticker = (entry.get("ticker") or "").strip().upper()
                if not ticker or ticker in prohibited:
                    continue
                portfolio_tickers.append({
                    "ticker":   ticker,
                    "name":     entry.get("name", ticker),
                    "category": label,
                    "stage":    entry.get("stage", "probe"),
                    "weight":   _STAGE_W.get(entry.get("stage", "probe"), 5),
                })

        cat_weights = _normalize_cat_weights(portfolio_tickers)

        from equity_intel.trading.rebalance import build_rebalance_plan
        plan = build_rebalance_plan(
            portfolio_tickers=portfolio_tickers,
            category_weights_pct=cat_weights,
            adapter=adapter,
            account_value=acct_val,
            buy_threshold_pct=buy_threshold,
            sell_threshold_pct=sell_threshold,
            pause_sell_side=pause_sells,
            dry_run=False,
        )
        return jsonify(plan)

    @app.route("/api/news-blocks/latest")
    def api_news_blocks_latest():  # type: ignore[return]
        """
        Return the newest 24h news-blocks synthesis from intelligence/.

        Only selects ``news_blocks_*.json`` files produced by
        equity-synthesize-news-blocks.  Returns a diagnostic payload when
        no file exists so the My Views tab can render a useful message.

        This endpoint returns AI-generated analysis for research purposes only.
        It is NOT an execution instruction and must not be connected to any
        trading or order-management system.
        """
        intel_dir = _intelligence_dir()

        if not intel_dir.exists():
            diag = _news_blocks_diagnostic()
            return jsonify({
                "available": False,
                **diag,
            })

        candidates = sorted(
            intel_dir.glob("news_blocks_*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )

        if not candidates:
            diag = _news_blocks_diagnostic()
            return jsonify({
                "available": False,
                **diag,
            })

        json_path = candidates[0]
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.error("news_blocks_parse_error", path=str(json_path), error=str(exc))
            return jsonify({
                "available": False,
                "message": f"news_blocks file exists but could not be parsed: {exc}",
            })

        return jsonify(data)

    @app.route("/api/intelligence/latest")
    def api_intelligence_latest():  # type: ignore[return]
        """
        Return the newest LM Studio synthesis report from intelligence/.

        Only selects ``stocks_*.json`` files — never ``gemini_news_*.json``
        or any other intermediate files.

        This endpoint returns AI-generated analysis for research purposes.
        It is NOT an execution instruction and must not be connected to
        any trading or order-management system without explicit human review.
        """
        try:
            result = _load_latest_intelligence()
            return jsonify(result)
        except Exception as exc:
            logger.error("intelligence_api_error", error=str(exc))
            return jsonify({"available": False, "message": str(exc)}), 500

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
    # News feed API                                                     #
    # ---------------------------------------------------------------- #

    @app.route("/api/news")
    def api_news():  # type: ignore[return]
        """
        Return recent news articles from the local DB.

        Query params:
            tickers   comma-separated list (default: all configured tickers)
            days      look-back window in days (default: 7)
            keywords  comma-separated keyword filter on title/summary
            limit     max articles (default: 60)
        """
        import datetime as _dt
        from equity_intel.db.models import NewsArticle
        from sqlalchemy import or_, func

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
        try:
            limit = min(int(request.args.get("limit", 60)), 200)
        except (ValueError, TypeError):
            limit = 60

        raw_kw = request.args.get("keywords", "").strip()
        keywords = [k.strip().lower() for k in raw_kw.split(",") if k.strip()] if raw_kw else []

        cutoff = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=days)

        session = SessionLocal()
        try:
            q = (
                session.query(NewsArticle)
                .filter(
                    NewsArticle.ticker.in_(ticker_list),
                    NewsArticle.published_at >= cutoff,
                )
            )
            if keywords:
                kw_filters = []
                for kw in keywords:
                    pattern = f"%{kw}%"
                    kw_filters.append(
                        or_(
                            func.lower(NewsArticle.title).like(pattern),
                            func.lower(NewsArticle.summary).like(pattern),
                        )
                    )
                q = q.filter(or_(*kw_filters))

            articles = (
                q.order_by(NewsArticle.published_at.desc())
                .limit(limit)
                .all()
            )

            result = []
            for a in articles:
                all_tickers = []
                if a.tickers_json and isinstance(a.tickers_json, dict):
                    all_tickers = a.tickers_json.get("tickers", [])
                elif a.ticker:
                    all_tickers = [a.ticker]

                matched_kw = []
                if keywords:
                    text = f"{a.title or ''} {a.summary or ''}".lower()
                    matched_kw = [kw for kw in keywords if kw in text]

                result.append({
                    "id": a.id,
                    "ticker": a.ticker,
                    "tickers": all_tickers,
                    "title": a.title,
                    "summary": a.summary,
                    "url": a.url,
                    "publisher": a.publisher,
                    "published_at": a.published_at.isoformat() if a.published_at else None,
                    "sentiment": (
                        a.sentiment_json.get("polygon_sentiment")
                        if a.sentiment_json and isinstance(a.sentiment_json, dict)
                        else None
                    ),
                    "matched_keywords": matched_kw,
                })
            return jsonify({
                "articles": result,
                "count": len(result),
                "tickers": ticker_list,
                "days": days,
                "keywords": keywords,
            })
        except Exception as exc:
            logger.error("news_api_error", error=str(exc))
            return jsonify({"error": str(exc)}), 500
        finally:
            session.close()

    @app.route("/api/news_brief")
    def api_news_brief():  # type: ignore[return]
        """
        Return per-ticker AI news signal using GPT-4o-mini.

        Groups recent news by ticker, sends headlines to OpenAI,
        returns a one-line market signal per ticker.

        Query params:
            tickers   comma-separated (default: all configured)
            days      look-back window in days (default: 3)
        """
        import datetime as _dt
        import json as _json
        from equity_intel.db.models import NewsArticle

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
            days = int(request.args.get("days", 3))
        except (ValueError, TypeError):
            days = 3

        cutoff = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=days)

        session = SessionLocal()
        try:
            articles = (
                session.query(NewsArticle)
                .filter(
                    NewsArticle.ticker.in_(ticker_list),
                    NewsArticle.published_at >= cutoff,
                )
                .order_by(NewsArticle.published_at.desc())
                .limit(300)
                .all()
            )
        except Exception as exc:
            logger.error("news_brief_db_error", error=str(exc))
            return jsonify({"error": str(exc)}), 500
        finally:
            session.close()

        # Group by ticker — top 5 headlines each
        by_ticker: dict = {}
        for a in articles:
            t = a.ticker or ""
            if t not in by_ticker:
                by_ticker[t] = []
            if len(by_ticker[t]) < 5:
                by_ticker[t].append(
                    f"[{a.publisher or 'news'}] {a.title or ''}"
                    + (f" — {(a.summary or '')[:120]}" if a.summary else "")
                )

        if not by_ticker:
            return jsonify({
                "signals": {
                    t: "No recent news in database — run sync_news to pull latest."
                    for t in ticker_list
                },
                "model": "none",
                "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            })

        news_block = ""
        for ticker, headlines in by_ticker.items():
            news_block += f"\n{ticker}:\n" + "\n".join(f"  • {h}" for h in headlines) + "\n"

        system_prompt = (
            "You are a financial news analyst. For each stock ticker below, write exactly one "
            "concise sentence (max 110 chars) summarizing the most market-relevant signal from "
            "the recent headlines. Focus on earnings, guidance, M&A, products, regulatory, or "
            "management events. If no significant news, say 'No major catalysts in this window.' "
            'Output ONLY valid JSON: {"TICKER": "signal sentence", ...} — no markdown, no extra keys.'
        )

        try:
            import openai as _openai
            client = _openai.OpenAI(api_key=openai_key)
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": f"Recent news headlines:{news_block}"},
                ],
                temperature=0.2,
                max_tokens=600,
                timeout=30,
            )
            raw_json = (resp.choices[0].message.content or "{}").strip()
            if raw_json.startswith("```"):
                raw_json = raw_json.split("\n", 1)[-1].rsplit("```", 1)[0]
            signals = _json.loads(raw_json)
        except Exception as exc:
            logger.error("news_brief_openai_error", error=str(exc))
            return jsonify({"error": f"OpenAI error: {exc}"}), 500

        for t in ticker_list:
            if t not in signals:
                signals[t] = "No recent news in database for this ticker."

        return jsonify({
            "signals": signals,
            "model": "gpt-4o-mini",
            "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        })


    # ---------------------------------------------------------------- #
    # Discovery Radar API                                               #
    # ---------------------------------------------------------------- #

    @app.route("/api/discovery/tickers")
    def api_discovery_tickers():
        """
        Return weekly discovery scores for candidate tickers.

        Query params: week, min_score, rec, limit
        """
        import datetime as _dt
        from equity_intel.db.models import TickerDiscoveryScore

        now = _dt.datetime.now(_dt.timezone.utc)
        iso = now.isocalendar()
        default_week = f"{iso[0]}-W{iso[1]:02d}"
        week_key = request.args.get("week", default_week)

        try:
            min_score = float(request.args.get("min_score", 0.0))
        except ValueError:
            min_score = 0.0
        try:
            limit = min(200, max(1, int(request.args.get("limit", 50))))
        except ValueError:
            limit = 50
        rec_filter = request.args.get("rec", None)

        prohibited_set = {
            t.strip().upper()
            for t in settings.prohibited_tickers.split(",") if t.strip()
        }
        trad_hedge_set = {
            t.strip().upper()
            for t in settings.trad_hedge_tickers.split(",") if t.strip()
        }

        try:
            with SessionLocal() as session:
                q = (
                    session.query(TickerDiscoveryScore)
                    .filter(
                        TickerDiscoveryScore.week_key == week_key,
                        TickerDiscoveryScore.total_score >= min_score,
                    )
                )
                if rec_filter:
                    q = q.filter(TickerDiscoveryScore.recommendation == rec_filter)
                rows = (
                    q.order_by(TickerDiscoveryScore.total_score.desc())
                    .limit(limit)
                    .all()
                )

                items = []
                for row in rows:
                    items.append({
                        "ticker": row.ticker,
                        "week_key": row.week_key,
                        "total_score": round(row.total_score, 4),
                        "mention_count": row.mention_count,
                        "unique_source_count": row.unique_source_count,
                        "unique_source_ticker_count": row.unique_source_ticker_count,
                        "prior_week_count": row.prior_week_count,
                        "four_week_avg": round(row.four_week_avg, 2),
                        "acceleration_score": round(row.acceleration_score, 4),
                        "mention_volume_score": round(row.mention_volume_score, 4),
                        "source_quality_score": round(row.source_quality_score, 4),
                        "breadth_score": round(row.breadth_score, 4),
                        "novelty_score": round(row.novelty_score, 4),
                        "recommendation": row.recommendation,
                        "exclusion_flag": row.exclusion_flag,
                        "is_prohibited": row.ticker in prohibited_set,
                        "is_trad_hedge": row.ticker in trad_hedge_set,
                        "evidence": row.evidence_json or [],
                    })

                return jsonify({
                    "week_key": week_key,
                    "total_candidates": len(items),
                    "probe_candidates": sum(
                        1 for i in items if i["recommendation"] == "probe_candidate"
                    ),
                    "tickers": items,
                })
        except Exception as exc:
            logger.error("discovery_api_error", error=str(exc))
            return jsonify({"error": str(exc), "tickers": []}), 500

    # ---------------------------------------------------------------- #
    # CORS — allow ai_portfolio.html (file:// or any local origin)     #
    # ---------------------------------------------------------------- #

    @app.after_request
    def add_cors_headers(response):  # type: ignore[return]
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
        response.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
        return response

    return app
