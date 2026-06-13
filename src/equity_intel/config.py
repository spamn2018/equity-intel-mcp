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
    rss_news_feeds: str = ""

    # Prices
    price_provider: str = "none"
    price_api_key: str = ""

    # MCP
    mcp_server_name: str = "equity-intelligence"

    # Logging
    log_level: str = "info"

    # Default tickers (used by sync workers and daily brief)
    default_tickers: str = "POWL,ETN,VST,NEE,ANET,MRVL,AMAT,LRCX,KLAC,MU,EQIX,DLR,IRM,MP,USAR,UUUU,QCOM,ON,CSCO,FSLR"

    # Tickers explicitly removed from the project universe
    prohibited_tickers: str = "AAPL,AA,AKAM,AMZN,APA,ASML,ATO,AVGO,AZN,BABA,BALL,BHP,BK,BNY,BP,BRK.B,BRK-B,CASY,CEG,CF,CIEN,CMI,EME,EXE,FTI,GLNCY,GLW,GOOG,GOOGL,HSBC,HWM,HXSCF,JBL,JNJ,JPM,LLY,META,MPC,MSFT,NSRGY,NTRA,NRG,NVDA,NVS,OVV,PR,PWR,RHHBY,SAN,SHEL,SNDK,SNSSY,SNX,SSNLF,STX,TCEHY,TM,TPR,TSLA,TSM,TTE,V,VLO,VRT,VTRS,WCC,WDC,WMT,XOM"

    # TradHedge: always-hold positions - synced for data but excluded from event/signal generation
    trad_hedge_tickers: str = "BAC,FLS,CI,STT,WASH,CTVA,CL,WLY,HIG,C"

    # Daily brief scheduler
    daily_brief_watchlist: str = ""
    daily_brief_tickers: str = ""
    daily_brief_format: str = "markdown"
    daily_brief_output_dir: str = "briefs"
    daily_brief_days: int = 7
    daily_brief_min_materiality: float = 0.2
    daily_brief_max_items: int = 80
    daily_brief_cache_ttl_seconds: int = 0  # 0 = disabled; 86400 = 24h cache
    gemini_model: str = "gemini-2.0-flash"
    gemini_api_key: str = ""

    # Trading signal generation
    trading_signals_enabled: bool = False
    trading_min_materiality: float = 0.3
    trading_min_confidence: float = 0.3
    trading_min_signal_strength: float = 0.4
    trading_require_primary_source: bool = False
    trading_allow_news_only_signals: bool = True
    trading_allow_probe_stage_signals: bool = False

    # Trade execution (Alpaca)
    trading_execution_enabled: bool = False
    trading_require_approval: bool = True
    alpaca_api_key: str = ""
    alpaca_secret_key: str = ""
    alpaca_paper: bool = True

    # Order / risk limits (used by risk.py and auto_rebalance)
    trading_max_spread_pct: float = 1.0
    trading_max_position_pct: float = 5.0
    trading_max_order_notional: float = 5000.0
    # Order type is always limit+day with notional (fractional) for buys, qty for sells

    # -----------------------------------------------------------------------
    # Computed convenience properties
    # -----------------------------------------------------------------------

    @property
    def tickers_list(self) -> List[str]:
        """Default tickers as a parsed list."""
        return [t.strip().upper() for t in self.default_tickers.split(",") if t.strip()]

    @property
    def prohibited_tickers_list(self) -> List[str]:
        """Prohibited tickers as a parsed list."""
        return [t.strip().upper() for t in self.prohibited_tickers.split(",") if t.strip()]

    @property
    def trad_hedge_list(self) -> List[str]:
        """TradHedge tickers as a parsed list."""
        return [t.strip().upper() for t in self.trad_hedge_tickers.split(",") if t.strip()]

    @property
    def sec_cache_path(self) -> str:
        """Alias for sec_cache_dir (used by SEC client)."""
        return self.sec_cache_dir


settings = Settings()
