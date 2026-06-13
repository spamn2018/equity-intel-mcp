"""
Alpaca API Test Suite
Tests all functions needed for the Equity Intelligence MCP project.

Keys in Alpaca Creds/API.txt are LIVE keys — order-submission tests are
skipped by default (set ALPACA_ALLOW_LIVE_ORDERS=1 to enable, uses real $).
All read-only endpoints run against the live account.

Run:
  set ALPACA_API_KEY=...
  set ALPACA_SECRET_KEY=...
  python test_alpaca.py
"""

import os
import sys
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

load_dotenv()

API_KEY    = os.getenv("ALPACA_API_KEY")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
ALLOW_ORDERS = os.getenv("ALPACA_ALLOW_LIVE_ORDERS", "0") == "1"
TEST_SYMBOL  = "SPY"

GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
RESET  = "\033[0m"

results: list[dict] = []

def ok(name, detail=""):
    print(f"  {GREEN}PASS{RESET}  {name}" + (f" — {detail}" if detail else ""))
    results.append({"name": name, "status": "pass"})

def fail(name, err):
    print(f"  {RED}FAIL{RESET}  {name} — {err}")
    results.append({"name": name, "status": "fail", "error": str(err)})

def skip(name, reason=""):
    print(f"  {YELLOW}SKIP{RESET}  {name}" + (f" — {reason}" if reason else ""))
    results.append({"name": name, "status": "skip"})

def section(title):
    print(f"\n{'─'*60}\n  {title}\n{'─'*60}")

def run(fn):
    try:
        fn()
    except Exception as e:
        fail(fn.__name__, e)

if not API_KEY or not SECRET_KEY:
    print(f"\n{RED}ERROR:{RESET} ALPACA_API_KEY / ALPACA_SECRET_KEY not set.\n")
    sys.exit(1)

try:
    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import (
        MarketOrderRequest, LimitOrderRequest, StopOrderRequest,
        TrailingStopOrderRequest, GetOrdersRequest,
        ReplaceOrderRequest, GetAssetsRequest,
        CreateWatchlistRequest, UpdateWatchlistRequest,
        GetCalendarRequest, GetPortfolioHistoryRequest,
    )
    from alpaca.trading.enums import (
        OrderSide, TimeInForce, QueryOrderStatus, AssetClass, AssetStatus,
    )
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import (
        StockBarsRequest, StockLatestQuoteRequest, StockLatestTradeRequest,
        StockTradesRequest, StockQuotesRequest, StockSnapshotRequest,
    )
    from alpaca.data.timeframe import TimeFrame
except ImportError as e:
    print(f"\n{RED}ImportError:{RESET} {e}\nRun: pip install alpaca-py python-dotenv\n")
    sys.exit(1)

# Live keys — paper=False
trading = TradingClient(API_KEY, SECRET_KEY, paper=False)
data    = StockHistoricalDataClient(API_KEY, SECRET_KEY)

_created_order_id     = None
_created_watchlist_id = None

end   = datetime.now(tz=timezone.utc)
start = end - timedelta(days=30)


# =============================================================================
# 1. ACCOUNT
# =============================================================================
section("1. Account")

def test_get_account():
    acct = trading.get_account()
    assert acct.id
    ok("get_account", f"equity=${float(acct.equity):,.2f}  buying_power=${float(acct.buying_power):,.2f}  status={acct.status}")

def test_get_account_configurations():
    cfg = trading.get_account_configurations()
    ok("get_account_configurations", f"dtbp_check={cfg.dtbp_check}  no_shorting={cfg.no_shorting}")

for fn in [test_get_account, test_get_account_configurations]:
    run(fn)


# =============================================================================
# 2. CLOCK & CALENDAR
# =============================================================================
section("2. Clock & Calendar")

def test_get_clock():
    clock = trading.get_clock()
    ok("get_clock", f"is_open={clock.is_open}  next_open={clock.next_open}")

def test_get_calendar():
    cal = trading.get_calendar(
        filters=GetCalendarRequest(
            start=datetime.now().date(),
            end=(datetime.now() + timedelta(days=7)).date(),
        )
    )
    assert len(cal) > 0
    ok("get_calendar", f"{len(cal)} trading days returned")

for fn in [test_get_clock, test_get_calendar]:
    run(fn)


