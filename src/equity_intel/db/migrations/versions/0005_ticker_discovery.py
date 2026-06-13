"""Add ticker_mentions and ticker_discovery_scores tables.

Revision ID: 0005
Revises: 0004
Create Date: 2026-05-27 00:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── ticker_mentions ────────────────────────────────────────────────
    op.create_table(
        "ticker_mentions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("mentioned_ticker", sa.String(20), nullable=False),
        sa.Column("source_ticker", sa.String(20), nullable=True),
        sa.Column("source_type", sa.String(50), nullable=False),
        sa.Column("source_id", sa.String(256), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("week_key", sa.String(8), nullable=True),
        sa.Column("context", sa.Text(), nullable=True),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="1.0"),
        sa.Column("exclusion_flag", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "mentioned_ticker", "source_type", "source_id",
            name="uq_ticker_mention_source",
        ),
    )
    op.create_index(
        "ix_ticker_mentions_mentioned_ticker",
        "ticker_mentions",
        ["mentioned_ticker"],
    )
    op.create_index(
        "ix_ticker_mentions_source_ticker",
        "ticker_mentions",
        ["source_ticker"],
    )
    op.create_index(
        "ix_ticker_mentions_source_type",
        "ticker_mentions",
        ["source_type"],
    )
    op.create_index(
        "ix_ticker_mentions_occurred_at",
        "ticker_mentions",
        ["occurred_at"],
    )
    op.create_index(
        "ix_ticker_mentions_week_key",
        "ticker_mentions",
        ["week_key"],
    )

    # ── ticker_discovery_scores ────────────────────────────────────────
    op.create_table(
        "ticker_discovery_scores",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ticker", sa.String(20), nullable=False),
        sa.Column("week_key", sa.String(8), nullable=False),
        sa.Column("mention_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("unique_source_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("unique_source_ticker_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("prior_week_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("four_week_avg", sa.Float(), nullable=False, server_default="0"),
        sa.Column("acceleration_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("mention_volume_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("source_quality_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("breadth_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("novelty_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("total_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("recommendation", sa.String(30), nullable=False, server_default="watch"),
        sa.Column("exclusion_flag", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("evidence_json", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "ticker", "week_key",
            name="uq_discovery_score_ticker_week",
        ),
    )
    op.create_index(
        "ix_ticker_discovery_scores_ticker",
        "ticker_discovery_scores",
        ["ticker"],
    )
    op.create_index(
        "ix_ticker_discovery_scores_week_key",
        "ticker_discovery_scores",
        ["week_key"],
    )


def downgrade() -> None:
    op.drop_table("ticker_discovery_scores")
    op.drop_table("ticker_mentions")
