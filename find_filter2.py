with open(r'C:\Users\noleg\Desktop\Claude\Projects\AI Portfolio\ai_portfolio.html', encoding='utf-8') as f:
    lines = f.readlines()
# Print lines 300-370 (statsbar area) and lines 900-960 (filter/sort logic)
print('=== STATSBAR (300-370) ===')
for i, l in enumerate(lines[299:370], 300):
    print('%d: %s' % (i, l.rstrip()[:150]))
print()
print('=== FILTER LOGIC (900-970) ===')
for i, l in enumerate(lines[899:970], 900):
    print('%d: %s' % (i, l.rstrip()[:150]))
