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
LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_OUTPUT_TOKENS", "3000"))

RISK_EVENT_TYPES = {
    "bankruptcy_or_going_concern", "restatement", "litigation",
    "regulatory", "offering_or_dilution", "management_change",
    "unusual_price_volume",
}


# ===========================================================================
# LM STUDIO CLIENT
# ===========================================================================

class LLMHangError(RuntimeError):
    pass


def _lms(*args, timeout=180):
    result = subprocess.run(
        [LMS_CLI, *args], capture_output=True, text=True, timeout=timeout
    )
    if result.returncode != 0:
        raise RuntimeError(f"lms {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _load_model(model):
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
    raise ValueError(f"Could not extract JSON. Output starts:\n{text[:400]}")


def _complete(messages, model, temperature=0.1, max_tokens=2500):
    model = _ensure_model(model)
    idle_s = int(os.getenv("LLM_TOKEN_IDLE_TIMEOUT_SECONDS", "300"))
    payload = {
        "model": model, "messages": messages,
        "temperature": temperature, "max_tokens": max_tokens, "stream": True,
    }
    if LLM_PROVIDER != "openai":
        payload["context_length"] = LMS_CONTEXT
        payload["ttl"] = int(os.getenv("LMSTUDIO_TTL_SECONDS", "60"))

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

    try:
        try:
            return _strip_think(_call())
        except _Http400 as e:
            if LLM_PROVIDER == "openai":
                raise RuntimeError(f"OpenAI 400: {e.body[:400]}")
            print(f"  [LMS] 400 -- reloading and retrying. Body: {e.body[:200]}")
            _load_model(model)
            return _strip_think(_call())
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
        (model,         messages,                temperature),
        (model,         _append(messages, strict), 0.05),
        (FALLBACK_MODEL, _append(messages, strict), 0.05),
    ]
    last_err = ValueError("no attempts")
    for m, msgs, t in attempts:
        try:
            raw = _complete(msgs, m, t, max_tokens)
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


def _find_briefs(folder):
    files = list(folder.rglob("brief_*.json"))
    files.sort(key=lambda p: p.name, reverse=True)
    return files


def _read_brief(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _brief_date(data, path):
    ga = data.get("generated_at", "")
    if ga and len(ga) >= 10:
        return ga[:10]
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

    for path in brief_files:
        data = _read_brief(path)
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
            "source_file":     str(path),
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


def synthesize(aggregated_text, model):
    user_content = (
        f"You are synthesizing equity intelligence from {PROJECT_NAME}.\n\n"
        "The data below was aggregated from multiple daily catalyst briefs. "
        "Find patterns across briefs, rank signals by materiality and frequency, "
        "and surface actionable intelligence.\n\n"
        f"{aggregated_text}\n\n"
        f"Return a JSON object matching this exact schema:\n{SYNTHESIS_SCHEMA_JSON}\n\n"
        "Rules:\n"
        "- DOMINANT THEMES: event types, sectors, or macro themes repeating across briefs.\n"
        "- TOP SIGNALS: tickers with strongest cross-brief signal (mentions x materiality). "
        "Flag price moves.\n"
        "- CONVERGING THESES: directional arguments appearing across multiple tickers or briefs.\n"
        "- DIVERGING VIEWS: contradictory signals on the same ticker or theme.\n"
        "- RISKS: restatement, bankruptcy, dilution, litigation -- by frequency and severity.\n"
        "- ACTIONABLE INTELLIGENCE: specific tickers and events to investigate next.\n"
        "- Return ONLY the JSON object. No markdown, no explanations.\n"
    )
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user",   "content": user_content},
    ]
    print(f"    Running synthesis ({len(aggregated_text):,} chars)...")
    return _complete_json(messages, model, max_tokens=LLM_MAX_TOKENS)


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
            print(f"[error] {e}")
        return

    folder = Path(args.folder).expanduser().resolve() if args.folder else BRIEFS_DIR
    print(f"  Folder : {folder}")
    if args.days:
        print(f"  Window : last {args.days} days")
    print(f"  Model  : {args.model}")
    print()

    if not folder.exists():
        print(f"[error] Briefs folder does not exist: {folder}")
        print("  Run sync_all.bat first to generate brief files.")
        sys.exit(1)

    files = _find_briefs(folder)
    if not files:
        print("[warn] No brief_*.json files found. Run sync_all.bat first.")
        return

    print(f"Found {len(files)} brief file(s):")
    for f in files[:5]:
        print(f"  {f.name}")
    if len(files) > 5:
        print(f"  ... and {len(files) - 5} more")
    print()

    if args.scan_only:
        return

    print("Aggregating catalyst data...")
    agg = aggregate(files, days=args.days)
    dr  = agg["date_range"]
    print(f"  {agg['brief_count']} brief(s) | {dr.get('earliest','')} to {dr.get('latest','')}")
    print(f"  {len(agg['tickers'])} tickers | {len(agg['key_events'])} key events |"
          f" {len(agg['risks'])} risk events | {len(agg['event_types'])} event types")
    print()

    if agg["brief_count"] == 0:
        print("[warn] No briefs passed the date filter.")
        return

    aggregated_text = format_for_llm(agg)

    if args.aggregate_only:
        print("-- AGGREGATED DATA (--aggregate-only) ----------------------")
        print(aggregated_text)
        return

    print("Running LLM synthesis...")
    try:
        intelligence, model_used = synthesize(aggregated_text, args.model)
    except LLMHangError as e:
        print(f"\n[error] LLM hang: {e}")
        sys.exit(1)
    except (ValueError, RuntimeError) as e:
        print(f"\n[error] Synthesis failed: {e}")
        sys.exit(1)

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