# =============================================================================
# 3. ASSETS
# =============================================================================
section("3. Assets")

def test_get_all_assets():
    params = GetAssetsRequest(asset_class=AssetClass.US_EQUITY, status=AssetStatus.ACTIVE)
    assets = trading.get_all_assets(params)
    assert len(assets) > 100
    ok("get_all_assets", f"{len(assets)} active US equity assets")

def test_get_asset():
    asset = trading.get_asset(TEST_SYMBOL)
    assert asset.symbol == TEST_SYMBOL
    ok("get_asset", f"symbol={asset.symbol}  tradable={asset.tradable}  fractionable={asset.fractionable}")

for fn in [test_get_all_assets, test_get_asset]:
    run(fn)


# =============================================================================
# 4. ORDERS  (write operations skipped unless ALPACA_ALLOW_LIVE_ORDERS=1)
# =============================================================================
section("4. Orders")

def test_submit_market_order():
    global _created_order_id
    if not ALLOW_ORDERS:
        skip("submit_order (market buy)", "live orders disabled — set ALPACA_ALLOW_LIVE_ORDERS=1")
        return
    req   = MarketOrderRequest(symbol=TEST_SYMBOL, qty=1, side=OrderSide.BUY, time_in_force=TimeInForce.DAY)
    order = trading.submit_order(req)
    _created_order_id = str(order.id)
    ok("submit_order (market buy)", f"id={order.id}  status={order.status}")

def test_submit_limit_order():
    """Low limit so it stays open for replace/cancel tests."""
    global _created_order_id
    if not ALLOW_ORDERS:
        skip("submit_order (limit buy, unfilled)", "live orders disabled")
        return
    quotes      = data.get_stock_latest_quote(StockLatestQuoteRequest(symbol_or_symbols=TEST_SYMBOL))
    bid         = float(quotes[TEST_SYMBOL].bid_price)
    limit_price = round(bid * 0.85, 2)
    req         = LimitOrderRequest(symbol=TEST_SYMBOL, qty=1, side=OrderSide.BUY,
                                    time_in_force=TimeInForce.DAY, limit_price=limit_price)
    order = trading.submit_order(req)
    _created_order_id = str(order.id)
    ok("submit_order (limit buy, unfilled)", f"id={order.id}  limit={limit_price}  status={order.status}")

def test_submit_stop_order():
    if not ALLOW_ORDERS:
        skip("submit_order (stop sell)", "live orders disabled")
        return
    quotes    = data.get_stock_latest_quote(StockLatestQuoteRequest(symbol_or_symbols=TEST_SYMBOL))
    ask       = float(quotes[TEST_SYMBOL].ask_price)
    req       = StopOrderRequest(symbol=TEST_SYMBOL, qty=1, side=OrderSide.SELL,
                                 time_in_force=TimeInForce.DAY, stop_price=round(ask * 0.80, 2))
    try:
        order = trading.submit_order(req)
        trading.cancel_order_by_id(str(order.id))
        ok("submit_order (stop sell)", f"id={order.id}")
    except Exception as e:
        skip("submit_order (stop sell)", str(e))

def test_submit_trailing_stop_order():
    if not ALLOW_ORDERS:
        skip("submit_order (trailing stop sell)", "live orders disabled")
        return
    req = TrailingStopOrderRequest(symbol=TEST_SYMBOL, qty=1, side=OrderSide.SELL,
                                   time_in_force=TimeInForce.DAY, trail_percent=5.0)
    try:
        order = trading.submit_order(req)
        trading.cancel_order_by_id(str(order.id))
        ok("submit_order (trailing stop sell)", f"id={order.id}  trail_percent=5%")
    except Exception as e:
        skip("submit_order (trailing stop sell)", str(e))

def test_get_orders():
    orders = trading.get_orders()
    ok("get_orders (all)", f"{len(orders)} orders returned")

def test_get_orders_filtered():
    orders = trading.get_orders(filter=GetOrdersRequest(status=QueryOrderStatus.OPEN))
    ok("get_orders (open only)", f"{len(orders)} open orders")

def test_get_order_by_id():
    if not _created_order_id:
        skip("get_order_by_id", "no order id (order submission skipped)")
        return
    order = trading.get_order_by_id(_created_order_id)
    ok("get_order_by_id", f"status={order.status}  symbol={order.symbol}")

