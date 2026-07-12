"""
Execution service.

execute_approved_signals() is the final step in the trading pipeline:
it queries pending TradeSignal rows, evaluates each through the risk
policy, and submits approved orders to the broker adapter.

Safety guarantees
-----------------
- Broker submission is impossible when TRADING_EXECUTION_ENABLED=False.
- In that case the function returns immediately with an empty list.
- Every order attempt (success or failure) creates a TradeOrder row.
- Every blocked decision creates a TradingDecisionLog row.
- Signal status is updated after every outcome.
"""
from __future__ import annotations

import datetime
from collections import Counter
from typing import Any, Dict, List, Optional

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from equity_intel.config import settings as _default_settings
from equity_intel.db.models import TradingDecisionLog, TradeOrder, TradeSignal
from equity_intel.logging_config import get_logger
from equity_intel.trading.broker_factory import get_broker_adapter
from equity_intel.trading.risk import evaluate_signal_for_execution

logger = get_logger(__name__)

_RETRY_WINDOW_HOURS = 24.0
_OPEN_ORDER_STATUSES = {
    "accepted",
    "accepted_for_bidding",
    "calculated",
    "held",
    "new",
    "partially_filled",
    "pending_cancel",
    "pending_new",
    "pending_replace",
    "pre_accepted",
    "queued",
    "stopped",
    "suspended",
}


def _build_broker(cfg) -> Optional[Any]:
    """Construct the configured broker adapter. Returns None if keys/config are missing."""
    broker = get_broker_adapter(cfg)
    if broker is None:
        logger.warning("broker_unavailable -- execution disabled", provider=getattr(cfg, "broker_provider", "alpaca"))
    return broker


