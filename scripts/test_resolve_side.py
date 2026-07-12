import sys
sys.path.insert(0, r'C:\Users\noleg\Desktop\Claude\Projects\Stocks\src')
from equity_intel.trading.signals import _resolve_side

cases = [
    ('other',       'positive_news',           'buy'),
    ('other',       'negative_news',           'sell'),
    ('regulatory',  'fda_approval',            'buy'),
    ('regulatory',  'crl',                     'sell'),
    ('regulatory',  'complete_response_letter','sell'),
    ('regulatory',  'fda_rejection',           'sell'),
    ('other',       'material_impairment',     'sell'),
    ('other',       'exit_costs',              'sell'),
    ('other',       'merger_acquisition',      'buy'),
    ('earnings',    None,                      'buy'),
    ('guidance_lowered', None,                 'sell'),
    ('other',       None,                      'monitor'),
    ('regulatory',  None,                      'monitor'),
]

all_ok = True
for typ, sub, expected in cases:
    got = _resolve_side(typ, sub)
    status = 'OK  ' if got == expected else 'FAIL'
    if got != expected:
        all_ok = False
    print(f'  {status}  type={typ:<22} sub={str(sub):<30} expected={expected:<8} got={got}')

print()
print('ALL PASS' if all_ok else 'FAILURES ABOVE')
