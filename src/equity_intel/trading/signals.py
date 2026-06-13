"""
Trade signal generator.

Converts pipeline catalyst brief output (from get_watchlist_brief) into
TradeSignal records.  Never submits broker orders -- signal generation is
purely a data transformation step.

Signal strength formula (deterministic, explainable):
    strength = 0.50 * materiality
             + 0.30 * confidence
             + 0.10 * novelty
             + 0.10 * source_quality_bonus      (1.0 if primary-source, else 0.5)
    clamped to [0.0, 1.0]

TradHedge tickers are double-guarded here: even if a signal somehow
arrived for one of them it will be dropped.
"""
from __future__ import annotations

import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from equity_intel.config import settings as _default_settings
from equity_intel.db.models import TradingDecisionLog, TradeSignal
from equity_intel.logging_config import get_logger

logger = get_logger(__name__)

# Event-type to signal-side mapping

_BUY_TYPES: frozenset[str] = frozenset(
    {
        "earnings",
        "guidance_raised",
        "buyback",
        "merger_acquisition",
        "activist_stake",
        "positive_news",
    }
)

_SELL_TYPES: frozenset[str] = frozenset(
    {
        "guidance_lowered",
        "guidance_cut",
        "offering_or_dilution",
        "bankruptcy_or_going_concern",
        "restatement",
        "regulatory",
        "litigation",
        "delisting",
        "negative_news",
        "going_concern",
        "material_weakness",
        "sec_investigation",
    }
)

_MONITOR_TYPES: frozenset[str] = frozenset(
    {
        "management_change",
        "insider_transaction",
        "other",
        "news",
    }
)

# Subtype overrides (checked before type)
_BUY_SUBTYPES: frozenset[str] = frozenset(
    {"guidance_raised", "beat_estimates", "buyback_announced", "activist_stake"}
)

_SELL_SUBTYPES: frozenset[str] = frozenset(
    {
        "guidance_lowered",
        "guidance_cut",
        "dilution",
        "going_concern",
        "material_weakness",
        "sec_investigation",
        "delisting_notice",
        "crl",
        "complete_response_letter",
        "fda_rejection",
    }
)


def _resolve_side(event_type: Optional[str], event_subtype: Optional[str]) -> str:
    """Map an event_type / event_subtype to a signal side."""
    sub = (event_subtype or "").lower()
    typ = (event_type or "").lower()

    if sub in _SELL_SUBTYPES or typ in _SELL_TYPES:
        return "sell"
    if sub in _BUY_SUBTYPES or typ in _BUY_TYPES:
        return "buy"
    return "monitor"


def _signal_strength(
    materiality: float,
    confidence: float,
    novelty: float,
    has_primary_source: bool,
) -> float:
    mat = max(0.0, min(1.0, materiality))
    conf = max(0.0, min(1.0, confidence))
    nov = max(0.0, min(1.0, novelty))
    bonus = 1.0 if has_primary_source else 0.5
    raw = (
        0.50 * mat
        + 0.30 * conf
        + 0.10 * nov
        + 0.10 * bonus
    )
    return max(0.0, min(1.0, raw))


def _reason_codes(
    catalyst: Dict[str, Any],
    side: str,
    strength: float,
    min_signal_strength: float,
    min_materiality: float,
    min_confidence: float,
) -> List[str]:
    codes: List[str] = []
    if catalyst.get("has_primary_source"):
        codes.append("primary_source_confirmed")
    else:
        codes.append("news_only_source")
    if (catalyst.get("materiality_score") or 0) >= min_materiality:
        codes.append("materiality_threshold_met")
    if (catalyst.get("confidence_score") or 0) >= min_confidence:
        codes.append("confidence_threshold_met")
    if strength >= min_signal_strength:
        codes.append("strength_threshold_met")
    if side == "buy":
        codes.append("positive_event_type")
    elif side in ("sell", "reduce"):
        codes.append("negative_event_type")
    elif side == "monitor":
        codes.append("neutral_event_type")
    rs = catalyst.get("research_stage", "")
    if rs == "probe":
        codes.append("probe_stage_ticker")
    return codes


def _risk_flags(catalyst: Dict[str, Any]) -> List[str]:
    flags: List[str] = []
    if not catalyst.get("has_primary_source"):
        flags.append("no_primary_source")
    if catalyst.get("research_stage") == "probe":
        flags.append("probe_stage_ticker")
    if (catalyst.get("filing_count") or 0) == 0 and (catalyst.get("news_count") or 0) > 0:
        flags.append("news_only_signal")
    caution = catalyst.get("caution", "")
    if caution and len(caution) > 20:
        flags.append("pipeline_caution_present")
    return flags


