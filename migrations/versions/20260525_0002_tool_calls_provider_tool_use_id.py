"""tool_calls.provider_tool_use_id: raw Anthropic tool_use.id for continuation (ADR-008, BUG-4)

Adds tool_calls.provider_tool_use_id (TEXT NOT NULL): the raw Anthropic tool_use.id ("toolu_...",
opaque, NOT a UUID). Used as tool_result.tool_use_id on continuation so the id pair in the replayed
Anthropic history matches (Anthropic rejects a mismatch with 400 → backend 502). The public domain
toolCallId stays the UUID tool_calls.id.

Single migration (not split expand/contract): prod is not yet running and the raw anthropic id was
never persisted before, so no backfill is possible and no rolling-update constraint applies (see
ADR-008 §Migration: a single NOT NULL migration is permitted for non-rolling environments). Any
pre-existing dev/test tool_calls rows are unbillable for continuation and must be cleared before
upgrade.

Revision ID: 0002_provider_tool_use_id
Revises: 0001_initial
Create Date: 2026-05-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_provider_tool_use_id"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tool_calls",
        sa.Column("provider_tool_use_id", sa.Text(), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("tool_calls", "provider_tool_use_id")
