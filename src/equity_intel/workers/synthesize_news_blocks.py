"""
Worker: synthesize 24-hour news blocks for the My Views dashboard tab.

Queries the last 24 h of news_articles from the local DB, ranks and
balances articles across tickers, calls LM Studio (or OpenAI) in JSON
mode to produce exactly 8 synthesized blocks, then writes the result to:

    intelligence/news_blocks_YYYYMMDD_HHMMSS.json

Each block:
    {
        "ticker":          "NVDA",
        "category":        "Chips",
        "importance":      "high",       # high | medium | low
        "headline":        "...",        # synthesized 1-sentence headline
        "why_it_matters":  "...",        # 2-3 sentence analysis
        "related_tickers": ["AMD"],       # other tickers affected
        "sources":         [{"title": "...", "url": "...", "publisher": "..."}],
        "article_count":   3,
        "window_hours":    24
    }

Usage:
    equity-synthesize-news-blocks
    equity-synthesize-news-blocks --hours 24 --blocks 8 --dry-run
"""
from __future__ import annotations

import copy
import datetime
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import click
import requests
from equity_intel.lmstudio_runtime import (
    note_model_usage,
    register_atexit_unload,
    wait_for_local_model_capacity,
)

# ---------------------------------------------------------------------------
# Paths / config
# ---------------------------------------------------------------------------

_HERE = Path(__file__).resolve()
# Walk up to project root (where pyproject.toml lives)
_ROOT = _HERE
for _ in range(8):
    if (_ROOT / "pyproject.toml").exists():
        break
    _ROOT = _ROOT.parent

INTEL_DIR = _ROOT / "intelligence"

try:
    from dotenv import load_dotenv

    load_dotenv(_ROOT / ".env")
except Exception:
    pass

LLM_PROVIDER   = os.getenv(
    "LLM_PROVIDER",
    "openai" if os.getenv("OPENAI_API_KEY") else "lmstudio",
).lower()
BASE_URL        = (
    "https://api.openai.com/v1"
    if LLM_PROVIDER == "openai"
    else os.getenv("LMSTUDIO_BASE_URL", "http://127.0.0.1:1234/v1")
)
DEFAULT_MODEL   = (
    os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    if LLM_PROVIDER == "openai"
    else os.getenv("LMSTUDIO_MODEL", "qwen/qwen3-14b")
)
API_KEY         = (
    os.getenv("OPENAI_API_KEY", "")
    if LLM_PROVIDER == "openai"
    else "lm-studio"
)
LMS_CONTEXT     = int(os.getenv("LMSTUDIO_CONTEXT", "16384"))
LLM_MAX_TOKENS  = int(os.getenv("LLM_MAX_OUTPUT_TOKENS", "3000"))
IDLE_TIMEOUT    = int(os.getenv("LLM_TOKEN_IDLE_TIMEOUT_SECONDS", "300"))

note_model_usage(DEFAULT_MODEL)
register_atexit_unload("stocks-news-block-synthesis")

# Category map - mirrors _CAT_MAP in app.py
_CAT_MAP: Dict[str, str] = {
    "NVDA": "Chips",   "AMD": "Chips",    "AVGO": "Chips",
    "MSFT": "Hyperscalers", "GOOGL": "Hyperscalers", "AMZN": "Hyperscalers",
    "TSLA": "Robotics", "ISRG": "Robotics", "SYM": "Robotics",
    "META": "Software", "PLTR": "Software", "AI": "Software",
    "BOTZ": "ETFs",    "ROBO": "ETFs",
}

DEFAULT_TICKERS = [
    t.strip().upper()
    for t in os.getenv(
        "DEFAULT_TICKERS",
        "POWL,ETN,VST,NEE,ANET,MRVL,AMAT,LRCX,KLAC,MU,EQIX,DLR,IRM,MP,USAR,UUUU,QCOM,ON,CSCO,FSLR",
    ).split(",")
    if t.strip()
]

