import os, subprocess, sys

repo = r"C:\Users\noleg\Desktop\Claude\Projects\Stocks"

lock = os.path.join(repo, ".git", "index.lock")
if os.path.exists(lock):
    os.remove(lock)
    print("Removed index.lock")

files = [
    ".env.example",
    "run.bat",
    "src/equity_intel/config.py",
    "src/equity_intel/news/polygon.py",
    "src/equity_intel/sec/cik.py",
    "src/equity_intel/sec/client.py",
    "src/equity_intel/sec/filings.py",
    "src/equity_intel/workers/run_daily_brief.py",
    "src/equity_intel/workers/sync_news.py",
    "synthesize.py",
    "tests/test_daily_brief.py",
]

msg = (
    "fix: pipeline visibility, news limits, and synthesis failure chain\n\n"
    "News fetch:\n"
    "- polygon.py: default window 24h (days=1), cap 10 stories/ticker\n"
    "  Over-fetches 5x raw to survive bias filter; hard caps after filtering\n"
    "- sync_news.py: matching defaults (days=1, limit_per_ticker=10)\n\n"
    "Config / daily brief:\n"
    "- config.py: daily_brief_days default 1 -> 7 (1 day produced empty briefs)\n"
    "- .env.example: updated comment and default to match\n"
    "- run_daily_brief.py: always-print diagnostic block (DAILY_BRIEF_DAYS,\n"
    "  window, min_materiality, max_items, catalyst count). Warns to stderr\n"
    "  when total_catalysts == 0\n\n"
    "synthesize.py -- fix silent-success on real failures:\n"
    "- no briefs + dashboard API unavailable -> sys.exit(1)\n"
    "- brief_count == 0 after date filter -> sys.exit(1) with hint\n"
    "- zero catalyst signal in aggregation -> sys.exit(1) with hint\n"
    "- --list-models connection failure -> sys.exit(1)\n"
    "- LM Studio ConnectionError -> targeted error message\n\n"
    "run.bat:\n"
    "- Step 9: warning mentions synthesis will try API fallback\n"
    "- Step 9b: today-only brief (--days 1, briefs/today/) for freshness layer\n"
    "- Step 12: hard exit /b 1 on synthesize.py failure\n"
    "- Step 12: intelligence/ existence check after synthesis\n\n"
    "tests/test_daily_brief.py:\n"
    "- Config default assertion updated: daily_brief_days must be 7\n"
    "- Regression test for 1-day empty-brief failure\n"
    "- CLI tests: resolved days, catalyst count, zero-catalyst warning\n"
    "- 30/30 passing"
)

non_ignored = [f for f in files if f != "run.bat"]
for cmd in [
    ["git", "add"] + non_ignored,
    ["git", "add", "-f", "run.bat"],   # run.bat is in .gitignore, force-add
    ["git", "commit", "-m", msg],
    ["git", "push"],
]:
    r = subprocess.run(cmd, cwd=repo, capture_output=True, text=True)
    out = (r.stdout + r.stderr).strip()
    if out:
        print(out)
    if r.returncode != 0:
        sys.exit(r.returncode)

print("Done.")
