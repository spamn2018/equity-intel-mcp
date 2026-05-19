"""Abstract base class for price data providers."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional
import datetime


class PriceProvider(ABC):
    """Interface that all price providers must implement."""

    @abstractmethod
    async def fetch_daily_bars(
        self,
        ticker: str,
        start: datetime.date,
        end: datetime.date,
    ) -> List[Dict[str, Any]]:
        """
        Fetch daily OHLCV bars for a ticker.

        Returns list of dicts with:
          - ticker: str
          - timestamp: datetime (UTC)
          - open, high, low, close, volume: float
          - adjusted_close: float | None
          - interval: '1d'
          - provider: str
          - raw: dict
        """
        ...

    @property
    @abstractmethod
    def name(self) -> str:
        """Provider name string, e.g. 'polygon'."""
        ...
