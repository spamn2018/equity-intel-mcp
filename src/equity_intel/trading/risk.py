"""
Risk / execution policy layer.

evaluate_signal_for_execution() applies all pre-flight checks before a
signal is handed to the broker adapter.  Every blocked decision is
persisted to TradingDecisionLog so there is a complete audit trail.

No broker orders are submitted here.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from equity_intel.db.models import TradingDecisionLog, TradeSignal
from equity_intel.logging_config import get_logger
from equity_intel.trading.alpaca_adapter import AlpacaBrokerAdapter

logger = get_logger(__name__)


def _get_price_via_gemini(ticker: str, api_key: str):
    import json, urllib.request
    if not api_key:
        return None
    url = ("https://generativelanguage.googleapis.com/v1beta/models/"
           "gemini-2.0-flash-latest:generateContent")
    body = json.dumps({"contents": [{"parts": [{"text":
        f"What is the current stock price of {ticker}? Reply with only the number in USD, no symbol or text."
    }]}]}).encode()
    req = urllib.request.Request(url, data=body, method="POST",
        headers={"Content-Type": "application/json", "X-goog-api-key": api_key})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        text = (data.get("candidates",[{}])[0].get("content",{})
                .get("parts",[{}])[0].get("text","").strip()
                .replace("$","").replace(",",""))
        return float(text)
    except Exception:
        return None


def _log_decision(
    session: Session,
    signal: TradeSignal,
    decision: str,
    reason: str,
    details: Optional[Dict[str, Any]] = None,
) -> None:
    entry = TradingDecisionLog(
        trade_signal_id=signal.id,
        ticker=signal.ticker,
        decision=decision,
        reason=reason,
        details_json=details,
    )
    session.add(entry)


def evaluate_signal_for_execution(
    session: Session,
    signal: TradeSignal,
    broker: AlpacaBrokerAdapter,
    cfg,
) -> Dict[str, Any]:
    """
    Evaluate a TradeSignal against all risk and execution policy rules.

    Returns
    -------
    {
        "allowed": bool,
        "reasons": [str, ...],   # human-readable list of block reasons
        "retriable": bool,      # True for transient failures retried later
        "order": {               # present only when allowed=True
            "symbol": str,
            "side": "buy" | "sell",
            "qty": float | None,
            "notional": float | None,
            "order_type": str,
            "time_in_force": str,
        } | None
    }

    All blocked decisions are written to TradingDecisionLog.
    """
    reasons: List[str] = []

    def block(reason: str, details: Optional[Dict] = None) -> None:
        reasons.append(reason)
        _log_decision(session, signal, "blocked", reason, details)

    def blocked_response(retriable: bool = False) -> Dict[str, Any]:
        return {"allowed": False, "retriable": retriable, "reasons": reasons, "order": None}

    # 1. Master execution kill-switch
    if not cfg.trading_execution_enabled:
        block("TRADING_EXECUTION_ENABLED=False -- broker submission disabled")
        return blocked_response()

    # 2. Approval gate
    if cfg.trading_require_approval and signal.status != "approved":
        block(
            f"TRADING_REQUIRE_APPROVAL=True but signal status is '{signal.status}'",
            {"signal_status": signal.status},
        )
        return blocked_response()

    # 3. Executable sides only
    executable_sides = {"buy", "sell", "reduce"}
    if signal.signal_side not in executable_sides:
        block(
            f"signal_side '{signal.signal_side}' is not executable (allowed: buy/sell/reduce)",
            {"signal_side": signal.signal_side},
        )
        return blocked_response()

    # 4. Signal strength
    # Exception: if this is a buy signal and we have no existing position,
    # bypass the strength threshold. A zero-position ticker should be entered
    # on any valid buy signal -- the threshold is for adding to existing positions.
    strength = signal.signal_strength or 0.0
    is_buy_signal = signal.signal_side == "buy"
    _no_position_bypass = False
    if is_buy_signal and strength < cfg.trading_min_signal_strength:
        # Peek at existing position -- cheap call, worth it to avoid blocking new entries
        try:
            _peek_pos = broker.get_position(signal.ticker) if hasattr(broker, "get_position") else None
            _no_position_bypass = (_peek_pos is None or float(_peek_pos.get("market_value") or 0) == 0)
        except Exception:
            _no_position_bypass = False

    if not _no_position_bypass and strength < cfg.trading_min_signal_strength:
        block(
            "signal_strength " + str(round(strength, 3)) + " < min " + str(cfg.trading_min_signal_strength)
            + " (no zero-position bypass: position already exists or peek failed)",
            {"strength": strength, "min": cfg.trading_min_signal_strength},
        )
        return blocked_response()

    if _no_position_bypass and strength < cfg.trading_min_signal_strength:
        logger.info(
            "signal_strength_threshold_bypassed_no_position",
            ticker=signal.ticker,
            strength=round(strength, 3),
            min_strength=cfg.trading_min_signal_strength,
        )

    # Fetch live broker state
    try:
        account = broker.get_account()
    except Exception as exc:
        block(f"broker.get_account() failed: {exc}")
        return blocked_response(retriable=True)

    # 5. Account must not be trading blocked
    if account.get("trading_blocked"):
        block("Alpaca account is trading_blocked")
        return blocked_response()

    # 6. Buying power / portfolio value
    buying_power = account.get("buying_power") or 0.0
    portfolio_value = account.get("portfolio_value") or account.get("equity") or 0.0

    # 7. Get price for order sizing. Spread is irrelevant for limit orders.
    # Fall back to Gemini if Alpaca has no quote (after-hours, etc).
    symbol = signal.ticker
    mid_price = 0.0
    quote = {}
    try:
        quote = broker.get_quote(symbol)
        mid_price = quote.get("mid") or quote.get("ask") or quote.get("bid") or 0.0
    except Exception as exc:
        logger.warning("alpaca_quote_failed", ticker=symbol, error=str(exc))

    if not mid_price:
        mid_price = _get_price_via_gemini(symbol, getattr(cfg, "gemini_api_key", "")) or 0.0
        if mid_price:
            logger.info("gemini_price_used", ticker=symbol, price=mid_price)
        else:
            # Truly no price available - retry next hourly run
            reasons.append(f"price unavailable for {symbol} via Alpaca and Gemini")
            _log_decision(session, signal, "blocked", f"price unavailable for {symbol} -- will retry", {"quote": quote})
            return blocked_response(retriable=True)

    # 8. Duplicate open order check
    try:
        if broker.has_open_order(symbol):
            block(
                f"Open order already exists for {symbol}",
                {"symbol": symbol},
            )
            return blocked_response(retriable=True)
    except Exception as exc:
        block(f"Could not check open orders: {exc}")
        return blocked_response(retriable=True)

    # Determine effective side
    raw_side = signal.signal_side
    broker_side = "sell" if raw_side in ("sell", "reduce") else "buy"

    # Sell / reduce: need an existing position
    current_position = None
    current_qty = 0.0
    if broker_side == "sell":
        try:
            current_position = broker.get_position(symbol)
        except Exception as exc:
            block(f"Could not fetch position for {symbol}: {exc}")
            return blocked_response(retriable=True)

        if not current_position:
            block(
                f"No existing position for {symbol} -- cannot sell/reduce",
                {"symbol": symbol},
            )
            return blocked_response()
        current_qty = float(current_position.get("qty") or 0.0)

    # 9. Compute order size
    if broker_side == "buy":
        # Position capacity: how much more we can add before hitting the limit
        per_ticker_pct = signal.max_position_pct or cfg.trading_max_position_pct
        global_pct = cfg.trading_max_position_pct
        effective_pct = min(per_ticker_pct, global_pct) / 100.0

        max_position_value = portfolio_value * effective_pct

        # Current position value
        existing_pos = None
        try:
            existing_pos = broker.get_position(symbol)
        except Exception:
            pass
        current_value = (
            float(existing_pos.get("market_value") or 0.0) if existing_pos else 0.0
        )
        allowed_capacity = max(0.0, max_position_value - current_value)

        if allowed_capacity <= 0:
            block(
                f"Position already at or above max ({effective_pct*100:.1f}% of portfolio)",
                {"max_position_value": max_position_value, "current_value": current_value},
            )
            return blocked_response()

        # 10. Check buying power
        order_notional = min(
            cfg.trading_max_order_notional,
            allowed_capacity,
            buying_power,
        )

        if order_notional <= 0:
            block(
                f"Insufficient buying power (available: {buying_power:.2f})",
                {"buying_power": buying_power, "needed": cfg.trading_max_order_notional},
            )
            return blocked_response()

        # Buy: use notional (dollar amount) -- enables fractional shares via Alpaca
        # Limit price = mid price; fills at that price or better.
        limit_price = mid_price
        order_qty = None  # notional-based, no qty needed

    else:
        # Sell: use qty from existing position; limit price = bid
        if raw_side == "reduce":
            order_qty = round(current_qty * 0.5, 9)
        else:
            order_qty = round(current_qty, 9)
        order_notional = order_qty * mid_price
        limit_price = mid_price

    # 11. Notional / qty must be > 0
    if broker_side == "buy" and order_notional <= 0:
        block(
            f"Insufficient buying power (available: {buying_power:.2f})",
            {"buying_power": buying_power, "needed": cfg.trading_max_order_notional},
        )
        return blocked_response()

    if broker_side == "sell" and (order_qty is None or order_qty <= 0):
        block(
            f"Sell qty is zero or negative for {symbol}",
            {"qty": order_qty},
        )
        return blocked_response()

    # All checks passed -- log and return order spec
    _log_decision(
        session, signal, "allowed",
        (
            f"All risk checks passed -- {broker_side} {symbol} "
            f"{'notional=$' + str(round(order_notional, 2)) if broker_side == 'buy' else 'qty=' + str(order_qty)} "
            f"@ limit {limit_price:.4f}"
        ),
        {
            "side": broker_side,
            "notional": order_notional if broker_side == "buy" else None,
            "qty": order_qty if broker_side == "sell" else None,
            "limit_price": limit_price,
        },
    )

    return {
        "allowed": True,
        "retriable": False,
        "reasons": reasons,
        "order": {
            "symbol": symbol,
            "side": broker_side,
            "notional": round(order_notional, 2) if broker_side == "buy" else None,
            "qty": order_qty if broker_side == "sell" else None,
            "limit_price": limit_price,
            "order_type": "limit",
            "time_in_force": "day",
        },
    }
