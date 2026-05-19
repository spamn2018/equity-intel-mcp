"""Add institutional_holdings table for 13F-HR data.

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-19 00:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "institutional_holdings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("filing_id", sa.Integer(), sa.ForeignKey("filings.id"), nullable=False),
        # Manager
        sa.Column("manager_cik", sa.String(10), nullable=True),
        sa.Column("manager_name", sa.String(512), nullable=True),
        # Security
        sa.Column("issuer_name", sa.String(512), nullable=True),
        sa.Column("cusip", sa.String(9), nullable=True),
        sa.Column("title_of_class", sa.String(100), nullable=True),
        # Position
        sa.Column("value_usd", sa.BigInteger(), nullable=True),
        sa.Column("shares", sa.BigInteger(), nullable=True),
        sa.Column("share_type", sa.String(10), nullable=True),
        sa.Column("put_call", sa.String(10), nullable=True),
        sa.Column("investment_discretion", sa.String(20), nullable=True),
        # Dates
        sa.Column("report_date", sa.DateTime(), nullable=True),
        sa.Column("filing_date", sa.DateTime(), nullable=True),
        # Resolved company link
        sa.Column("ticker", sa.String(20), nullable=True),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id"), nullable=True),
        # Meta
        sa.Column("raw_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "filing_id", "cusip", "share_type",
            name="uq_inst_holding_filing_cusip_type",
        ),
    )
    op.create_index("ix_inst_holdings_filing_id", "institutional_holdings", ["filing_id"])
    op.create_index("ix_inst_holdings_manager_cik", "institutional_holdings", ["manager_cik"])
    op.create_index("ix_inst_holdings_cusip", "institutional_holdings", ["cusip"])
    op.create_index("ix_inst_holdings_ticker", "institutional_holdings", ["ticker"])
    op.create_index("ix_inst_holdings_company_id", "institutional_holdings", ["company_id"])
    op.create_index("ix_inst_holdings_report_date", "institutional_holdings", ["report_date"])
    op.create_index("ix_inst_holdings_filing_date", "institutional_holdings", ["filing_date"])


def downgrade() -> None:
    op.drop_table("institutional_holdings")
