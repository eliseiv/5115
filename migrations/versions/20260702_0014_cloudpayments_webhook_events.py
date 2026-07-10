"""cloudpayments_webhook_events — RU (broadapps/CloudPayments) webhook dedup journal (ADR-050)

Creates the ``cloudpayments_webhook_events`` table (billing-cloudpayments/04-data-model.md): the
single deduplication point for RU payment callbacks (broadapps → CloudPayments format).
``transaction_id`` is the PRIMARY KEY (= UNIQUE), which backs ``INSERT ... ON CONFLICT
(transaction_id) DO NOTHING RETURNING transaction_id`` in the webhook service. ``user_id`` is a FK
to ``users(id) ON DELETE CASCADE``; an index on ``user_id`` supports future per-user diagnostic
lookups (no hot-path query on MVP). ``payload`` stores only a SANITIZED allowlist projection (no
card data / bearer) — see 04-data-model.md §Санитизированный payload.

Expand-only: only CREATE TABLE + CREATE INDEX, no backfill, no changes to existing tables
(subscriptions/ledger/wallets/users are reused by the service via their existing schema). No new
enum types.

Chain: 0001 -> ... -> 0013 -> 0014 (single head). down_revision is the FULL revision id of 0013
(``0013_byok_provider``), NOT the short ``0013`` — the short form would break the Alembic chain.

NOTE: the ``revision`` id MUST stay <= 32 chars — Alembic's ``alembic_version.version_num`` column
is VARCHAR(32); a longer id fails the UPDATE with StringDataRightTruncationError and breaks every
migrate step. Hence the abbreviated ``0014_cp_webhook_events`` (22 chars) rather than the full
``0014_cloudpayments_webhook_events`` (33 chars).

Revision ID: 0014_cp_webhook_events
Revises: 0013_byok_provider
Create Date: 2026-07-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014_cp_webhook_events"
down_revision: str | None = "0013_byok_provider"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "cloudpayments_webhook_events",
        sa.Column("transaction_id", sa.Text(), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("product_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "processed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("transaction_id"),
    )
    op.create_index(
        "ix_cloudpayments_webhook_events_user_id",
        "cloudpayments_webhook_events",
        ["user_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_cloudpayments_webhook_events_user_id",
        table_name="cloudpayments_webhook_events",
    )
    op.drop_table("cloudpayments_webhook_events")
