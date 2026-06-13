import json
from pathlib import Path
from requests_oauthlib import OAuth1Session

t = json.loads(Path(r'C:\Users\noleg\Desktop\Claude\Projects\Stocks\.etrade_token.json').read_text())
session = OAuth1Session(t['consumer_key'], t['consumer_secret'], t['access_token'], t['access_secret'])

base = 'https://apisb.etrade.com' if t['sandbox'] else 'https://api.etrade.com'
print(f"Mode: {'SANDBOX' if t['sandbox'] else 'LIVE'}")
print(f"Token obtained: {t['obtained_at']}")
print()

resp = session.get(f'{base}/v1/accounts/list', headers={'Accept': 'application/json'})
print('Status:', resp.status_code)
print(resp.text[:2000])
