"""SQLAlchemy ORM models for the equity intelligence schema."""
from __future__ import annotations

import datetime
from typing import Any, Dict, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# Alias: migrations use JSONB explicitly; models use cross-dialect JSON
JSONB = JSON


class Base(DeclarativeBase):
    pass


def now_utc() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class Company(Base):
    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticker: Mapped[str] = mapped_column(String(20), unique=True, nullable=False, index=True)
    cik: Mapped[Optional[str]] = mapped_column(String(10), unique=True, index=True)
    name: Mapped[Optional[str]] = mapped_column(String(512))
    exchange: Mapped[Optional[str]] = mapped_column(String(50))
    sic: Mapped[Optional[str]] = mapped_column(String(10))
    sector: Mapped[Optional[str]] = mapped_column(String(256))
    industry: Mapped[Optional[str]] = mapped_column(String(256))
    fiscal_year_end: Mapped[Optional[str]] = mapped_column(String(5))  # MM-DD
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )

    filings: Mapped[list["Filing"]] = relationship("Filing", back_populates="company")
    facts: Mapped[list["CompanyFact"]] = relationship("CompanyFact", back_populates="company")
    events: Mapped[list["Event"]] = relationship("Event", back_populates="company")
    news_articles: Mapped[list["NewsArticle"]] = relationship(
        "NewsArticle", back_populates="company"
    )

    def __repr__(self) -> str:
        return f"<Company {self.ticker} cik={self.cik}>"


class Filing(Base):
    __tablename__ = "filings"
    __table_args__ = (UniqueConstraint("accession_number", name="uq_filings_accession"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(Integer, ForeignKey("companies.id"), index=True)
    accession_number: Mapped[str] = mapped_column(String(25), nullable=False, index=True)
    form_type: Mapped[Optional[str]] = mapped_column(String(30), index=True)
    filing_date: Mapped[Optional[datetime.date]] = mapped_column(DateTime, index=True)
    report_date: Mapped[Optional[datetime.date]] = mapped_column(DateTime)
    acceptance_datetime: Mapped[Optional[datetime.datetime]] = mapped_column(
        DateTime(timezone=True)
    )
    primary_document: Mapped[Optional[str]] = mapped_column(String(512))
    filing_url: Mapped[Optional[str]] = mapped_column(Text)
    primary_document_url: Mapped[Optional[str]] = mapped_column(Text)
    sec_index_url: Mapped[Optional[str]] = mapped_column(Text)
    items: Mapped[Optional[str]] = mapped_column(Text)  # comma-separated 8-K items
    raw_metadata_json: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )

    company: Mapped["Company"] = relationship("Company", back_populates="filings")
    documents: Mapped[list["FilingDocument"]] = relationship(
        "FilingDocument", back_populates="filing"
    )

    def __repr__(self) -> str:
        return f"<Filing {self.form_type} {self.accession_number}>"


class FilingDocument(Base):
    __tablename__ = "filing_documents"
    __table_args__ = (UniqueConstraint("filing_id", "document_url", name="uq_filing_doc_url"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    filing_id: Mapped[int] = mapped_column(Integer, ForeignKey("filings.id"), index=True)
    document_url: Mapped[Optional[str]] = mapped_column(Text)
    document_type: Mapped[Optional[str]] = mapped_column(String(50))
    filename: Mapped[Optional[str]] = mapped_column(String(512))
    html_text: Mapped[Optional[str]] = mapped_column(Text)
    plain_text: Mapped[Optional[str]] = mapped_column(Text)
    parsed_sections_json: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )

    filing: Mapped["Filing"] = relationship("Filing", back_populates="documents")


class CompanyFact(Base):
    __tablename__ = "company_facts"
    __table_args__ = (
        UniqueConstraint(
            "company_id", "taxonomy", "concept", "end_date", "fiscal_period", "accession_number",
            name="uq_company_fact",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(Integer, ForeignKey("companies.id"), index=True)
    taxonomy: Mapped[Optional[str]] = mapped_column(String(20))  # us-gaap, dei, etc.
    concept: Mapped[Optional[str]] = mapped_column(String(256), index=True)
    label: Mapped[Optional[str]] = mapped_column(String(512))
    description: Mapped[Optional[str]] = mapped_column(Text)
    unit: Mapped[Optional[str]] = mapped_column(String(50))
    value: Mapped[Optional[float]] = mapped_column(Float)
    fiscal_year: Mapped[Optional[int]] = mapped_column(Integer)
    fiscal_period: Mapped[Optional[str]] = mapped_column(String(10))  # FY, Q1, Q2, Q3
    form_type: Mapped[Optional[str]] = mapped_column(String(30))
    filed_date: Mapped[Optional[datetime.date]] = mapped_column(DateTime)
    end_date: Mapped[Optional[datetime.date]] = mapped_column(DateTime, index=True)
    accession_number: Mapped[Optional[str]] = mapped_column(String(25))
    raw_json: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc
    )

    company: Mapped["Company"] = relationship("Company", back_populates="facts")


class NewsArticle(Base):
    __tablename__ = "news_articles"
    __table_args__ = (
        UniqueConstraint("provider", "provider_id", name="uq_news_provider_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[Optional[str]] = mapped_column(String(50))
    provider_id: Mapped[Optional[str]] = mapped_column(String(256))
    ticker: Mapped[Optional[str]] = mapped_column(String(20), index=True)
    company_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("companies.id"), index=True, nullable=True
    )
    title: Mapped[Optional[str]] = mapped_column(Text)
    summary: Mapped[Optional[str]] = mapped_column(Text)
    body: Mapped[Optional[str]] = mapped_column(Text)
    url: Mapped[Optional[str]] = mapped_column(Text)
    publisher: Mapped[Optional[str]] = mapped_column(String(256))
    author: Mapped[Optional[str]] = mapped_column(String(256))
    published_at: Mapped[Optional[datetime.datetime]] = mapped_column(
        DateTime(timezone=True), index=True
    )
    tickers_json: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB)
    sentiment_json: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB)
    raw_json: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc
    )

    company: Mapped[Optional["Company"]] = relationship("Company", back_populates="news_articles")


class PressRelease(Base):
    __tablename__ = "press_releases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("companies.id"), index=True, nullable=True
    )
    ticker: Mapped[Optional[str]] = mapped_column(String(20), index=True)
    title: Mapped[Optional[str]] = mapped_column(Text)
    body: Mapped[Optional[str]] = mapped_column(Text)
    url: Mapped[Optional[str]] = mapped_column(Text, unique=True)
    source: Mapped[Optional[str]] = mapped_column(String(256))
    published_at: Mapped[Optional[datetime.datetime]] = mapped_column(
        DateTime(timezone=True), index=True
    )
    raw_json: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc
    )


