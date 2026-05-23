"""
Standalone price fetch diagnostic.
Run from the Stocks folder:  python test_prices.py

Tests yfinance against the full 14-ticker watchlist and prints exactly
what succeeds, what fails, and why.  Also verifies the /api/prices
endpoint if the Flask server is running.
"""
import datetime
import sys

TICKERS = [
    "NVDA", "AMD", "AVGO",          # Chips
    "MSFT", "GOOGL", "AMZN",        # Hyperscalers
    "TSLA", "ISRG", "SYM",          # Robotics
    "META", "PLTR", "AI",            # Software
    "BOTZ", "ROBO",                  # ETFs
]

# ── 1. yfinance import check ───────────────────────────────────────────────
print("=" * 60)
print("1. yfinance import")
print("=" * 60)
try:
    import yfinance as yf
    print(f"   OK — yfinance {yf.__version__}")
except ImportError as e:
    print(f"   FAIL — {e}")
    print("   Run: pip install yfinance")
    sys.exit(1)

# ── 2. New fast_info approach via YahooPriceProvider ─────────────────────
print()
print("=" * 60)
print("2. YahooPriceProvider.fetch_quotes() — new fast_info approach")
print("=" * 60)
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))
try:
    from equity_intel.prices.yahoo import YahooPriceProvider
    provider = YahooPriceProvider()
    quotes = provider.fetch_quotes(TICKERS)
    ok, bad = [], []
    for t in TICKERS:
        q = quotes.get(t, {})
        if q.get("price") is not None:
            print(f"   OK:    {t:<6} ${q['price']:.2f}  {q.get('change_pct', 0):+.2f}%  H={q.get('day_high')}  L={q.get('day_low')}")
            ok.append(t)
        else:
            print(f"   FAIL:  {t:<6} {q.get('error', 'no data')}")
            bad.append(t)
    print(f"\n   {len(ok)}/14 succeeded")
    if bad:
        print(f"   Failed: {bad}")
except Exception as e:
    print(f"   ERROR: {e}")

# ── 3. Fallback: yf.Ticker().fast_info per-ticker ─────────────────────────
print()
print("=" * 60)
print("3. Per-ticker yf.Ticker().fast_info fallback")
print("=" * 60)
results = {}
for t in TICKERS:
    try:
        ticker_obj = yf.Ticker(t)
        fi = ticker_obj.fast_info
        price = getattr(fi, "last_price", None)
        prev  = getattr(fi, "previous_close", None)
        hi    = getattr(fi, "day_high", None)
        lo    = getattr(fi, "day_low", None)
        if price is not None:
            chg_pct = round((price - prev) / prev * 100, 2) if prev else None
            print(f"   OK:    {t:<6} ${price:.2f}  chg={chg_pct:+.2f}%  H={hi:.2f}  L={lo:.2f}")
            results[t] = {"price": price, "prev_close": prev, "change_pct": chg_pct,
                          "day_high": hi, "day_low": lo, "error": None}
        else:
            print(f"   EMPTY: {t:<6} fast_info returned no price")
            results[t] = {"price": None, "error": "no price in fast_info"}
    except Exception as e:
        print(f"   FAIL:  {t:<6} {e}")
        results[t] = {"price": None, "error": str(e)}

ok  = [t for t, v in results.items() if v["price"] is not None]
bad = [t for t, v in results.items() if v["price"] is None]
print(f"\n   {len(ok)}/14 succeeded via fast_info")
if bad:
    print(f"   Failed: {bad}")

# ── 4. Hit the live /api/prices endpoint ──────────────────────────────────
print()
print("=" * 60)
print("4. Live /api/prices endpoint check")
print("=" * 60)
try:
    import urllib.request, json as _json
    url = "http://127.0.0.1:5173/api/prices?tickers=" + ",".join(TICKERS)
    with urllib.request.urlopen(url, timeout=35) as resp:
        data = _json.loads(resp.read())
    quotes = data.get("quotes", {})
    no_price = [t for t, q in quotes.items() if q.get("price") is None]
    has_price = [t for t, q in quotes.items() if q.get("price") is not None]
    print(f"   {len(has_price)}/14 returned a price from the API")
    for t in TICKERS:
        q = quotes.get(t, {})
        if q.get("price"):
            print(f"   OK:    {t:<6} ${q['price']:.2f}  {q.get('change_pct', 0):+.2f}%")
        else:
            print(f"   FAIL:  {t:<6} error={q.get('error', 'missing')}")
except Exception as e:
    print(f"   Endpoint unavailable: {e}")
    print("   (Start the pipeline first, or check port 5173)")

print()
print("=" * 60)
print("Done.")
print("=" * 60)
