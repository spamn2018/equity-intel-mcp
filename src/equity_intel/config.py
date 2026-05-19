"""Central configuration via environment variables / .env file."""
from __future__ import annotations

from pathlib import Path
from typing import List

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # App identity
    app_name: str = "equity-intelligence-mcp"
    app_contact_email: str = "user@example.com"

    # Database
    database_url: str = "postgresql://localhost/equity_intel"

    # SEC EDGAR
    sec_user_agent: str = "equity-intelligence-mcp user@example.com"
    sec_rate_limit_rps: float = 8.0
    sec_cache_dir: str = ".cache/sec"
    sec_cache_ttl_seconds: int = 3600

    # News
    news_provider: str = "none"
    polygon_api_key: str = ""

    # Prices
    price_provider: str = "none"
    price_api_key: str = ""

    # MCP
    mcp_server_name: str = "equity-intelligence"

    # Logging
    log_level: str = "info"

    # Default tickers (used by sync workers and daily brief if DAILY_BRIEF_WATCHLIST is unset)
    default_tickers: str = "AAPL,MSFT,GOOGL,AMZN,NVDA,TSLA,META,NFLX"

    # Daily brief scheduler
    daily_brief_watchlist: str = ""
    daily_brief_output_dir: str = "briefs"
    daily_brief_days: int = 1
    daily_brief_min_materiality: float = 0.3
    daily_brief_format: str = "json"
    daily_brief_max_items: int = 30

    @property
    def tickers_list(self) -> List[str]:
        return [t.strip().upper() for t in self.default_tickers.split(",") if t.strip()]

    @property
    def daily_brief_tickers(self) -> List[str]:
        """Tickers for the daily brief. Falls back to default_tickers if not set."""
        src = self.daily_brief_watchlist.strip() or self.default_tickers
        return [t.strip().upper() for t in src.split(",") if t.strip()]

    @property
    def sec_cache_path(self) -> Path:
        return Path(self.sec_cache_dir)


# Singleton
settings = Settings()
