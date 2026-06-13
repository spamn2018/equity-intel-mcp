import sys, os
sys.path.insert(0, r'C:\Users\noleg\Desktop\Claude\Projects\Stocks\src')
os.chdir(r'C:\Users\noleg\Desktop\Claude\Projects\Stocks')
from equity_intel.config import settings
from equity_intel.trading.alpaca_adapter import AlpacaBrokerAdapter

b = AlpacaBrokerAdapter(settings.alpaca_api_key, settings.alpaca_secret_key, settings.alpaca_paper)
for ticker in ['BAC', 'CL', 'NET', 'ON']:
    try:
        q = b.get_quote(ticker)
        print('%-6s  %s' % (ticker, q))
    except Exception as e:
        print('%-6s  ERROR: %s' % (ticker, e))
