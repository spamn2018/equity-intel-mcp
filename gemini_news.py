#!/usr/bin/env python3
"""OpenAI local-news summarizer for the equity watchlist.

Gemini has been removed from the critical path. This script reads already
synced Polygon/local news from equity_intel.db and asks OpenAI to summarize
the most market-relevant developments by ticker.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

load_dotenv(override=True)

HERE = Path(__file__).parent.resolve()
OUTPUT_DIR = HERE / "intelligence"
SRC_DIR = HERE / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_NEWS_MODEL", os.getenv("OPENAI_MODEL", "gpt-4o-mini"))
OPENAI_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/") + "/chat/completions"

_raw_tickers = os.getenv("DEFAULT_TICKERS", "POWL,ETN,VST,NEE,ANET,MRVL,AMAT,LRCX,KLAC,MU,EQIX,DLR,IRM,MP,USAR,UUUU,QCOM,ON,CSCO,FSLR")
TICKERS = [t.strip().upper() for t in _raw_tickers.split(",") if t.strip()]

def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)

def _coerce_aware(value: dt.datetime | None) -> dt.datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value

def query_local_news(tickers: list[str], days: int, limit: int = 250) -> list[dict[str, Any]]:
    from equity_intel.db.models import NewsArticle
    from equity_intel.db.session import SessionLocal
    cutoff = _utc_now() - dt.timedelta(days=days)
    ticker_set = {t.upper() for t in tickers}
    session = SessionLocal()
    try:
        rows = (
            session.query(NewsArticle)
            .filter(NewsArticle.published_at >= cutoff)
            .order_by(NewsArticle.published_at.desc())
            .limit(limit)
            .all()
        )
        articles: list[dict[str, Any]] = []
        for row in rows:
            ticker = (row.ticker or "").upper()
            if ticker not in ticker_set:
                continue
            published_at = _coerce_aware(row.published_at)
            articles.append({
                "id": row.id,
                "ticker": ticker,
                "title": row.title or "",
                "summary": (row.summary or row.body or "")[:500],
                "url": row.url or "",
                "publisher": row.publisher or "",
                "published_at": published_at.isoformat() if published_at else "",
            })
        return articles
    finally:
        session.close()

def group_articles(articles: list[dict[str, Any]], max_per_ticker: int = 5) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for article in articles:
        ticker = article["ticker"]
        grouped.setdefault(ticker, [])
        if len(grouped[ticker]) < max_per_ticker:
            grouped[ticker].append(article)
    return dict(sorted(grouped.items(), key=lambda kv: (-len(kv[1]), kv[0])))

def build_prompt(grouped: dict[str, list[dict[str, Any]]], days: int) -> str:
    lines = [
        f"Summarize market-moving local news from the last {days} day(s).",
        "Use only the articles below. Do not invent new facts or sources.",
        "Return only JSON keyed by ticker.",
        "Each ticker value must include: headlines, summary, sentiment, key_catalyst.",
        "sentiment must be bullish, bearish, neutral, or mixed.",
        "",
    ]
    for ticker, articles in grouped.items():
        lines.append(f"### {ticker}")
        for article in articles:
            when = (article.get("published_at") or "")[:16].replace("T", " ")
            title = article.get("title") or "Untitled"
            summary = article.get("summary") or ""
            publisher = article.get("publisher") or "News"
            url = article.get("url") or ""
            lines.append(f"- [{when}] {publisher}: {title}")
            if summary:
                lines.append(f"  Summary: {summary[:300]}")
            if url:
                lines.append(f"  URL: {url}")
        lines.append("")
    return "\n".join(lines)

def _extract_json(text: str) -> dict[str, Any]:
    text = re.sub(r"^```(?:json)?\s*", "", text.strip())
    text = re.sub(r"\s*```$", "", text).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise
        data = json.loads(text[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("OpenAI response was not a JSON object")
    return data

def summarize_with_openai(grouped: dict[str, list[dict[str, Any]]], days: int) -> dict[str, Any]:
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not set")
    prompt = build_prompt(grouped, days)
    payload = {
        "model": OPENAI_MODEL,
        "messages": [
            {"role": "system", "content": "You are an equity news analyst. Produce concise factual ticker-level research summaries from provided local news only. This is not investment advice."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.1,
        "max_tokens": 1800,
        "response_format": {"type": "json_object"},
    }
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    response = requests.post(OPENAI_URL, headers=headers, json=payload, timeout=90)
    response.raise_for_status()
    raw = response.json()["choices"][0]["message"].get("content") or "{}"
    return _extract_json(raw)

def fallback_summary(grouped: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    fallback: dict[str, Any] = {}
    for ticker, articles in grouped.items():
        top = articles[0]
        fallback[ticker] = {
            "headlines": [a.get("title", "") for a in articles[:3] if a.get("title")],
            "summary": (top.get("summary") or top.get("title") or "Recent local news found.")[:500],
            "sentiment": "neutral",
            "key_catalyst": top.get("title") or None,
        }
    return fallback

def save_news(ticker_news: dict[str, Any], grouped: dict[str, list[dict[str, Any]]], tickers: list[str]) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = OUTPUT_DIR / f"openai_news_{timestamp}.json"
    sources = []
    for articles in grouped.values():
        for article in articles[:3]:
            if article.get("url"):
                sources.append({"url": article["url"], "title": article.get("title", ""), "publisher": article.get("publisher", ""), "ticker": article.get("ticker", "")})
    payload = {
        "generated_at": _utc_now().isoformat(),
        "provider": "openai_local_news",
        "model": OPENAI_MODEL,
        "tickers_queried": tickers,
        "news": ticker_news,
        "grounding_sources": sources[:30],
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return path

def main() -> None:
    parser = argparse.ArgumentParser(description="Local news summarizer via OpenAI")
    parser.add_argument("--days", type=int, default=7, help="News window in days (default: 7)")
    parser.add_argument("--scan-only", action="store_true", help="Print config and DB counts only")
    args = parser.parse_args()
    print("\n" + "=" * 60)
    print(f"  local_news.py  |  {dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)
    print("  Provider: OpenAI over local Polygon/news DB")
    print(f"  Model   : {OPENAI_MODEL}")
    print(f"  Tickers : {', '.join(TICKERS)}")
    print(f"  Window  : last {args.days} day(s)")
    print()
    articles = query_local_news(TICKERS, days=args.days)
    grouped = group_articles(articles)
    print(f"Loaded {len(articles)} local article(s) across {len(grouped)} ticker(s)")
    if args.scan_only:
        for ticker, items in grouped.items():
            print(f"  {ticker}: {len(items)} article(s)")
        return
    if not grouped:
        path = save_news({}, {}, TICKERS)
        print(f"No local news found. Saved empty artifact -> {path.name}")
        return
    try:
        ticker_news = summarize_with_openai(grouped, args.days)
    except Exception as exc:
        print(f"[warn] OpenAI news summarization failed: {exc}")
        print("       Writing deterministic fallback from local headlines.")
        ticker_news = fallback_summary(grouped)
    valid = {k: v for k, v in ticker_news.items() if isinstance(v, dict)}
    path = save_news(ticker_news, grouped, TICKERS)
    print(f"\n  Saved -> {path.name}")
    print(f"  Got summaries for {len(valid)} ticker(s)")
    print()
    for sym, info in list(valid.items())[:8]:
        sentiment = info.get("sentiment", "?")
        catalyst = (info.get("key_catalyst") or "").strip()
        print(f"  {sym:<6} [{sentiment:<8}] {catalyst[:70]}")
    if len(valid) > 8:
        print(f"  ... and {len(valid) - 8} more")
    print()

if __name__ == "__main__":
    main()
