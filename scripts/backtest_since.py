import sys, datetime, statistics
sys.path.insert(0, r'C:\Users\noleg\Desktop\Claude\Projects\Stocks\src')
from equity_intel.db.models import SignalOutcome, TradeSignal
from equity_intel.db.session import SessionLocal

# Naive cutoff -- matches how created_at is stored
CUTOFF = datetime.datetime(2026, 7, 7, 0, 0, 0)

with SessionLocal() as session:
    rows = (
        session.query(SignalOutcome, TradeSignal)
        .join(TradeSignal, SignalOutcome.trade_signal_id == TradeSignal.id)
        .filter(TradeSignal.created_at >= CUTOFF)
        .all()
    )

unique_signals = set(r.TradeSignal.id for r in rows)
print(f"\nSignals created on or after 2026-07-07: {len(unique_signals)}")
print(f"Outcome rows: {len(rows)}\n")

for horizon in [1, 5, 10]:
    subset = [r for r in rows if r.SignalOutcome.horizon_days == horizon]
    returns = [r.SignalOutcome.forward_return_pct for r in subset if r.SignalOutcome.forward_return_pct is not None]
    if not returns:
        print(f"Horizon {horizon}d: no mature outcomes yet\n")
        continue

    wins = sum(1 for x in returns if x > 0)
    print(f"=== Horizon: {horizon} trading day(s) -- n={len(returns)} ===")
    print(f"  Avg forward return : {sum(returns)/len(returns):+.2f}%")
    print(f"  Median             : {statistics.median(returns):+.2f}%")
    print(f"  Win rate           : {wins}/{len(returns)} = {wins/len(returns)*100:.1f}%")

    for label in ["executed", "blocked", "expired"]:
        grp = [r.SignalOutcome.forward_return_pct for r in subset
               if r.TradeSignal.status == label and r.SignalOutcome.forward_return_pct is not None]
        if grp:
            print(f"    {label:<12} n={len(grp):<3} avg={sum(grp)/len(grp):+.2f}%")

    for etype in sorted(set(r.TradeSignal.event_type for r in subset if r.TradeSignal.event_type)):
        grp = [r.SignalOutcome.forward_return_pct for r in subset
               if r.TradeSignal.event_type == etype and r.SignalOutcome.forward_return_pct is not None]
        if grp:
            print(f"    {etype:<26} n={len(grp):<3} avg={sum(grp)/len(grp):+.2f}%")
    print()
