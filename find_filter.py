import re
with open(r'C:\Users\noleg\Desktop\Claude\Projects\AI Portfolio\ai_portfolio.html', encoding='utf-8') as f:
    html = f.read()
lines = html.split('\n')
keywords = ['HIGH MATERIALITY', 'highMat', 'statFilter', 'activeFilter', 'filterActive', 'statClick', 'MATERIALITY', 'stat-active', 'filterStat']
for i, line in enumerate(lines):
    for kw in keywords:
        if kw.lower() in line.lower():
            print('%d: %s' % (i, line.strip()[:150]))
            break
