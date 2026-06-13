import sys
sys.stdout.reconfigure(encoding='utf-8')
with open(r'C:\Users\noleg\Desktop\Claude\Projects\AI Portfolio\ai_portfolio.html', encoding='utf-8') as f:
    lines = f.readlines()
# Print render/filter/setFilter logic
print('=== JS FILTER/RENDER (900-1000) ===')
for i, l in enumerate(lines[899:1000], 900):
    print('%d: %s' % (i, l.rstrip()[:150]))