class MarketPrice(Base):
    __tablename__ = "market_prices"
    __table_args__ = (
        UniqueConstraint("ticker", "timestamp", "interval", name="uq_market_price"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticker: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    timestamp: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), index=True)
    open: Mapped[Optional[float]] = mapped_column(Float)
    high: Mapped[Optional[float]] = mapped_column(Float)
    low: Mapped[Optional[float]] = mapped_column(Float)
    close: Mapped[Optional[float]] = mapped_column(Float)
    volume: Mapped[Optional[float]] = mapped_column(Float)
    adjusted_close: Mapped[Optional[float]] = mapped_column(Float)
    interval: Mapped[Optional[str]] = mapped_column(String(10))  # 1d, 1h, etc.
    provider: Mapped[Optional[str]] = mapped_column(String(50))
    raw_json: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc
    )


class EventCluster(Base):
    """
    Groups related events that share the same ticker, event type, and time window
    (ISO week). Accumulates filing_ids, news_ids, aggregate scores, and price
    reaction data as new evidence arrives.
    """
    __tablename__ = "event_clusters"
    __table_args__ = (
        UniqueConstraint("cluster_key", name="uq_event_cluster_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cluster_key: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    ticker: Mapped[Optional[str]] = mapped_column(String(20), index=True)
    event_type: Mapped[Optional[str]] = mapped_column(String(50), index=True)
    event_subtype: Mapped[Optional[str]] = mapped_column(String(100))
    title: Mapped[Optional[str]] = mapped_column(Text)
    summary: Mapped[Optional[str]] = mapped_column(Text)
    first_seen_at: Mapped[Optional[datetime.datetime]] = mapped_column(
        DateTime(timezone=True), index=True
    )
    last_seen_at: Mapped[Optional[datetime.datetime]] = mapped_column(
        DateTime(timezone=True), index=True
    )
    event_count: Mapped[int] = mapped_column(Integer, default=0)
    filing_count: Mapped[int] = mapped_column(Integer, default=0)
    news_count: Mapped[int] = mapped_column(Integer, default=0)
    materiality_score: Mapped[Optional[float]] = mapped_column(Float, index=True)
    confidence_score: Mapped[Optional[float]] = mapped_column(Float)
    novelty_score: Mapped[Optional[float]] = mapped_column(Float)
    price_reaction_json: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB)
    filing_ids: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB)   # {"ids": [int, ...]}
    news_ids: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB)     # {"ids": [int, ...]}
    source_urls: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB)  # {"urls": [str, ...]}
    evidence_json: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB)
    caution: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )

    events: Mapped[list["Event"]] = relationship("Event", back_populates="cluster")

    def __repr__(self) -> str:
        return f"<EventCluster {self.cluster_key} count={self.event_count}>"


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("companies.id"), index=True, nullable=True
    )
    cluster_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("event_clusters.id"), index=True, nullable=True
    )
    ticker: Mapped[Optional[str]] = mapped_column(String(20), index=True)
    event_type: Mapped[Optional[str]] = mapped_column(String(50), index=True)
    event_subtype: Mapped[Optional[str]] = mapped_column(String(100))
    title: Mapped[Optional[str]] = mapped_column(Text)
    summary: Mapped[Optional[str]] = mapped_column(Text)
    source_type: Mapped[Optional[str]] = mapped_column(String(30))  # filing, news, price
    source_id: Mapped[Optional[int]] = mapped_column(Integer)  # FK into source table
    source_url: Mapped[Optional[str]] = mapped_column(Text)
    occurred_at: Mapped[Optional[datetime.datetime]] = mapped_column(
        DateTime(timezone=True), index=True
    )
    detected_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc
    )
    materiality_score: Mapped[Optional[float]] = mapped_column(Float, index=True)
    novelty_score: Mapped[Optional[float]] = mapped_column(Float)
    confidence_score: Mapped[Optional[float]] = mapped_column(Float)
    price_reaction_json: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB)
    evidence_json: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )

    company: Mapped[Optional["Company"]] = relationship("Company", back_populates="events")
    cluster: Mapped[Optional["EventCluster"]] = relationship("EventCluster", back_populates="events")

    def __repr__(self) -> str:
        return f"<Event {self.event_type} {self.ticker} {self.occurred_at}>"


