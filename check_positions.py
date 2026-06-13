import sys, os
sys.path.insert(0, r'C:\Users\noleg\Desktop\Claude\Projects\Stocks\src')
os.chdir(r'C:\Users\noleg\Desktop\Claude\Projects\Stocks')
from equity_intel.config import settings
from equity_intel.trading.alpaca_adapter import AlpacaBrokerAdapter
b = AlpacaBrokerAdapter(settings.alpaca_api_key, settings.alpaca_secret_key, settings.alpaca_paper)
positions = b.get_positions()
trad_hedge = set(settings.trad_hedge_list)
print('POSITIONS:')
for p in sorted(positions, key=lambda x: x['symbol']):
    th = ' [TH]' if p['symbol'] in trad_hedge else ''
    print('  %-6s  qty=%-10s  val=$%10.2f%s' % (p['symbol'], p.get('qty'), float(p.get('market_value') or 0), th))
held = {p['symbol'] for p in positions}
missing = trad_hedge - held
print('TRAD HEDGE MISSING:', sorted(missing))
try:
    orders = b.get_orders(status='all', limit=50)
    print('RECENT ORDERS:')
    for o in sorted(orders, key=lambda x: str(x.get('submitted_at','')), reverse=True)[:20]:
        print('  %-19s  %-6s  %-4s  %-12s  notional=%s' % (
            str(o.get('submitted_at',''))[:19], o.get('symbol',''), o.get('side',''),
            o.get('status',''), o.get('notional')))
except Exception as e:
    print('orders error:', e)
