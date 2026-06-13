import sys
sys.stdout.reconfigure(encoding='utf-8')

with open(r'C:\Users\noleg\Desktop\Claude\Projects\AI Portfolio\ai_portfolio.html', encoding='utf-8') as f:
    src = f.read()

# 1. Add _statFilter variable near _filterCat
old_var = "let _filterCat=\"ALL\""
new_var = "let _filterCat=\"ALL\",_statFilter=null"
assert old_var in src, "_filterCat not found"
src = src.replace(old_var, new_var, 1)

# 2. Update visibleHoldings() to apply stat filter after category filter
old_vh = """function visibleHoldings(){
  const list=_filterCat===\"ALL\"?[...PORTFOLIO]:PORTFOLIO.filter(h=>h.category===_filterCat);"""
new_vh = """function visibleHoldings(){
  let list=_filterCat===\"ALL\"?[...PORTFOLIO]:PORTFOLIO.filter(h=>h.category===_filterCat);
  if(_statFilter===\"highmat\") list=list.filter(h=>(topFor(h.ticker)?.materiality_score||0)>=.7);
  else if(_statFilter===\"gainers\") list=list.filter(h=>(_quotes.get(h.ticker)?.change_pct||0)>0);
  else if(_statFilter===\"losers\") list=list.filter(h=>(_quotes.get(h.ticker)?.change_pct||0)<0);
  else if(_statFilter===\"catalysts\") list=list.filter(h=>(_eventsByTicker.get(h.ticker)||[]).length>0);"""
assert old_vh in src, "visibleHoldings not found"
src = src.replace(old_vh, new_vh, 1)

# 3. Add setStatFilter function after setSort
old_setsort = """function setSort(k){
  _sortKey=k;
  document.querySelectorAll(\".sort-btn\").forEach(b=>b.classList.toggle(\"active\",b.dataset.sort===k));
  renderTable();
}"""
new_setsort = """function setSort(k){
  _sortKey=k;
  document.querySelectorAll(\".sort-btn\").forEach(b=>b.classList.toggle(\"active\",b.dataset.sort===k));
  renderTable();
}
function setStatFilter(key){
  _statFilter=_statFilter===key?null:key;
  document.querySelectorAll(\".sstat\").forEach(el=>el.classList.toggle(\"sstat-active\",el.dataset.filter===key&&_statFilter===key));
  renderTable();
}"""
assert old_setsort in src, "setSort not found"
src = src.replace(old_setsort, new_setsort, 1)

# 4. Clear stat filter when setFilter('ALL') called (patch setFilter)
old_setfilter = """function setFilter(cat){
  _filterCat=cat;
  document.querySelectorAll(\".fpill\").forEach(b=>b.classList.toggle(\"active\",b.dataset.cat===cat));
  renderDonut(cat===\"ALL\"?null:cat); highlightCat(cat===\"ALL\"?null:cat); renderTable();
}"""
new_setfilter = """function setFilter(cat){
  _filterCat=cat;
  if(cat===\"ALL\"){ _statFilter=null; document.querySelectorAll(\".sstat\").forEach(el=>el.classList.remove(\"sstat-active\")); }
  document.querySelectorAll(\".fpill\").forEach(b=>b.classList.toggle(\"active\",b.dataset.cat===cat));
  renderDonut(cat===\"ALL\"?null:cat); highlightCat(cat===\"ALL\"?null:cat); renderTable();
}"""
assert old_setfilter in src, "setFilter not found"
src = src.replace(old_setfilter, new_setfilter, 1)

# 5. Add onclick + data-filter to the clickable stat cards and CSS for cursor/hover
old_highmat = """<div class="sstat"><div class="sstat-val" id="s-highmat">☃</div><div class="sstat-lbl">High Materiality</div></div>"""
# Try with actual content
import re
old_highmat_pat = r'<div class="sstat"><div class="sstat-val" id="s-highmat">[^<]*</div><div class="sstat-lbl">High Materiality</div></div>'
old_gainers_pat = r'<div class="sstat"><div class="sstat-val green" id="s-gainers">[^<]*</div><div class="sstat-lbl">Gainers Today</div></div>'
old_losers_pat  = r'<div class="sstat"><div class="sstat-val red" id="s-losers">[^<]*</div><div class="sstat-lbl">Losers Today</div></div>'
old_cats_pat    = r'<div class="sstat"><div class="sstat-val blue" id="s-catalysts">[^<]*</div><div class="sstat-lbl">Catalysts \(7d\)</div></div>'

def add_filter(m, key):
    inner = m.group(0)
    # Wrap in clickable sstat with data-filter and onclick
    inner = inner.replace('<div class="sstat">', '<div class="sstat sstat-clickable" data-filter="%s" onclick="setStatFilter(\'%s\')">' % (key, key))
    return inner

src = re.sub(old_highmat_pat, lambda m: add_filter(m, 'highmat'), src)
src = re.sub(old_gainers_pat, lambda m: add_filter(m, 'gainers'), src)
src = re.sub(old_losers_pat,  lambda m: add_filter(m, 'losers'),  src)
src = re.sub(old_cats_pat,    lambda m: add_filter(m, 'catalysts'), src)

# 6. Add CSS for sstat-clickable and sstat-active
old_sstat_css = ".sstat{display:flex;flex-direction:column;justify-content:center;padding:0 18px;border-right:1px solid var(--border);min-width:100px}"
new_sstat_css = (
    ".sstat{display:flex;flex-direction:column;justify-content:center;padding:0 18px;border-right:1px solid var(--border);min-width:100px}"
    ".sstat-clickable{cursor:pointer;transition:background .15s}"
    ".sstat-clickable:hover{background:rgba(26,107,191,.08)}"
    ".sstat-active{background:rgba(26,107,191,.15)!important;outline:2px solid var(--blue)}"
)
assert old_sstat_css in src, "sstat CSS not found"
src = src.replace(old_sstat_css, new_sstat_css, 1)

with open(r'C:\Users\noleg\Desktop\Claude\Projects\AI Portfolio\ai_portfolio.html', 'w', encoding='utf-8') as f:
    f.write(src)
print("done")
