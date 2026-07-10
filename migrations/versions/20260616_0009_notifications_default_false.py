"""user_preferences.notifications_enabled server_default true -> false (ADR-032)

Privacy-by-default: the contract default of ``notificationsEnabled`` becomes ``false`` so the iOS
client requests the system push permission FIRST and only then enables notifications via
``PATCH /v1/preferences``. The service lazy-default (``_defaults()``) is changed to ``False`` in the
same change; this migration keeps the column ``server_default`` consistent with it.

Default-only change: ``ALTER COLUMN notifications_enabled SET DEFAULT false``. Existing
``user_preferences`` rows are NOT touched — no UPDATE / backfill — so any explicit user choice
already stored is preserved (ADR-032 §1). downgrade restores ``server_default true``.

Chain: 0001 -> 0002 -> 0003 -> 0004 -> 0005 -> 0006 -> 0007 -> 0008 -> 0009 (single head).
down_revision is the FULL revision id of 0008 (``0008_adapty_webhook_events``).

Revision ID: 0009_notifications_default_false
Revises: 0008_adapty_webhook_events
Create Date: 2026-06-16
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_notifications_default_false"
down_revision: str | None = "0008_adapty_webhook_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Default-only change: new / no-row users get notifications_enabled=false (privacy-by-default).
    # Existing rows are NOT updated (no backfill) — explicit user choices kept (ADR-032 §1).
    op.alter_column(
        "user_preferences",
        "notifications_enabled",
        server_default=sa.text("false"),
    )


def downgrade() -> None:
    # Restore the previous contract default (true). Existing rows are not touched.
    op.alter_column(
        "user_preferences",
        "notifications_enabled",
        server_default=sa.text("true"),
    )
