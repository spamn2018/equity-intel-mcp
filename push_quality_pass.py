import os, subprocess, sys

repo = r"C:\Users\noleg\Desktop\Claude\Projects\Stocks"

lock = os.path.join(repo, ".git", "index.lock")
if os.path.exists(lock):
    os.remove(lock)
    print("Removed index.lock")

for cmd in [
    ["git", "add", "synthesize.py"],
    ["git", "commit", "-m",
     "feat: synthesize.py quality pass — signal selection, JSON mode, validation+repair\n\n"
     "Ports three patterns from Podcasts Pull weekly_synthesis.py into the\n"
     "equity synthesizer:\n\n"
     "1. JSON mode (response_format: {type: json_object})\n"
     "   LM Studio 0.3+ grammar-constrained generation. Added json_mode param\n"
     "   to _complete(); _complete_json() always passes json_mode=True. Eliminates\n"
     "   the most common JSON extraction failures before the retry path is needed.\n\n"
     "2. Domain-balanced signal selection\n"
     "   Added EVENT_TYPE_TO_DOMAIN (5 domains: earnings_guidance, corporate_action,\n"
     "   risk_legal, product_macro, market_technical), _score_catalyst(), and\n"
     "   _select_balanced_signals(agg, max_per_domain=3). Curates top signals by\n"
     "   domain before the LLM call so the prompt is a balanced digest, not a raw\n"
     "   dump. synthesize() now accepts selected_signals and injects a curated\n"
     "   SELECTED SIGNALS block above the full aggregated data.\n\n"
     "3. Synthesis validation + targeted repair pass\n"
     "   _validate_synthesis() checks which domains had curated signals but are\n"
     "   absent from top_signals, key_risks, and dominant_themes. Also checks\n"
     "   actionable_intelligence is non-empty. _repair_synthesis() runs a targeted\n"
     "   second LLM call to patch only the identified gaps. Both wired into main()\n"
     "   after the primary synthesis call.\n\n"
     "Also adds SYNTHESIS_JSON_SCHEMA (strict JSON Schema) for future OpenAI\n"
     "Structured Outputs support — used only when LLM_PROVIDER=openai."],
    ["git", "push"],
]:
    r = subprocess.run(cmd, cwd=repo, capture_output=True, text=True)
    out = (r.stdout + r.stderr).strip()
    if out:
        print(out)
    if r.returncode != 0:
        sys.exit(r.returncode)

print("Done.")
