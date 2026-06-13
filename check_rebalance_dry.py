import sys, os
sys.path.insert(0, r'C:\Users\noleg\Desktop\Claude\Projects\Stocks\src')
os.chdir(r'C:\Users\noleg\Desktop\Claude\Projects\Stocks')
from equity_intel.config import settings
from equity_intel.trading.alpaca_adapter import AlpacaBrokerAdapter
from equity_intel.workers.auto_rebalance import _load_portfolio_tickers, _normalize_cat_weights
from equity_intel.trading.rebalance import build_rebalance_plan

print('TRADING_EXECUTION_ENABLED:', settings.trading_execution_enabled)
print('ALPACA_PAPER:', settings.alpaca_paper)

b = AlpacaBrokerAdapter(settings.alpaca_api_key, settings.alpaca_secret_key, settings.alpaca_paper)
acct = b.get_account()
print('buying_power: $%s' % acct.get('buying_power'))
print('equity: $%s' % acct.get('equity'))

tickers = _load_portfolio_tickers(settings)
cat_weights = _normalize_cat_weights(tickers)
trad_hedge_syms = set(settings.trad_hedge_list)

plan = build_rebalance_plan(
    portfolio_tickers=tickers,
    category_weights_pct=cat_weights,
    adapter=b,
    buy_threshold_pct=5.0,
    sell_threshold_pct=10.0,
    pause_sell_side=False,
    dry_run=True,
    trad_hedge_syms=trad_hedge_syms,
)

print()
if 'error' in plan:
    print('ERROR:', plan['error'])
else:
    print('ORDERS (%d):' % len(plan.get('orders', [])))
    for o in plan.get('orders', []):
        print('  %-6s  %-4s  shares=%-8s  drift=%+.1fpp  %s' % (
            o['ticker'], o['side'], o.get('shares',''), o.get('drift_pct',0), o.get('rationale','')[:80]))
    print()
    print('SKIPPED TH:')
    th = set(settings.trad_hedge_list)
    for s in plan.get('skipped', []):
        if s['ticker'] in th:
            print('  %-6s  %s' % (s['ticker'], s['reason']))
    print()
    s = plan['stats']
    print('available_funds: $%.2f  buys: %d  sells: %d' % (plan['available_funds'], s['buys'], s['sells']))