def generate_trade_signals_from_brief(
    session: Session,
    brief: Dict[str, Any],
    *,
    min_materiality: float,
    min_confidence: float,
    min_signal_strength: float,
    require_primary_source: bool,
    allow_news_only: bool,
    allow_probe_stage: bool,
    cfg=None,
) -> List[TradeSignal]:
    """
    Convert a watchlist brief dict into TradeSignal ORM objects and persist them.

    Parameters
    ----------
    session              : open SQLAlchemy session (caller manages commit)
    brief                : output of get_watchlist_brief(...)
    min_materiality      : drop catalysts below this threshold
    min_confidence       : drop catalysts below this threshold
    min_signal_strength  : catalysts below this become monitor-only
    require_primary_source : drop catalysts without a SEC filing
    allow_news_only      : if False, drop news-only catalysts
    allow_probe_stage    : if False, downgrade probe tickers to monitor
    cfg                  : settings object (defaults to module-level singleton)

    Returns
    -------
    List of persisted (or updated) TradeSignal objects.
    """
    cfg = cfg or _default_settings

    # Double-guard: TradHedge tickers must never receive trade signals
    trad_hedge = set(cfg.trad_hedge_list)

    catalysts: List[Dict[str, Any]] = brief.get("catalysts", [])
    generated: List[TradeSignal] = []
    skipped = 0

    for catalyst in catalysts:
        ticker = (catalyst.get("ticker") or "").upper()
        if not ticker:
            skipped += 1
            continue

        # TradHedge guard -- belt-and-suspenders
        if ticker in trad_hedge:
            logger.debug("signal_skipped_tradhedge", ticker=ticker)
            skipped += 1
            continue

        mat = float(catalyst.get("materiality_score") or 0.0)
        conf = float(catalyst.get("confidence_score") or 0.0)
        nov = float(catalyst.get("novelty_score") or 0.0)
        has_primary = bool(catalyst.get("has_primary_source", False))
        filing_count = int(catalyst.get("filing_count") or 0)
        news_count = int(catalyst.get("news_count") or 0)
        research_stage = catalyst.get("research_stage", "")

        # Gate 1: materiality
        if mat < min_materiality:
            _log_decision(session, None, ticker, "skipped",
                          "materiality " + str(round(mat, 3)) + " < " + str(min_materiality),
                          {"materiality": mat, "threshold": min_materiality})
            skipped += 1
            continue

        # Gate 2: confidence
        if conf < min_confidence:
            _log_decision(session, None, ticker, "skipped",
                          "confidence " + str(round(conf, 3)) + " < " + str(min_confidence),
                          {"confidence": conf, "threshold": min_confidence})
            skipped += 1
            continue

        # Gate 3 (require_primary_source) and Gate 4 (news-only) removed.
        # The LLM scorer assigns materiality/confidence based on actual content
        # regardless of source type. A news article scored by the LLM is as
        # valid a signal input as an SEC filing.

        # Determine side
        event_type = catalyst.get("event_type")
        event_subtype = catalyst.get("event_subtype")
        side = _resolve_side(event_type, event_subtype)

        # Gate 5: probe stage
        if research_stage == "probe" and not allow_probe_stage:
            side = "monitor"

        # Compute strength
        strength = _signal_strength(mat, conf, nov, has_primary)

        # Below min strength -> downgrade to monitor
        if strength < min_signal_strength:
            side = "monitor"

        # Build metadata
        reason_codes = _reason_codes(
            catalyst, side, strength,
            min_signal_strength, min_materiality, min_confidence,
        )
        risk_flags = _risk_flags(catalyst)
        rationale = _build_rationale(catalyst, side, strength, reason_codes)

        max_pos_pct = _ticker_max_position_pct(ticker)

        cluster_id: Optional[int] = catalyst.get("cluster_id")
        event_id: Optional[int] = None

        # Upsert: prevent duplicate signals for the same source
        existing = _find_existing(session, ticker, cluster_id, event_id, side)
        if existing:
            existing.signal_strength = strength
            existing.materiality_score = mat
            existing.confidence_score = conf
            existing.novelty_score = nov
            existing.rationale = rationale
            existing.reason_codes_json = {"codes": reason_codes}
            existing.risk_flags_json = {"flags": risk_flags}
            existing.evidence_json = _build_evidence(catalyst)
            existing.price_context_json = catalyst.get("price_move")
            existing.updated_at = datetime.datetime.now(datetime.timezone.utc)
            logger.info("signal_updated", ticker=ticker, side=side, strength=round(strength, 4))
            generated.append(existing)
            continue

        # Create new signal
        sig = TradeSignal(
            ticker=ticker,
            signal_side=side,
            signal_strength=strength,
            status="generated",
            source="cluster" if cluster_id else "event",
            source_catalyst_id=catalyst.get("cluster_key"),
            source_cluster_id=cluster_id,
            source_event_id=event_id,
            generated_at=datetime.datetime.now(datetime.timezone.utc),
            materiality_score=mat,
            confidence_score=conf,
            novelty_score=nov,
            event_type=event_type,
            event_subtype=event_subtype,
            title=catalyst.get("title"),
            rationale=rationale,
            reason_codes_json={"codes": reason_codes},
            risk_flags_json={"flags": risk_flags},
            evidence_json=_build_evidence(catalyst),
            price_context_json=catalyst.get("price_move"),
            research_stage=research_stage or None,
            max_position_pct=max_pos_pct,
        )
        session.add(sig)
        session.flush()

        logger.info(
            "signal_generated",
            ticker=ticker,
            side=side,
            strength=round(strength, 4),
            event_type=event_type,
            cluster_id=cluster_id,
        )
        generated.append(sig)

    logger.info(
        "signal_generation_complete",
        generated=len(generated),
        skipped=skipped,
        total_catalysts=len(catalysts),
    )
    return generated


