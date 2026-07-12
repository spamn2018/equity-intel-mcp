"""Add expected_price and slippage_pct to trade_orders.

expected_price: mid-price at order submission time (float, nullable).
slippage_pct: (filled_avg_price - expected_price) / expected_price * 100
              computed when the order is reconciled as filled (float, nullable).

Revision ID: 0008
Revises: 0007
Create Date: 2026-07-09 00:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("trade_orders") as batch_op:
        batch_op.add_column(sa.Column("expected_price", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("slippage_pct", sa.Float(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("trade_orders") as batch_op:
        batch_op.drop_column("slippage_pct")
        batch_op.drop_column("expected_price")
