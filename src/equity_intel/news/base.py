"""Abstract base class for news providers."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional


class NewsProvider(ABC):
    """Interface that all news providers must implement."""

    @abstractmethod
    async def fetch_news(
        self,
        ticker: str,
        days: int = 7,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """
        Fetch recent news articles for a ticker.

        Returns list of dicts with at minimum:
          - provider: str
          - provider_id: str (unique article ID from provider)
          - ticker: str
          - title: str
          - summary: str | None
          - url: str
          - publisher: str | None
          - published_at: datetime
          - tickers: list[str]
          - raw: dict (full provider response)
        """
        ...

    @property
    @abstractmethod
    def name(self) -> str:
        """Provider name string, e.g. 'polygon'."""
        ...
