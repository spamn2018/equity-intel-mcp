"""
Live validation script for Polygon/Massive news + price providers.

Usage:
    python scripts/live_validate.py
    python scripts/live_validate.py --tickers AAPL,MSFT --days 7
"""
from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import sys
from pathlib import Path

# Ensure src is on the path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env", override=True)

from equity_intel.config import Settings

WATCHLIST = ["AAPL", "MSFT", "NVDA", "TSLA", "AMD", "META", "GOOGL", "SMCI", "MSTR", "PLTR"]


async def validate_news(tickers: list[str], days: int, api_key: str, inter_delay: float = 0.5) -> dict:
    from equity_intel.news.polygon import PolygonNewsProvider

    results = {}
    async with PolygonNewsProvider(api_key=api_key) as provider:
        for i, ticker in enumerate(tickers):
            if i > 0:
                await asyncio.sleep(inter_delay)
            try:
                articles = await provider.fetch_news(ticker, days=days, limit=10)
                first = articles[0] if articles else {}
                results[ticker] = {
                    "status": "ok",
                    "count": len(articles),
                    "sample_title": first.get("title", "")[:80],
                    "sample_sentiment": first.get("sentiment"),
                    "sample_published_at": str(first.get("published_at", "")),
                    "sample_publisher": first.get("publisher", ""),
                }
            except Exception as exc:
                results[ticker] = {"status": "error", "error": str(exc)}
    return results


async def validate_prices(tickers: list[str], days: int, api_key: str, inter_delay: float = 1.0) -> dict:
    from equity_intel.prices.polygon import PolygonPriceProvider

    results = {}
    end = datetime.date.today()
    start = end - datetime.timedelta(days=days)

    async with PolygonPriceProvider(api_key=api_key) as provider:
        for i, ticker in enumerate(tickers):
            if i > 0:
                await asyncio.sleep(inter_delay)
            try:
                bars = await provider.fetch_daily_bars(ticker, start, end)
                await asyncio.sleep(inter_delay)
                prev = await provider.fetch_previous_close(ticker)
                last = bars[-1] if bars else {}
                results[ticker] = {
                    "status": "ok",
                    "bars_count": len(bars),
                    "last_date": last.get("timestamp", datetime.datetime.min).date().isoformat() if bars else None,
                    "last_close": last.get("close"),
                    "last_volume": int(last.get("volume", 0)) if bars else None,
                    "prev_close": prev["close"] if prev else None,
                    "prev_date": prev["timestamp"].date().isoformat() if prev else None,
                    "ohlcv_complete": all(
                        last.get(k) is not None for k in ("open", "high", "low", "close", "volume")
                    ) if bars else False,
                }
            except Exception as exc:
                results[ticker] = {"status": "error", "error": str(exc)}
    return results


async def main(tickers: list[str], days: int) -> None:
    s = Settings()

    if not s.polygon_api_key:
        print("ERROR: POLYGON_API_KEY not set in .env", file=sys.stderr)
        sys.exit(1)

    print(f"provider=polygon  key_prefix={s.polygon_api_key[:8]}...")
    print(f"tickers={tickers}  days={days}")
    print()

    print("--- NEWS VALIDATION ---")
    news = await validate_news(tickers, days=days, api_key=s.polygon_api_key)
    news_ok = 0
    for ticker, r in news.items():
        if r["status"] == "ok":
            news_ok += 1
            print(
                f"  {ticker}: OK  count={r['count']}  sentiment={r['sample_sentiment']}"
                f"\n    title: {r['sample_title']}"
                f"\n    publisher: {r['sample_publisher']}  published: {r['sample_published_at']}"
            )
        else:
            print(f"  {ticker}: FAIL  {r['error']}")
    print(f"\nNews: {news_ok}/{len(tickers)} tickers OK")

    print()
    print("--- PRICE VALIDATION ---")
    prices = await validate_prices(tickers, days=days, api_key=s.polygon_api_key)
    price_ok = 0
    for ticker, r in prices.items():
        if r["status"] == "ok":
            price_ok += 1
            print(
                f"  {ticker}: OK  bars={r['bars_count']}  last={r['last_date']}  "
                f"close={r['last_close']}  prev_close={r['prev_close']}  ohlcv_complete={r['ohlcv_complete']}"
            )
        else:
            print(f"  {ticker}: FAIL  {r['error']}")
    print(f"\nPrices: {price_ok}/{len(tickers)} tickers OK")

    # Summarize failures
    failed_news = [t for t, r in news.items() if r["status"] != "ok"]
    failed_prices = [t for t, r in prices.items() if r["status"] != "ok"]
    if failed_news or failed_prices:
        print("\n--- FAILURE SUMMARY ---")
        if failed_news:
            print(f"  News failures: {failed_news}")
        if failed_prices:
            print(f"  Price failures: {failed_prices}")
    else:
        print("\nAll tickers passed news and price validation.")

    # Write machine-readable results
    out = {"news": news, "prices": prices, "timestamp": datetime.datetime.utcnow().isoformat()}
    out_path = Path(__file__).parent.parent / "live_validation_results.json"
    out_path.write_text(json.dumps(out, indent=2, default=str))
    print(f"\nResults written to {out_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--tickers", default=",".join(WATCHLIST))
    parser.add_argument("--days", type=int, default=10)
    args = parser.parse_args()
    ticker_list = [t.strip().upper() for t in args.tickers.split(",")]
    asyncio.run(main(ticker_list, days=args.days))
