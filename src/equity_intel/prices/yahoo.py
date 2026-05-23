"""
Yahoo Finance price provider (via yfinance).

Provides:
  - fetch_daily_bars()   — historical OHLCV (matches PriceProvider interface)
  - fetch_quotes()       — live/delayed snapshot for a list of tickers (bulk, fast)

Live quotes are ~15-min delayed for free accounts.
No API key required.

Install: pip install yfinance
"""
from __future__ import annotations

import datetime
from typing import Any, Dict, List, Optional

from equity_intel.prices.base import PriceProvider
from equity_intel.logging_config import get_logger

logger = get_logger(__name__)


def _yf():
    """Lazy import so yfinance is optional."""
    try:
        import yfinance as yf
        return yf
    except ImportError as e:
        raise ImportError(
            "yfinance is not installed. Run: pip install yfinance"
        ) from e


class YahooPriceProvider(PriceProvider):
    """Yahoo Finance price provider using yfinance."""

    @property
    def name(self) -> str:
        return "yahoo"

    # ------------------------------------------------------------------ #
    # PriceProvider interface                                              #
    # ------------------------------------------------------------------ #

    async def fetch_daily_bars(
        self,
        ticker: str,
        start: datetime.date,
        end: datetime.date,
    ) -> List[Dict[str, Any]]:
        """Fetch daily OHLCV bars from Yahoo Finance."""
        yf = _yf()
        try:
            df = yf.download(
                ticker,
                start=start.isoformat(),
                end=end.isoformat(),
                progress=False,
                auto_adjust=True,
            )
            rows = []
            for ts, row in df.iterrows():
                rows.append({
                    "ticker": ticker.upper(),
                    "timestamp": ts.to_pydatetime().replace(tzinfo=datetime.timezone.utc),
                    "open": float(row.get("Open", 0) or 0),
                    "high": float(row.get("High", 0) or 0),
                    "low": float(row.get("Low", 0) or 0),
                    "close": float(row.get("Close", 0) or 0),
                    "volume": float(row.get("Volume", 0) or 0),
                    "adjusted_close": float(row.get("Close", 0) or 0),
                    "interval": "1d",
                    "provider": self.name,
                    "raw": {},
                })
            return rows
        except Exception as exc:
            logger.warning("yahoo_daily_bars_error", ticker=ticker, error=str(exc))
            return []

    # ------------------------------------------------------------------ #
    # Live quote snapshot (not in base interface — Yahoo-specific)        #
    # ------------------------------------------------------------------ #

    def fetch_quotes(self, tickers: List[str]) -> Dict[str, Dict[str, Any]]:
        """
        Fetch live (15-min delayed) quote snapshots for a list of tickers.

        Uses yf.Ticker.fast_info (parallelised via threads) — reliable across
        yfinance versions and works correctly for ETFs, small-caps, and
        non-standard symbols that trip up yf.download() multi-ticker mode.

        Returns a dict keyed by uppercase ticker.
        """
        import concurrent.futures

        yf = _yf()

        if not tickers:
            return {}

        clean = [t.strip().upper() for t in tickers if t.strip()]
        now_iso = datetime.datetime.now(tz=datetime.timezone.utc).isoformat()

        def _fetch_one(ticker: str) -> Dict[str, Any]:
            """Fetch a single ticker via fast_info; fall back to history() if needed."""
            try:
                obj = yf.Ticker(ticker)
                fi = obj.fast_info

                price     = _safe(fi, "last_price")
                prev      = _safe(fi, "previous_close")
                day_high  = _safe(fi, "day_high")
                day_low   = _safe(fi, "day_low")
                wk52_high = _safe(fi, "fifty_two_week_high")
                wk52_low  = _safe(fi, "fifty_two_week_low")

                # fast_info can return 0.0 for ETFs — fall back to history
                if price is None or price == 0.0:
                    hist = obj.history(period="2d", auto_adjust=True)
                    if not hist.empty:
                        price = float(hist["Close"].iloc[-1])
                        prev  = float(hist["Close"].iloc[-2]) if len(hist) >= 2 else price
                        day_high = float(hist["High"].iloc[-1])
                        day_low  = float(hist["Low"].iloc[-1])

                if price is None:
                    return _error_quote(ticker, "No price data available")

                if prev is None or prev == 0.0:
                    prev = price
                change     = round(price - prev, 4)
                change_pct = round((change / prev) * 100, 4) if prev else 0.0

                return {
                    "ticker":            ticker,
                    "price":             _round(price),
                    "prev_close":        _round(prev),
                    "change":            _round(change),
                    "change_pct":        _round(change_pct),
                    "day_high":          _round(day_high),
                    "day_low":           _round(day_low),
                    "volume":            None,
                    "fifty_two_wk_high": _round(wk52_high),
                    "fifty_two_wk_low":  _round(wk52_low),
                    "currency":          "USD",
                    "market_state":      "UNKNOWN",
                    "as_of":             now_iso,
                    "error":             None,
                }
            except Exception as exc:
                logger.warning("yahoo_quote_ticker_error", ticker=ticker, error=str(exc))
                return _error_quote(ticker, str(exc))

        results: Dict[str, Dict[str, Any]] = {}
        # 8 threads: fast enough for 14 tickers, won't overwhelm Yahoo
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            futures = {pool.submit(_fetch_one, t): t for t in clean}
            for future in concurrent.futures.as_completed(futures):
                ticker = futures[future]
                try:
                    results[ticker] = future.result()
                except Exception as exc:
                    results[ticker] = _error_quote(ticker, str(exc))

        return results


# ── Helpers ──────────────────────────────────────────────────────────────────

def _safe(obj: Any, attr: str) -> Optional[float]:
    try:
        v = getattr(obj, attr, None)
        if v is None:
            return None
        f = float(v)
        return None if (f != f) else f   # NaN check
    except Exception:
        return None


def _round(v: Optional[float], dp: int = 2) -> Optional[float]:
    if v is None:
        return None
    try:
        return round(v, dp)
    except Exception:
        return None


def _error_quote(ticker: str, msg: str) -> Dict[str, Any]:
    return {
        "ticker": ticker,
        "price": None, "prev_close": None,
        "change": None, "change_pct": None,
        "day_high": None, "day_low": None,
        "volume": None,
        "fifty_two_wk_high": None, "fifty_two_wk_low": None,
        "currency": "USD", "market_state": "UNKNOWN",
        "as_of": datetime.datetime.now(tz=datetime.timezone.utc).isoformat(),
        "error": msg,
    }