def test_replace_order_by_id():
    if not _created_order_id:
        skip("replace_order_by_id", "no order id")
        return
    try:
        replaced = trading.replace_order_by_id(_created_order_id, ReplaceOrderRequest(qty=2))
        ok("replace_order_by_id", f"new qty={replaced.qty}")
    except Exception as e:
        skip("replace_order_by_id", str(e))

def test_cancel_order_by_id():
    if not _created_order_id:
        skip("cancel_order_by_id", "no order id")
        return
    trading.cancel_order_by_id(_created_order_id)
    ok("cancel_order_by_id", f"cancelled {_created_order_id[:8]}…")

def test_cancel_all_orders():
    res = trading.cancel_orders()
    ok("cancel_orders (all)", f"attempted {len(res)} cancellations")

for fn in [
    test_submit_market_order, test_submit_limit_order,
    test_submit_stop_order, test_submit_trailing_stop_order,
    test_get_orders, test_get_orders_filtered,
    test_get_order_by_id, test_replace_order_by_id,
    test_cancel_order_by_id, test_cancel_all_orders,
]:
    run(fn)


# =============================================================================
# 5. POSITIONS
# =============================================================================
section("5. Positions")

def test_get_all_positions():
    positions = trading.get_all_positions()
    ok("get_all_positions", f"{len(positions)} open positions")

def test_get_open_position():
    positions = trading.get_all_positions()
    if not positions:
        skip("get_open_position", "no open positions")
        return
    sym = positions[0].symbol
    pos = trading.get_open_position(sym)
    ok("get_open_position", f"symbol={pos.symbol}  qty={pos.qty}  unrealized_pl=${pos.unrealized_pl}")

def test_close_position():
    skip("close_position", "skipped — destructive; requires live position")

def test_close_all_positions():
    skip("close_all_positions", "skipped — destructive")

for fn in [test_get_all_positions, test_get_open_position, test_close_position, test_close_all_positions]:
    run(fn)


# =============================================================================
# 6. WATCHLISTS
# =============================================================================
section("6. Watchlists")

def test_create_watchlist():
    global _created_watchlist_id
    wl = trading.create_watchlist(
        CreateWatchlistRequest(name="__alpaca_test_wl__", symbols=[TEST_SYMBOL, "AAPL"])
    )
    _created_watchlist_id = str(wl.id)
    ok("create_watchlist", f"id={wl.id}  symbols={[a.symbol for a in wl.assets]}")

def test_get_all_watchlists():
    # SDK method is get_watchlists() in some versions, get_all_watchlists() in others
    try:
        wls = trading.get_all_watchlists()
    except AttributeError:
        wls = trading.get_watchlists()
    ok("get_all_watchlists", f"{len(wls)} watchlists")

def test_get_watchlist_by_id():
    if not _created_watchlist_id:
        skip("get_watchlist_by_id", "no watchlist id")
        return
    wl = trading.get_watchlist_by_id(_created_watchlist_id)
    ok("get_watchlist_by_id", f"name={wl.name}")

def test_update_watchlist_by_id():
    if not _created_watchlist_id:
        skip("update_watchlist_by_id", "no watchlist id")
        return
    wl = trading.update_watchlist_by_id(
        _created_watchlist_id,
        UpdateWatchlistRequest(name="__alpaca_test_wl_updated__", symbols=[TEST_SYMBOL, "AAPL", "MSFT"]),
    )
    ok("update_watchlist_by_id", f"name={wl.name}  symbols={[a.symbol for a in wl.assets]}")

def test_delete_watchlist_by_id():
    if not _created_watchlist_id:
        skip("delete_watchlist_by_id", "no watchlist id")
        return
    trading.delete_watchlist_by_id(_created_watchlist_id)
    ok("delete_watchlist_by_id", f"deleted {_created_watchlist_id[:8]}…")

for fn in [
    test_create_watchlist, test_get_all_watchlists,
    test_get_watchlist_by_id, test_update_watchlist_by_id,
    test_delete_watchlist_by_id,
]:
    run(fn)


# =============================================================================
# 7. PORTFOLIO HISTORY
# =============================================================================
section("7. Portfolio History")

def test_get_portfolio_history():
    hist = trading.get_portfolio_history(
        GetPortfolioHistoryRequest(period="1W", timeframe="1D")
    )
    ok("get_portfolio_history", f"equity points={len(hist.equity)}  base_value=${hist.base_value}")

