"""
rebalance.py -- Portfolio rebalancing engine.

Computes the delta between current Alpaca positions and a target allocation,
then generates a cash/proceeds-directed order plan.

Design
------
- Execution is ALWAYS gated behind TRADING_EXECUTION_ENABLED=True in .env.
- Preview mode returns the full plan without touching Alpaca.
- Two allocation methods are supported today; more can be added later:

    portfolio_weights  -- donut weights (Trad Hedge pinned to 5%, rest
                         proportional by conviction; equal split within
                         each category among its tracked tickers).

    ai_suggestion      -- last AI-generated suggestion from /api/suggest
                         (requires a cached _suggestion with pct fields).

- All monetary amounts are in USD.
- Fractional shares are supported if the account has fractional trading.

Rebalancing policy (cash/proceeds-directed, loss-protected)
------------------------------------------------------------
Buy-only rebalancing is the default. Sell-side rebalancing only happens when
a position is both overweight AND profitable (current price > avg entry price).
The engine will never sell a loser just because allocations drifted.

Usage (preview only, never executes):
    plan = build_rebalance_plan(
        portfolio_tickers=[...],
        category_weights={...},
        method="portfolio_weights",
    )
"""
from __future__ import annotations

import datetime
import math
from typing import Any, Dict, List, Optional


def _pct_to_decimal(pct: float) -> float:
    """Convert a percentage (e.g. 15.0) to decimal (0.15)."""
    return pct / 100.0


def compute_target_allocations(
    portfolio_tickers: List[Dict[str, Any]],
    category_weights_pct: Dict[str, float],
    account_value: float,
) -> Dict[str, float]:
    """
    Compute target dollar allocation per ticker.

    Each category receives category_weights_pct[cat] of account_value.
    Tickers within a category are weighted equally.

    Returns
    -------
    dict mapping ticker -> target_dollar_value
    """
    cat_counts: Dict[str, int] = {}
    for t in portfolio_tickers:
        cat = t.get("category", "")
        cat_counts[cat] = cat_counts.get(cat, 0) + 1

    targets: Dict[str, float] = {}
    for t in portfolio_tickers:
        ticker = t["ticker"].upper()
        cat = t.get("category", "")
        cat_pct = category_weights_pct.get(cat, 0.0)
        n = cat_counts.get(cat, 1) or 1
        targets[ticker] = account_value * _pct_to_decimal(cat_pct) / n

    return targets


def _get_current_positions(adapter: Any) -> Dict[str, Dict[str, Any]]:
    """Return {SYMBOL: position_dict} from Alpaca."""
    positions = adapter.get_positions()
    return {p["symbol"].upper(): p for p in positions}


def _get_quotes(
    adapter: Any,
    symbols: List[str],
) -> Dict[str, float]:
    """
    Return {SYMBOL: mid_price} for each symbol.
    Falls back to current_price from positions if quote fails.
    """
    prices: Dict[str, float] = {}
    for sym in symbols:
        try:
            q = adapter.get_quote(sym)
            prices[sym] = q.get("mid") or q.get("mid_price") or q.get("ask") or q.get("ask_price") or 0.0
        except Exception:
            prices[sym] = 0.0
    return prices


