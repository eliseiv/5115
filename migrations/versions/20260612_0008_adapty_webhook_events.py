"""adapty_webhook_events — Adapty subscription webhook dedup journal (ADR-029)

Creates the ``adapty_webhook_events`` table (billing-adapty/04-data-model.md): the single
deduplication point for Adapty subscription events. ``event_id`` is the PRIMARY KEY (= UNIQUE),
which backs ``INSERT ... ON CONFLICT (event_id) DO NOTHING RETURNING event_id`` in the webhook
service. ``user_id`` is a FK to ``users(id) ON DELETE CASCADE``; an index on ``user_id`` supports
future per-user diagnostic lookups (no hot-path query on MVP).

No new enum types, no changes to existing tables (subscriptions/ledger/wallets/users are reused
by the service via their existing schema).

Chain: 0001 -> 0002 -> 0003 -> 0004 -> 0005 -> 0006 -> 0007 -> 0008 (single head). down_revision
is the FULL revision id of 0007 (``0007_project_id_nullable``).

Revision ID: 0008_adapty_webhook_events
Revises: 0007_project_id_nullable
Create Date: 2026-06-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008_adapty_webhook_events"
down_revision: str | None = "0007_project_id_nullable"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "adapty_webhook_events",
        sa.Column("event_id", sa.Text(), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "processed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("event_id"),
    )
    op.create_index(
        "ix_adapty_webhook_events_user_id",
        "adapty_webhook_events",
        ["user_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_adapty_webhook_events_user_id", table_name="adapty_webhook_events")
    op.drop_table("adapty_webhook_events")