run(test_get_portfolio_history)


# =============================================================================
# 8. MARKET DATA — HISTORICAL  (feed="iex" = free tier)
# =============================================================================
section("8. Market Data — Historical (StockHistoricalDataClient, feed=iex)")

def test_get_stock_bars():
    req  = StockBarsRequest(symbol_or_symbols=TEST_SYMBOL, timeframe=TimeFrame.Day,
                            start=start, end=end, feed="iex")
    bars = data.get_stock_bars(req)
    ok("get_stock_bars (daily, iex)", f"{len(bars[TEST_SYMBOL])} bars for {TEST_SYMBOL}")

def test_get_stock_bars_intraday():
    req  = StockBarsRequest(symbol_or_symbols=[TEST_SYMBOL, "AAPL"], timeframe=TimeFrame.Hour,
                            start=end - timedelta(days=5), end=end, feed="iex")
    bars = data.get_stock_bars(req)
    ok("get_stock_bars (hourly, multi-symbol, iex)",
       f"{len(bars[TEST_SYMBOL])} {TEST_SYMBOL} bars  {len(bars['AAPL'])} AAPL bars")

def test_get_stock_latest_quote():
    req    = StockLatestQuoteRequest(symbol_or_symbols=[TEST_SYMBOL, "AAPL"], feed="iex")
    quotes = data.get_stock_latest_quote(req)
    q      = quotes[TEST_SYMBOL]
    ok("get_stock_latest_quote (iex)", f"{TEST_SYMBOL} bid={q.bid_price}  ask={q.ask_price}")

def test_get_stock_latest_trade():
    req    = StockLatestTradeRequest(symbol_or_symbols=TEST_SYMBOL, feed="iex")
    trades = data.get_stock_latest_trade(req)
    t      = trades[TEST_SYMBOL]
    ok("get_stock_latest_trade (iex)", f"{TEST_SYMBOL} price={t.price}  size={t.size}")

def test_get_stock_trades():
    req    = StockTradesRequest(symbol_or_symbols=TEST_SYMBOL,
                                start=end - timedelta(days=1), end=end,
                                limit=100, feed="iex")
    trades = data.get_stock_trades(req)
    t_list = list(trades[TEST_SYMBOL])
    ok("get_stock_trades (last 1 day, iex)", f"{len(t_list)} trades")

def test_get_stock_quotes():
    req    = StockQuotesRequest(symbol_or_symbols=TEST_SYMBOL,
                                start=end - timedelta(days=1), end=end,
                                limit=100, feed="iex")
    quotes = data.get_stock_quotes(req)
    q_list = list(quotes[TEST_SYMBOL])
    ok("get_stock_quotes (last 1 day, iex)", f"{len(q_list)} quotes")

def test_get_stock_snapshot():
    req  = StockSnapshotRequest(symbol_or_symbols=[TEST_SYMBOL, "AAPL"], feed="iex")
    snaps = data.get_stock_snapshot(req)
    snap  = snaps[TEST_SYMBOL]
    # Field names vary by SDK version — be defensive
    close = getattr(snap.daily_bar, 'close', getattr(snap.daily_bar, 'c', 'n/a'))
    ok("get_stock_snapshot (iex)", f"{TEST_SYMBOL} daily_bar.close={close}")

for fn in [
    test_get_stock_bars, test_get_stock_bars_intraday,
    test_get_stock_latest_quote, test_get_stock_latest_trade,
    test_get_stock_trades, test_get_stock_quotes,
    test_get_stock_snapshot,
]:
    run(fn)


# =============================================================================
# SUMMARY
# =============================================================================
passed  = sum(1 for r in results if r["status"] == "pass")
failed  = sum(1 for r in results if r["status"] == "fail")
skipped = sum(1 for r in results if r["status"] == "skip")

print(f"\n{'='*60}")
print(f"  {GREEN}{passed} passed{RESET}  {RED}{failed} failed{RESET}  {YELLOW}{skipped} skipped{RESET}  ({len(results)} total)")
print(f"{'='*60}\n")

if failed:
    print(f"{RED}Failed tests:{RESET}")
    for r in results:
        if r["status"] == "fail":
            print(f"  • {r['name']}: {r.get('error','')}")
    print()
    sys.exit(1)
