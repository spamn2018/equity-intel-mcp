import subprocess, sys

repo = r"C:\Users\noleg\Desktop\Claude\Projects\Stocks"

for cmd in [
    ["git", "add", "src/equity_intel/news/polygon.py"],
    ["git", "commit", "-m",
     "fix: polygon news filter - tickers count guard instead of title keyword match\n\n"
     "Old filter required the ticker symbol (e.g. NVDA) to appear literally in the\n"
     "article title, skipping 90-99% of legitimate articles that use the company name\n"
     "instead (e.g. 'Nvidia', 'Google', 'Microsoft').\n\n"
     "New filter: skip articles with >15 tickers in their tickers array. These are\n"
     "13F / portfolio roundup articles. Genuine company news has 1-5 tickers."],
    ["git", "push"],
]:
    r = subprocess.run(cmd, cwd=repo, capture_output=True, text=True)
    out = r.stdout or r.stderr
    if out.strip():
        print(out.strip())
    if r.returncode != 0:
        sys.exit(r.returncode)
print("Done.")
