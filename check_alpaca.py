import sys, os
sys.path.insert(0, r'C:\Users\noleg\Desktop\Claude\Projects\Stocks\src')
os.chdir(r'C:\Users\noleg\Desktop\Claude\Projects\Stocks')
from equity_intel.config import settings
from equity_intel.trading.alpaca_adapter import AlpacaBrokerAdapter
b = AlpacaBrokerAdapter(settings.alpaca_api_key, settings.alpaca_secret_key, settings.alpaca_paper)
try:
    a = b.get_account()
    print("OK status:", a.get('status'), "buying_power:", a.get('buying_power'))
except Exception as e:
    print("FAILED:", e)