def execute_approved_signals(
    session: Session,
    cfg=None,
    *,
    limit: Optional[int] = None,
    dry_run: bool = False,
) -> List[TradeOrder]:
    """
    Query pending signals and attempt to execute them through Alpaca.

    Parameters
    ----------
    session  : open SQLAlchemy session (caller manages commit)
    cfg      : settings object (defaults to module-level singleton)
    limit    : optional cap on number of signals to process
    dry_run  : if True, evaluate risk but never submit broker orders

    Returns
    -------
    List of TradeOrder objects created during this run.
    """
    cfg = cfg or _default_settings

    if not cfg.trading_execution_enabled and not dry_run:
        logger.info("execution_skipped", reason="TRADING_EXECUTION_ENABLED=False")
        return []

    broker = _build_broker(cfg) if not dry_run else None
    if not dry_run and broker is None:
        logger.warning("execution_aborted", reason="broker credentials/config not set for " + str(getattr(cfg, "broker_provider", "alpaca")))
        return []

    q = session.query(TradeSignal).filter(
        or_(
            TradeSignal.status.in_(["generated", "approved", "pending_fill", "failed"]),
            and_(
                TradeSignal.status == "executed",
                TradeSignal.orders.any(TradeOrder.filled_at.is_(None)),
            ),
        )
    )
    if cfg.trading_require_approval:
        q = q.filter(TradeSignal.status.in_(["approved", "pending_fill", "failed", "executed"]))
    if limit:
        q = q.limit(limit)
    signals = q.all()

    if not signals:
        logger.info("execution_nothing_to_process")
        return []

    orders_created: List[TradeOrder] = []
    n_executed = 0
    n_blocked = 0
    n_failed = 0

    for signal in signals:
        if dry_run:
            logger.info(
                "dry_run_signal",
                ticker=signal.ticker,
                side=signal.signal_side,
                strength=signal.signal_strength,
                status=signal.status,
            )
            continue

        if signal.status == "failed":
            if _age_hours(signal) > _RETRY_WINDOW_HOURS:
                signal.status = "expired"
                signal.updated_at = _utc_now()
                logger.info("signal_expired", ticker=signal.ticker, age_hours=round(_age_hours(signal), 1))
                continue
            retry_status = _retry_status(signal, cfg)
            signal.status = retry_status
            signal.updated_at = _utc_now()
            logger.info("signal_retry_reset", ticker=signal.ticker, status=retry_status)

            if cfg.trading_require_approval and retry_status != "approved":
                # Needs human re-approval: leave it "generated" for review
                # instead of letting the approval gate mark it "blocked".
                logger.info("signal_awaiting_reapproval", ticker=signal.ticker)
                continue

        if signal.status in {"pending_fill", "executed"}:
            reconcile_result = _reconcile_submitted_signal(session, signal, broker, cfg)
            session.flush()
            if reconcile_result == "wait":
                continue
            if reconcile_result == "filled":
                n_executed += 1
                continue
            if reconcile_result == "expired":
                continue

        try:
            result = evaluate_signal_for_execution(session, signal, broker, cfg)
        except Exception as exc:
            logger.error("risk_evaluation_error", ticker=signal.ticker, error=str(exc))
            signal.status = _retry_status(signal, cfg)
            signal.updated_at = _utc_now()
            _add_decision_log(session, signal, "failed", "risk evaluation raised: " + str(exc))
            n_failed += 1
            continue

        if not result["allowed"]:
            if result.get("retriable"):
                now = _utc_now()
                age_hours = _age_hours(signal, now)
                if age_hours > _RETRY_WINDOW_HOURS:
                    signal.status = "expired"
                    signal.updated_at = now
                    logger.info("signal_expired", ticker=signal.ticker, age_hours=round(age_hours, 1))
                else:
                    logger.info(
                        "signal_will_retry",
                        ticker=signal.ticker,
                        reasons=result["reasons"],
                        age_hours=round(age_hours, 1),
                    )
            else:
                signal.status = "blocked"
                signal.updated_at = _utc_now()
                n_blocked += 1
                logger.info("signal_blocked", ticker=signal.ticker, reasons=result["reasons"])
            continue

        order_spec = result["order"]
        order = TradeOrder(
            trade_signal_id=signal.id,
            ticker=signal.ticker,
            side=order_spec["side"],
            qty=order_spec["qty"],
            notional=order_spec["notional"],
            order_type=order_spec["order_type"],
            time_in_force=order_spec["time_in_force"],
            broker=getattr(cfg, "broker_provider", "alpaca"),
            status="pending",
            expected_price=order_spec.get("expected_price"),
            raw_request_json=order_spec,
        )
        session.add(order)
        # flush deferred until after broker call -- keeps SQLite write lock
        # out of the network I/O window

        try:
            now = _utc_now()
            _otype = order_spec.get("order_type", "limit")
            if _otype == "market":
                response = broker.submit_market_order(
                    symbol=order_spec["symbol"],
                    side=order_spec["side"],
                    notional=order_spec.get("notional"),
                    qty=order_spec.get("qty"),
                )
            else:
                response = broker.submit_limit_order(
                    symbol=order_spec["symbol"],
                    side=order_spec["side"],
                    limit_price=order_spec["limit_price"],
                    notional=order_spec.get("notional"),
                    qty=order_spec.get("qty"),
                )
            order.broker_order_id = response.get("broker_order_id")
            order.status = _normalize_status(response.get("status")) or "submitted"
            order.submitted_at = now
            order.raw_response_json = response

            signal.status = "pending_fill"
            signal.updated_at = now

            _add_decision_log(
                session,
                signal,
                "submitted",
                "order submitted broker_id=" + str(order.broker_order_id),
                {"broker_order_id": order.broker_order_id},
            )
            orders_created.append(order)
            logger.info(
                "order_submitted",
                ticker=signal.ticker,
                side=order_spec["side"],
                qty=order_spec["qty"],
                broker_order_id=order.broker_order_id,
            )

        except Exception as exc:
            order.status = "failed"
            order.failure_reason = str(exc)
            order.raw_response_json = {"error": str(exc)}

            signal.status = _retry_status(signal, cfg)
            signal.updated_at = _utc_now()

            _add_decision_log(session, signal, "failed", "broker submission error: " + str(exc))
            n_failed += 1
            logger.error("order_failed", ticker=signal.ticker, error=str(exc))

        session.flush()

    logger.info(
        "execution_run_complete",
        considered=len(signals),
        executed=n_executed,
        blocked=n_blocked,
        failed=n_failed,
        dry_run=dry_run,
    )
    return orders_created


