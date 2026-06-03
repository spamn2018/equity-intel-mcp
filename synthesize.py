#!/usr/bin/env python3
"""
synthesize.py -- Equity Intelligence Synthesizer (standalone, second-pass)

Reads brief_YYYYMMDD.json files produced by equity-run-daily-brief,
aggregates catalyst data across them, then runs the local LLM (LM Studio)
to synthesize patterns, rank signals, and surface actionable intelligence.

This is a standalone single-file script. No extra modules needed beyond
the packages already installed in the project venv.

Usage:
    python synthesize.py                    # reads from briefs/ folder
    python synthesize.py --days 14          # last 14 days only
    python synthesize.py --scan-only        # list files, no LLM
    python synthesize.py --aggregate-only   # show aggregated data, no LLM
    python synthesize.py --list-models      # show loaded LM Studio models
    python synthesize.py --folder PATH      # custom briefs folder
    python synthesize.py --model MODEL_ID   # override LM Studio model
"""

from __future__ import annotations

import argparse
import concurrent.futures
import copy
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import date as date_type, datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(override=True)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

HERE          = Path(__file__).parent.resolve()
BRIEFS_DIR    = HERE / "briefs"
OUTPUT_DIR    = HERE / "intelligence"
PROJECT_NAME  = "Stocks"
SRC_DIR       = HERE / "src"

if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from equity_intel.lmstudio_runtime import (
    note_model_usage,
    register_atexit_unload,
    wait_for_local_model_capacity,
)

SOLO_INTEL_DIR = Path(
    r"C:\Users\noleg\Desktop\Claude\Projects\SOLO BUILDS\solo-intel\intelligence"
)
DOMAIN_CTX    = (
    "Equity catalyst intelligence across a tracked watchlist. "
    "Focus on: which tickers keep appearing with high materiality, what event "
    "types are dominating (earnings, M&A, dilution, regulatory), which tickers "
    "have confirmed price moves tied to catalysts, what risks (restatement, "
    "going concern, litigation) are surfacing, and what actionable signals "
    "emerge from the cross-brief aggregate. US equities only."
)

# LM Studio
LLM_PROVIDER   = os.getenv("LLM_PROVIDER", "lmstudio").lower()
BASE_URL       = (
    "https://api.openai.com/v1"
    if LLM_PROVIDER == "openai"
    else os.getenv("LMSTUDIO_BASE_URL", "http://127.0.0.1:1234/v1")
)
DEFAULT_MODEL  = os.getenv("LMSTUDIO_MODEL",          "qwen/qwen3-14b")
FALLBACK_MODEL = os.getenv("LMSTUDIO_FALLBACK_MODEL", "qwen/qwen3-4b")
API_KEY        = (
    os.getenv("OPENAI_API_KEY", "lm-studio") if LLM_PROVIDER == "openai" else "lm-studio"
)
LMS_CLI        = os.getenv("LMS_CLI", r"C:\Users\noleg\.lmstudio\bin\lms.exe")
LMS_CONTEXT    = int(os.getenv("LMSTUDIO_CONTEXT", "16384"))
LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_OUTPUT_TOKENS", "6000"))

register_atexit_unload("stocks-second-pass-synthesis")

# Probe lms.exe once at import time â€” catches architecture mismatches before
# subprocess.run can trigger a Windows "This app can't run on your PC" dialog.
def _probe_lms_cli() -> bool:
    try:
        r = subprocess.run(
            [LMS_CLI, "--version"],
            capture_output=True, timeout=10,
        )
        return True
    except (OSError, PermissionError, FileNotFoundError):
        return False

LMS_CLI_AVAILABLE: bool = (
    LLM_PROVIDER != "openai"
    and os.path.isfile(LMS_CLI)
    and _probe_lms_cli()
)

RISK_EVENT_TYPES = {
    "bankruptcy_or_going_concern", "restatement", "litigation",
    "regulatory", "offering_or_dilution", "management_change",
    "unusual_price_volume",
}

# Dashboard API base â€” started at step 0, always running by step 12
DASHBOARD_API = os.getenv("DASHBOARD_API", "http://127.0.0.1:5173")

DEFAULT_TICKERS = [
    t.strip().upper()
    for t in os.getenv("DEFAULT_TICKERS", "POWL,ETN,VST,NEE,ANET,MRVL,AMAT,LRCX,KLAC,MU,EQIX,DLR,IRM,MP,USAR,UUUU,QCOM,ON,CSCO,FSLR").split(",")
    if t.strip()
]


# ---------------------------------------------------------------------------
# Domain classification (maps equity event types â†’ synthesis domains)
# ---------------------------------------------------------------------------

EQUITY_DOMAINS = [
    "earnings_guidance",   # earnings, guidance, analyst_rating
    "corporate_action",    # M&A, buyback, dividend, dilution, insider, activist
    "risk_legal",          # litigation, regulatory, bankruptcy, restatement, mgmt change
    "product_macro",       # product_announcement, macro_sensitive_news
    "market_technical",    # unusual_price_volume, other
]

EVENT_TYPE_TO_DOMAIN: dict[str, str] = {
    "earnings":                    "earnings_guidance",
    "guidance":                    "earnings_guidance",
    "analyst_rating":              "earnings_guidance",
    "merger_acquisition":          "corporate_action",
    "buyback":                     "corporate_action",
    "dividend":                    "corporate_action",
    "offering_or_dilution":        "corporate_action",
    "activist_stake":              "corporate_action",
    "insider_transaction":         "corporate_action",
    "litigation":                  "risk_legal",
    "regulatory":                  "risk_legal",
    "bankruptcy_or_going_concern": "risk_legal",
    "restatement":                 "risk_legal",
    "management_change":           "risk_legal",
    "product_announcement":        "product_macro",
    "macro_sensitive_news":        "product_macro",
    "unusual_price_volume":        "market_technical",
    "other":                       "market_technical",
}


def _score_catalyst(c: dict) -> float:
    """Score a key_event dict for signal selection priority."""
    mat  = float(c.get("materiality", 0))
    conf = float(c.get("confidence", 0))
    score = mat * 0.6 + conf * 0.3
    # Bonus for confirmed price moves
    if c.get("price_move"):
        score += 0.08
    # Bonus for risk events (actionable)
    if c.get("event_type") in RISK_EVENT_TYPES:
        score += 0.05
    # Penalty for very short why text (likely low-signal)
    why = c.get("why") or c.get("title") or ""
    if len(why) < 20:
        score -= 0.10
    return min(score, 1.0)