SYSTEM_PROMPT = """\
You are a US equity financial-news analyst. You will be given a list of recent news articles
(from the last 24 hours) grouped by stock ticker.

Your task: produce exactly {n_blocks} synthesized news blocks as a JSON object.

Output ONLY valid JSON in this exact schema - no markdown, no code fences, no explanation:
{{
  "blocks": [
    {{
      "ticker":          "NVDA",
      "category":        "Chips",
      "importance":      "high",
      "headline":        "One synthesized sentence (max 120 chars) capturing the key development.",
      "why_it_matters":  "Two to three sentences explaining market significance, catalyst type, and potential impact.",
      "related_tickers": ["AMD", "AVGO"],
      "sources":         [{{"title": "Article title", "url": "https://...", "publisher": "Publisher name"}}]
    }}
  ]
}}

Rules:
- Each block covers ONE ticker. Use the ticker with the most significant news.
- importance must be exactly "high", "medium", or "low" based on market relevance.
- headline must be factual and cite evidence from the articles, max 120 chars.
- why_it_matters must explain the catalyst type (earnings, guidance, M&A, regulatory, etc.) and why it moves the stock.
- related_tickers lists other watchlist tickers affected by the same theme.
- sources must list the actual articles used (up to 3 per block).
- Cover a variety of tickers - do not put all blocks on one ticker.
- US equities only. Do not discuss crypto.
- This output is for research only. Not investment advice. Not a trading instruction.
- Generate exactly {n_blocks} blocks. No more, no less.\
"""


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _query_24h_news(hours: int) -> List[Dict[str, Any]]:
    """Pull recent news from the local DB, return as list of dicts."""
    sys.path.insert(0, str(_ROOT / "src"))
    from equity_intel.db.models import NewsArticle
    from equity_intel.db.session import SessionLocal

    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours)
    session = SessionLocal()
    try:
        articles = (
            session.query(NewsArticle)
            .filter(NewsArticle.published_at >= cutoff)
            .order_by(NewsArticle.published_at.desc())
            .limit(500)
            .all()
        )
        results = []
        for a in articles:
            results.append({
                "id":           a.id,
                "ticker":       a.ticker or "",
                "title":        a.title or "",
                "summary":      (a.summary or "")[:300],
                "url":          a.url or "",
                "publisher":    a.publisher or "",
                "published_at": a.published_at.isoformat() if a.published_at else "",
            })
        return results
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Article selection / ranking
# ---------------------------------------------------------------------------

def _rank_and_select(
    articles: List[Dict[str, Any]],
    tracked_tickers: List[str],
    max_per_ticker: int = 5,
    total_max: int = 60,
) -> Dict[str, List[Dict[str, Any]]]:
    """
    Group articles by ticker, keep top max_per_ticker per ticker.
    Returns a dict { ticker -> [articles] } for tickers with any coverage.
    """
    by_ticker: Dict[str, List[Dict[str, Any]]] = {}
    for art in articles:
        t = art["ticker"].upper()
        if t not in tracked_tickers:
            continue
        by_ticker.setdefault(t, [])
        if len(by_ticker[t]) < max_per_ticker:
            by_ticker[t].append(art)

    # Trim to total_max across all tickers (most-covered tickers first)
    result: Dict[str, List[Dict[str, Any]]] = {}
    count = 0
    for t in sorted(by_ticker, key=lambda k: len(by_ticker[k]), reverse=True):
        if count >= total_max:
            break
        available = by_ticker[t]
        take = min(len(available), total_max - count)
        result[t] = available[:take]
        count += take
    return result


# ---------------------------------------------------------------------------
# LLM call
# ---------------------------------------------------------------------------

def _strip_think(text: str) -> str:
    return re.sub(r"<think>[\s\S]*?</think>", "", text).strip()


