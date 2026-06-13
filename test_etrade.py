"""
test_etrade.py — Standalone E*TRADE sandbox API connectivity test
-----------------------------------------------------------------
Read-only. Does NOT place orders or move funds.

Tests (sandbox):
  1. OAuth — get request token, authorize, get access token
  2. Accounts — list accounts (confirms auth works end-to-end)
  3. Market data — quote for NVDA (confirms market data access)

OAuth flow (runs once per session):
  Step 1: Script fetches a request token automatically.
  Step 2: Script prints a URL — open it in your browser, log in to E*TRADE,
          and copy the 5-character verification code shown on screen.
  Step 3: Paste the code back here. Script exchanges it for an access token
          and proceeds with the tests.

  Token expires at midnight ET, or after 2 hours of inactivity.

Run:
    python test_etrade.py              # sandbox (default)
    python test_etrade.py --prod       # production (key must be approved first)

Requirements: requests, requests-oauthlib  (pip install requests requests-oauthlib)
Credentials:  ETRADE_SANDBOX_CONSUMER_KEY / SECRET in .env
"""

import argparse
import os
import sys
import webbrowser
from pathlib import Path

try:
    from requests_oauthlib import OAuth1Session
except ImportError:
    raise SystemExit("❌  Missing dependency — run: pip install requests requests-oauthlib")

# ---------------------------------------------------------------------------
# Load .env
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

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

PASS = "✅"
FAIL = "❌"
INFO = "ℹ️ "


def get_config(prod: bool) -> dict:
    prefix = "ETRADE_PROD" if prod else "ETRADE_SANDBOX"
    key    = os.environ.get(f"{prefix}_CONSUMER_KEY", "")
    secret = os.environ.get(f"{prefix}_CONSUMER_SECRET", "")
    if not key or not secret:
        raise SystemExit(
            f"❌  Missing {prefix}_CONSUMER_KEY or {prefix}_CONSUMER_SECRET in .env"
        )
    base_api  = "https://api.etrade.com"   if prod else "https://apisb.etrade.com"
    base_auth = "https://us.etrade.com"
    return dict(key=key, secret=secret, base_api=base_api, base_auth=base_auth, prod=prod)


# ---------------------------------------------------------------------------
# OAuth flow
# ---------------------------------------------------------------------------

def oauth_login(cfg: dict) -> OAuth1Session:
    """Walk through E*TRADE OAuth 1.0a and return an authenticated session."""

    print(f"\n{'=== E*TRADE API connectivity test (PRODUCTION) ===' if cfg['prod'] else '=== E*TRADE API connectivity test (SANDBOX) ==='}\n")
    print("── Step 1/3: Fetching request token …")

    # 1. Request token
    session = OAuth1Session(
        cfg["key"],
        client_secret=cfg["secret"],
        callback_uri="oob",
    )
    resp = session.fetch_request_token(
        f"{cfg['base_api']}/oauth/request_token",
    )
    request_token        = resp["oauth_token"]
    request_token_secret = resp["oauth_token_secret"]
    print(f"   {PASS} Request token obtained")

    # 2. Authorize
    auth_url = (
        f"{cfg['base_auth']}/e/t/etws/authorize"
        f"?key={cfg['key']}&token={request_token}"
    )
    print(f"\n── Step 2/3: Browser authorization required")
    print(f"   Opening: {auth_url}")
    print(f"   If the browser doesn't open, copy-paste the URL above manually.")
    try:
        webbrowser.open(auth_url)
    except Exception:
        pass

    verifier = input("\n   Paste the 5-character verification code from E*TRADE: ").strip()
    if not verifier:
        raise SystemExit("❌  No verification code provided — aborting.")

    # 3. Access token
    print("\n── Step 3/3: Exchanging verification code for access token …")
    session = OAuth1Session(
        cfg["key"],
        client_secret=cfg["secret"],
        resource_owner_key=request_token,
        resource_owner_secret=request_token_secret,
        verifier=verifier,
    )
    resp = session.fetch_access_token(f"{cfg['base_api']}/oauth/access_token")
    access_token        = resp["oauth_token"]
    access_token_secret = resp["oauth_token_secret"]

    # Return a clean session with the final access credentials
    authed = OAuth1Session(
        cfg["key"],
        client_secret=cfg["secret"],
        resource_owner_key=access_token,
        resource_owner_secret=access_token_secret,
    )
    print(f"   {PASS} Access token obtained — session active until midnight ET\n")
    return authed, cfg["base_api"]


# ---------------------------------------------------------------------------
# API tests
# ---------------------------------------------------------------------------

def test_accounts(session: OAuth1Session, base_api: str) -> str | None:
    """List accounts — returns the first accountIdKey found."""
    url  = f"{base_api}/v1/accounts/list.json"
    resp = session.get(url, timeout=15)
    if resp.status_code != 200:
        print(f"  {FAIL}  Accounts /list — HTTP {resp.status_code}: {resp.text[:200]}")
        return None

    data     = resp.json()
    accounts = (
        data.get("AccountListResponse", {})
            .get("Accounts", {})
            .get("Account", [])
    )
    if not accounts:
        print(f"  {FAIL}  Accounts /list — no accounts returned")
        return None

    print(f"  {PASS}  Accounts /list")
    for acct in accounts:
        print(f"       Account: {acct.get('accountId','?')}  "
              f"type={acct.get('accountType','?')}  "
              f"status={acct.get('accountStatus','?')}")
    return accounts[0].get("accountIdKey")


def test_quote(session: OAuth1Session, base_api: str, symbol: str = "NVDA") -> None:
    """Fetch a market quote."""
    url  = f"{base_api}/v1/market/quote/{symbol}.json"
    resp = session.get(url, timeout=15)
    if resp.status_code != 200:
        print(f"  {FAIL}  Market quote ({symbol}) — HTTP {resp.status_code}: {resp.text[:200]}")
        return

    data   = resp.json()
    quotes = (
        data.get("QuoteResponse", {})
            .get("QuoteData", [])
    )
    if not quotes:
        print(f"  {FAIL}  Market quote ({symbol}) — no data returned")
        return

    print(f"  {PASS}  Market quote ({symbol})")
    for q in quotes:
        product = q.get("Product", {})
        all_q   = q.get("All", {})
        print(f"       Symbol : {product.get('symbol','?')}")
        print(f"       Last   : {all_q.get('lastTrade', all_q.get('ask', '?'))}")
        print(f"       Ask    : {all_q.get('ask','?')}  Bid: {all_q.get('bid','?')}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="E*TRADE API connectivity test")
    parser.add_argument("--prod", action="store_true", help="Use production (not sandbox)")
    args = parser.parse_args()

    cfg = get_config(prod=args.prod)

    try:
        session, base_api = oauth_login(cfg)
    except Exception as exc:
        print(f"\n{FAIL}  OAuth failed: {exc}")
        sys.exit(1)

    all_passed = True

    try:
        acct_key = test_accounts(session, base_api)
        all_passed &= acct_key is not None
    except Exception as exc:
        print(f"  {FAIL}  Accounts — exception: {exc}")
        all_passed = False

    try:
        test_quote(session, base_api, symbol="NVDA")
    except Exception as exc:
        print(f"  {FAIL}  Market quote — exception: {exc}")
        all_passed = False

    print()
    if all_passed:
        print("All tests passed — E*TRADE API is wired up correctly.\n")
    else:
        print("One or more tests failed — see errors above.\n")


if __name__ == "__main__":
    main()
