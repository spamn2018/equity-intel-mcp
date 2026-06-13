# E*TRADE Auth Script — Setup Guide

Standalone, no phone, no human interaction after this one-time setup.

---

## What this does

`etrade_auth.py` runs the full E*TRADE OAuth 1.0a flow headlessly every morning:
generates a VIP 2FA code → drives a browser through login → captures the verifier →
saves `access_token` + `access_secret` to `.etrade_token.json`.

Your pipeline reads that file to make authenticated API calls.

---

## One-time setup (do this once)

### 1. Install dependencies

```
pip install requests requests-oauthlib python-vipaccess playwright python-dotenv
playwright install chromium
```

### 2. Get E*TRADE API credentials

1. Go to https://developer.etrade.com/getting-started
2. Log in with your E*TRADE account
3. Create an application
4. Copy the **Consumer Key** and **Consumer Secret**

### 3. Provision a VIP credential

```
vipaccess provision
```

Output looks like:

```
Credential created successfully:
        Credential ID: VSMT12345678
        Secret: JBSWY3DPEHPK3PXP...
```

Save those two values — you'll need them in the next step.

### 4. Register the VIP credential with E*TRADE

1. Log in at etrade.com
2. Go to **Security Settings** → **Two-Factor Authentication**
3. Add a new VIP credential
4. Enter the **Credential ID** from step 3 (e.g. `VSMT12345678`)
5. Enter the current 6-digit code (run `vipaccess totp VSMT12345678` to get it)
6. Save

### 5. Create your .env.etrade file

```
cp .env.etrade.example .env.etrade
```

Fill in every value:
- `ETRADE_CONSUMER_KEY` — from step 2
- `ETRADE_CONSUMER_SECRET` — from step 2
- `ETRADE_USERNAME` — your E*TRADE login username
- `ETRADE_PASSWORD` — your E*TRADE login password
- `VIP_CREDENTIAL_ID` — from step 3
- `VIP_SECRET` — the base32 secret from step 3
- `ETRADE_SANDBOX` — set `true` first to test, `false` for live

### 6. Test it

```
cd C:\Users\noleg\Desktop\Claude\Projects\Stocks
python etrade_auth.py
```

On success you'll see:

```
=== E*TRADE Auth Script ===
Requesting OAuth request token...
Got request token: abc12345…
Generated VIP code (valid ~18s)
Navigating to authorization URL...
Filled username
Filled password
Clicked login button...
2FA prompt detected
Filled VIP 2FA code
Verifier found via selector '#oauth_pin': abcd…
Exchanging verifier for access token...
Access token obtained: xyz98765…
Token saved to: .etrade_token.json
=== Auth complete. Token ready for market open. ===
```

### 7. Verify the token file

```
type .etrade_token.json
```

Should contain `consumer_key`, `access_token`, `access_secret`, `obtained_at`.

---

## Using the token in your pipeline

```python
from etrade_auth import load_token
from requests_oauthlib import OAuth1Session

t = load_token()  # reads .etrade_token.json
session = OAuth1Session(
    t["consumer_key"], t["consumer_secret"],
    t["access_token"], t["access_secret"],
)
resp = session.get("https://api.etrade.com/v1/accounts/list")
print(resp.json())
```

---

## Scheduling (daily before market open)

Run `etrade_auth.py` automatically at 5:55 AM ET each morning.

Ask Claude to set up a scheduled task, or use Windows Task Scheduler / cron.

---

## Troubleshooting

**2FA page not found / selector mismatch**
E*TRADE occasionally redesigns their login pages. Check the `debug/` folder —
the script saves an HTML snapshot and screenshot whenever it can't find an element.
Open the HTML and find the actual field `id` or `name`, then update the selectors
in `browser_authorize()`.

**VIP code rejected**
Make sure your system clock is accurate (VIP codes are time-based).
Also verify the `VIP_CREDENTIAL_ID` is registered in E*TRADE security settings.

**`oauth` module not found**
`python-vipaccess` depends on `oath`. Install both:
```
pip install python-vipaccess oath
```

**Sandbox vs live**
Always test with `ETRADE_SANDBOX=true` first. The sandbox API is identical but
uses paper money and won't affect your real account.
