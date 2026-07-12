#!/usr/bin/env python3
"""
alpaca_trader.py — Standalone Alpaca paper/live trading script.

Connects to Alpaca's API for stock trading. Reads credentials from .env.
Supports paper trading (default) and live trading.

Usage:
    python alpaca_trader.py --status          # Account info + buying power
    python alpaca_trader.py --positions       # Show open positions
    python alpaca_trader.py --buy AAPL 100    # Buy $100 of AAPL (limit @ mid)
    python alpaca_trader.py --sell AAPL 1     # Sell 1 share of AAPL (limit @ mid)
    python alpaca_trader.py --quote NVDA      # Get latest quote
    python alpaca_trader.py --watchlist       # Show prices for ai_tickers.json
    python alpaca_trader.py --history NVDA 5  # Last 5 days of NVDA bars
    python alpaca_trader.py --orders          # Show recent orders
    python alpaca_trader.py --test            # Run full connectivity test

Credentials: Set ALPACA_API_KEY and ALPACA_SECRET_KEY in .env
Paper mode:  Set ALPACA_PAPER=true in .env (default)

Venv: C:\\Users\\noleg\\Desktop\\Claude\\Projects\\Stocks\\alpaca_venv
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── Load .env ─────────────────────────────────────────────────────────────────
ENV_FILE = Path(__file__).parent / ".env"

def _load_env():
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        if k not in os.environ:
            os.environ[k] = v.strip().strip('"').strip("'")

_load_env()

API_KEY    = os.environ.get("ALPACA_API_KEY", "")
SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY", "")
PAPER      = os.environ.get("ALPACA_PAPER", "true").lower() in ("1", "true", "yes")

if not API_KEY or not SECRET_KEY:
    print("ERROR: Set ALPACA_API_KEY and ALPACA_SECRET_KEY in .env")
    print(f"  .env location: {ENV_FILE}")
    print("  Get keys from: https://app.alpaca.markets/paper/dashboard/overview")
    print("  (Paper trading keys are free, no deposit needed)")
    sys.exit(1)

# ── Alpaca imports ────────────────────────────────────────────────────────────
try:
    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import LimitOrderRequest
    from alpaca.trading.enums import OrderSide, TimeInForce, OrderStatus
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockLatestQuoteRequest, StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
except ImportError:
    print("ERROR: alpaca-py not installed.")
    print("  Run: alpaca_venv\\Scripts\\pip.exe install alpaca-py")
    sys.exit(1)

# ── Clients ───────────────────────────────────────────────────────────────────
trading_client = TradingClient(API_KEY, SECRET_KEY, paper=PAPER)
data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)

MODE = "PAPER" if PAPER else "LIVE"


# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_status():
    """Show account status and buying power."""
    acct = trading_client.get_account()
    print(f"=== Alpaca Account ({MODE}) ===")
    print(f"  Account #:     {acct.account_number}")
    print(f"  Status:        {acct.status}")
    print(f"  Cash:          ${float(acct.cash):,.2f}")
    print(f"  Buying Power:  ${float(acct.buying_power):,.2f}")
    print(f"  Portfolio Val:  ${float(acct.portfolio_value):,.2f}")
    print(f"  Equity:        ${float(acct.equity):,.2f}")
    print(f"  Day Trading:   {'Yes' if acct.pattern_day_trader else 'No'}")
    print(f"  Trading Blocked: {acct.trading_blocked}")
    return True


def cmd_positions():
    """Show all open positions."""
    positions = trading_client.get_all_positions()
    if not positions:
        print("No open positions.")
        return True
    print(f"=== Open Positions ({MODE}) ===")
    print(f"{'Symbol':<8} {'Qty':<8} {'Entry':>10} {'Current':>10} {'P&L':>12} {'P&L%':>8}")
    print("-" * 60)
    for p in positions:
        sym = p.symbol
        qty = float(p.qty)
        entry = float(p.avg_entry_price)
        current = float(p.current_price)
        pnl = float(p.unrealized_pl)
        pnl_pct = float(p.unrealized_plpc) * 100
        print(f"{sym:<8} {qty:<8.2f} ${entry:>9.2f} ${current:>9.2f} ${pnl:>10.2f} {pnl_pct:>7.1f}%")
    return True


def cmd_orders():
    """Show recent orders."""
    orders = trading_client.get_orders()
    if not orders:
        print("No recent orders.")
        return True
    print(f"=== Recent Orders ({MODE}) ===")
    for o in orders[:10]:
        filled = f"filled@${float(o.filled_avg_price):.2f}" if o.filled_avg_price else o.status.value
        print(f"  {o.submitted_at.strftime('%m/%d %H:%M')}  {o.side.value:<5} {o.symbol:<8} x{o.qty}  {filled}")
    return True


def cmd_quote(symbol: str):
    """Get latest quote for a symbol."""
    req = StockLatestQuoteRequest(symbol_or_symbols=symbol)
    quotes = data_client.get_stock_latest_quote(req)
    if symbol not in quotes:
        print(f"No quote found for {symbol}")
        return False
    q = quotes[symbol]
    print(f"=== {symbol} Latest Quote ===")
    print(f"  Bid: ${q.bid_price:.2f} x{q.bid_size}")
    print(f"  Ask: ${q.ask_price:.2f} x{q.ask_size}")
    mid = (q.bid_price + q.ask_price) / 2
    spread = q.ask_price - q.bid_price
    print(f"  Mid: ${mid:.2f}  Spread: ${spread:.2f}")
    return True


def cmd_history(symbol: str, days: int):
    """Show recent daily bars."""
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=days + 5)  # extra days for weekends
    req = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame.Day,
        start=start,
        limit=days,
    )
    bars = data_client.get_stock_bars(req)
    if symbol not in bars or not bars[symbol]:
        print(f"No bars for {symbol}")
        return False
    print(f"=== {symbol} Last {days} Days ===")
    print(f"{'Date':<12} {'Open':>10} {'High':>10} {'Low':>10} {'Close':>10} {'Volume':>12}")
    print("-" * 68)
    for b in bars[symbol]:
        print(f"{b.timestamp.strftime('%Y-%m-%d'):<12} ${b.open:>9.2f} ${b.high:>9.2f} ${b.low:>9.2f} ${b.close:>9.2f} {b.volume:>11,}")
    return True


def _kill_switch_engaged() -> bool:
    """Project rule: no order submission unless TRADING_EXECUTION_ENABLED=true."""
    enabled = os.environ.get("TRADING_EXECUTION_ENABLED", "false").lower() in ("1", "true", "yes")
    if not enabled:
        print("BLOCKED: TRADING_EXECUTION_ENABLED is not true in .env -- no order submitted.")
    return not enabled


def _latest_mid(symbol: str) -> float:
    """Mid price from the latest quote, for limit pricing."""
    req = StockLatestQuoteRequest(symbol_or_symbols=symbol)
    quotes = data_client.get_stock_latest_quote(req)
    q = quotes[symbol]
    bid = float(q.bid_price or 0)
    ask = float(q.ask_price or 0)
    mid = (bid + ask) / 2 if (bid and ask) else (ask or bid)
    if not mid:
        raise ValueError(f"No usable quote for {symbol}")
    return mid


def cmd_buy(symbol: str, notional: float):
    """Limit DAY buy for a dollar amount (project rules: limit only; buys use notional)."""
    if _kill_switch_engaged():
        return False
    limit_price = round(_latest_mid(symbol), 2)
    print(f"Placing BUY order: {symbol} ${notional:.2f} limit@{limit_price} ({MODE})")
    req = LimitOrderRequest(
        symbol=symbol,
        notional=round(notional, 2),
        side=OrderSide.BUY,
        time_in_force=TimeInForce.DAY,
        limit_price=limit_price,
    )
    order = trading_client.submit_order(req)
    print(f"  Order ID: {order.id}")
    print(f"  Status:   {order.status.value}")
    print(f"  Type:     {order.order_type.value}")
    return True


def cmd_sell(symbol: str, qty: float):
    """Limit DAY sell for a share qty (project rules: limit only; sells use qty)."""
    if _kill_switch_engaged():
        return False
    limit_price = round(_latest_mid(symbol), 2)
    print(f"Placing SELL order: {symbol} x{qty} limit@{limit_price} ({MODE})")
    req = LimitOrderRequest(
        symbol=symbol,
        qty=qty,
        side=OrderSide.SELL,
        time_in_force=TimeInForce.DAY,
        limit_price=limit_price,
    )
    order = trading_client.submit_order(req)
    print(f"  Order ID: {order.id}")
    print(f"  Status:   {order.status.value}")
    return True


def cmd_watchlist():
    """Show current prices for tickers in ai_tickers.json."""
    config_path = Path(__file__).parent / "config" / "ai_tickers.json"
    if not config_path.exists():
        print(f"Ticker config not found: {config_path}")
        return False
    config = json.loads(config_path.read_text(encoding="utf-8"))
    symbols = []
    for section in config.values():
        if isinstance(section, dict) and "tickers" in section:
            for t in section["tickers"]:
                symbols.append(t["ticker"])
    if not symbols:
        print("No tickers found in config.")
        return False

    # Batch quote
    req = StockLatestQuoteRequest(symbol_or_symbols=symbols)
    quotes = data_client.get_stock_latest_quote(req)

    print(f"=== AI Portfolio Watchlist ({len(symbols)} tickers) ===")
    print(f"{'Symbol':<8} {'Bid':>10} {'Ask':>10} {'Mid':>10} {'Spread':>8}")
    print("-" * 50)
    for sym in symbols:
        if sym in quotes:
            q = quotes[sym]
            mid = (q.bid_price + q.ask_price) / 2
            spread = q.ask_price - q.bid_price
            print(f"{sym:<8} ${q.bid_price:>9.2f} ${q.ask_price:>9.2f} ${mid:>9.2f} ${spread:>7.2f}")
        else:
            print(f"{sym:<8} --no quote--")
    return True


def cmd_test():
    """Full connectivity and functionality test."""
    print(f"=== Alpaca Connectivity Test ({MODE}) ===\n")
    errors = 0

    # 1. Account
    print("1. Account access...", end=" ")
    try:
        acct = trading_client.get_account()
        print(f"OK — ${float(acct.buying_power):,.2f} buying power")
    except Exception as e:
        print(f"FAIL: {e}")
        errors += 1

    # 2. Positions
    print("2. Positions read...", end=" ")
    try:
        pos = trading_client.get_all_positions()
        print(f"OK — {len(pos)} position(s)")
    except Exception as e:
        print(f"FAIL: {e}")
        errors += 1

    # 3. Orders
    print("3. Orders read...", end=" ")
    try:
        orders = trading_client.get_orders()
        print(f"OK — {len(orders)} recent order(s)")
    except Exception as e:
        print(f"FAIL: {e}")
        errors += 1

    # 4. Quote
    print("4. Market data (NVDA quote)...", end=" ")
    try:
        req = StockLatestQuoteRequest(symbol_or_symbols="NVDA")
        quotes = data_client.get_stock_latest_quote(req)
        if "NVDA" in quotes:
            q = quotes["NVDA"]
            print(f"OK — bid=${q.bid_price:.2f} ask=${q.ask_price:.2f}")
        else:
            print("FAIL — no quote returned")
            errors += 1
    except Exception as e:
        print(f"FAIL: {e}")
        errors += 1

    # 5. Historical bars
    print("5. Historical bars (AAPL 3d)...", end=" ")
    try:
        req = StockBarsRequest(
            symbol_or_symbols="AAPL",
            timeframe=TimeFrame.Day,
            start=datetime.now(timezone.utc) - timedelta(days=10),
            limit=3,
        )
        bars = data_client.get_stock_bars(req)
        if "AAPL" in bars and bars["AAPL"]:
            print(f"OK — {len(bars['AAPL'])} bar(s)")
        else:
            print("FAIL — no bars returned")
            errors += 1
    except Exception as e:
        print(f"FAIL: {e}")
        errors += 1

    # 6. Ticker config
    print("6. Ticker config...", end=" ")
    config_path = Path(__file__).parent / "config" / "ai_tickers.json"
    if config_path.exists():
        config = json.loads(config_path.read_text(encoding="utf-8"))
        count = sum(len(s.get("tickers", [])) for s in config.values() if isinstance(s, dict))
        print(f"OK — {count} tickers across {len([k for k in config if isinstance(config[k], dict) and 'tickers' in config[k]])} categories")
    else:
        print("FAIL — ai_tickers.json not found")
        errors += 1

    print(f"\n{'='*40}")
    if errors == 0:
        print("ALL TESTS PASSED. Alpaca is ready.")
    else:
        print(f"{errors} test(s) FAILED.")
    return errors == 0


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Alpaca stock trading — standalone script",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--status", action="store_true", help="Account info")
    parser.add_argument("--positions", action="store_true", help="Open positions")
    parser.add_argument("--orders", action="store_true", help="Recent orders")
    parser.add_argument("--quote", metavar="SYM", help="Latest quote for symbol")
    parser.add_argument("--history", nargs=2, metavar=("SYM", "DAYS"), help="Daily bars")
    parser.add_argument("--buy", nargs=2, metavar=("SYM", "USD"), help="Limit buy for $USD (notional)")
    parser.add_argument("--sell", nargs=2, metavar=("SYM", "QTY"), help="Limit sell (share qty)")
    parser.add_argument("--watchlist", action="store_true", help="AI ticker watchlist")
    parser.add_argument("--test", action="store_true", help="Full connectivity test")
    args = parser.parse_args()

    if args.test:
        cmd_test()
    elif args.status:
        cmd_status()
    elif args.positions:
        cmd_positions()
    elif args.orders:
        cmd_orders()
    elif args.quote:
        cmd_quote(args.quote.upper())
    elif args.history:
        cmd_history(args.history[0].upper(), int(args.history[1]))
    elif args.buy:
        cmd_buy(args.buy[0].upper(), float(args.buy[1]))
    elif args.sell:
        cmd_sell(args.sell[0].upper(), float(args.sell[1]))
    elif args.watchlist:
        cmd_watchlist()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
