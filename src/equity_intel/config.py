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
    polygon_calls_per_minute: int = 5  # Polygon free-tier default; raise if on a paid plan

    # LLM provider switch (used by strategy_review/hypothesis_generator.py and
    # events/llm_scorer.py). Loaded from .env via pydantic-settings so these
    # values are correct even when a script is run directly (not just via
    # run.bat, which only forces provider=openai for the podcast-intel step).
    llm_provider: str = ""  # empty = auto-detect (openai if openai_api_key set, else lmstudio)
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    lmstudio_base_url: str = "http://127.0.0.1:1234/v1"
    lmstudio_model: str = "qwen/qwen3-14b"
    lmstudio_context: int = 8192
    lmstudio_ttl_seconds: int = 60
    llm_token_idle_timeout_seconds: int = 60

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

    # DeepSeek (price fallback in trading/risk.py when the broker has no quote)
    deepseek_api_key: str = ""
    deepseek_model: str = "deepseek-v4-flash"

    # Trading signal generation
    trading_signals_enabled: bool = False
    trading_min_materiality: float = 0.3
    trading_min_confidence: float = 0.3
    trading_min_signal_strength: float = 0.4
    trading_allow_probe_stage_signals: bool = False

    # Trade execution (Alpaca)
    trading_execution_enabled: bool = False
    trading_require_approval: bool = True
    alpaca_api_key: str = ""
    alpaca_secret_key: str = ""
    alpaca_paper: bool = True

    # Broker selection -- which adapter execution.py/auto_rebalance.py/etc.
    # actually submit orders through. "alpaca" is the long-standing default;
    # switching to "etrade" requires .etrade_token.json to already exist
    # (see etrade_auth.py) and account_id to match a real E*TRADE account.
    broker_provider: str = "alpaca"  # "alpaca" | "etrade"
    etrade_account_id: str = "913307581"  # Individual Brokerage only -- never the Roth IRA
    etrade_token_file: str = ".etrade_token.json"

    # Order / risk limits (used by risk.py and auto_rebalance)
    trading_max_position_pct: float = 5.0
    trading_max_order_notional: float = 5000.0
    trading_order_type: str = "limit"  # "limit" | "market" -- TRADING_ORDER_TYPE in .env
    trading_allow_shorts: bool = False  # set True in .env to execute sell/reduce signals

    # Day-trade mode: force-close all signal-driven positions before market close.
    # TradHedge positions (trad_hedge_tickers) are always excluded -- those are
    # intentional always-hold positions, never part of the day-trade book.
    trading_day_trade_mode: bool = False
    trading_day_trade_close_time_et: str = "15:55"  # HH:MM, US/Eastern, 24h
    trading_day_trade_close_window_minutes: int = 20  # safety guard around close time
    trading_regular_hours_only: bool = True
    trading_regular_hours_open_et: str = "09:30"
    trading_regular_hours_close_et: str = "15:55"
    trading_buy_cutoff_et: str = "15:00"  # HH:MM ET -- buys blocked after this time; sells still allowed until close
    trading_reconcile_stale_minutes: int = 30
    trading_health_lookback_hours: int = 48

    # Strategy review auto-apply
    strategy_review_auto_apply_enabled: bool = False
    strategy_review_policy_file: str = ".cache/strategy_review/auto_applied_policy.json"
    strategy_review_run_before_signal_generation_enabled: bool = False
    strategy_review_window_sessions: int = 20
    strategy_review_backtest_interval: str = "5m"
    strategy_review_artifact_output_dir: str = "strategy_review_artifacts"

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

    @property
    def llm_provider_resolved(self) -> str:
        """LLM_PROVIDER with the same auto-detect fallback the old os.getenv
        call sites used: explicit LLM_PROVIDER wins, otherwise openai if an
        OpenAI key is configured, otherwise lmstudio."""
        if self.llm_provider:
            return self.llm_provider.lower()
        return "openai" if self.openai_api_key else "lmstudio"


settings = Settings()