def _extract_json(text: str) -> Any:
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
            return json.loads(text[s : e + 1])
        except json.JSONDecodeError:
            pass
    raise ValueError(f"Could not extract JSON from LLM output:\n{text[:400]}")


def _complete(messages: list, max_tokens: int = 2500) -> str:
    idle_s = IDLE_TIMEOUT
    wait_for_local_model_capacity("stocks-news-block-synthesis", DEFAULT_MODEL)
    payload: Dict[str, Any] = {
        "model":       DEFAULT_MODEL,
        "messages":    messages,
        "temperature": 0.15,
        "max_tokens":  max_tokens,
        "stream":      True,
        "response_format": {"type": "json_object"},
    }
    if LLM_PROVIDER != "openai":
        payload["context_length"] = LMS_CONTEXT
        payload["ttl"] = int(os.getenv("LMSTUDIO_TTL_SECONDS", "60"))

    headers = {"Authorization": f"Bearer {API_KEY}"}
    url     = f"{BASE_URL}/chat/completions"

    try:
        resp = requests.post(url, headers=headers, json=payload, stream=True,
                             timeout=(10, idle_s))
        resp.raise_for_status()
        chunks: List[str] = []
        last_token = time.monotonic()
        for raw in resp.iter_lines():
            if not raw:
                continue
            line = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
            if not line.startswith("data: "):
                continue
            data = line[6:]
            if data.strip() == "[DONE]":
                break
            try:
                delta = json.loads(data)["choices"][0]["delta"].get("content", "")
                if delta:
                    chunks.append(delta)
                    last_token = time.monotonic()
            except (json.JSONDecodeError, KeyError, IndexError):
                pass
        return _strip_think("".join(chunks))
    except requests.exceptions.ConnectionError:
        raise RuntimeError(
            f"Cannot connect to LM Studio at {BASE_URL}. "
            "Make sure LM Studio is running and the local server is enabled."
        )
    except requests.exceptions.HTTPError as exc:
        raise RuntimeError(
            f"LM Studio HTTP {exc.response.status_code}: {exc.response.text[:300]}"
        )


# ---------------------------------------------------------------------------
# Synthesis
# ---------------------------------------------------------------------------

def _build_prompt(
    by_ticker: Dict[str, List[Dict[str, Any]]],
    n_blocks: int,
    expected_tickers: List[str],
) -> str:
    """Build the user message from the grouped articles."""
    lines: List[str] = [
        "Here are the news articles from the last 24 hours grouped by ticker.",
        "Required tickers, exactly one block per ticker: "
        + ", ".join(expected_tickers),
        f"Generate exactly {n_blocks} synthesis blocks.\n",
    ]
    for ticker, arts in by_ticker.items():
        cat = _CAT_MAP.get(ticker, "Other")
        lines.append(f"### {ticker} ({cat})")
        for a in arts:
            pub_short = (a["published_at"] or "")[:16].replace("T", " ")
            lines.append(
                f"  - [{a['publisher'] or 'News'}] {a['title']}"
                + (f" - {a['summary'][:200]}" if a["summary"] else "")
                + f"  ({pub_short})  url={a['url']}"
            )
        lines.append("")
    return "\n".join(lines)


def _fallback_block(
    ticker: str,
    articles: List[Dict[str, Any]],
    hours: int,
) -> Dict[str, Any]:
    """Build a conservative card when the LLM omits an eligible ticker."""
    top = articles[0] if articles else {}
    title = top.get("title") or f"{ticker} has recent portfolio news"
    summary = top.get("summary") or title
    source = {
        "title": title,
        "url": top.get("url", ""),
        "publisher": top.get("publisher", ""),
    }
    return {
        "ticker": ticker,
        "category": _CAT_MAP.get(ticker, "Other"),
        "importance": "medium",
        "headline": title[:120],
        "why_it_matters": summary[:420],
        "related_tickers": [],
        "sources": [source],
        "article_count": len(articles),
        "window_hours": hours,
        "fallback": True,
    }