def _select_balanced_signals(agg: dict, max_per_domain: int = 3) -> list[dict]:
    """
    Curate a domain-balanced set of top signals from the aggregated brief data.

    Mirrors weekly_synthesis.py's _select_balanced_signals() pattern:
    pick the highest-scoring signals up to max_per_domain per domain,
    prioritising breadth (unique tickers) over depth.
    """
    candidates = []
    seen_ticker_domain: dict[str, int] = {}

    for event in agg.get("key_events", []):
        evt_type = event.get("event_type", "other")
        domain = EVENT_TYPE_TO_DOMAIN.get(evt_type, "market_technical")
        score = _score_catalyst(event)
        candidates.append({**event, "domain": domain, "score": score})

    # Also promote tickers with high avg_materiality that don't appear in key_events
    ticker_syms_in_events = {c["ticker"] for c in candidates}
    for t in agg.get("tickers", [])[:15]:
        if t["symbol"] in ticker_syms_in_events:
            continue
        if t["avg_materiality"] < 0.5:
            continue
        evt_type = t.get("dominant_event_type", "other")
        domain = EVENT_TYPE_TO_DOMAIN.get(evt_type, "market_technical")
        ctx_list = t.get("contexts", [{}])
        latest_ctx = ctx_list[0] if ctx_list else {}
        candidates.append({
            "ticker":     t["symbol"],
            "event_type": evt_type,
            "title":      latest_ctx.get("title", ""),
            "why":        latest_ctx.get("why", ""),
            "materiality": t["avg_materiality"],
            "confidence":  t["avg_confidence"],
            "date":        latest_ctx.get("date", ""),
            "source_links": t.get("source_links", [])[:2],
            "domain":     domain,
            "score":      t["avg_materiality"] * 0.5,
        })

    # Group by domain, sort by score, pick top N per domain (prefer unique tickers)
    by_domain: dict[str, list] = {d: [] for d in EQUITY_DOMAINS}
    for c in candidates:
        if c["domain"] in by_domain:
            by_domain[c["domain"]].append(c)

    selected = []
    for domain, sigs in by_domain.items():
        if not sigs:
            continue
        sigs.sort(key=lambda x: -x["score"])
        chosen: list[dict] = []
        tickers_used: set[str] = set()
        # First pass: one per ticker
        for s in sigs:
            if len(chosen) >= max_per_domain:
                break
            if s["ticker"] not in tickers_used:
                tickers_used.add(s["ticker"])
                chosen.append(s)
        # Second pass: fill remaining slots
        for s in sigs:
            if len(chosen) >= max_per_domain:
                break
            if s not in chosen:
                chosen.append(s)
        for s in chosen:
            s["support_level"] = (
                "multi_source" if len({x["ticker"] for x in chosen}) >= 2
                else "single_source_strong"
            )
        selected.extend(chosen)

    selected.sort(key=lambda x: -x["score"])
    return selected


# ===========================================================================
# LM STUDIO CLIENT
# ===========================================================================

class LLMHangError(RuntimeError):
    pass


def _lms(*args, timeout=180):
    if not LMS_CLI_AVAILABLE:
        raise RuntimeError(
            "lms.exe is not available on this system (architecture mismatch or missing). "
            "Load your model manually in LM Studio before running synthesis."
        )
    result = subprocess.run(
        [LMS_CLI, *args], capture_output=True, encoding='utf-8', errors='replace', timeout=timeout
    )
    if result.returncode != 0:
        raise RuntimeError(f"lms {' '.join(args)} failed: {(result.stderr or '').strip()}")
    return (result.stdout or '').strip()


def _load_model(model):
    if not LMS_CLI_AVAILABLE:
        print(
            f"  [LMS] lms.exe unavailable â€” skipping headless load of '{model}'.\n"
            f"  Assuming model is already loaded in LM Studio GUI."
        )
        return
    wait_for_local_model_capacity("stocks-synthesize", model)
    note_model_usage(model)
    print(f"  Loading {model} (context {LMS_CONTEXT})...", end="", flush=True)
    _lms("unload", "--all", timeout=15)
    time.sleep(1)
    load_args = ["load", "-c", str(LMS_CONTEXT), "-y"]
    gpu_layers = os.getenv("LMSTUDIO_GPU_LAYERS", "").strip()
    if gpu_layers:
        load_args += ["--gpu-layers", gpu_layers]
    load_args.append(model)
    _lms(*load_args, timeout=180)
    time.sleep(2)
    print(" done")


def _loaded_models():
    try:
        r = requests.get(
            f"{BASE_URL}/models",
            headers={"Authorization": f"Bearer {API_KEY}"},
            timeout=5,
        )
        if r.status_code == 200:
            return [m["id"] for m in r.json().get("data", [])]
    except Exception:
        pass
    return []


def list_models():
    r = requests.get(
        f"{BASE_URL}/models",
        headers={"Authorization": f"Bearer {API_KEY}"},
        timeout=10,
    )
    r.raise_for_status()
    return [m["id"] for m in r.json().get("data", [])]


def _ensure_model(model):
    if LLM_PROVIDER == "openai":
        return model
    wait_for_local_model_capacity("stocks-synthesize", model)
    note_model_usage(model)
    if model in _loaded_models():
        return model
    print(f"  [LMS] '{model}' not loaded -- loading headlessly...")
    _load_model(model)
    return model


def _strip_think(text):
    return re.sub(r"<think>[\s\S]*?</think>", "", text).strip()


