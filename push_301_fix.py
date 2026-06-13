import os, subprocess, sys

repo = r"C:\Users\noleg\Desktop\Claude\Projects\Stocks"

lock = os.path.join(repo, ".git", "index.lock")
if os.path.exists(lock):
    os.remove(lock)
    print("Removed index.lock")

for cmd in [
    ["git", "add",
     "src/equity_intel/sec/client.py",
     "src/equity_intel/news/polygon.py"],
    ["git", "commit", "-m",
     "fix: eliminate 301 redirects on Archives fetches; fix polygon title NameError\n\n"
     "## SEC Archives 301 fix (client.py)\n\n"
     "The SEC Archives server returns 301 for zero-padded CIK segments\n"
     "(e.g. /edgar/data/0001045810/) and redirects to the un-padded form.\n"
     "Previous fix only applied to newly-built URLs. Existing DB records still\n"
     "stored the padded form so every document download hit the redirect.\n\n"
     "Added _normalize_archives_url() which strips leading zeros from the CIK\n"
     "segment via regex, called in get_filing_document() before the HTTP fetch.\n"
     "Covers DB-stored URLs, new URLs, and any future code path.\n\n"
     "## polygon.py NameError fix\n\n"
     "When the title-keyword filter was replaced with the tickers-count guard,\n"
     "the 'title = article.get(\"title\", \"\")' assignment was removed but the\n"
     "'title' reference in the normalized dict was left behind.\n"
     "Fixed: replaced bare 'title' with article.get('title', '') inline."],
    ["git", "push"],
]:
    r = subprocess.run(cmd, cwd=repo, capture_output=True, text=True)
    out = (r.stdout + r.stderr).strip()
    if out:
        print(out)
    if r.returncode != 0:
        sys.exit(r.returncode)

print("Done.")
