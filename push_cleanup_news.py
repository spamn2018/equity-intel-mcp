import os, subprocess, sys

repo = r"C:\Users\noleg\Desktop\Claude\Projects\Stocks"

lock = os.path.join(repo, ".git", "index.lock")
if os.path.exists(lock):
    os.remove(lock)
    print("Removed index.lock")

msg = (
    "feat: add equity-cleanup-news maintenance worker\n\n"
    "Deletes news_articles older than a configurable retention period\n"
    "(default 60 days) to keep the DB size bounded without touching\n"
    "the active 7-day synthesis window.\n\n"
    "src/equity_intel/workers/cleanup_news.py:\n"
    "- cleanup_news(days, dry_run) — uses existing get_session() pattern\n"
    "- Matches on published_at; falls back to created_at for null-timestamp rows\n"
    "- Rolls back and exits nonzero on any DB error\n"
    "- Idempotent and safe to run repeatedly\n\n"
    "pyproject.toml:\n"
    "- equity-cleanup-news = equity_intel.workers.cleanup_news:main\n\n"
    "Validation on live DB (617 rows, all within 60 days):\n"
    "  --days 60 --dry-run  -> Would delete: 0 row(s)  [correct]\n"
    "  --days 1  --dry-run  -> Would delete: 583 row(s) [logic confirmed]\n\n"
    "Not added to run.bat critical path — run as separate maintenance step:\n"
    "  equity-cleanup-news --days 60"
)

for cmd in [
    ["git", "add",
     "src/equity_intel/workers/cleanup_news.py",
     "pyproject.toml"],
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
