import os, subprocess, sys

repo = r"C:\Users\noleg\Desktop\Claude\Projects\Stocks"

lock = os.path.join(repo, ".git", "index.lock")
if os.path.exists(lock):
    os.remove(lock)
    print("Removed index.lock")

msg = (
    "fix: run.bat cmd window closes automatically when dashboard stops\n\n"
    "- Capture dashboard PID at startup via PowerShell Start-Process -PassThru\n"
    "- Replace `pause` with a :monitor_dashboard loop that polls the PID every 5s\n"
    "- When pythonw.exe (dashboard) exits, the cmd window closes itself\n"
    "- Removes stale window clutter when UI is stopped between runs\n"
    "- MCP config hint still printed once after sync completes"
)

for cmd in [
    ["git", "add", "-f", "run.bat"],
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