def build_execution_health_summary(
    session: Session,
    cfg=None,
    *,
    now: Optional[datetime.datetime] = None,
) -> Dict[str, Any]:
    """
    Build an operator-facing execution health snapshot from persisted DB state.
    """
    cfg = cfg or _default_settings
    now = now or _utc_now()
    if now.tzinfo is None:
        now = now.replace(tzinfo=datetime.timezone.utc)
    stale_cutoff = now - datetime.timedelta(
        minutes=getattr(cfg, "trading_reconcile_stale_minutes", 30)
    )
    lookback_cutoff = now - datetime.timedelta(
        hours=getattr(cfg, "trading_health_lookback_hours", 48)
    )

    def _as_utc(value: Optional[datetime.datetime]) -> Optional[datetime.datetime]:
        if value is None:
            return None
        return value if value.tzinfo is not None else value.replace(tzinfo=datetime.timezone.utc)

    pending_fill_signals = session.query(TradeSignal).filter(TradeSignal.status == "pending_fill").all()
    failed_signals = session.query(TradeSignal).filter(TradeSignal.status == "failed").all()
    recent_signals = session.query(TradeSignal).filter(TradeSignal.updated_at >= lookback_cutoff).all()
    recent_orders = session.query(TradeOrder).filter(TradeOrder.created_at >= lookback_cutoff).all()
    recent_logs = session.query(TradingDecisionLog).filter(TradingDecisionLog.created_at >= lookback_cutoff).all()
    open_orders = [
        order for order in session.query(TradeOrder).all()
        if _normalize_status(order.status) in _OPEN_ORDER_STATUSES
    ]

    stale_pending_fill = [
        signal for signal in pending_fill_signals
        if (_as_utc(signal.updated_at) or _as_utc(signal.created_at) or now) <= stale_cutoff
    ]
    stale_open_orders = [
        order for order in open_orders
        if (_as_utc(order.updated_at) or _as_utc(order.submitted_at) or _as_utc(order.created_at) or now) <= stale_cutoff
    ]

    recent_status_counts = Counter(signal.status for signal in recent_signals if signal.status)
    recent_decision_counts = Counter(log.decision for log in recent_logs if log.decision)
    recent_block_reason_counts = Counter(
        log.reason for log in recent_logs if log.decision == "blocked" and log.reason
    )
    recent_failed_order_count = sum(
        1 for order in recent_orders if _normalize_status(order.status) == "failed"
    )

    return {
        "as_of_utc": now.isoformat(),
        "lookback_hours": getattr(cfg, "trading_health_lookback_hours", 48),
        "stale_after_minutes": getattr(cfg, "trading_reconcile_stale_minutes", 30),
        "pending_fill_count": len(pending_fill_signals),
        "stale_pending_fill_count": len(stale_pending_fill),
        "open_order_count": len(open_orders),
        "stale_open_order_count": len(stale_open_orders),
        "failed_signal_count": len(failed_signals),
        "recent_failed_order_count": recent_failed_order_count,
        "recent_signal_status_counts": dict(recent_status_counts),
        "recent_decision_counts": dict(recent_decision_counts),
        "top_block_reasons": [
            {"reason": reason, "count": count}
            for reason, count in recent_block_reason_counts.most_common(5)
        ],
        "attention_required": bool(stale_pending_fill or stale_open_orders or failed_signals),
    }


