#!/usr/bin/env python3
"""
gemini_news.py — Real-time news fetcher for the equity watchlist.

Uses Gemini Flash with Google Search grounding to pull the latest
market-moving news for each tracked ticker.

Usage:
    python gemini_news.py                  # all tickers from .env
    python gemini_news.py --days 3         # last 3 days only
    python gemini_news.py --scan-only      # print what would be queried

Output: intelligence/gemini_news_YYYYMMDD_HHMMSS.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(override=True)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

HERE      = Path(__file__).parent.resolve()
OUTPUT_DIR = HERE / "intelligence"

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL   = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
GEMINI_URL     = (
    f"https://generativelanguage.googleapis.com/v1beta/models"
    f"/{GEMINI_MODEL}:generateContent"
)

_raw_tickers = os.getenv(
    "DEFAULT_TICKERS",
    "NVDA,AMD,AVGO,MSFT,GOOGL,AMZN,TSLA,ISRG,SYM,META,PLTR,AI,BOTZ,ROBO",
)
TICKERS = [t.strip() for t in _raw_tickers.split(",") if t.strip()]


# ---------------------------------------------------------------------------
# Gemini call
# ---------------------------------------------------------------------------

def fetch_news(tickers: list[str], days: int = 7) -> tuple[dict, list]:
    """
    Call Gemini Flash with Google Search grounding.
    Returns (ticker_news_dict, grounding_sources_list).
    """
    ticker_list = ", ".join(tickers)
    prompt = (
        f"Search for the latest news and market-moving developments for these "
        f"US equity tickers over the past {days} days: {ticker_list}\n\n"
        "For each ticker that has meaningful recent news, provide:\n"
        "- Earnings surprises, guidance changes, or financial results\n"
        "- Analyst rating changes or price target updates\n"
        "- Regulatory events, FDA actions, or legal developments\n"
        "- M&A activity, partnerships, or major product announcements\n"
        "- Insider transactions or unusual institutional activity\n"
        "- Any other material market-moving developments\n\n"
        "Return ONLY a JSON object where each key is a ticker symbol and the value is:\n"
        "{\n"
        '  "headlines": ["brief headline 1", "brief headline 2", ...],\n'
        '  "summary": "2-3 sentence summary of what matters most for this ticker",\n'
        '  "sentiment": "bullish|bearish|neutral|mixed",\n'
        '  "key_catalyst": "single most important recent development, or null"\n'
        "}\n\n"
        "Skip tickers with no notable recent news. "
        "Return ONLY the JSON object. No markdown fences, no explanation."
    )

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "tools": [{"google_search": {}}],
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": 4096,
        },
    }
    headers = {
        "Content-Type": "application/json",
        "X-goog-api-key": GEMINI_API_KEY,
    }

    resp = requests.post(GEMINI_URL, headers=headers, json=payload, timeout=90)
    resp.raise_for_status()
    data = resp.json()

    # Extract generated text
    text = ""
    for candidate in data.get("candidates", []):
        for part in candidate.get("content", {}).get("parts", []):
            if "text" in part:
                text += part["text"]

    # Extract grounding sources
    grounding: list[dict] = []
    for candidate in data.get("candidates", []):
        gm = candidate.get("groundingMetadata", {})
        for chunk in gm.get("groundingChunks", []):
            web = chunk.get("web", {})
            if web.get("uri"):
                grounding.append({"url": web["uri"], "title": web.get("title", "")})

    # Parse JSON out of the response text
    text = re.sub(r"<think>[\s\S]*?</think>", "", text).strip()
    text = re.sub(r"^```(?:json)?\s*", "", text).strip()
    text = re.sub(r"\s*```$", "", text).strip()

    ticker_news: dict
    try:
        ticker_news = json.loads(text)
    except json.JSONDecodeError:
        s, e = text.find("{"), text.rfind("}")
        if s != -1 and e > s:
            try:
                ticker_news = json.loads(text[s : e + 1])
            except json.JSONDecodeError:
                ticker_news = {"_raw": text}
        else:
            ticker_news = {"_raw": text}

    return ticker_news, grounding


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def save_news(ticker_news: dict, grounding: list, tickers: list[str]) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = OUTPUT_DIR / f"gemini_news_{ts}.json"
    payload = {
        "generated_at":    datetime.now().isoformat(),
        "model":           GEMINI_MODEL,
        "tickers_queried": tickers,
        "news":            ticker_news,
        "grounding_sources": grounding[:30],
    }
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="gemini_news.py — Real-time ticker news via Gemini Flash"
    )
    parser.add_argument(
        "--days", type=int, default=7, help="News window in days (default: 7)"
    )
    parser.add_argument(
        "--scan-only", action="store_true", help="Print config and exit without calling API"
    )
    args = parser.parse_args()

    print(f"\n{'='*60}")
    print(f"  gemini_news.py  |  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}")
    print(f"  Model   : {GEMINI_MODEL}")
    print(f"  Tickers : {', '.join(TICKERS)}")
    print(f"  Window  : last {args.days} day(s)")
    print()

    if args.scan_only:
        print("(--scan-only: skipping API call)")
        return

    if not GEMINI_API_KEY:
        print("[error] GEMINI_API_KEY not set in .env — skipping.")
        return

    print("Fetching real-time news via Gemini + Google Search...")
    try:
        ticker_news, grounding = fetch_news(TICKERS, days=args.days)
    except Exception as exc:
        print(f"[error] Gemini API failed: {exc}")
        return

    valid = {k: v for k, v in ticker_news.items() if k != "_raw"}
    print(f"  Got news for {len(valid)} tickers")
    if grounding:
        print(f"  {len(grounding)} grounding source(s)")

    path = save_news(ticker_news, grounding, TICKERS)
    print(f"\n  Saved → {path.name}")
    print()

    for sym, info in list(valid.items())[:8]:
        if not isinstance(info, dict):
            continue
        sent     = info.get("sentiment", "?")
        catalyst = (info.get("key_catalyst") or "").strip()
        print(f"  {sym:<6} [{sent:<8}] {catalyst[:70]}")

    if len(valid) > 8:
        print(f"  ... and {len(valid) - 8} more")
    print()


if __name__ == "__main__":
    main()
