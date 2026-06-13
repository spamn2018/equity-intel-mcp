"""Add trade_signals, trade_orders, trading_decision_log tables.

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-27 00:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── trade_signals ──────────────────────────────────────────────────
    op.create_table(
        "trade_signals",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ticker", sa.String(20), nullable=False),
        sa.Column("signal_side", sa.String(20), nullable=False),
        sa.Column("signal_strength", sa.Float(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="generated"),
        # Source linkage
        sa.Column("source", sa.String(50), nullable=True),
        sa.Column("source_catalyst_id", sa.String(128), nullable=True),
        sa.Column("source_cluster_id", sa.Integer(), sa.ForeignKey("event_clusters.id"), nullable=True),
        sa.Column("source_event_id", sa.Integer(), sa.ForeignKey("events.id"), nullable=True),
        # Timing
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        # Scores
        sa.Column("materiality_score", sa.Float(), nullable=True),
        sa.Column("confidence_score", sa.Float(), nullable=True),
        sa.Column("novelty_score", sa.Float(), nullable=True),
        # Catalyst metadata
        sa.Column("event_type", sa.String(50), nullable=True),
        sa.Column("event_subtype", sa.String(100), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("reason_codes_json", sa.JSON(), nullable=True),
        sa.Column("risk_flags_json", sa.JSON(), nullable=True),
        sa.Column("evidence_json", sa.JSON(), nullable=True),
        sa.Column("price_context_json", sa.JSON(), nullable=True),
        sa.Column("research_stage", sa.String(30), nullable=True),
        sa.Column("max_position_pct", sa.Float(), nullable=True),
        # Approval
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approved_by", sa.String(256), nullable=True),
        sa.Column("rejected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        # Timestamps
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "ticker", "source_cluster_id", "source_event_id", "signal_side",
            name="uq_trade_signal_source",
        ),
    )
    op.create_index("ix_trade_signals_ticker", "trade_signals", ["ticker"])
    op.create_index("ix_trade_signals_status", "trade_signals", ["status"])
    op.create_index("ix_trade_signals_source_cluster_id", "trade_signals", ["source_cluster_id"])
    op.create_index("ix_trade_signals_source_event_id", "trade_signals", ["source_event_id"])

    # ── trade_orders ───────────────────────────────────────────────────
    op.create_table(
        "trade_orders",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("trade_signal_id", sa.Integer(), sa.ForeignKey("trade_signals.id"), nullable=True),
        sa.Column("ticker", sa.String(20), nullable=False),
        sa.Column("side", sa.String(10), nullable=True),
        sa.Column("qty", sa.Float(), nullable=True),
        sa.Column("notional", sa.Float(), nullable=True),
        sa.Column("order_type", sa.String(20), nullable=True),
        sa.Column("time_in_force", sa.String(10), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("broker", sa.String(50), nullable=True),
        sa.Column("broker_order_id", sa.String(256), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("filled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("filled_avg_price", sa.Float(), nullable=True),
        sa.Column("raw_request_json", sa.JSON(), nullable=True),
        sa.Column("raw_response_json", sa.JSON(), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_trade_orders_ticker", "trade_orders", ["ticker"])
    op.create_index("ix_trade_orders_trade_signal_id", "trade_orders", ["trade_signal_id"])

    # ── trading_decision_log ───────────────────────────────────────────
    op.create_table(
        "trading_decision_log",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("trade_signal_id", sa.Integer(), sa.ForeignKey("trade_signals.id"), nullable=True),
        sa.Column("ticker", sa.String(20), nullable=True),
        sa.Column("decision", sa.String(20), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("details_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_trading_decision_log_ticker", "trading_decision_log", ["ticker"])
    op.create_index("ix_trading_decision_log_decision", "trading_decision_log", ["decision"])
    op.create_index("ix_trading_decision_log_trade_signal_id", "trading_decision_log", ["trade_signal_id"])


def downgrade() -> None:
    op.drop_table("trading_decision_log")
    op.drop_table("trade_orders")
    op.drop_table("trade_signals")
