"""Add event_clusters table and cluster_id FK on events.

Revision ID: 0002
Revises: 0001
Create Date: 2025-01-01 00:00:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "event_clusters",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("cluster_key", sa.String(128), nullable=False),
        sa.Column("ticker", sa.String(20), nullable=True),
        sa.Column("event_type", sa.String(50), nullable=True),
        sa.Column("event_subtype", sa.String(100), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("event_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("filing_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("news_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("materiality_score", sa.Float(), nullable=True),
        sa.Column("confidence_score", sa.Float(), nullable=True),
        sa.Column("novelty_score", sa.Float(), nullable=True),
        sa.Column("price_reaction_json", sa.JSON(), nullable=True),
        sa.Column("filing_ids", sa.JSON(), nullable=True),
        sa.Column("news_ids", sa.JSON(), nullable=True),
        sa.Column("source_urls", sa.JSON(), nullable=True),
        sa.Column("evidence_json", sa.JSON(), nullable=True),
        sa.Column("caution", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("cluster_key", name="uq_event_cluster_key"),
    )
    op.create_index("ix_event_clusters_ticker", "event_clusters", ["ticker"])
    op.create_index("ix_event_clusters_event_type", "event_clusters", ["event_type"])
    op.create_index("ix_event_clusters_first_seen_at", "event_clusters", ["first_seen_at"])
    op.create_index("ix_event_clusters_last_seen_at", "event_clusters", ["last_seen_at"])
    op.create_index("ix_event_clusters_materiality_score", "event_clusters", ["materiality_score"])

    # Use batch mode for SQLite compatibility (no ALTER TABLE … ADD CONSTRAINT support)
    with op.batch_alter_table("events") as batch_op:
        batch_op.add_column(
            sa.Column("cluster_id", sa.Integer(), nullable=True),
        )
    op.create_index("ix_events_cluster_id", "events", ["cluster_id"])


def downgrade() -> None:
    op.drop_index("ix_events_cluster_id", table_name="events")
    with op.batch_alter_table("events") as batch_op:
        batch_op.drop_column("cluster_id")
    op.drop_index("ix_event_clusters_materiality_score", table_name="event_clusters")
    op.drop_index("ix_event_clusters_last_seen_at", table_name="event_clusters")
    op.drop_index("ix_event_clusters_first_seen_at", table_name="event_clusters")
    op.drop_index("ix_event_clusters_event_type", table_name="event_clusters")
    op.drop_index("ix_event_clusters_ticker", table_name="event_clusters")
    op.drop_table("event_clusters")