# --- Helpers ---


def _log_decision(
    session: Session,
    signal_id: Optional[int],
    ticker: str,
    decision: str,
    reason: str,
    details: Optional[Dict[str, Any]] = None,
) -> None:
    entry = TradingDecisionLog(
        trade_signal_id=signal_id,
        ticker=ticker,
        decision=decision,
        reason=reason,
        details_json=details,
    )
    session.add(entry)


def _find_existing(
    session: Session,
    ticker: str,
    cluster_id: Optional[int],
    event_id: Optional[int],
    side: str,
) -> Optional[TradeSignal]:
    """
    Look for an existing non-terminal signal for the same source.
    Terminal statuses (executed, rejected) are not reused.
    """
    q = session.query(TradeSignal).filter(
        TradeSignal.ticker == ticker,
        TradeSignal.signal_side == side,
        TradeSignal.status.notin_(["executed", "rejected", "expired", "failed"]),
    )
    if cluster_id is not None:
        q = q.filter(TradeSignal.source_cluster_id == cluster_id)
    elif event_id is not None:
        q = q.filter(TradeSignal.source_event_id == event_id)
    else:
        return None
    return q.first()


def _build_evidence(catalyst: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "cluster_key": catalyst.get("cluster_key"),
        "cluster_id": catalyst.get("cluster_id"),
        "source_links": catalyst.get("source_links", []),
        "related_filings": catalyst.get("related_filings", []),
        "related_news": catalyst.get("related_news", []),
        "source_summary": catalyst.get("source_summary"),
        "why_it_matters": catalyst.get("why_it_matters"),
    }


def _build_rationale(
    catalyst: Dict[str, Any],
    side: str,
    strength: float,
    reason_codes: List[str],
) -> str:
    ticker = catalyst.get("ticker", "")
    title = catalyst.get("title") or catalyst.get("event_type") or "catalyst"
    mat = catalyst.get("materiality_score") or 0
    strength_label = "strong" if strength >= 0.8 else "moderate" if strength >= 0.6 else "weak"
    side_label = {
        "buy": "bullish",
        "sell": "bearish",
        "reduce": "reduce-risk",
        "monitor": "watch-only",
        "avoid": "avoid",
    }.get(side, side)

    codes_str = ", ".join(reason_codes) if reason_codes else "none"
    return (
        strength_label + " " + side_label + " signal for " + ticker + " "
        "(materiality=" + str(round(mat, 2)) + ", strength=" + str(round(strength, 2)) + ") "
        "from: " + str(title) + ". Codes: " + codes_str + "."
    )


def _ticker_max_position_pct(ticker: str) -> Optional[float]:
    """
    Look up per-ticker max_position_pct from research_universe config.
    Returns None if not configured (global limit will apply).
    """
    try:
        from equity_intel.research_universe import get_ticker_metadata
        meta = get_ticker_metadata()
        ticker_meta = meta.get(ticker.upper(), {})
        return ticker_meta.get("max_position_pct")
    except Exception:
        return None
