"""dialog_mode: session-fixed dialog-mode enum + two columns (ADR-055)

Expand-only (03-data-model.md, ADR-055). Sprint-2 scope ONLY:
- new enum ``dialog_mode`` (smart|deep_thinking|study_learn|search) created whole (CREATE TYPE);
- ``chat_sessions.dialog_mode`` (NOT NULL DEFAULT 'smart') — session-fixed dialog mode;
- ``user_preferences.default_dialog_mode`` (NOT NULL DEFAULT 'smart') — the per-user default.

``server_default 'smart'`` covers existing rows without a backfill (same pattern as
``assistant_mode`` in migration 0004). The provider-gate (deep_thinking/study_learn/search require
OpenAI, ADR-059) is enforced at session creation in the orchestrator, NOT in the schema — any value
is storable on any instance.

PostgreSQL pitfall (architect-reviewer, migration 0004): a value added to an enum via
``ALTER TYPE ADD VALUE`` cannot be USED in the same transaction it is added in. The dialog_mode enum
is therefore created WHOLE with ``CREATE TYPE`` (``postgresql.ENUM(...).create``) and both columns
reference it immediately with ``create_type=False`` — no ADD VALUE, no same-transaction hazard.

Chain: 0001 -> ... -> 0014 -> 0015 (single head). ``down_revision`` is the FULL revision id of
migration 0014 (``0014_cp_webhook_events`` — the abbreviated id it actually uses, NOT
``0014_cloudpayments_webhook_events``), otherwise the Alembic chain breaks. The ``revision`` id
stays <= 32 chars (``alembic_version.version_num`` is VARCHAR(32)): ``0015_dialog_mode`` (16 chars).

Revision ID: 0015_dialog_mode
Revises: 0014_cp_webhook_events
Create Date: 2026-07-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015_dialog_mode"
down_revision: str | None = "0014_cp_webhook_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DIALOG_MODE_VALUES = ("smart", "deep_thinking", "study_learn", "search")


def upgrade() -> None:
    # 1. New enum dialog_mode — created WHOLE (CREATE TYPE, NOT add-value) so both columns below
    #    can reference it in this same transaction (ADR-055; migration 0004 add-value pitfall).
    dialog_mode = postgresql.ENUM(*_DIALOG_MODE_VALUES, name="dialog_mode")
    dialog_mode.create(op.get_bind(), checkfirst=True)

    # 2. chat_sessions.dialog_mode — session-fixed; server_default 'smart' backfills existing rows.
    op.add_column(
        "chat_sessions",
        sa.Column(
            "dialog_mode",
            postgresql.ENUM(*_DIALOG_MODE_VALUES, name="dialog_mode", create_type=False),
            nullable=False,
            server_default=sa.text("'smart'"),
        ),
    )

    # 3. user_preferences.default_dialog_mode — per-user default; server_default 'smart'.
    op.add_column(
        "user_preferences",
        sa.Column(
            "default_dialog_mode",
            postgresql.ENUM(*_DIALOG_MODE_VALUES, name="dialog_mode", create_type=False),
            nullable=False,
            server_default=sa.text("'smart'"),
        ),
    )


def downgrade() -> None:
    # Drop the columns FIRST (they reference the type), then the enum type itself.
    op.drop_column("user_preferences", "default_dialog_mode")
    op.drop_column("chat_sessions", "dialog_mode")
    postgresql.ENUM(name="dialog_mode").drop(op.get_bind(), checkfirst=True)