def _extract_json(text):
    text = _strip_think(text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", text)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    s, e = text.find("{"), text.rfind("}")
    if s != -1 and e > s:
        try:
            return json.loads(text[s: e + 1])
        except json.JSONDecodeError:
            pass
    # Last resort: truncated JSON â€” close any open braces/brackets and retry
    if s != -1:
        fragment = text[s:]
        open_b = fragment.count("{") - fragment.count("}")
        open_sq = fragment.count("[") - fragment.count("]")
        # close any open string first (odd number of unescaped quotes)
        in_str = False
        for ch in fragment:
            if ch == '"':
                in_str = not in_str
        closing = ('"' if in_str else "") + ("]" * max(0, open_sq)) + ("}" * max(0, open_b))
        try:
            return json.loads(fragment + closing)
        except json.JSONDecodeError:
            pass
    raise ValueError(f"Could not extract JSON. Output starts:\n{text[:400]}")


def _complete(messages, model, temperature=0.1, max_tokens=2500, json_mode=False):
    model = _ensure_model(model)
    # Per-token idle timeout: if no new token arrives within this many seconds,
    # treat as a hang. Reduced from 300 â†’ 90 to catch stalled inference quickly.
    idle_s = int(os.getenv("LLM_TOKEN_IDLE_TIMEOUT_SECONDS", "90"))
    # Hard wall-clock cap on the entire generation. Prevents the machine from
    # freezing when a large model runs out of VRAM/RAM and swaps heavily.
    total_s = int(os.getenv("LLM_TOTAL_TIMEOUT_SECONDS", "480"))  # 8 min
    payload = {
        "model": model, "messages": messages, "stream": True,
    }
    model_lower = model.lower()
    uses_completion_token_param = (
        LLM_PROVIDER == "openai"
        and (model_lower.startswith(("o1", "o3", "o4", "gpt-5")))
    )
    if uses_completion_token_param:
        payload["max_completion_tokens"] = max_tokens
    else:
        payload["temperature"] = temperature
        payload["max_tokens"] = max_tokens

    if LLM_PROVIDER != "openai":
        payload["context_length"] = LMS_CONTEXT
        payload["ttl"] = int(os.getenv("LMSTUDIO_TTL_SECONDS", "60"))
    # Grammar-constrained JSON generation â€” supported by LM Studio 0.3+ and OpenAI
    if json_mode:
        payload["response_format"] = {"type": "json_object" if LLM_PROVIDER == "openai" else "text"}

    headers = {"Authorization": f"Bearer {API_KEY}"}
    url = f"{BASE_URL}/chat/completions"

    class _Http400(Exception):
        def __init__(self, body):
            self.body = body

    def _call():
        resp = requests.post(url, headers=headers, json=payload, stream=True,
                             timeout=(10, idle_s))
        if resp.status_code == 400:
            body = resp.text
            resp.close()
            raise _Http400(body)
        try:
            resp.raise_for_status()
            chunks = []
            for raw in resp.iter_lines():
                if not raw:
                    continue
                line = (raw.decode("utf-8", errors="replace")
                        if isinstance(raw, bytes) else raw)
                if not line.startswith("data: "):
                    continue
                data = line[6:]
                if data.strip() == "[DONE]":
                    break
                try:
                    delta = json.loads(data)["choices"][0]["delta"].get("content", "")
                    if delta:
                        chunks.append(delta)
                except (json.JSONDecodeError, KeyError, IndexError):
                    pass
            return "".join(chunks)
        except requests.exceptions.ReadTimeout:
            raise LLMHangError(f"LM Studio hang: no token for {idle_s}s.")
        finally:
            resp.close()

    def _guarded_call():
        """Run _call() inside a thread so we can enforce a hard wall-clock limit."""
        try:
            return _strip_think(_call())
        except _Http400 as e:
            if LLM_PROVIDER == "openai":
                raise RuntimeError(f"OpenAI 400: {e.body[:400]}")
            print(f"  [LMS] 400 -- reloading and retrying. Body: {e.body[:200]}")
            _load_model(model)
            return _strip_think(_call())

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as _ex:
            _fut = _ex.submit(_guarded_call)
            try:
                return _fut.result(timeout=total_s)
            except concurrent.futures.TimeoutError:
                raise LLMHangError(
                    f"LLM synthesis exceeded the {total_s}s wall-clock limit "
                    f"(LLM_TOTAL_TIMEOUT_SECONDS). "
                    "LM Studio may be RAM-starved. "
                    "Try: smaller model, fewer pipeline steps before synthesis, "
                    "or set LLM_TOTAL_TIMEOUT_SECONDS=600 for a longer cap."
                )
    except LLMHangError:
        raise
    except requests.exceptions.ConnectionError:
        raise RuntimeError(f"Cannot connect to LM Studio at {BASE_URL}.")
    except requests.exceptions.HTTPError as e:
        raise RuntimeError(
            f"LM Studio HTTP {e.response.status_code}: {e.response.text[:400]}"
        )


def _complete_json(messages, model, temperature=0.1, max_tokens=2500):
    strict = (
        "\n\nCRITICAL: Your response must be ONLY a valid JSON object. "
        "No markdown, no explanation, no code fences. Start with { and end with }."
    )

    def _append(msgs, suffix):
        msgs = copy.deepcopy(msgs)
        for m in reversed(msgs):
            if m["role"] == "user":
                m["content"] += suffix
                break
        return msgs

    attempts = [
        (model, messages, temperature),
        (model, _append(messages, strict), 0.05),
    ]
    if LLM_PROVIDER != "openai" or FALLBACK_MODEL.lower().startswith(("gpt-", "o1", "o3", "o4")):
        attempts.append((FALLBACK_MODEL, _append(messages, strict), 0.05))
    last_err = ValueError("no attempts")
    for m, msgs, t in attempts:
        try:
            raw = _complete(msgs, m, t, max_tokens, json_mode=True)
            return _extract_json(raw), m
        except (ValueError, RuntimeError) as e:
            last_err = e
            print(f"  [LMS] JSON failed ({m}): {e}")
    raise ValueError(f"All attempts failed. Last: {last_err}")


# ===========================================================================
# AGGREGATOR
# ===========================================================================

@dataclass
class _Ticker:
    symbol: str
    company_name: str
    sector: str
    mentions: int = 0
    materiality_scores: list = field(default_factory=list)
    confidence_scores: list = field(default_factory=list)
    event_types: list = field(default_factory=list)
    contexts: list = field(default_factory=list)
    source_links: list = field(default_factory=list)
    has_price_move: bool = False

    @property
    def avg_mat(self):
        return (round(sum(self.materiality_scores) / len(self.materiality_scores), 3)
                if self.materiality_scores else 0.0)

    @property
    def avg_conf(self):
        return (round(sum(self.confidence_scores) / len(self.confidence_scores), 3)
                if self.confidence_scores else 0.0)

    @property
    def dominant_type(self):
        if not self.event_types:
            return "unknown"
        counts = {}
        for t in self.event_types:
            counts[t] = counts.get(t, 0) + 1
        return max(counts, key=counts.__getitem__)

    def to_dict(self):
        return {
            "symbol":             self.symbol,
            "company_name":       self.company_name,
            "sector":             self.sector,
            "mentions":           self.mentions,
            "avg_materiality":    self.avg_mat,
            "avg_confidence":     self.avg_conf,
            "event_types":        sorted(set(self.event_types)),
            "dominant_event_type": self.dominant_type,
            "has_price_move":     self.has_price_move,
            "contexts":           self.contexts[:5],
            "source_links":       self.source_links[:5],
        }


def _load_podcast_intel() -> str:
    """
    Load the most recent solo-intel intelligence JSON produced against
    Podcasts Pull, filter to equity tickers, return a formatted text block.
    Returns empty string if nothing is available.
    """
    try:
        if not SOLO_INTEL_DIR.exists():
            return ""
        files = sorted(
            SOLO_INTEL_DIR.glob("*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not files:
            return ""
        data = json.loads(files[0].read_text(encoding="utf-8"))
        lines = []
        generated = data.get("generated_at", "")[:19].replace("T", " ")
        lines.append(f"Source file : {files[0].name}  (generated {generated})")
        if data.get("one_sentence_takeaway"):
            lines.append(f"Takeaway    : {data['one_sentence_takeaway']}")
        lines.append("")
        signals = data.get("top_signals", [])
        if signals:
            lines.append("Top signals from podcasts:")
            for s in signals[:10]:
                asset     = s.get("asset", "")
                signal    = s.get("signal", "").upper()
                conviction = s.get("conviction", "")
                why       = s.get("why", "")
                pattern   = s.get("mention_pattern", "")
                lines.append(
                    f"  {asset:<6} [{signal:<8}] conviction={conviction}"
                    f" | {pattern}"
                )
                if why:
                    lines.append(f"    {why[:140]}")
            lines.append("")
        risks = [r for r in data.get("key_risks", []) if r.get("severity") == "high"]
        if risks:
            lines.append("High risks flagged in podcasts:")
            for r in risks[:5]:
                lines.append(f"  [HIGH] {r.get('risk','')[:140]}")
            lines.append("")
        actions = data.get("actionable_intelligence", [])
        if actions:
            lines.append("Podcast actionable intelligence:")
            for a in actions[:4]:
                urg    = a.get("urgency", "?").upper()
                action = a.get("action", "")
                rat    = a.get("rationale", "")
                lines.append(f"  [{urg}] {action[:120]}")
                if rat:
                    lines.append(f"    {rat[:120]}")
            lines.append("")
        return "\n".join(lines)
    except Exception:
        return ""


def _load_gemini_news() -> str:
    """
    Load the most recent gemini_news_*.json from the intelligence/ folder.
    Returns a formatted text block, or empty string if unavailable.
    """
    try:
        if not OUTPUT_DIR.exists():
            return ""
        files = sorted(
            OUTPUT_DIR.glob("gemini_news_*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not files:
            return ""
        data    = json.loads(files[0].read_text(encoding="utf-8"))
        news    = data.get("news", {})
        gen_at  = data.get("generated_at", "")[:19].replace("T", " ")
        sources = data.get("grounding_sources", [])
        if not news or "_raw" in news:
            return ""
        lines = []
        lines.append(f"Source file : {files[0].name}  (generated {gen_at})")
        lines.append(f"Grounding   : {len(sources)} web source(s)")
        lines.append("")
        for sym, info in news.items():
            if not isinstance(info, dict):
                continue
            sent     = info.get("sentiment", "?")
            catalyst = (info.get("key_catalyst") or "").strip()
            summary  = (info.get("summary") or "").strip()
            lines.append(f"  {sym:<6} [{sent}]")
            if catalyst:
                lines.append(f"    Key catalyst : {catalyst[:160]}")
            if summary:
                lines.append(f"    Summary      : {summary[:200]}")
            headlines = info.get("headlines", [])
            for h in headlines[:3]:
                lines.append(f"    Â· {str(h)[:140]}")
            lines.append("")
        return "\n".join(lines)
    except Exception:
        return ""


def _fetch_api_brief(tickers=None, days=7):
    """
    Pull live catalyst data from the running EquityIntel dashboard API.

    Used as a fallback when briefs/ contains no brief_*.json files â€” the
    dashboard is always started at step 0, so it is online by the time
    synthesize.py runs at step 12.

    Returns a list containing one brief dict (same structure as brief_*.json),
    or an empty list if the API is unreachable.
    """
    import urllib.parse
    tickers_str = ",".join(tickers or DEFAULT_TICKERS)
    params = urllib.parse.urlencode({
        "tickers":  tickers_str,
        "days":     days,
        "min_mat":  "0.2",
        "max_items": "150",
    })
    url = f"{DASHBOARD_API}/api/brief?{params}"
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        # Inject generated_at so date logic works downstream
        if "generated_at" not in data:
            data["generated_at"] = datetime.now(timezone.utc).isoformat()
        data.setdefault("source", "api")
        print(f"  [api] Fetched {data.get('total_catalysts', 0)} catalysts "
              f"from {DASHBOARD_API} (days={days})")
        return [data]
    except Exception as exc:
        print(f"  [warn] Dashboard API unreachable ({url}): {exc}")
        return []


def _find_briefs(folder):
    files = list(folder.rglob("brief_*.json"))
    files.sort(key=lambda p: p.name, reverse=True)
    return files


def _read_brief(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _brief_date(data, path=None):
    ga = data.get("generated_at", "")
    if ga and len(ga) >= 10:
        return ga[:10]
    if path is not None:
        stem = path.stem  # brief_20260524
        if stem.startswith("brief_") and len(stem) >= 14:
            raw = stem[6:]
            return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    return ""


def aggregate(brief_files, days=None):
    cutoff = None
    if days is not None:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date()

    tickers = {}
    evt_counts = {}
    key_events = []
    risks = []
    takeaways = []
    briefs_read = []
    dates = []

    for item in brief_files:
        # Accept either a Path (file on disk) or a pre-loaded dict (from API)
        if isinstance(item, dict):
            data = item
            path = None
        else:
            data = _read_brief(item)
            path = item
        if not data:
            continue

        bdate = _brief_date(data, path)
        if cutoff and bdate:
            try:
                if date_type.fromisoformat(bdate) < cutoff:
                    continue
            except ValueError:
                pass

        briefs_read.append({
            "brief_date":      bdate,
            "watchlist":       data.get("watchlist", []),
            "total_catalysts": data.get("total_catalysts", 0),
            "source_file":     str(path) if path else data.get("source", "api"),
        })
        if bdate:
            dates.append(bdate)

        summary = data.get("brief_summary", "")
        if summary:
            takeaways.append({"takeaway": summary, "date": bdate})

        for c in data.get("catalysts", []):
            sym = (c.get("ticker") or "").strip().upper()
            if not sym:
                continue

            if sym not in tickers:
                tickers[sym] = _Ticker(
                    symbol=sym,
                    company_name=c.get("company_name") or "",
                    sector=c.get("sector") or "",
                )
            t = tickers[sym]
            t.mentions += 1

            mat = c.get("materiality_score")
            if mat is not None:
                t.materiality_scores.append(float(mat))
            conf = c.get("confidence_score")
            if conf is not None:
                t.confidence_scores.append(float(conf))

            evt = (c.get("event_type") or "").strip()
            if evt:
                t.event_types.append(evt)
                evt_counts[evt] = evt_counts.get(evt, 0) + 1

            why   = (c.get("why_it_matters") or "").strip()
            title = (c.get("title") or "").strip()
            if why or title:
                t.contexts.append({
                    "date": bdate, "event_type": evt,
                    "title": title, "why": why,
                })

            for link in (c.get("source_links") or []):
                if link and link not in t.source_links:
                    t.source_links.append(link)

            if c.get("price_move"):
                t.has_price_move = True

            if (mat or 0) >= 0.5:
                key_events.append({
                    "ticker":       sym,
                    "event_type":   evt,
                    "event_subtype": c.get("event_subtype") or "",
                    "title":        title,
                    "why":          why,
                    "materiality":  float(mat or 0),
                    "confidence":   float(conf or 0),
                    "date":         bdate,
                    "source_links": (c.get("source_links") or [])[:3],
                })

            if evt in RISK_EVENT_TYPES:
                sev = float(mat or 0)
                risks.append({
                    "risk":       title or why,
                    "ticker":     sym,
                    "event_type": evt,
                    "severity":   ("high" if sev >= 0.7
                                   else "medium" if sev >= 0.4 else "low"),
                    "date":       bdate,
                })

    sorted_tickers = sorted(
        [t.to_dict() for t in tickers.values()],
        key=lambda x: (-x["mentions"], -x["avg_materiality"]),
    )
    sorted_evt = sorted(evt_counts.items(), key=lambda x: -x[1])
    sorted_events = sorted(key_events, key=lambda x: -x["materiality"])
    sorted_risks = sorted(
        risks,
        key=lambda x: {"high": 3, "medium": 2, "low": 1}.get(x["severity"], 0),
        reverse=True,
    )

    return {
        "briefs_read":  briefs_read,
        "brief_count":  len(briefs_read),
        "date_range":   {
            "earliest": min(dates) if dates else None,
            "latest":   max(dates) if dates else None,
        },
        "tickers":      sorted_tickers,
        "event_types":  [{"event_type": t, "count": c} for t, c in sorted_evt],
        "key_events":   sorted_events[:40],
        "risks":        sorted_risks[:20],
        "takeaways":    takeaways,
    }


def format_for_llm(agg):
    lines = []
    lines.append(f"PROJECT: {PROJECT_NAME}")
    lines.append(f"DOMAIN: {DOMAIN_CTX}")
    lines.append(f"DAILY BRIEFS ANALYZED: {agg['brief_count']}")
    dr = agg["date_range"]
    if dr["earliest"]:
        lines.append(f"DATE RANGE: {dr['earliest']} to {dr['latest']}")
    lines.append("")

    if agg["tickers"]:
        lines.append("-- TICKERS (by mention frequency x materiality) --")
        for t in agg["tickers"][:30]:
            name = t["company_name"] or t["symbol"]
            pflag = " [PRICE MOVE]" if t["has_price_move"] else ""
            lines.append(
                f"  {t['symbol']} ({name}) | sector={t['sector']} |"
                f" {t['mentions']}x | mat={t['avg_materiality']} |"
                f" conf={t['avg_confidence']} | type={t['dominant_event_type']}{pflag}"
            )
            for ctx in t["contexts"][:2]:
                lines.append(f"    [{ctx['date']}] {ctx['title']}")
                if ctx["why"]:
                    lines.append(f"      {ctx['why'][:130]}")
        lines.append("")

    if agg["event_types"]:
        lines.append("-- EVENT TYPES (by frequency) --")
        for et in agg["event_types"][:15]:
            lines.append(f"  {et['event_type']} ({et['count']}x)")
        lines.append("")

    if agg["key_events"]:
        lines.append("-- HIGH-MATERIALITY EVENTS --")
        for e in agg["key_events"][:25]:
            sub = f"/{e['event_subtype']}" if e["event_subtype"] else ""
            lines.append(
                f"  [{e['date']} | {e['ticker']}]"
                f" [{e['event_type']}{sub}] {e['title']}"
            )
            if e["why"]:
                lines.append(f"    {e['why'][:160]}")
        lines.append("")

    if agg["risks"]:
        lines.append("-- RISKS --")
        for r in agg["risks"][:15]:
            lines.append(
                f"  [{r['severity'].upper()} | {r['ticker']}]"
                f" [{r['event_type']}] {r['risk'][:160]}"
            )
        lines.append("")

    if agg["takeaways"]:
        lines.append("-- DAILY BRIEF SUMMARIES --")
        for t in agg["takeaways"][:7]:
            lines.append(f"  [{t['date']}] {t['takeaway']}")
        lines.append("")

    # â”€â”€ Podcast intelligence (solo-intel second-pass on Podcasts Pull) â”€â”€
    podcast_ctx = _load_podcast_intel()
    if podcast_ctx:
        lines.append("-- PODCAST INTELLIGENCE (solo-intel / Podcasts Pull) --")
        lines.append(podcast_ctx)
        lines.append("")
    else:
        lines.append("-- PODCAST INTELLIGENCE -- [not available]")
        lines.append("")

    # â”€â”€ Real-time news (Gemini Flash + Google Search grounding) â”€â”€â”€â”€â”€â”€â”€â”€â”€
    gemini_ctx = _load_gemini_news()
    if gemini_ctx:
        lines.append("-- REAL-TIME NEWS (Gemini Flash / Google Search) --")
        lines.append("-- REAL-TIME NEWS (Gemini Flash / Google Search) --")
        lines.append(gemini_ctx)
        lines.append("")
    else:
        lines.append("-- REAL-TIME NEWS -- [not available]")
        lines.append("")

    return "\n".join(lines)


# ===========================================================================
# SYNTHESIS SCHEMA + PROMPT
# ===========================================================================

SYNTHESIS_SCHEMA = {
    "one_sentence_takeaway": "string -- the single most important signal",
    "summary": "string -- 3-5 paragraph synthesis",
    "dominant_themes": [
        {"theme": "str", "strength": "high|medium|low", "evidence": "str"}
    ],
    "top_signals": [
        {
            "asset": "ticker symbol",
            "signal": "bullish|bearish|neutral|mixed",
            "conviction": "high|medium|low",
            "mention_pattern": "str",
            "why": "str",
        }
    ],
    "converging_theses": [
        {"thesis": "str", "sources": "str", "bull_case": "str", "bear_case": "str"}
    ],
    "diverging_views": [
        {"topic": "str", "view_a": "str", "view_b": "str", "sources": "str"}
    ],
    "key_risks": [
        {"risk": "str", "severity": "high|medium|low", "frequency": "str"}
    ],
    "actionable_intelligence": [
        {"action": "str", "urgency": "high|medium|low", "rationale": "str"}
    ],
    "notable_quotes": [
        {
            "quote": "str",
            "speaker": "str",
            "source": "ticker and date",
            "why_it_matters": "str",
        }
    ],
    "tags": ["str"],
}
SYNTHESIS_SCHEMA_JSON = json.dumps(SYNTHESIS_SCHEMA, indent=2)

# Strict JSON Schema passed for OpenAI Structured Outputs.
# For LM Studio we fall back to the json_object response_format + retry path.
SYNTHESIS_JSON_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "one_sentence_takeaway", "summary", "dominant_themes",
        "top_signals", "converging_theses", "diverging_views",
        "key_risks", "actionable_intelligence", "notable_quotes", "tags",
    ],
    "properties": {
        "one_sentence_takeaway": {"type": "string"},
        "summary":               {"type": "string"},
        "dominant_themes": {
            "type": "array",
            "items": {
                "type": "object", "additionalProperties": False,
                "required": ["theme", "strength", "evidence"],
                "properties": {
                    "theme":    {"type": "string"},
                    "strength": {"type": "string", "enum": ["high", "medium", "low"]},
                    "evidence": {"type": "string"},
                },
            },
        },
        "top_signals": {
            "type": "array",
            "items": {
                "type": "object", "additionalProperties": False,
                "required": ["asset", "signal", "conviction", "mention_pattern", "why"],
                "properties": {
                    "asset":           {"type": "string"},
                    "signal":          {"type": "string", "enum": ["bullish", "bearish", "neutral", "mixed"]},
                    "conviction":      {"type": "string", "enum": ["high", "medium", "low"]},
                    "mention_pattern": {"type": "string"},
                    "why":             {"type": "string"},
                },
            },
        },
        "converging_theses": {
            "type": "array",
            "items": {
                "type": "object", "additionalProperties": False,
                "required": ["thesis", "sources", "bull_case", "bear_case"],
                "properties": {
                    "thesis":    {"type": "string"},
                    "sources":   {"type": "string"},
                    "bull_case": {"type": "string"},
                    "bear_case": {"type": "string"},
                },
            },
        },
        "diverging_views": {
            "type": "array",
            "items": {
                "type": "object", "additionalProperties": False,
                "required": ["topic", "view_a", "view_b", "sources"],
                "properties": {
                    "topic":   {"type": "string"},
                    "view_a":  {"type": "string"},
                    "view_b":  {"type": "string"},
                    "sources": {"type": "string"},
                },
            },
        },
        "key_risks": {
            "type": "array",
            "items": {
                "type": "object", "additionalProperties": False,
                "required": ["risk", "severity", "frequency"],
                "properties": {
                    "risk":      {"type": "string"},
                    "severity":  {"type": "string", "enum": ["high", "medium", "low"]},
                    "frequency": {"type": "string"},
                },
            },
        },
        "actionable_intelligence": {
            "type": "array",
            "items": {
                "type": "object", "additionalProperties": False,
                "required": ["action", "urgency", "rationale"],
                "properties": {
                    "action":    {"type": "string"},
                    "urgency":   {"type": "string", "enum": ["high", "medium", "low"]},
                    "rationale": {"type": "string"},
                },
            },
        },
        "notable_quotes": {
            "type": "array",
            "items": {
                "type": "object", "additionalProperties": False,
                "required": ["quote", "speaker", "source", "why_it_matters"],
                "properties": {
                    "quote":          {"type": "string"},
                    "speaker":        {"type": "string"},
                    "source":         {"type": "string"},
                    "why_it_matters": {"type": "string"},
                },
            },
        },
        "tags": {"type": "array", "items": {"type": "string"}},
    },
}

SYSTEM_PROMPT = (
    "You are a senior equity research analyst. You receive pre-aggregated, "
    "structured data from multiple daily watchlist catalyst briefs produced by "
    "EquityIntel. Synthesize patterns across briefs, surface the most actionable "
    "signals, and produce a clean second-pass intelligence report. "
    "Be precise, conservative, and evidence-backed. Do not invent facts. "
    "Use cautious language: prefer 'likely related to', 'may reflect', 'appears to'. "
    "Never give investment advice or assert causation. "
    "Return only valid JSON matching the requested schema. "
    "/no_think"
)


def _format_selected_signals(selected_signals: list[dict]) -> str:
    """Format curated signals into a compact block for the LLM prompt."""
    if not selected_signals:
        return "(none)"
    lines = []
    for s in selected_signals:
        domain = s.get("domain", "")
        support = s.get("support_level", "single_source_strong")
        ticker = s.get("ticker", "")
        evt = s.get("event_type", "")
        date = s.get("date", "")
        mat = s.get("materiality", 0)
        conf = s.get("confidence", 0)
        lines.append(f"[{domain} | {support}] {ticker} / {evt} / {date} mat={mat:.2f} conf={conf:.2f}")
        title = s.get("title", "")
        if title:
            lines.append(f"  title: {title}")
        why = s.get("why", "")
        if why:
            lines.append(f"  why:   {why[:140]}")
    return "\n".join(lines)


def synthesize(aggregated_text: str, model: str, selected_signals: list[dict] | None = None):
    signals_block = _format_selected_signals(selected_signals or [])
    domain_counts: dict[str, int] = {}
    for s in (selected_signals or []):
        d = s.get("domain", "")
        domain_counts[d] = domain_counts.get(d, 0) + 1
    dc_str = ", ".join(f"{d}:{n}" for d, n in sorted(domain_counts.items()))
    if dc_str:
        print(f"    Signal domains: {dc_str}")

    user_content = (
        f"You are synthesizing equity intelligence from {PROJECT_NAME}.\n\n"
        "The data below was aggregated from multiple daily catalyst briefs. "
        "Find patterns across briefs, rank signals by materiality and frequency, "
        "and surface actionable intelligence.\n\n"
        f"DOMAIN-BALANCED SELECTED SIGNALS ({len(selected_signals or [])} total):\n"
        f"{signals_block}\n\n"
        f"FULL AGGREGATED DATA:\n{aggregated_text}\n\n"
        f"Return a JSON object matching this exact schema:\n{SYNTHESIS_SCHEMA_JSON}\n\n"
        "Rules:\n"
        "- DOMINANT THEMES: event types, sectors, or macro themes repeating across briefs. "
        "Cover all domains that had selected signals above.\n"
        "- TOP SIGNALS: tickers with strongest cross-brief signal (mentions Ã— materiality). "
        "Flag price moves. Aim for 4-6 signals.\n"
        "- CONVERGING THESES: directional arguments appearing across multiple tickers or briefs.\n"
        "- DIVERGING VIEWS: contradictory signals on the same ticker or theme.\n"
        "- RISKS: restatement, bankruptcy, dilution, litigation â€” by frequency and severity.\n"
        "- ACTIONABLE INTELLIGENCE: specific tickers and events to investigate next. "
        "Aim for 2-4 items.\n"
        "- Be specific: use names, numbers, exact claims. Return ONLY the JSON object.\n"
    )
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user",   "content": user_content},
    ]
    print(f"    Running synthesis ({len(aggregated_text):,} chars)...")
    return _complete_json(messages, model, max_tokens=LLM_MAX_TOKENS)


# ===========================================================================
# SYNTHESIS QUALITY VALIDATION & REPAIR
# ===========================================================================

_DROPPED_DOMAIN_RE = re.compile(r"Domain '([^']+)' had signals but is absent")


def _validate_synthesis(synthesis: dict, selected_signals: list[dict]) -> list[str]:
    """
    Check that all domains with curated signals are represented in the output.
    Returns a list of warning strings (empty = clean).
    """
    warnings: list[str] = []

    # Which domains had selected signals?
    signal_domains = {s.get("domain", "") for s in selected_signals if s.get("domain")}

    # Which domains are covered by top_signals and key_risks?
    covered_tickers = {s.get("asset", "").upper() for s in synthesis.get("top_signals", [])}
    covered_tickers |= {r.get("risk", "")[:10].upper() for r in synthesis.get("key_risks", [])}

    # Map covered tickers back to domains via selected_signals
    covered_domains: set[str] = set()
    for s in selected_signals:
        if s.get("ticker", "").upper() in covered_tickers:
            covered_domains.add(s.get("domain", ""))

    # Also treat any dominant_themes as covering their implicit domain
    for t in synthesis.get("dominant_themes", []):
        theme_text = (t.get("theme", "") + " " + t.get("evidence", "")).lower()
        for domain, keywords in {
            "earnings_guidance": ["earnings", "guidance", "analyst", "revenue", "eps"],
            "corporate_action":  ["merger", "acquisition", "buyback", "dividend", "dilution", "insider"],
            "risk_legal":        ["litigation", "regulatory", "bankruptcy", "restatement", "management"],
            "product_macro":     ["product", "launch", "macro", "tariff", "rate", "fed"],
            "market_technical":  ["volume", "price move", "unusual", "momentum"],
        }.items():
            if any(kw in theme_text for kw in keywords):
                covered_domains.add(domain)

    for domain in signal_domains:
        if domain and domain not in covered_domains:
            warnings.append(
                f"Domain '{domain}' had signals but is absent from top_signals, "
                "key_risks, and dominant_themes."
            )

    # Sanity: top_signals should have 4+ entries when data is available
    n_signals = len(synthesis.get("top_signals", []))
    if selected_signals and n_signals < 2:
        warnings.append(f"top_signals has only {n_signals} entries despite {len(selected_signals)} curated signals.")

    # actionable_intelligence should be non-empty
    if not synthesis.get("actionable_intelligence"):
        warnings.append("actionable_intelligence is empty.")

    return warnings


REPAIR_SCHEMA_JSON = json.dumps({
    "actionable_intelligence": [
        {"action": "str", "urgency": "high|medium|low", "rationale": "str"}
    ],
    "additional_top_signals": [
        {"asset": "ticker", "signal": "bullish|bearish|neutral|mixed",
         "conviction": "high|medium|low", "mention_pattern": "str", "why": "str"}
    ],
}, indent=2)


def _repair_synthesis(
    synthesis: dict,
    selected_signals: list[dict],
    warnings: list[str],
    model: str,
) -> dict:
    """
    Targeted second LLM call to patch gaps identified by _validate_synthesis().
    Mirrors weekly_synthesis.py's _repair_omitted_domains() pattern.
    """
    missing_domains = set()
    for w in warnings:
        m = _DROPPED_DOMAIN_RE.search(w)
        if m:
            missing_domains.add(m.group(1))

    needs_actions = any("actionable_intelligence is empty" in w for w in warnings)
    needs_signals = any("top_signals has only" in w for w in warnings)

    if not missing_domains and not needs_actions and not needs_signals:
        return synthesis  # nothing to repair

    md_str = ", ".join(sorted(missing_domains)) if missing_domains else "(none)"
    print(f"  [repair] running targeted repair â€” missing domains: {md_str}")

    relevant = [s for s in selected_signals if not missing_domains or s.get("domain") in missing_domains]
    sig_lines = []
    for s in relevant[:12]:
        sig_lines.append(
            f"[{s.get('domain')}] {s.get('ticker')} / {s.get('event_type')} / "
            f"{s.get('date')}: {s.get('title') or s.get('why', '')[:100]}"
        )
    signals_block = "\n".join(sig_lines) or "(none)"

    patch_prompt = (
        f"The equity synthesis is missing coverage for: {md_str}.\n\n"
        "Using ONLY the signals below, add:\n"
        "1. Any missing top_signals for uncovered domains (as additional_top_signals)\n"
        "2. 2-3 actionable_intelligence items if currently empty\n\n"
        f"SIGNALS TO COVER:\n{signals_block}\n\n"
        f"Return ONLY JSON matching:\n{REPAIR_SCHEMA_JSON}\n\n"
        "Do not invent facts. Use cautious language. Return ONLY the JSON object."
    )
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user",   "content": patch_prompt},
    ]
    try:
        patch, _ = _complete_json(messages, model, max_tokens=1200)
    except (ValueError, RuntimeError) as e:
        print(f"  [repair] WARNING: repair call failed: {e} â€” skipping.")
        return synthesis

    # Merge patch into synthesis
    extra_signals = patch.get("additional_top_signals", [])
    if extra_signals:
        synthesis.setdefault("top_signals", []).extend(extra_signals)
        print(f"  [repair] appended {len(extra_signals)} signal(s)")

    if needs_actions or not synthesis.get("actionable_intelligence"):
        new_actions = patch.get("actionable_intelligence", [])
        if new_actions:
            synthesis["actionable_intelligence"] = new_actions
            print(f"  [repair] set {len(new_actions)} actionable_intelligence item(s)")

    return synthesis


# ===========================================================================
# STORAGE
# ===========================================================================

def _slugify(name):
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def _run_id():
    return f"{_slugify(PROJECT_NAME)}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"


def _render_markdown(d):
    lines = []
    now    = d.get("generated_at", "")[:19].replace("T", " ")
    dr     = d.get("date_range", {})
    period = (f"{dr.get('earliest','')} to {dr.get('latest','')}"
              if dr.get("earliest") else "")

    lines.append(f"# Equity Intelligence Brief -- {PROJECT_NAME}")
    lines.append(f"\n**Generated:** {now}  ")
    lines.append(f"**Model:** {d.get('model_used','unknown')}  ")
    lines.append(f"**Briefs analyzed:** {d.get('brief_count', 0)}  ")
    if period:
        lines.append(f"**Period:** {period}  ")
    lines.append("")

    if d.get("one_sentence_takeaway"):
        lines.append(f"> **{d['one_sentence_takeaway']}**\n")

    def section(title, items, fmt):
        if not items:
            return
        lines.append(f"## {title}\n")
        for item in items:
            try:
                lines.append(f"- {fmt(item)}")
            except (KeyError, TypeError):
                lines.append(f"- {item}")
        lines.append("")

    if d.get("summary"):
        lines.append("## Summary\n")
        lines.append(d["summary"] + "\n")

    section("Dominant Themes", d.get("dominant_themes", []),
            lambda x: (f"**[{x.get('strength','').upper()}]** {x['theme']}\n"
                       f"  > {x.get('evidence','')}"))
    section("Top Signals", d.get("top_signals", []),
            lambda x: (f"**{x['asset']}** -- {x.get('signal','').upper()} "
                       f"({x.get('conviction','')}) | {x.get('mention_pattern','')}\n"
                       f"  > {x.get('why','')}"))
    section("Converging Theses", d.get("converging_theses", []),
            lambda x: (f"**{x['thesis']}**\n  Sources: {x.get('sources','')}\n"
                       f"  Bull: {x.get('bull_case','')}\n"
                       f"  Bear: {x.get('bear_case','')}"))
    section("Diverging Views", d.get("diverging_views", []),
            lambda x: (f"**{x['topic']}**\n"
                       f"  {x.get('view_a','')} vs. {x.get('view_b','')}\n"
                       f"  ({x.get('sources','')})"))
    section("Key Risks", d.get("key_risks", []),
            lambda x: (f"**[{x.get('severity','?').upper()}]** {x['risk']} "
                       f"-- {x.get('frequency','')}"))
    section("Actionable Intelligence", d.get("actionable_intelligence", []),
            lambda x: (f"**[{x.get('urgency','?').upper()}]** {x['action']}\n"
                       f"  > {x.get('rationale','')}"))
    section("Notable Quotes", d.get("notable_quotes", []),
            lambda x: (f"\"{x['quote']}\"\n"
                       f"  -- {x.get('speaker','')} | {x.get('source','')}\n"
                       f"  > {x.get('why_it_matters','')}"))

    if d.get("tags"):
        lines.append("## Tags\n")
        lines.append(" ".join(f"`{t}`" for t in d["tags"]) + "\n")

    if d.get("briefs"):
        lines.append("## Briefs Analyzed\n")
        for b in d["briefs"]:
            wl = ", ".join(b.get("watchlist", [])[:8])
            lines.append(
                f"- [{b.get('brief_date','')}]"
                f" {b.get('total_catalysts', 0)} catalysts -- {wl}"
            )
        lines.append("")

    return "\n".join(lines)


def save_report(intelligence, agg, model_used):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    run_id = _run_id()
    payload = {
        "source_project": PROJECT_NAME,
        "generated_at":   datetime.now().isoformat(),
        "model_used":     model_used,
        "brief_count":    agg.get("brief_count", 0),
        "date_range":     agg.get("date_range", {}),
        "briefs":         agg.get("briefs_read", []),
        **intelligence,
    }
    json_path = OUTPUT_DIR / f"{run_id}.json"
    md_path   = OUTPUT_DIR / f"{run_id}.md"
    json_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    md_path.write_text(_render_markdown(payload), encoding="utf-8")
    return md_path, json_path


# ===========================================================================
# MAIN
# ===========================================================================

def main():
    parser = argparse.ArgumentParser(
        description="synthesize.py -- Equity intelligence second-pass synthesizer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "\nExamples:\n"
            "  python synthesize.py\n"
            "  python synthesize.py --days 14\n"
            "  python synthesize.py --scan-only\n"
            "  python synthesize.py --aggregate-only\n"
            "  python synthesize.py --list-models\n"
        ),
    )
    parser.add_argument("--folder",         "-f",
                        help=f"Briefs folder (default: {BRIEFS_DIR})")
    parser.add_argument("--days",           type=int,
                        help="Only include last N days of briefs")
    parser.add_argument("--model",          "-m", default=DEFAULT_MODEL,
                        help=f"LM Studio model (default: {DEFAULT_MODEL})")
    parser.add_argument("--scan-only",      action="store_true",
                        help="List brief files only, no aggregation or LLM")
    parser.add_argument("--aggregate-only", action="store_true",
                        help="Aggregate and print formatted data, skip LLM")
    parser.add_argument("--list-models",    action="store_true",
                        help="Show loaded LM Studio models and exit")
    args = parser.parse_args()

    print(f"\n{'='*60}")
    print(f"  synthesize.py  |  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}")

    if args.list_models:
        try:
            models = list_models()
            print("Loaded models:" if models else "No models loaded.")
            for m in models:
                print(f"  {m}")
        except Exception as e:
            print(f"[error] Could not list models from {BASE_URL}: {e}")
            sys.exit(1)
        return

    folder = Path(args.folder).expanduser().resolve() if args.folder else BRIEFS_DIR
    print(f"  Folder : {folder}")
    if args.days:
        print(f"  Window : last {args.days} days")
    print(f"  Model  : {args.model}")
    print()

    if not folder.exists():
        print(f"[warn] Briefs folder does not exist: {folder} â€” will try API fallback.")
        files = []
    else:
        files = _find_briefs(folder)
    if files:
        print(f"Found {len(files)} brief file(s):")
        for f in files[:5]:
            print(f"  {f.name}")
        if len(files) > 5:
            print(f"  ... and {len(files) - 5} more")
        print()
        agg_input = files
    else:
        print("[warn] No brief_*.json files found â€” falling back to live API data.")
        brief_dicts = _fetch_api_brief(days=args.days or 7)
        if not brief_dicts:
            print("[error] Dashboard API also unavailable. Cannot synthesize.")
            print("  Start the dashboard first: run.bat (step 0), then re-run synthesize.py.")
            sys.exit(1)
        agg_input = brief_dicts

    if args.scan_only:
        return

    print("Aggregating catalyst data...")
    agg = aggregate(agg_input, days=args.days)
    dr  = agg["date_range"]
    print(f"  {agg['brief_count']} brief(s) | {dr.get('earliest','')} to {dr.get('latest','')}")
    print(f"  {len(agg['tickers'])} tickers | {len(agg['key_events'])} key events |"
          f" {len(agg['risks'])} risk events | {len(agg['event_types'])} event types")
    print()

    if agg["brief_count"] == 0:
        print("[error] No briefs passed the date filter. Cannot synthesize.")
        print("  Check DAILY_BRIEF_DAYS, DAILY_BRIEF_MIN_MATERIALITY, and whether events/clusters were built.")
        sys.exit(1)

    # Require at least some usable catalyst signal before hitting the LLM
    has_catalyst_signal = bool(agg.get("tickers") or agg.get("key_events") or
                               agg.get("risks") or agg.get("event_types"))
    if not has_catalyst_signal:
        print("[error] Aggregated briefs contain 0 catalyst signals.")
        print("  Check DAILY_BRIEF_DAYS, DAILY_BRIEF_MIN_MATERIALITY, and whether events/clusters were built.")
        print("  Run: equity-build-events && equity-cluster-events, then equity-run-daily-brief --days 7")
        sys.exit(1)

    aggregated_text = format_for_llm(agg)

    # Domain-balanced signal selection (mirrors Podcasts Pull weekly_synthesis pattern)
    selected_signals = _select_balanced_signals(agg)
    if selected_signals:
        dc: dict[str, int] = {}
        for s in selected_signals:
            dc[s.get("domain", "")] = dc.get(s.get("domain", ""), 0) + 1
        dc_str = ", ".join(f"{d}:{n}" for d, n in sorted(dc.items()))
        print(f"  Selected {len(selected_signals)} signals for LLM ({dc_str})")
    print()

    if args.aggregate_only:
        print("-- AGGREGATED DATA (--aggregate-only) ----------------------")
        print(aggregated_text)
        print("\n-- SELECTED SIGNALS --")
        print(_format_selected_signals(selected_signals))
        return

    print("Running LLM synthesis...")
    try:
        intelligence, model_used = synthesize(aggregated_text, args.model, selected_signals)
    except LLMHangError as e:
        print(f"\n[error] LLM hang: {e}")
        sys.exit(1)
    except (ValueError, RuntimeError) as e:
        msg = str(e)
        if "Cannot connect to LM Studio" in msg or "LM Studio" in msg:
            print(f"\n[error] Could not connect to LM Studio API at {BASE_URL}.")
            print("  Check that LM Studio is running, the local server is enabled,")
            print("  and the expected model is loaded.")
        else:
            print(f"\n[error] Synthesis failed: {e}")
        sys.exit(1)

    # Validate coverage and run repair pass if needed
    warnings = _validate_synthesis(intelligence, selected_signals)
    if warnings:
        for w in warnings:
            print(f"  [quality] {w}")
        intelligence = _repair_synthesis(intelligence, selected_signals, warnings, model_used)
        # Re-validate after repair
        post_warnings = _validate_synthesis(intelligence, selected_signals)
        if post_warnings:
            print(f"  [quality] {len(post_warnings)} warning(s) remain after repair.")
    intelligence["quality_warnings"] = warnings

    md_path, json_path = save_report(intelligence, agg, model_used)

    print(f"\n{'='*60}")
    print("  INTELLIGENCE BRIEF")
    print(f"{'='*60}")
    if intelligence.get("one_sentence_takeaway"):
        print(f"\n  {intelligence['one_sentence_takeaway']}\n")

    for s in intelligence.get("top_signals", [])[:5]:
        print(f"  {s.get('asset','')} -- {s.get('signal','').upper()} ({s.get('conviction','')})")

    actions = intelligence.get("actionable_intelligence", [])
    if actions:
        print(f"\n  Actionable ({len(actions)}):")
        for a in actions[:4]:
            print(f"    [{a.get('urgency','?').upper()}] {a.get('action','')[:90]}")

    high_risks = [r for r in intelligence.get("key_risks", []) if r.get("severity") == "high"]
    if high_risks:
        print(f"\n  High Risks ({len(high_risks)}):")
        for r in high_risks[:3]:
            print(f"    ! {r.get('risk','')[:90]}")

    print(f"\n  Saved:")
    print(f"    {md_path}")
    print(f"    {json_path}")
    print()


if __name__ == "__main__":
    main()
