"""figma-gap sprint 1: chats/profile/preferences fields + byok status extension

Expand-only (03-data-model.md, ADR-012/ADR-016). Sprint 1 scope ONLY:
- new enum ``assistant_mode`` (chat|code) created whole (CREATE TYPE);
- ``chat_sessions``: title (nullable), assistant_mode (NOT NULL DEFAULT 'chat'),
  is_pinned (NOT NULL DEFAULT FALSE) + list/sort index ix_sessions_user_pinned_updated;
- ``users.display_name`` (nullable, Profile screen);
- ``user_preferences`` table (preferences module);
- ``byok_key_status`` enum extended with 'validating'/'offline'/'expired' (ADR-016).

Sprint-2/3 objects (chat_sessions.workspace_project_id, workspace_projects/_files, snippets,
attachments, device_push_tokens, attachment_kind enum) are intentionally NOT created here —
they belong to their own sprints/migrations.

PostgreSQL pitfall (architect-reviewer): a value added to an enum via ALTER TYPE ADD VALUE
cannot be USED in the same transaction it is added in. We do not use the new byok_key_status
values in any DDL of this migration (columns keep DEFAULT 'missing'), so plain ADD VALUE is
safe. On PostgreSQL 12+ (stack pins PostgreSQL 16, docs/02-tech-stack.md) ``ALTER TYPE ... ADD
VALUE`` is permitted inside a transaction block — the only restriction is the just-added value
cannot be referenced in the same transaction, which we never do. Therefore the ADD VALUE
statements run as plain ``op.execute`` inside Alembic's migration transaction in both online and
offline (--sql) mode. We do NOT alter the connection's isolation level: Alembic's env wraps the
run in ``context.begin_transaction()``, so switching an already-in-transaction bind to AUTOCOMMIT
raises ``InvalidRequestError`` (isolation_level may not be altered mid-transaction).

Revision ID: 0004_figma_gap_sprint1
Revises: 0003_website_builder
Create Date: 2026-06-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_figma_gap_sprint1"
down_revision: str | None = "0003_website_builder"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NEW_BYOK_STATUS_VALUES = ("validating", "offline", "expired")


def _add_byok_enum_values() -> None:
    """ALTER TYPE byok_key_status ADD VALUE ... inside the migration transaction.

    ``ADD VALUE IF NOT EXISTS`` is idempotent (safe re-run). On PostgreSQL 12+ this DDL is
    allowed inside a transaction block; the only restriction is the new value cannot be USED in
    the same transaction — which this migration never does (columns keep DEFAULT 'missing').
    Statements are emitted with plain ``op.execute`` so both online and offline (--sql) modes
    produce identical, transaction-safe SQL. We deliberately do not touch the connection's
    isolation level: the bind is already inside Alembic's transaction, so switching it to
    AUTOCOMMIT would raise InvalidRequestError.
    """
    for value in _NEW_BYOK_STATUS_VALUES:
        op.execute(f"ALTER TYPE byok_key_status ADD VALUE IF NOT EXISTS '{value}'")


def upgrade() -> None:
    # 1. New enum assistant_mode (chat|code) — created whole (CREATE TYPE, NOT add-value).
    assistant_mode = postgresql.ENUM("chat", "code", name="assistant_mode")
    assistant_mode.create(op.get_bind(), checkfirst=True)

    # 2. users.display_name (Profile screen, nullable).
    op.add_column("users", sa.Column("display_name", sa.Text(), nullable=True))

    # 3. chat_sessions: title / assistant_mode / is_pinned (Sprint 1 fields only).
    op.add_column("chat_sessions", sa.Column("title", sa.Text(), nullable=True))
    op.add_column(
        "chat_sessions",
        sa.Column(
            "assistant_mode",
            postgresql.ENUM("chat", "code", name="assistant_mode", create_type=False),
            nullable=False,
            server_default=sa.text("'chat'"),
        ),
    )
    op.add_column(
        "chat_sessions",
        sa.Column(
            "is_pinned",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    # List/sort index: pinned first, then by recency (chats module, BR-CH-3).
    op.create_index(
        "ix_sessions_user_pinned_updated",
        "chat_sessions",
        ["user_id", sa.text("is_pinned DESC"), sa.text("updated_at DESC")],
    )

    # 4. user_preferences (preferences module).
    op.create_table(
        "user_preferences",
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "default_assistant_mode",
            postgresql.ENUM("chat", "code", name="assistant_mode", create_type=False),
            nullable=False,
            server_default=sa.text("'chat'"),
        ),
        sa.Column(
            "notifications_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "code_defaults",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    # 5. Extend byok_key_status enum (ADR-016) — outside the migration transaction.
    _add_byok_enum_values()


def downgrade() -> None:
    # PostgreSQL cannot remove a value from an enum; the byok_key_status additions and the
    # assistant_mode type are left in place on downgrade (expand-only contract). We only drop
    # the structures this migration added that ARE removable.
    op.drop_table("user_preferences")
    op.drop_index("ix_sessions_user_pinned_updated", table_name="chat_sessions")
    op.drop_column("chat_sessions", "is_pinned")
    op.drop_column("chat_sessions", "assistant_mode")
    op.drop_column("chat_sessions", "title")
    op.drop_column("users", "display_name")
    # assistant_mode enum is intentionally NOT dropped: chat_sessions.assistant_mode default
    # references it only while the column exists; after dropping the column the type is unused,
    # but leaving it is harmless and keeps downgrade order simple. Drop explicitly if needed.
    postgresql.ENUM(name="assistant_mode").drop(op.get_bind(), checkfirst=True)
