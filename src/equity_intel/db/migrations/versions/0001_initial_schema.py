"""Initial schema

Revision ID: 0001
Revises:
Create Date: 2024-01-01 00:00:00.000000

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # companies
    op.create_table(
        "companies",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ticker", sa.String(20), nullable=False),
        sa.Column("cik", sa.String(10), nullable=True),
        sa.Column("name", sa.String(512), nullable=True),
        sa.Column("exchange", sa.String(50), nullable=True),
        sa.Column("sic", sa.String(10), nullable=True),
        sa.Column("sector", sa.String(256), nullable=True),
        sa.Column("industry", sa.String(256), nullable=True),
        sa.Column("fiscal_year_end", sa.String(5), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ticker"),
        sa.UniqueConstraint("cik"),
    )
    op.create_index("ix_companies_ticker", "companies", ["ticker"])
    op.create_index("ix_companies_cik", "companies", ["cik"])

    # filings
    op.create_table(
        "filings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("accession_number", sa.String(25), nullable=False),
        sa.Column("form_type", sa.String(30), nullable=True),
        sa.Column("filing_date", sa.DateTime(), nullable=True),
        sa.Column("report_date", sa.DateTime(), nullable=True),
        sa.Column("acceptance_datetime", sa.DateTime(timezone=True), nullable=True),
        sa.Column("primary_document", sa.String(512), nullable=True),
        sa.Column("filing_url", sa.Text(), nullable=True),
        sa.Column("primary_document_url", sa.Text(), nullable=True),
        sa.Column("sec_index_url", sa.Text(), nullable=True),
        sa.Column("items", sa.Text(), nullable=True),
        sa.Column("raw_metadata_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("accession_number", name="uq_filings_accession"),
    )
    op.create_index("ix_filings_company_id", "filings", ["company_id"])
    op.create_index("ix_filings_accession_number", "filings", ["accession_number"])
    op.create_index("ix_filings_form_type", "filings", ["form_type"])
    op.create_index("ix_filings_filing_date", "filings", ["filing_date"])

    # filing_documents
    op.create_table(
        "filing_documents",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("filing_id", sa.Integer(), nullable=False),
        sa.Column("document_url", sa.Text(), nullable=True),
        sa.Column("document_type", sa.String(50), nullable=True),
        sa.Column("filename", sa.String(512), nullable=True),
        sa.Column("html_text", sa.Text(), nullable=True),
        sa.Column("plain_text", sa.Text(), nullable=True),
        sa.Column("parsed_sections_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["filing_id"], ["filings.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("filing_id", "document_url", name="uq_filing_doc_url"),
    )
    op.create_index("ix_filing_documents_filing_id", "filing_documents", ["filing_id"])

    # company_facts
    op.create_table(
        "company_facts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("taxonomy", sa.String(20), nullable=True),
        sa.Column("concept", sa.String(256), nullable=True),
        sa.Column("label", sa.String(512), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("unit", sa.String(50), nullable=True),
        sa.Column("value", sa.Float(), nullable=True),
        sa.Column("fiscal_year", sa.Integer(), nullable=True),
        sa.Column("fiscal_period", sa.String(10), nullable=True),
        sa.Column("form_type", sa.String(30), nullable=True),
        sa.Column("filed_date", sa.DateTime(), nullable=True),
        sa.Column("end_date", sa.DateTime(), nullable=True),
        sa.Column("accession_number", sa.String(25), nullable=True),
        sa.Column("raw_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "company_id", "taxonomy", "concept", "end_date", "fiscal_period", "accession_number",
            name="uq_company_fact",
        ),
    )
    op.create_index("ix_company_facts_company_id", "company_facts", ["company_id"])
    op.create_index("ix_company_facts_concept", "company_facts", ["concept"])
    op.create_index("ix_company_facts_end_date", "company_facts", ["end_date"])

    # news_articles
    op.create_table(
        "news_articles",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(50), nullable=True),
        sa.Column("provider_id", sa.String(256), nullable=True),
        sa.Column("ticker", sa.String(20), nullable=True),
        sa.Column("company_id", sa.Integer(), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("publisher", sa.String(256), nullable=True),
        sa.Column("author", sa.String(256), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("tickers_json", sa.JSON(), nullable=True),
        sa.Column("sentiment_json", sa.JSON(), nullable=True),
        sa.Column("raw_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider", "provider_id", name="uq_news_provider_id"),
    )
    op.create_index("ix_news_articles_ticker", "news_articles", ["ticker"])
    op.create_index("ix_news_articles_company_id", "news_articles", ["company_id"])
    op.create_index("ix_news_articles_published_at", "news_articles", ["published_at"])

    # press_releases
    op.create_table(
        "press_releases",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=True),
        sa.Column("ticker", sa.String(20), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("source", sa.String(256), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("raw_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("url"),
    )
    op.create_index("ix_press_releases_ticker", "press_releases", ["ticker"])
    op.create_index("ix_press_releases_published_at", "press_releases", ["published_at"])

    # market_prices
    op.create_table(
        "market_prices",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ticker", sa.String(20), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("open", sa.Float(), nullable=True),
        sa.Column("high", sa.Float(), nullable=True),
        sa.Column("low", sa.Float(), nullable=True),
        sa.Column("close", sa.Float(), nullable=True),
        sa.Column("volume", sa.Float(), nullable=True),
        sa.Column("adjusted_close", sa.Float(), nullable=True),
        sa.Column("interval", sa.String(10), nullable=True),
        sa.Column("provider", sa.String(50), nullable=True),
        sa.Column("raw_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ticker", "timestamp", "interval", name="uq_market_price"),
    )
    op.create_index("ix_market_prices_ticker", "market_prices", ["ticker"])
    op.create_index("ix_market_prices_timestamp", "market_prices", ["timestamp"])

    # events
    op.create_table(
        "events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=True),
        sa.Column("ticker", sa.String(20), nullable=True),
        sa.Column("event_type", sa.String(50), nullable=True),
        sa.Column("event_subtype", sa.String(100), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("source_type", sa.String(30), nullable=True),
        sa.Column("source_id", sa.Integer(), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("materiality_score", sa.Float(), nullable=True),
        sa.Column("novelty_score", sa.Float(), nullable=True),
        sa.Column("confidence_score", sa.Float(), nullable=True),
        sa.Column("price_reaction_json", sa.JSON(), nullable=True),
        sa.Column("evidence_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_events_company_id", "events", ["company_id"])
    op.create_index("ix_events_ticker", "events", ["ticker"])
    op.create_index("ix_events_event_type", "events", ["event_type"])
    op.create_index("ix_events_occurred_at", "events", ["occurred_at"])
    op.create_index("ix_events_materiality_score", "events", ["materiality_score"])

    # Full-text search indexes — PostgreSQL GIN indexes are skipped for SQLite.
    # When running against PostgreSQL, uncomment the block below:
    # op.execute("""
    #     CREATE INDEX IF NOT EXISTS idx_filing_docs_fts
    #     ON filing_documents USING GIN (to_tsvector('english', coalesce(plain_text, '')));
    # """)
    # op.execute("""
    #     CREATE INDEX IF NOT EXISTS idx_news_articles_fts
    #     ON news_articles USING GIN (
    #         to_tsvector('english', coalesce(title, '') || ' ' || coalesce(summary, '') || ' ' || coalesce(body, ''))
    #     );
    # """)
    # op.execute("""
    #     CREATE INDEX IF NOT EXISTS idx_events_fts
    #     ON events USING GIN (to_tsvector('english', coalesce(title, '') || ' ' || coalesce(summary, '')));
    # """)


def downgrade() -> None:
    op.drop_table("events")
    op.drop_table("market_prices")
    op.drop_table("press_releases")
    op.drop_table("news_articles")
    op.drop_table("company_facts")
    op.drop_table("filing_documents")
    op.drop_table("filings")
    op.drop_table("companies")