# ── Trading models ────────────────────────────────────────────────────────────


class TradeSignal(Base):
    """
    A generated trade signal derived from one pipeline catalyst/cluster.

    Status lifecycle:
        generated -> approved  (manual approval or auto when REQUIRE_APPROVAL=False)
                  -> rejected  (manual rejection)
                  -> expired   (TTL exceeded before execution)
                  -> blocked   (risk policy blocked execution)
                  -> executed  (broker order submitted successfully)
                  -> failed    (broker submission raised an exception)

    Signal side values: buy | sell | reduce | monitor | avoid
    """
    __tablename__ = "trade_signals"
    __table_args__ = (
        UniqueConstraint(
            "ticker", "source_cluster_id", "source_event_id", "signal_side",
            name="uq_trade_signal_source",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    ticker: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    signal_side: Mapped[str] = mapped_column(String(20), nullable=False)   # buy/sell/reduce/monitor/avoid
    signal_strength: Mapped[Optional[float]] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="generated", index=True)

    # Source linkage
    source: Mapped[Optional[str]] = mapped_column(String(50))          # "cluster" | "event" | "brief"
    source_catalyst_id: Mapped[Optional[str]] = mapped_column(String(128))  # cluster_key or similar
    source_cluster_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("event_clusters.id"), nullable=True, index=True)
    source_event_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("events.id"), nullable=True, index=True)

    # Signal timing
    generated_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    expires_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime(timezone=True))

    # Scores from pipeline
    materiality_score: Mapped[Optional[float]] = mapped_column(Float)
    confidence_score: Mapped[Optional[float]] = mapped_column(Float)
    novelty_score: Mapped[Optional[float]] = mapped_column(Float)

    # Catalyst metadata
    event_type: Mapped[Optional[str]] = mapped_column(String(50))
    event_subtype: Mapped[Optional[str]] = mapped_column(String(100))
    title: Mapped[Optional[str]] = mapped_column(Text)
    rationale: Mapped[Optional[str]] = mapped_column(Text)
    reason_codes_json: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB)  # list of code strings
    risk_flags_json: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB)
    evidence_json: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB)
    price_context_json: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB)

    # Research stage of the ticker at signal time
    research_stage: Mapped[Optional[str]] = mapped_column(String(30))

    # Position sizing constraint from ticker metadata
    max_position_pct: Mapped[Optional[float]] = mapped_column(Float)

    # Approval workflow
    approved_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime(timezone=True))
    approved_by: Mapped[Optional[str]] = mapped_column(String(256))
    rejected_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime(timezone=True))
    rejection_reason: Mapped[Optional[str]] = mapped_column(Text)

    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc)

    orders: Mapped[list["TradeOrder"]] = relationship("TradeOrder", back_populates="signal")
    decision_logs: Mapped[list["TradingDecisionLog"]] = relationship("TradingDecisionLog", back_populates="signal")

    def __repr__(self) -> str:
        return f"<TradeSignal {self.signal_side.upper()} {self.ticker} strength={self.signal_strength} status={self.status}>"