def _normalize_blocks(
    blocks: List[Dict[str, Any]],
    expected_tickers: List[str],
    by_ticker: Dict[str, List[Dict[str, Any]]],
    hours: int,
) -> List[Dict[str, Any]]:
    """Enforce exactly one card for each expected ticker."""
    expected = set(expected_tickers)
    by_block_ticker: Dict[str, Dict[str, Any]] = {}
    for blk in blocks:
        if not isinstance(blk, dict):
            continue
        ticker = (blk.get("ticker") or "").upper()
        if ticker not in expected or ticker in by_block_ticker:
            continue
        blk["ticker"] = ticker
        blk["category"] = blk.get("category") or _CAT_MAP.get(ticker, "Other")
        blk["window_hours"] = hours
        by_block_ticker[ticker] = blk

    normalized: List[Dict[str, Any]] = []
    for ticker in expected_tickers:
        normalized.append(
            by_block_ticker.get(ticker)
            or _fallback_block(ticker, by_ticker.get(ticker, []), hours)
        )
    return normalized


def synthesize_news_blocks(
    hours: int = 24,
    n_blocks: int = 8,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Main synthesis routine.

    Returns a dict with:
        { "available": bool, "blocks": [...], "generated_at": "...",
          "article_count": int, "window_hours": int }
    """
    click.echo(f"\n  24h News Block Synthesis")
    click.echo(f"    Window   : last {hours} hours")
    click.echo(f"    Blocks   : {n_blocks}")
    click.echo(f"    Mode     : {'dry-run (no LLM call)' if dry_run else 'live'}")
    click.echo()

    # 1. Pull articles from DB
    click.echo("  [1/4] Querying DB for recent news...")
    try:
        articles = _query_24h_news(hours)
    except Exception as exc:
        raise RuntimeError(f"DB query failed: {exc}") from exc

    click.echo(f"         Found {len(articles)} articles in the last {hours}h")

    if not articles:
        return {
            "available": False,
            "blocks":    [],
            "message":   f"No news articles in the last {hours} hours. Run equity-sync-news first.",
            "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "article_count": 0,
            "window_hours":  hours,
        }

    # 2. Rank and select
    click.echo("  [2/4] Ranking and selecting articles...")
    by_ticker = _rank_and_select(articles, DEFAULT_TICKERS)
    total_selected = sum(len(v) for v in by_ticker.values())
    click.echo(f"         Selected {total_selected} articles across {len(by_ticker)} tickers: "
               f"{', '.join(sorted(by_ticker.keys()))}")

    if not by_ticker:
        return {
            "available": False,
            "blocks":    [],
            "message":   "No articles matched tracked tickers. Check DEFAULT_TICKERS in .env.",
            "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "article_count": 0,
            "window_hours":  hours,
        }

    target_blocks = min(n_blocks, len(by_ticker))
    shortfall_note = ""
    if target_blocks < n_blocks:
        shortfall_note = (
            f"Only {target_blocks} strong ticker group(s) found in the last {hours}h; "
            f"generating {target_blocks} real block(s) instead of padding to {n_blocks}."
        )
        click.echo(f"         {shortfall_note}")

    # 3. Build prompt
    expected_tickers = list(by_ticker.keys())[:target_blocks]
    user_msg = _build_prompt(by_ticker, target_blocks, expected_tickers)
    system_msg = SYSTEM_PROMPT.format(n_blocks=target_blocks)
    messages = [
        {"role": "system", "content": system_msg},
        {"role": "user",   "content": user_msg},
    ]

    if dry_run:
        click.echo("  [3/4] Dry-run - skipping LLM call.")
        click.echo(f"\n  User prompt preview ({len(user_msg)} chars):")
        click.echo(user_msg[:600] + ("..." if len(user_msg) > 600 else ""))
        return {
            "available": False,
            "blocks":    [],
            "message":   "Dry-run mode - no LLM call made.",
            "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "article_count": total_selected,
            "window_hours":  hours,
            "requested_blocks": n_blocks,
            "target_blocks": target_blocks,
            "shortfall_note": shortfall_note,
        }

    # 4. Call LLM
    provider_label = "OpenAI" if LLM_PROVIDER == "openai" else "LM Studio"
    click.echo(f"  [3/4] Calling {provider_label}...")
    t0 = time.monotonic()
    raw = _complete(messages, max_tokens=LLM_MAX_TOKENS)
    elapsed = time.monotonic() - t0
    click.echo(f"         LLM returned {len(raw)} chars in {elapsed:.1f}s")

    # 5. Parse
    click.echo("  [4/4] Parsing JSON output...")
    try:
        parsed = _extract_json(raw)
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc

    blocks = parsed.get("blocks", [])
    if not isinstance(blocks, list):
        raise RuntimeError(f"LLM returned unexpected schema: {list(parsed.keys())}")
    blocks = _normalize_blocks(blocks, expected_tickers, by_ticker, hours)

    # Annotate each block with category and window
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    for blk in blocks:
        t = (blk.get("ticker") or "").upper()
        blk["ticker"]       = t
        blk["category"]     = blk.get("category") or _CAT_MAP.get(t, "Other")
        blk["window_hours"] = hours

    click.echo(f"         Parsed {len(blocks)} block(s)")

    return {
        "available":     True,
        "blocks":        blocks,
        "generated_at":  now_iso,
        "article_count": total_selected,
        "window_hours":  hours,
        "model_used":    DEFAULT_MODEL,
        "requested_blocks": n_blocks,
        "target_blocks": target_blocks,
        "shortfall_note": shortfall_note,
    }


def _save_output(data: Dict[str, Any]) -> Path:
    """Write the synthesis result to intelligence/news_blocks_YYYYMMDD_HHMMSS.json."""
    INTEL_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = INTEL_DIR / f"news_blocks_{ts}.json"
    out_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return out_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@click.command("equity-synthesize-news-blocks")
@click.option("--hours",   default=24,    show_default=True, type=int,
              help="Look-back window in hours for news articles.")
@click.option("--blocks",  default=8,     show_default=True, type=int,
              help="Number of synthesis blocks to generate.")
@click.option("--dry-run", is_flag=True,  default=False,
              help="Query DB and build prompt, but skip the LLM call.")
@click.option("--log-level", default="warning", show_default=True,
              help="Logging level (debug, info, warning, error).")
def main(hours: int, blocks: int, dry_run: bool, log_level: str) -> None:
    """
    Synthesize recent news articles into structured blocks for the My Views tab.

    Reads from the local DB (equity_intel.db), calls LM Studio in JSON mode,
    and writes output to intelligence/news_blocks_YYYYMMDD_HHMMSS.json.

    Run this as a pipeline step (after equity-sync-news, before the dashboard).
    """
    from equity_intel.logging_config import configure_logging
    configure_logging(log_level)

    try:
        result = synthesize_news_blocks(hours=hours, n_blocks=blocks, dry_run=dry_run)
    except RuntimeError as exc:
        click.echo(f"\n  ERROR: {exc}", err=True)
        sys.exit(1)

    if not result.get("available") or dry_run:
        msg = result.get("message", "No output generated.")
        click.echo(f"  {msg}\n")
        # dry-run and no-data are soft exits - non-blocking in run.bat
        sys.exit(0)

    try:
        out_path = _save_output(result)
    except Exception as exc:
        click.echo(f"\n  ERROR: Could not write output: {exc}", err=True)
        sys.exit(1)

    click.echo(f"\n  OK: {len(result['blocks'])} block(s) written to:")
    click.echo(f"    {out_path}\n")


if __name__ == "__main__":
    main()
