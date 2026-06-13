"""
test_kraken.py — Standalone Kraken API connectivity test
---------------------------------------------------------
Read-only. Does NOT place orders or move funds.

Tests:
  1. Public  — GET /0/public/Time        (connectivity, no auth)
  2. Public  — GET /0/public/AssetPairs  (checks for xStock pairs)
  3. Private — POST /0/private/Balance   (HMAC-SHA512 auth check)

─── IMPORTANT: Kraken equity products and US availability ───────────────────

Kraken has TWO separate equity products. Neither currently supports
programmatic REST trading for US users:

  A) xStocks — tokenized stocks (AAPLx, TSLAx, NVDAx, etc.)
       - Pair format  : AAPLx/USD  (asset_class: tokenized_asset)
       - API access   : YES — same Spot REST API, same auth
       - US access    : ❌ BLOCKED — explicitly excluded (US, UK, CA, AU)
       - What they are: blockchain tokens backed 1:1 by underlying shares,
                        held in custody by Alpaca Securities (FINRA/SIPC).
                        No voting rights. Dividends reinvested via rebasing.

  B) Kraken Securities — actual US-listed stocks & ETFs (11,000+)
       - Powered by   : Kraken Securities LLC (FINRA-registered broker-dealer)
       - US access    : ✅ Available to US users (state restrictions may apply)
       - API access   : ❌ NOT YET — currently app/desktop-only
       - Commission   : Zero

  Bottom line for a US-based trading pipeline:
    - Auth works fine (confirmed below)
    - No US equity pairs are accessible via the REST API today
    - Watch for Kraken Securities to open API access — when they do,
      pair format will likely follow their existing spot conventions

Run:
    python test_kraken.py

Requirements: requests  (pip install requests)
Credentials:  KRAKEN_API_KEY and KRAKEN_PRIVATE_KEY in .env (or env vars)
"""

import base64
import hashlib
import hmac
import os
import time
import urllib.parse
from pathlib import Path

try:
    import requests
except ImportError:
    raise SystemExit("❌  'requests' not installed — run: pip install requests")

# ---------------------------------------------------------------------------
# Load credentials from .env
# ---------------------------------------------------------------------------

def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv(Path(__file__).parent / ".env")

API_KEY     = os.environ.get("KRAKEN_API_KEY", "")
PRIVATE_KEY = os.environ.get("KRAKEN_PRIVATE_KEY", "")
BASE_URL    = "https://api.kraken.com"

# ---------------------------------------------------------------------------
# Auth helper (HMAC-SHA512 per Kraken REST spec)
# ---------------------------------------------------------------------------

def _sign(uri_path: str, data: dict) -> str:
    post_data = urllib.parse.urlencode(data)
    encoded   = (str(data["nonce"]) + post_data).encode()
    message   = uri_path.encode() + hashlib.sha256(encoded).digest()
    mac       = hmac.new(base64.b64decode(PRIVATE_KEY), message, hashlib.sha512)
    return base64.b64encode(mac.digest()).decode()


def _nonce() -> int:
    return int(time.time() * 1000)

# ---------------------------------------------------------------------------
# API calls
# ---------------------------------------------------------------------------

def test_server_time() -> dict:
    r = requests.get(f"{BASE_URL}/0/public/Time", timeout=10)
    r.raise_for_status()
    return r.json()


def test_asset_pairs() -> dict:
    """Check what pairs are live — specifically look for xStock pairs."""
    r = requests.get(f"{BASE_URL}/0/public/AssetPairs", timeout=15)
    r.raise_for_status()
    return r.json()


def test_balance() -> dict:
    """Private — read account balances. No side effects."""
    if not API_KEY or not PRIVATE_KEY:
        return {"error": ["Missing KRAKEN_API_KEY or KRAKEN_PRIVATE_KEY in .env"]}

    uri_path = "/0/private/Balance"
    data     = {"nonce": _nonce()}
    headers  = {
        "API-Key":  API_KEY,
        "API-Sign": _sign(uri_path, data),
    }
    r = requests.post(f"{BASE_URL}{uri_path}", headers=headers, data=data, timeout=10)
    r.raise_for_status()
    return r.json()

# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

PASS = "✅"
FAIL = "❌"
INFO = "ℹ️ "
WARN = "⚠️ "

# xStock tickers use lowercase 'x' suffix
XSTOCK_TICKERS = ["AAPLx", "TSLAx", "NVDAx", "MSFTx", "AMZNx", "GOOGLx", "METAx"]


def _ok(label: str, result: dict) -> bool:
    errors = result.get("error", [])
    if errors:
        print(f"  {FAIL}  {label}")
        for e in errors:
            print(f"       {e}")
        return False
    print(f"  {PASS}  {label}")
    return True


def main() -> None:
    print("\n=== Kraken API connectivity test ===\n")
    all_passed = True

    # 1. Server time
    try:
        res = test_server_time()
        ok  = _ok("Public /Time", res)
        if ok:
            print(f"       Server unix time : {res['result']['unixtime']}")
        all_passed &= ok
    except Exception as exc:
        print(f"  {FAIL}  Public /Time — {exc}")
        all_passed = False

    # 2. Asset pairs — probe for xStock availability
    try:
        res    = test_asset_pairs()
        ok     = _ok("Public /AssetPairs", res)
        if ok:
            pairs   = res.get("result", {})
            xstocks = [k for k in pairs if any(t in k for t in XSTOCK_TICKERS)]
            if xstocks:
                print(f"       {PASS} xStock pairs found — API access live: {xstocks[:5]}")
            else:
                print(f"       {WARN} No xStock pairs visible (geo-restricted for US accounts)")
                print(f"       {INFO}  Kraken Securities (US stocks) has no REST API yet")
                print(f"            Total crypto pairs available: {len(pairs)}")
        all_passed &= ok
    except Exception as exc:
        print(f"  {FAIL}  Public /AssetPairs — {exc}")
        all_passed = False

    # 3. Private balance (auth check)
    try:
        res = test_balance()
        ok  = _ok("Private /Balance (auth)", res)
        if ok:
            balances = {k: v for k, v in res.get("result", {}).items() if float(v) > 0}
            if balances:
                print("       Non-zero balances:")
                for asset, amt in list(balances.items())[:8]:
                    print(f"         {asset:8s}: {float(amt):.6f}")
                if len(balances) > 8:
                    print(f"         ... and {len(balances) - 8} more")
            else:
                print("       Account reachable — no non-zero balances")
        all_passed &= ok
    except Exception as exc:
        print(f"  {FAIL}  Private /Balance — {exc}")
        all_passed = False

    print()
    if all_passed:
        print("Auth and connectivity confirmed. See module docstring for US equity trading status.\n")
    else:
        print("One or more tests failed — see errors above.\n")


if __name__ == "__main__":
    main()