class TradeOrder(Base):
    """A broker order attempt/result linked to a TradeSignal."""
    __tablename__ = "trade_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trade_signal_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("trade_signals.id"), nullable=True, index=True)

    ticker: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    side: Mapped[Optional[str]] = mapped_column(String(10))        # buy | sell
    qty: Mapped[Optional[float]] = mapped_column(Float)
    notional: Mapped[Optional[float]] = mapped_column(Float)
    order_type: Mapped[Optional[str]] = mapped_column(String(20))  # market | limit
    time_in_force: Mapped[Optional[str]] = mapped_column(String(10))

    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    broker: Mapped[Optional[str]] = mapped_column(String(50))      # "alpaca"
    broker_order_id: Mapped[Optional[str]] = mapped_column(String(256))

    submitted_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime(timezone=True))
    filled_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime(timezone=True))
    filled_avg_price: Mapped[Optional[float]] = mapped_column(Float)

    raw_request_json: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB)
    raw_response_json: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB)
    failure_reason: Mapped[Optional[str]] = mapped_column(Text)

    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc)

    signal: Mapped[Optional["TradeSignal"]] = relationship("TradeSignal", back_populates="orders")

    def __repr__(self) -> str:
        return f"<TradeOrder {self.side} {self.ticker} qty={self.qty} status={self.status}>"


class TradingDecisionLog(Base):
    """
    Records every policy decision made about a signal — allowed, blocked, skipped, etc.
    Provides a full audit trail for execution decisions.
    """
    __tablename__ = "trading_decision_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trade_signal_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("trade_signals.id"), nullable=True, index=True)
    ticker: Mapped[Optional[str]] = mapped_column(String(20), index=True)

    # decision values: allowed | blocked | skipped | executed | failed
    decision: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    reason: Mapped[Optional[str]] = mapped_column(Text)
    details_json: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB)

    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=now_utc)

    signal: Mapped[Optional["TradeSignal"]] = relationship("TradeSignal", back_populates="decision_logs")

    def __repr__(self) -> str:
        return f"<TradingDecisionLog {self.decision} {self.ticker} signal_id={self.trade_signal_id}>"


class InstitutionalHolding(Base):
    """
    One row per position held by an institutional manager in a single 13F-HR filing.

    The manager is identified by manager_cik / manager_name (the filer).
    The held security is identified by cusip, issuer_name, and (when resolved)
    ticker and company_id (FK into the companies table).

    value_usd is stored in *thousands of dollars* as reported in the SEC XML.
    """
    __tablename__ = "institutional_holdings"
    __table_args__ = (
        UniqueConstraint(
            "filing_id", "cusip", "share_type",
            name="uq_inst_holding_filing_cusip_type",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    filing_id: Mapped[int] = mapped_column(Integer, ForeignKey("filings.id"), index=True)

    # Manager (filer of the 13F-HR)
    manager_cik: Mapped[Optional[str]] = mapped_column(String(10), index=True)
    manager_name: Mapped[Optional[str]] = mapped_column(String(512))

    # The held security (as reported in the information table)
    issuer_name: Mapped[Optional[str]] = mapped_column(String(512))
    cusip: Mapped[Optional[str]] = mapped_column(String(9), index=True)
    title_of_class: Mapped[Optional[str]] = mapped_column(String(100))

    # Position size
    value_usd: Mapped[Optional[int]] = mapped_column(BigInteger)   # thousands of USD
    shares: Mapped[Optional[int]] = mapped_column(BigInteger)
    share_type: Mapped[Optional[str]] = mapped_column(String(10))  # SH or PRN

    # Options (blank for most equity positions)
    put_call: Mapped[Optional[str]] = mapped_column(String(10))

    # Discretion (SOLE, SHARED, OTHER)
    investment_discretion: Mapped[Optional[str]] = mapped_column(String(20))

    # Report period (quarter-end date from the 13F header)
    report_date: Mapped[Optional[datetime.date]] = mapped_column(DateTime, index=True)
    filing_date: Mapped[Optional[datetime.date]] = mapped_column(DateTime, index=True)

    # Resolved link to our companies table (nullable — filled by the sync worker)
    ticker: Mapped[Optional[str]] = mapped_column(String(20), index=True)
    company_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("companies.id"), index=True, nullable=True
    )

    raw_json: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )

    filing: Mapped["Filing"] = relationship("Filing")
    company: Mapped[Optional["Company"]] = relationship("Company")

    def __repr__(self) -> str:
        return (
            f"<InstitutionalHolding manager={self.manager_cik} "
            f"cusip={self.cusip} shares={self.shares}>"
        )


