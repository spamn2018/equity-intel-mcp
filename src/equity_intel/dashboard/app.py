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
import statistics
from collections import Counter
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


def _safe_iso(value: Any) -> Any:
    """Return an ISO string when available, otherwise pass through None."""
    return value.isoformat() if value is not None else None


def _safe_date_label(value: Any) -> str | None:
    """Return a YYYY-MM-DD label for date-like values when possible."""
    if value in (None, ""):
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()[:10]
    return str(value)[:10]


def _fetch_spy_benchmark(session_dates: List[Any], cfg=None) -> Dict[str, Any] | None:
    """Fetch SPY daily closes over the signal date range from Polygon."""
    cfg = cfg or settings
    api_key = getattr(cfg, "polygon_api_key", None)
    if not api_key:
        return None

    labels = [label for label in (_safe_date_label(value) for value in session_dates) if label]
    if not labels:
        return None

    min_date = min(labels)
    max_date = max(labels)
    url = (
        "https://api.polygon.io/v2/aggs/ticker/SPY/range/1/day/"
        f"{min_date}/{max_date}"
    )
    try:
        import requests

        resp = requests.get(
            url,
            params={"adjusted": "true", "sort": "asc", "apiKey": api_key},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results", [])
        if len(results) < 2:
            return None

        import datetime as _dt

        first_close = results[0]["c"]
        last_close = results[-1]["c"]
        first_dt = _dt.datetime.fromtimestamp(results[0]["t"] / 1000).strftime("%Y-%m-%d")
        last_dt = _dt.datetime.fromtimestamp(results[-1]["t"] / 1000).strftime("%Y-%m-%d")
        pct = (last_close - first_close) / first_close * 100
        return {
            "available": True,
            "start_date": first_dt,
            "end_date": last_dt,
            "start_price": round(first_close, 4),
            "end_price": round(last_close, 4),
            "return_pct": round(pct, 3),
            "trading_days": len(results),
        }
    except Exception as exc:
        logger.warning("dashboard_spy_benchmark_error", error=str(exc))
        return None


def _build_same_day_report(rows: List[Any], cfg=None) -> Dict[str, Any]:
    """Build the same benchmark view used by the CLI same-day report."""
    by_status: Dict[str, List[Any]] = {}
    for row in rows:
        by_status.setdefault(row.outcome_status, []).append(row)

    report: Dict[str, Any] = {
        "total_count": len(rows),
        "status_counts": {status: len(group) for status, group in sorted(by_status.items())},
        "ok_count": len(by_status.get("ok", [])),
        "sides": {},
        "benchmark": {
            "available": False,
            "message": "S&P 500 benchmark unavailable - no POLYGON_API_KEY or network error",
        },
        "log_lines": [],
    }
    report["log_lines"].append(f"Same-day backtest report - n={len(rows)} total")
    for status, group in sorted(by_status.items()):
        report["log_lines"].append(f"{status}: n={len(group)}")

    ok_rows = by_status.get("ok", [])
    side_labels = (
        ("buy", "long P&L"),
        ("sell", "exit avoidance - closes longs, not short P&L"),
    )
    for side, label in side_labels:
        side_rows = [row for row in ok_rows if row.signal_side == side]
        returns = [row.net_return_pct for row in side_rows if row.net_return_pct is not None]
        win_count = sum(1 for value in returns if value > 0)
        side_report = {
            "label": label,
            "count": len(returns),
            "avg_net_return_pct": round(statistics.mean(returns), 3) if returns else None,
            "median_net_return_pct": round(statistics.median(returns), 3) if returns else None,
            "win_rate_pct": round((win_count / len(returns)) * 100.0, 1) if returns else None,
            "example_rows": [],
        }
        for row in side_rows[:5]:
            side_report["example_rows"].append({
                "trade_signal_id": row.trade_signal_id,
                "ticker": row.ticker,
                "signal_side": row.signal_side,
                "session_date": _safe_date_label(row.session_date),
                "entry_timestamp": _safe_iso(row.entry_timestamp),
                "entry_price": row.entry_price,
                "exit_timestamp": _safe_iso(row.exit_timestamp),
                "exit_price": row.exit_price,
                "net_return_pct": row.net_return_pct,
                "win_loss": row.win_loss,
                "flag": row.flag,
                "log_line": (
                    f"signal={row.trade_signal_id} {row.ticker} {row.signal_side} "
                    f"session={_safe_date_label(row.session_date)} entry={_safe_iso(row.entry_timestamp)}@{row.entry_price} "
                    f"exit={_safe_iso(row.exit_timestamp)}@{row.exit_price} net_ret={row.net_return_pct:+.3f}% "
                    f"{row.win_loss} flag={row.flag}"
                ),
            })
        report["sides"][side] = side_report
        if returns:
            report["log_lines"].append(
                f"{side} avg net {side_report['avg_net_return_pct']:+.3f}% | median {side_report['median_net_return_pct']:+.3f}% | win rate {side_report['win_rate_pct']:.1f}% | n={len(returns)}"
            )
            report["log_lines"].append(f"{side} examples:")
            report["log_lines"].extend(item["log_line"] for item in side_report["example_rows"])

    benchmark = _fetch_spy_benchmark([row.session_date for row in ok_rows if row.session_date is not None], cfg)
    if benchmark:
        buy_side = report["sides"].get("buy") or {}
        avg_buy = buy_side.get("avg_net_return_pct")
        alpha = round(avg_buy - benchmark["return_pct"], 3) if avg_buy is not None else None
        benchmark["signal_avg_buy_net_return_pct"] = avg_buy
        benchmark["alpha_per_trade_pct"] = alpha
        report["benchmark"] = benchmark
        report["log_lines"].append(
            f"SPY proxy: {benchmark['start_date']} -> {benchmark['end_date']} ({benchmark['trading_days']} trading days)"
        )
        report["log_lines"].append(
            f"SPY buy-and-hold {benchmark['return_pct']:+.3f}% from ${benchmark['start_price']:.2f} to ${benchmark['end_price']:.2f}"
        )
        if avg_buy is not None:
            report["log_lines"].append(f"Signal avg (buy) {avg_buy:+.3f}%")
            report["log_lines"].append(f"Alpha per trade {alpha:+.3f}%")

    return report

def _resolve_project_path(raw_path: str) -> Path:
    """Resolve a project-relative path against likely project roots."""
    path = Path(raw_path)
    if path.is_absolute():
        return path

    here = Path(__file__).resolve().parent
    for parent in [here, here.parent, here.parent.parent, here.parent.parent.parent]:
        candidate = parent / path
        if candidate.exists():
            return candidate
        if (parent / "pyproject.toml").exists():
            return candidate
    return here.parent.parent.parent / path


def _latest_strategy_review_artifact(cfg=None) -> Dict[str, Any]:
    """Return the newest saved strategy review artifact, if any."""
    cfg = cfg or settings
    artifact_dir = _resolve_project_path(
        getattr(cfg, "strategy_review_artifact_output_dir", "strategy_review_artifacts")
    )
    if not artifact_dir.exists():
        return {"available": False, "path": str(artifact_dir), "message": "No strategy review artifacts found yet."}

    candidates = sorted(
        artifact_dir.glob("*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        return {"available": False, "path": str(artifact_dir), "message": "No strategy review artifacts found yet."}

    artifact_path = candidates[0]
    try:
        data = json.loads(artifact_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("strategy_review_artifact_parse_error", path=str(artifact_path), error=str(exc))
        return {
            "available": False,
            "path": str(artifact_dir),
            "message": f"Latest strategy review artifact could not be parsed: {exc}",
        }

    review = data.get("review_result", data) if isinstance(data, dict) else {}
    survived = review.get("survived", []) if isinstance(review, dict) else []
    rejected = review.get("rejected", []) if isinstance(review, dict) else []
    auto_apply_result = review.get("auto_apply_result") if isinstance(review, dict) else None

    return {
        "available": True,
        "path": str(artifact_path),
        "generated_at": data.get("generated_at") or review.get("generated_at"),
        "status": review.get("status") or data.get("status"),
        "window_sessions": data.get("window_sessions") or review.get("window_sessions"),
        "survived_count": len(survived) if isinstance(survived, list) else 0,
        "rejected_count": len(rejected) if isinstance(rejected, list) else 0,
        "auto_apply_result": auto_apply_result,
        "survived_preview": survived[:3] if isinstance(survived, list) else [],
    }


def _build_trading_workflow_snapshot(cfg=None) -> Dict[str, Any]:
    """Assemble workflow state for the trading overview UI."""
    cfg = cfg or settings

    from equity_intel.db.models import (
        SameDaySignalOutcome,
        SignalOutcome,
        TickerDiscoveryScore,
        TradeOrder,
        TradeSignal,
        TradingDecisionLog,
    )

    import datetime as _dt

    directional_signal_count = 0
    signal_status_counts: Dict[str, int] = {}
    signal_side_counts: Dict[str, int] = {}
    recent_signals: List[Dict[str, Any]] = []

    order_status_counts: Dict[str, int] = {}
    decision_counts: Dict[str, int] = {}
    recent_orders: List[Dict[str, Any]] = []

    swing_summary: Dict[str, Dict[str, Any]] = {}
    same_day_summary: Dict[str, Any] = {}
    discovery_summary: Dict[str, Any] = {}
    performance_summary: Dict[str, Any] = {}

    def _coerce_float(value: Any) -> float | None:
        if value in (None, ""):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _build_compound_curve(
        values: List[tuple[str, float]],
        *,
        start_value: float = 10000.0,
        max_points: int = 40,
    ) -> List[Dict[str, Any]]:
        equity = start_value
        grouped: Dict[str, float] = {}
        for label, pct_return in sorted(values, key=lambda item: item[0]):
            equity *= 1.0 + (pct_return / 100.0)
            grouped[label] = round(equity, 2)
        points = [
            {"label": label, "equity": equity_value}
            for label, equity_value in grouped.items()
        ]
        if len(points) > max_points:
            points = points[-max_points:]
        return points

    with SessionLocal() as session:
        signal_rows = (
            session.query(TradeSignal)
            .order_by(TradeSignal.generated_at.desc())
            .limit(12)
            .all()
        )
        recent_signals = [
            {
                "id": row.id,
                "ticker": row.ticker,
                "signal_side": row.signal_side,
                "status": row.status,
                "signal_strength": row.signal_strength,
                "materiality_score": row.materiality_score,
                "confidence_score": row.confidence_score,
                "event_type": row.event_type,
                "title": row.title,
                "generated_at": _safe_iso(row.generated_at),
            }
            for row in signal_rows
        ]

        signal_status_counts = dict(
            Counter(getattr(row, "status", row[0]) for row in session.query(TradeSignal.status).all())
        )
        signal_side_counts = dict(
            Counter(getattr(row, "signal_side", row[0]) for row in session.query(TradeSignal.signal_side).all())
        )
        directional_signal_count = (
            session.query(TradeSignal)
            .filter(TradeSignal.signal_side.in_(("buy", "sell")))
            .count()
        )

        order_status_counts = dict(
            Counter(getattr(row, "status", row[0]) for row in session.query(TradeOrder.status).all())
        )
        decision_counts = dict(
            Counter(getattr(row, "decision", row[0]) for row in session.query(TradingDecisionLog.decision).all())
        )
        order_rows = (
            session.query(TradeOrder)
            .order_by(TradeOrder.created_at.desc())
            .limit(12)
            .all()
        )
        recent_orders = [
            {
                "id": row.id,
                "ticker": row.ticker,
                "side": row.side,
                "status": row.status,
                "broker": row.broker,
                "qty": row.qty,
                "notional": row.notional,
                "submitted_at": _safe_iso(row.submitted_at),
                "filled_at": _safe_iso(row.filled_at),
                "filled_avg_price": row.filled_avg_price,
                "failure_reason": row.failure_reason,
            }
            for row in order_rows
        ]

        swing_rows = (
            session.query(SignalOutcome)
            .filter(
                SignalOutcome.horizon_days.in_((1, 5, 10)),
                SignalOutcome.forward_return_pct.isnot(None),
            )
            .all()
        )
        swing_bucket: Dict[int, List[Any]] = {1: [], 5: [], 10: []}
        for row in swing_rows:
            swing_bucket.setdefault(row.horizon_days, []).append(row)
        for horizon in (1, 5, 10):
            rows = swing_bucket.get(horizon, [])
            returns = [row.forward_return_pct for row in rows if row.forward_return_pct is not None]
            wins = [row for row in rows if row.forward_return_pct is not None and row.forward_return_pct > 0]
            swing_summary[str(horizon)] = {
                "count": len(rows),
                "avg_return_pct": round(sum(returns) / len(returns), 3) if returns else None,
                "win_rate_pct": round((len(wins) / len(rows)) * 100.0, 1) if rows else None,
                "latest_computed_at": _safe_iso(max((row.computed_at for row in rows), default=None)),
            }

        same_day_rows = session.query(SameDaySignalOutcome).all()
        same_day_status_counts = dict(Counter(row.outcome_status for row in same_day_rows))
        same_day_ok_rows = [row for row in same_day_rows if row.gross_return_pct is not None]
        same_day_returns = [row.gross_return_pct for row in same_day_ok_rows if row.gross_return_pct is not None]
        same_day_wins = [row for row in same_day_ok_rows if row.gross_return_pct is not None and row.gross_return_pct > 0]
        same_day_summary = {
            "count": len(same_day_rows),
            "ok_count": len(same_day_ok_rows),
            "avg_gross_return_pct": round(sum(same_day_returns) / len(same_day_returns), 3) if same_day_returns else None,
            "win_rate_pct": round((len(same_day_wins) / len(same_day_ok_rows)) * 100.0, 1) if same_day_ok_rows else None,
            "latest_session_date": max((row.session_date for row in same_day_rows if row.session_date), default=None),
            "latest_computed_at": _safe_iso(max((row.computed_at for row in same_day_rows), default=None)),
            "outcome_status_counts": same_day_status_counts,
            "report": _build_same_day_report(same_day_rows, cfg),
        }

        now = _dt.datetime.now(_dt.timezone.utc)
        iso = now.isocalendar()
        week_key = f"{iso[0]}-W{iso[1]:02d}"
        discovery_rows = (
            session.query(TickerDiscoveryScore)
            .filter(TickerDiscoveryScore.week_key == week_key)
            .order_by(TickerDiscoveryScore.total_score.desc())
            .limit(8)
            .all()
        )
        discovery_summary = {
            "week_key": week_key,
            "count": len(discovery_rows),
            "probe_candidate_count": sum(1 for row in discovery_rows if row.recommendation == "probe_candidate"),
            "top_candidates": [
                {
                    "ticker": row.ticker,
                    "recommendation": row.recommendation,
                    "total_score": round(row.total_score, 4),
                    "mention_count": row.mention_count,
                    "unique_source_count": row.unique_source_count,
                }
                for row in discovery_rows
            ],
        }

        same_day_curve = _build_compound_curve(
            [
                (row.session_date, row.net_return_pct)
                for row in same_day_rows
                if row.session_date and row.net_return_pct is not None and row.outcome_status == "ok"
            ]
        )
        swing_curve_5d = _build_compound_curve(
            [
                (_safe_iso(row.t_horizon_date)[:10], row.forward_return_pct)
                for row in swing_bucket.get(5, [])
                if row.t_horizon_date is not None and row.forward_return_pct is not None
            ]
        )

        same_day_by_signal = {
            row.trade_signal_id: row
            for row in same_day_rows
            if row.trade_signal_id is not None
        }
        swing_5d_by_signal = {
            row.trade_signal_id: row
            for row in swing_bucket.get(5, [])
            if row.trade_signal_id is not None
        }
        filled_orders = (
            session.query(TradeOrder)
            .filter(TradeOrder.filled_at.isnot(None))
            .order_by(TradeOrder.filled_at.desc(), TradeOrder.id.desc())
            .all()
        )
        closed_results_preview = []
        estimated_same_day_results = []
        estimated_swing_results = []
        for order in filled_orders:
            same_day_row = same_day_by_signal.get(order.trade_signal_id) if order.trade_signal_id else None
            swing_5d_row = swing_5d_by_signal.get(order.trade_signal_id) if order.trade_signal_id else None
            same_day_estimate = (
                round(same_day_row.net_return_pct, 3)
                if same_day_row is not None and same_day_row.net_return_pct is not None
                else None
            )
            swing_5d_estimate = (
                round(swing_5d_row.forward_return_pct, 3)
                if swing_5d_row is not None and swing_5d_row.forward_return_pct is not None
                else None
            )
            if same_day_estimate is not None:
                estimated_same_day_results.append(same_day_estimate)
            if swing_5d_estimate is not None:
                estimated_swing_results.append(swing_5d_estimate)
            if len(closed_results_preview) < 8:
                closed_results_preview.append(
                    {
                        "id": order.id,
                        "ticker": order.ticker,
                        "side": order.side,
                        "filled_at": _safe_iso(order.filled_at),
                        "filled_avg_price": order.filled_avg_price,
                        "qty": order.qty,
                        "notional": order.notional,
                        "estimated_same_day_return_pct": same_day_estimate,
                        "estimated_swing_5d_return_pct": swing_5d_estimate,
                    }
                )

        broker_snapshot: Dict[str, Any] = {
            "provider": cfg.broker_provider,
            "available": False,
            "message": "Broker snapshot unavailable.",
            "account": None,
            "positions": [],
            "open_orders": [],
        }
        try:
            from equity_intel.trading.broker_factory import get_broker_adapter

            adapter = get_broker_adapter(cfg)
            if adapter is None:
                broker_snapshot["message"] = "Broker credentials are not configured for the active provider."
            else:
                broker_snapshot["account"] = adapter.get_account()
                broker_snapshot["positions"] = adapter.get_positions()
                broker_snapshot["open_orders"] = adapter.get_open_orders()
                broker_snapshot["available"] = True
                broker_snapshot["message"] = None
        except Exception as exc:
            broker_snapshot["message"] = str(exc)

        positions = broker_snapshot.get("positions") or []
        open_orders = broker_snapshot.get("open_orders") or []
        position_market_value = sum(_coerce_float(pos.get("market_value")) or 0.0 for pos in positions)
        unrealized_pl = sum(_coerce_float(pos.get("unrealized_pl")) or 0.0 for pos in positions)
        account = broker_snapshot.get("account") or {}
        account_equity = _coerce_float(account.get("equity"))

        performance_summary = {
            "broker": broker_snapshot,
            "live": {
                "account_equity": account_equity,
                "cash": _coerce_float(account.get("cash")),
                "buying_power": _coerce_float(account.get("buying_power")),
                "position_market_value": round(position_market_value, 2) if positions else None,
                "unrealized_pl": round(unrealized_pl, 2) if positions else None,
                "open_position_count": len(positions),
                "open_order_count": len(open_orders),
                "filled_order_count": len(filled_orders),
                "estimated_same_day_avg_return_pct": (
                    round(sum(estimated_same_day_results) / len(estimated_same_day_results), 3)
                    if estimated_same_day_results else None
                ),
                "estimated_swing_5d_avg_return_pct": (
                    round(sum(estimated_swing_results) / len(estimated_swing_results), 3)
                    if estimated_swing_results else None
                ),
                "closed_results_preview": closed_results_preview,
            },
            "curves": {
                "same_day": same_day_curve,
                "swing_5d": swing_curve_5d,
            },
        }

    policy_path = _resolve_project_path(getattr(cfg, "strategy_review_policy_file", ".cache/strategy_review/auto_applied_policy.json"))

    return {
        "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "mode": {
            "day_trade_mode": cfg.trading_day_trade_mode,
            "holding_style_label": "day trade" if cfg.trading_day_trade_mode else "swing",
            "primary_backtest": "same_day" if cfg.trading_day_trade_mode else "swing",
            "execution_enabled": cfg.trading_execution_enabled,
            "require_approval": cfg.trading_require_approval,
            "broker_provider": cfg.broker_provider,
            "close_time_et": cfg.trading_day_trade_close_time_et,
            "close_window_minutes": cfg.trading_day_trade_close_window_minutes,
        },
        "signal_generation": {
            "directional_signal_count": directional_signal_count,
            "status_counts": signal_status_counts,
            "side_counts": signal_side_counts,
            "recent_signals": recent_signals,
        },
        "execution": {
            "order_status_counts": order_status_counts,
            "decision_counts": decision_counts,
            "recent_orders": recent_orders,
        },
        "backtests": {
            "swing": swing_summary,
            "same_day": same_day_summary,
        },
        "performance": performance_summary,
        "strategy_review": {
            "auto_apply_enabled": cfg.strategy_review_auto_apply_enabled,
            "run_before_signal_generation_enabled": cfg.strategy_review_run_before_signal_generation_enabled,
            "policy_file": str(policy_path),
            "policy_file_exists": policy_path.exists(),
            "latest_artifact": _latest_strategy_review_artifact(cfg),
        },
        "discovery": discovery_summary,
    }


# ------------------------------------------------------------------ #
# Application factory                                                  #
# ------------------------------------------------------------------ #


def create_app(shutdown_on_idle: bool = False, idle_timeout: int = 3600) -> Flask:
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

    def _build_broker_adapter():
        """Instantiate the configured broker adapter (Alpaca or E*TRADE). Raises on missing keys/config."""
        from equity_intel.trading.broker_factory import get_broker_adapter
        adapter = get_broker_adapter(settings)
        if adapter is None:
            provider = getattr(settings, "broker_provider", "alpaca")
            raise ValueError(
                f"Broker credentials/config not set for provider={provider} "
                "(check ALPACA_API_KEY/ALPACA_SECRET_KEY or ETRADE_TOKEN_FILE/ETRADE_ACCOUNT_ID in .env)"
            )
        return adapter

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
            adapter = _build_broker_adapter()
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
            adapter = _build_broker_adapter()
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

    @app.route("/api/trading/workflow")
    def api_trading_workflow():  # type: ignore[return]
        """
        Return a workflow-oriented snapshot of the trading pipeline.

        This is a read-only summary for the dashboard UI. It explains the
        signal -> execution -> backtest -> review -> discovery chain using
        live project data where available.
        """
        try:
            return jsonify(_build_trading_workflow_snapshot(settings))
        except Exception as exc:
            logger.error("trading_workflow_api_error", error=str(exc))
            return jsonify({"error": str(exc)}), 500

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