def build_rebalance_plan(
    portfolio_tickers: List[Dict[str, Any]],
    category_weights_pct: Dict[str, float],
    adapter: Any,
    account_value: Optional[float] = None,
    buy_threshold_pct: float = 5.0,
    sell_threshold_pct: float = 10.0,
    pause_sell_side: bool = False,
    dry_run: bool = True,
    trad_hedge_syms: Optional[set] = None,
) -> Dict[str, Any]:
    """
    Compute and (optionally) execute a cash/proceeds-directed, loss-protected
    rebalance plan.

    Policy
    ------
    Sell  : only when (a) current weight >= target + sell_threshold_pct (default 10pp)
            AND (b) the position is profitable (current price > avg entry price).
            Losers are never sold due to allocation drift.
    Buy   : only when current weight <= target - buy_threshold_pct (default 5pp)
            AND available funds (free cash + sell proceeds) cover the order.
            Buys are allocated greedily by largest drift first until funds run out.
    Pause : if pause_sell_side=True all sell-side orders are suppressed.
    Never sell holding A solely to fund the purchase of underweight holding B.

    Parameters
    ----------
    portfolio_tickers
        List of ticker dicts from /api/portfolio/config.
    category_weights_pct
        Normalized category -> pct dict (must sum to ~100).
    adapter
        AlpacaBrokerAdapter instance.
    account_value
        Override account equity. Fetched from Alpaca if None.
    buy_threshold_pct
        Minimum underweight gap (percentage points) required to queue a buy.
    sell_threshold_pct
        Minimum overweight gap (percentage points) required to trigger a trim.
    pause_sell_side
        If True, suppress all sell-side rebalancing.
    dry_run
        If True, compute orders but do NOT submit them to Alpaca.

    Returns
    -------
    Plan dict with keys:
        generated_at, account_value, cash, available_funds, policy, dry_run,
        orders, skipped, warnings, execution_result, stats
    """
    from equity_intel.logging_config import get_logger
    log = get_logger(__name__)

    warnings: List[str] = []
    trad_hedge_syms = trad_hedge_syms or set()

    # 1. Account equity
    try:
        acct = adapter.get_account()
        equity = float(acct.get("equity") or acct.get("portfolio_value") or 0)
        cash = float(acct.get("cash") or 0)
        if account_value is not None:
            equity = account_value
    except Exception as exc:
        return {
            "error": "Could not fetch Alpaca account: " + str(exc),
            "dry_run": dry_run,
        }

    if equity <= 0:
        return {"error": "Account equity is zero or negative.", "dry_run": dry_run}

    # 2. Current positions
    try:
        positions = _get_current_positions(adapter)
    except Exception as exc:
        return {"error": "Could not fetch positions: " + str(exc), "dry_run": dry_run}

    # 3. Target allocations
    target_values = compute_target_allocations(
        portfolio_tickers, category_weights_pct, equity
    )

    # 4. Quotes for all tracked tickers
    all_syms = sorted(
        set(list(target_values.keys()) + list(positions.keys()))
    )
    try:
        prices = _get_quotes(adapter, all_syms)
    except Exception as exc:
        warnings.append("Quote fetch partial failure: " + str(exc))
        prices = {}

    for sym, pos in positions.items():
        if prices.get(sym, 0) == 0 and pos.get("current_price"):
            prices[sym] = float(pos["current_price"])

    # 5. Build order list
    skipped: List[Dict[str, Any]] = []

    universe_syms = {t["ticker"].upper() for t in portfolio_tickers}
    all_order_syms = universe_syms | set(positions.keys())

    # Pass 1: Exits and overweight trims
    sell_orders: List[Dict[str, Any]] = []
    estimated_sell_proceeds = 0.0

    if pause_sell_side:
        warnings.append(
            "Sell-side rebalancing is PAUSED (pause_sell_side=True). "
            "No overweight trims will be generated."
        )

    for sym in sorted(all_order_syms):
        price = prices.get(sym, 0.0)
        pos = positions.get(sym, {})
        current_value = float(pos.get("market_value") or 0)
        target_value = target_values.get(sym, 0.0)

        if sym not in universe_syms:
            if current_value > 0:
                shares = float(pos.get("qty") or 0)
                sell_orders.append({
                    "ticker": sym,
                    "side": "sell",
                    "target_pct": 0.0,
                    "current_pct": round(current_value / equity * 100, 2),
                    "drift_pct": round(-current_value / equity * 100, 2),
                    "target_value": 0.0,
                    "current_value": round(current_value, 2),
                    "delta_value": round(-current_value, 2),
                    "shares": shares,
                    "price": price,
                    "rationale": "Ticker removed from research universe -- close position",
                })
                estimated_sell_proceeds += current_value
            continue

        current_pct = current_value / equity * 100
        target_pct = target_value / equity * 100
        drift_pct = target_pct - current_pct
        delta_value = target_value - current_value

        if drift_pct <= -sell_threshold_pct:
            avg_entry = float(
                pos.get("avg_entry_price")
                or pos.get("cost_basis_per_share")
                or 0
            )
            unrealized_pl = float(pos.get("unrealized_pl") or 0)

            if avg_entry > 0:
                is_profitable = price > avg_entry
            else:
                is_profitable = unrealized_pl > 0

            if not is_profitable:
                skipped.append({
                    "ticker": sym,
                    "reason": (
                        "Overweight by " + str(round(abs(drift_pct), 1)) + "pp but at a loss "
                        "(avg_entry=$" + str(round(avg_entry, 2)) + ", current=$" + str(round(price, 2)) + ") -- "
                        "loss-protection: hold, do not sell"
                    ),
                })
                continue

            if pause_sell_side:
                skipped.append({
                    "ticker": sym,
                    "reason": (
                        "Overweight and profitable but sell-side rebalancing "
                        "is paused -- market-decline protection"
                    ),
                })
                continue

            if price <= 0:
                skipped.append({"ticker": sym, "reason": "No price available (overweight trim)"})
                warnings.append(sym + ": skipped -- no price data")
                continue

            trim_value = abs(delta_value)
            shares = math.floor((trim_value / price) * 100) / 100

            if shares < 0.01:
                skipped.append({"ticker": sym, "reason": "Overweight trim too small (<0.01 shares)"})
                continue

            ticker_meta = next((t for t in portfolio_tickers if t["ticker"].upper() == sym), {})
            sell_orders.append({
                "ticker": sym,
                "side": "sell",
                "target_pct": round(target_pct, 2),
                "current_pct": round(current_pct, 2),
                "drift_pct": round(drift_pct, 2),
                "target_value": round(target_value, 2),
                "current_value": round(current_value, 2),
                "delta_value": round(delta_value, 2),
                "shares": shares,
                "price": round(price, 4),
                "avg_entry_price": round(avg_entry, 4),
                "unrealized_pl": round(unrealized_pl, 2),
                "category": ticker_meta.get("category", ""),
                "rationale": (
                    "Trim profitable overweight: sell " + str(round(shares, 2)) + " sh @ ~$" + str(round(price, 2)) + " "
                    "(avg_entry=$" + str(round(avg_entry, 2)) + ", unrealized_pl=$" + str(round(unrealized_pl, 2)) + ") "
                    "-> target " + str(round(target_pct, 1)) + "% (currently " + str(round(current_pct, 1)) + "%, "
                    + str(round(abs(drift_pct), 1)) + "pp above target)"
                ),
            })
            estimated_sell_proceeds += shares * price

        elif drift_pct < 0:
            skipped.append({
                "ticker": sym,
                "reason": (
                    "Overweight by " + str(round(abs(drift_pct), 1)) + "pp but below "
                    + str(sell_threshold_pct) + "pp sell threshold -- no action"
                ),
            })

    # Pass 2: Underweight buys
    available_funds = cash + estimated_sell_proceeds
    buy_candidates: List[Dict[str, Any]] = []

    for sym in sorted(universe_syms):
        price = prices.get(sym, 0.0)
        pos = positions.get(sym, {})
        current_value = float(pos.get("market_value") or 0)
        target_value = target_values.get(sym, 0.0)

        current_pct = current_value / equity * 100
        target_pct = target_value / equity * 100
        drift_pct = target_pct - current_pct

        # TradHedge: unconditional buy when no position -- permanent allocation,
        # treat like the bond sleeve of a portfolio, no threshold applies.
        is_trad_hedge = sym in trad_hedge_syms
        no_position = current_value == 0
        qualifies = (
            (is_trad_hedge and no_position)       # TradHedge: bypass threshold entirely
            or drift_pct >= buy_threshold_pct      # All others: normal threshold
        )

        if qualifies:
            if price <= 0:
                skipped.append({"ticker": sym, "reason": "No price available (underweight buy)"})
                warnings.append(sym + ": skipped -- no price data")
                continue

            ticker_meta = next((t for t in portfolio_tickers if t["ticker"].upper() == sym), {})
            rationale_prefix = (
                "TradHedge initial buy (no position -- permanent allocation): "
                if is_trad_hedge and no_position
                else ""
            )
            buy_candidates.append({
                "ticker": sym,
                "side": "buy",
                "target_pct": round(target_pct, 2),
                "current_pct": round(current_pct, 2),
                "drift_pct": round(drift_pct, 2),
                "target_value": round(target_value, 2),
                "current_value": round(current_value, 2),
                "_full_delta": target_value - current_value,
                "_rationale_prefix": rationale_prefix,
                "price": round(price, 4),
                "category": ticker_meta.get("category", ""),
            })

        elif drift_pct > 0:
            skipped.append({
                "ticker": sym,
                "reason": (
                    "Underweight by " + str(round(drift_pct, 1)) + "pp but below "
                    + str(buy_threshold_pct) + "pp buy threshold -- no action"
                ),
            })

    buy_candidates.sort(key=lambda c: -c["drift_pct"])

    buy_orders: List[Dict[str, Any]] = []
    remaining_funds = available_funds

    for candidate in buy_candidates:
        if remaining_funds <= 0:
            skipped.append({
                "ticker": candidate["ticker"],
                "reason": "No available cash or proceeds to fund this buy",
            })
            continue

        buy_value = min(candidate["_full_delta"], remaining_funds)
        shares = math.floor((buy_value / candidate["price"]) * 100) / 100

        if shares < 0.01:
            skipped.append({"ticker": candidate["ticker"], "reason": "Buy too small (<0.01 shares)"})
            continue

        actual_cost = shares * candidate["price"]
        remaining_funds -= actual_cost

        prefix = candidate.pop("_rationale_prefix", "")
        order = {k: v for k, v in candidate.items() if k not in ("_full_delta",)}
        order["shares"] = shares
        order["delta_value"] = round(actual_cost, 2)
        order["rationale"] = (
            prefix
            + "Buy " + str(round(shares, 2)) + " sh @ ~$" + str(round(candidate["price"], 2)) + " "
            "-> target " + str(candidate["target_pct"]) + "% "
            "(currently " + str(candidate["current_pct"]) + "%, "
            + str(candidate["drift_pct"]) + "pp below target)"
        )
        buy_orders.append(order)

    sell_orders_sorted = sorted(sell_orders, key=lambda o: o["drift_pct"])
    orders = sell_orders_sorted + buy_orders

    plan: Dict[str, Any] = {
        "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
        "account_value": round(equity, 2),
        "cash": round(cash, 2),
        "available_funds": round(available_funds, 2),
        "dry_run": dry_run,
        "policy": (
            "Cash/proceeds-directed, loss-protected: "
            "trim only profitable positions >=" + str(sell_threshold_pct) + "pp overweight"
            + (" [SELL-SIDE PAUSED]" if pause_sell_side else "")
            + "; buy when >=" + str(buy_threshold_pct) + "pp underweight using available cash/proceeds only."
        ),
        "orders": orders,
        "skipped": skipped,
        "warnings": warnings,
        "execution_result": None,
        "stats": {
            "total_orders": len(orders),
            "buys": len(buy_orders),
            "sells": len(sell_orders),
            "skipped": len(skipped),
            "estimated_buy_value": round(sum(o["delta_value"] for o in buy_orders), 2),
            "estimated_sell_value": round(sum(abs(o["delta_value"]) for o in sell_orders), 2),
            "estimated_sell_proceeds": round(estimated_sell_proceeds, 2),
            "remaining_funds_after_buys": round(remaining_funds, 2),
        },
    }

    # 6. Execute if not dry_run
    if not dry_run:
        from equity_intel.config import settings
        if not getattr(settings, "trading_execution_enabled", False):
            plan["error"] = "TRADING_EXECUTION_ENABLED is False -- set it to True in .env to execute trades."
            return plan

        log.warning(
            "rebalance_execute_start",
            orders=len(orders),
            equity=equity,
            paper=getattr(adapter, "_paper", "unknown"),
        )
        results = []
        for order in orders:
            sym = order["ticker"]

            try:
                if adapter.has_open_order(sym):
                    results.append({
                        "ticker": sym,
                        "status": "skipped",
                        "reason": "open order already exists",
                    })
                    log.info("rebalance_order_skipped_open_order", ticker=sym)
                    continue
            except Exception as exc:
                log.warning("rebalance_open_order_check_failed", ticker=sym, error=str(exc))

            try:
                quote = adapter.get_quote(sym)
                limit_price = quote.get("ask") if order["side"] == "buy" else quote.get("bid")
                resp = adapter.submit_limit_order(
                    symbol=sym,
                    side=order["side"],
                    limit_price=limit_price,
                    notional=order["delta_value"] if order["side"] == "buy" else None,
                    qty=order["shares"] if order["side"] == "sell" else None,
                )
                results.append({
                    "ticker": sym,
                    "status": "submitted",
                    "broker_order_id": resp.get("broker_order_id"),
                    "side": order["side"],
                    "shares": order["shares"],
                })
                log.info(
                    "rebalance_order_submitted",
                    ticker=sym,
                    side=order["side"],
                    shares=order["shares"],
                    broker_order_id=resp.get("broker_order_id"),
                )
            except Exception as exc:
                results.append({
                    "ticker": sym,
                    "status": "error",
                    "error": str(exc),
                })
                log.error("rebalance_order_error", ticker=sym, error=str(exc))

        plan["execution_result"] = results

    return plan
