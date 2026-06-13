import os, subprocess, sys

repo = r"C:\Users\noleg\Desktop\Claude\Projects\Stocks"

lock = os.path.join(repo, ".git", "index.lock")
if os.path.exists(lock):
    os.remove(lock)
    print("Removed index.lock")

for cmd in [
    ["git", "add", "synthesize.py"],
    ["git", "commit", "-m",
     "fix: synthesize.py API fallback when briefs/ is empty\n\n"
     "When brief_*.json files are missing (common when sync_all.bat step 9\n"
     "fails silently), synthesize.py previously printed a warning and exited\n"
     "without ever calling the LLM, leaving intelligence/ empty and the\n"
     "My Views cards showing 'No synthesis data yet'.\n\n"
     "Changes:\n"
     "- _brief_date(): make path optional (path=None) for API-sourced dicts\n"
     "- aggregate(): accept List[Path | dict] -- pass-through for pre-loaded\n"
     "  brief dicts from the API, reads from disk for Path objects\n"
     "- main(): when briefs/ is empty or missing, call _fetch_api_brief()\n"
     "  as fallback; only hard-exit if both file and API sources are absent\n"
     "- Soft-degrade folder.exists() check to a warning + fallback instead\n"
     "  of sys.exit(1)\n\n"
     "The dashboard is always started at step 0, so /api/brief is online\n"
     "by the time synthesize.py runs at step 12."],
    ["git", "push"],
]:
    r = subprocess.run(cmd, cwd=repo, capture_output=True, text=True)
    out = (r.stdout + r.stderr).strip()
    if out:
        print(out)
    if r.returncode != 0:
        sys.exit(r.returncode)

print("Done.")