def _reconcile_submitted_signal(
    session: Session,
    signal: TradeSignal,
    broker: Any,
    cfg,
) -> str:
    """
    Reconcile an already-submitted order before attempting another one.

    Returns one of: wait, filled, retry, expired.
    """
    latest_order = _latest_order_for_signal(session, signal)
    if latest_order is None or not latest_order.broker_order_id:
        age_hours = _age_hours(signal)
        if age_hours > _RETRY_WINDOW_HOURS:
            signal.status = "expired"
            signal.updated_at = _utc_now()
            logger.info("signal_expired", ticker=signal.ticker, age_hours=round(age_hours, 1))
            return "expired"
        signal.status = _retry_status(signal, cfg)
        signal.updated_at = _utc_now()
        _add_decision_log(
            session,
            signal,
            "failed",
            "submitted signal had no broker order id; resetting for retry",
        )
        return "retry"

    try:
        broker_order = broker.get_order(latest_order.broker_order_id)
    except Exception as exc:
        logger.warning(
            "order_reconcile_failed",
            ticker=signal.ticker,
            broker_order_id=latest_order.broker_order_id,
            error=str(exc),
        )
        return "wait"

    now = _utc_now()
    broker_status = _normalize_status(broker_order.get("status"))
    latest_order.status = broker_status or latest_order.status or "submitted"
    latest_order.raw_response_json = broker_order
    latest_order.updated_at = now

    if broker_status == "filled":
        latest_order.filled_at = _parse_dt(broker_order.get("filled_at")) or now
        fill_price = _to_float(broker_order.get("filled_avg_price"))
        latest_order.filled_avg_price = fill_price
        # Slippage: (fill - expected) / expected * 100
        # Positive = paid more (buy) or received less (sell) than the mid at submission.
        expected = latest_order.expected_price
        if fill_price and expected and expected != 0:
            latest_order.slippage_pct = round((fill_price - expected) / expected * 100, 4)
        signal.status = "executed"
        signal.updated_at = now
        _add_decision_log(
            session,
            signal,
            "executed",
            "order filled broker_id=" + str(latest_order.broker_order_id),
            {
                "broker_order_id": latest_order.broker_order_id,
                "fill_price": fill_price,
                "expected_price": expected,
                "slippage_pct": latest_order.slippage_pct,
            },
        )
        logger.info(
            "order_filled",
            ticker=signal.ticker,
            broker_order_id=latest_order.broker_order_id,
            fill_price=fill_price,
            expected_price=expected,
            slippage_pct=latest_order.slippage_pct,
        )
        return "filled"

    if broker_status in _OPEN_ORDER_STATUSES:
        signal.status = "pending_fill"
        signal.updated_at = now
        logger.info(
            "order_still_open",
            ticker=signal.ticker,
            broker_order_id=latest_order.broker_order_id,
            order_status=broker_status,
        )
        return "wait"

    age_hours = _age_hours(signal, now)
    if age_hours > _RETRY_WINDOW_HOURS:
        signal.status = "expired"
        signal.updated_at = now
        logger.info("signal_expired", ticker=signal.ticker, age_hours=round(age_hours, 1))
        return "expired"

    latest_order.failure_reason = "order closed without fill: " + str(broker_status or "unknown")
    signal.status = _retry_status(signal, cfg)
    signal.updated_at = now
    _add_decision_log(
        session,
        signal,
        "failed",
        "previous order closed without fill; resetting for retry status=" + str(broker_status or "unknown"),
        {"broker_order_id": latest_order.broker_order_id, "status": broker_status},
    )
    logger.info(
        "order_retry_reset",
        ticker=signal.ticker,
        broker_order_id=latest_order.broker_order_id,
        order_status=broker_status,
    )
    return "retry"


def _latest_order_for_signal(session: Session, signal: TradeSignal) -> Optional[TradeOrder]:
    return (
        session.query(TradeOrder)
        .filter(TradeOrder.trade_signal_id == signal.id)
        .order_by(TradeOrder.created_at.desc(), TradeOrder.id.desc())
        .first()
    )


def _retry_status(signal: TradeSignal, cfg) -> str:
    """Status a signal returns to when reset for retry.

    Only signals with actual evidence of prior approval (explicit approval
    fields, or a current status that is only reachable after passing the
    approval gate) go back to "approved". Everything else returns to
    "generated" and must be approved again -- a bare status="failed" is
    never auto-promoted past the approval gate.
    """
    was_approved = bool(signal.approved_at or signal.approved_by)
    if signal.status in ("approved", "pending_fill", "executed"):
        was_approved = True
    return "approved" if was_approved else "generated"


def _age_hours(signal: TradeSignal, now: Optional[datetime.datetime] = None) -> float:
    now = now or _utc_now()
    created = signal.created_at
    if created and created.tzinfo is None:
        created = created.replace(tzinfo=datetime.timezone.utc)
    return (now - created).total_seconds() / 3600 if created else 999.0


def _utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _normalize_status(status: Optional[str]) -> Optional[str]:
    if status is None:
        return None
    return str(status).strip().lower()


def _parse_dt(value: Any) -> Optional[datetime.datetime]:
    if not value:
        return None
    if isinstance(value, datetime.datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=datetime.timezone.utc)
    try:
        parsed = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=datetime.timezone.utc)
    except ValueError:
        return None


def _to_float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _add_decision_log(
    session: Session,
    signal: TradeSignal,
    decision: str,
    reason: str,
    details: Any = None,
) -> None:
    session.add(TradingDecisionLog(
        trade_signal_id=signal.id,
        ticker=signal.ticker,
        decision=decision,
        reason=reason,
        details_json=details if isinstance(details, dict) else {"info": str(details)},
    ))