# ---------------------------------------------------------------------------
# Ticker Discovery Radar
# ---------------------------------------------------------------------------


class TickerMention(Base):
    """
    One raw mention of a ticker found while scanning ingested data.

    A mention links a *mentioned_ticker* (the one we spotted) to a
    *source_ticker* (the monitored company whose data contained it) plus
    provenance: source_type, source_id, context snippet, and URL.
    """
    __tablename__ = "ticker_mentions"
    __table_args__ = (
        UniqueConstraint(
            "mentioned_ticker", "source_type", "source_id",
            name="uq_ticker_mention_source",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mentioned_ticker: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    source_ticker: Mapped[Optional[str]] = mapped_column(String(20), index=True)
    source_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    # e.g. "news", "filing_document", "event", "event_cluster",
    #      "podcast_intelligence", "gemini_news_block", "lm_synthesis"
    source_id: Mapped[str] = mapped_column(String(256), nullable=False)
    occurred_at: Mapped[Optional[datetime.datetime]] = mapped_column(
        DateTime(timezone=True), index=True
    )
    week_key: Mapped[Optional[str]] = mapped_column(String(8), index=True)
    # ISO week string "YYYY-WNN"
    context: Mapped[Optional[str]] = mapped_column(Text)
    url: Mapped[Optional[str]] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    exclusion_flag: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc
    )

    def __repr__(self) -> str:
        return (
            f"<TickerMention {self.mentioned_ticker} via {self.source_type}/"
            f"{self.source_id} excl={self.exclusion_flag}>"
        )


class TickerDiscoveryScore(Base):
    """
    Weekly rolled-up discovery score for a candidate ticker.

    Rows are recomputed (upserted) each time the discovery worker runs;
    one row per (ticker, week_key).
    """
    __tablename__ = "ticker_discovery_scores"
    __table_args__ = (
        UniqueConstraint("ticker", "week_key", name="uq_discovery_score_ticker_week"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticker: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    week_key: Mapped[str] = mapped_column(String(8), nullable=False, index=True)
    # ISO week "YYYY-WNN"

    # Raw aggregation counts
    mention_count: Mapped[int] = mapped_column(Integer, default=0)
    unique_source_count: Mapped[int] = mapped_column(Integer, default=0)
    unique_source_ticker_count: Mapped[int] = mapped_column(Integer, default=0)

    # Historical comparison
    prior_week_count: Mapped[int] = mapped_column(Integer, default=0)
    four_week_avg: Mapped[float] = mapped_column(Float, default=0.0)

    # Score components [0, 1]
    acceleration_score: Mapped[float] = mapped_column(Float, default=0.0)
    mention_volume_score: Mapped[float] = mapped_column(Float, default=0.0)
    source_quality_score: Mapped[float] = mapped_column(Float, default=0.0)
    breadth_score: Mapped[float] = mapped_column(Float, default=0.0)
    novelty_score: Mapped[float] = mapped_column(Float, default=0.0)

    # Composite
    total_score: Mapped[float] = mapped_column(Float, default=0.0)

    # Promotion outcome
    recommendation: Mapped[str] = mapped_column(
        String(30), default="watch"
    )
    # "watch" | "probe_candidate" | "excluded"
    exclusion_flag: Mapped[bool] = mapped_column(Boolean, default=False)

    # Top evidence snippets (list of dicts)
    evidence_json: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB)

    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )

    def __repr__(self) -> str:
        return (
            f"<TickerDiscoveryScore {self.ticker} {self.week_key} "
            f"score={self.total_score:.3f} rec={self.recommendation}>"
        )
