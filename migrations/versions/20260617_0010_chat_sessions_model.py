"""chat_sessions.model TEXT NULL — user-selected model, session-fixed (ADR-034)

Adds the nullable ``chat_sessions.model`` column that fixes the user-selected model on a session at
creation (ADR-034 §3). ``NULL`` = «дефолтная модель инстанса» (the active provider's default model
resolved by the client at generation time, ``create_message(model=None)``).

Backward-compatible / expand-only: the column is nullable with NO backfill — existing rows stay
``NULL`` (= instance default), so a deploy without any ``ANTHROPIC_MODELS``/``OPENAI_MODELS`` env
and without the request ``model`` field keeps the exact current behavior. downgrade drops it.

Chain: 0001 -> ... -> 0009 -> 0010 (single head). down_revision is the FULL revision id of 0009
(``0009_notifications_default_false``).

Revision ID: 0010_chat_sessions_model
Revises: 0009_notifications_default_false
Create Date: 2026-06-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_chat_sessions_model"
down_revision: str | None = "0009_notifications_default_false"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ADR-034 §3: nullable, no backfill. NULL = instance default model (resolved by the client).
    op.add_column("chat_sessions", sa.Column("model", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("chat_sessions", "model")
