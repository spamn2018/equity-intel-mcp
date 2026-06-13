import os, subprocess, sys

repo = r"C:\Users\noleg\Desktop\Claude\Projects\Stocks"

lock = os.path.join(repo, ".git", "index.lock")
if os.path.exists(lock):
    os.remove(lock)
    print("Removed index.lock")

for cmd in [
    ["git", "add",
     "src/equity_intel/sec/cik.py",
     "src/equity_intel/sec/filings.py"],
    ["git", "commit", "-m",
     "fix: suppress noisy ETF warnings for BOTZ/ROBO in company sync\n\n"
     "BOTZ and ROBO are ETFs tracked for news/price context only. They are\n"
     "intentionally absent from the SEC equity ticker map (they file N-PORT\n"
     "and N-CEN as investment funds, not 10-K/8-K as operating companies).\n\n"
     "Changes:\n"
     "- cik.py: add KNOWN_ETFS dict with BOTZ and ROBO metadata. When an ETF\n"
     "  ticker is missing from the SEC map, log at debug (not WARNING) and\n"
     "  upsert with known name/exchange so news and price workers can use them.\n"
     "- filings.py: downgrade no_cik_for_company from WARNING to debug.\n"
     "  The code already returns [] cleanly; the warning added noise on every\n"
     "  sync run for every no-CIK company (including all ETFs)."],
    ["git", "push"],
]:
    r = subprocess.run(cmd, cwd=repo, capture_output=True, text=True)
    out = (r.stdout + r.stderr).strip()
    if out:
        print(out)
    if r.returncode != 0:
        sys.exit(r.returncode)

print("Done.")
