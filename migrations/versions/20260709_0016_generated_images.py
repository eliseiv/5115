"""generated_images — image-generation byte store with TTL (ADR-058, table 23)

Creates the ``generated_images`` table (03-data-model.md §23): the BYTEA byte store for images
produced by the ``image.generate`` server-side tool (OpenAI gpt-image-1). ``chat_steps.payload``
carries only the ``id`` (never bytes); fetch is ``GET /v1/images/{imageId}`` under JWT (foreign →
404). ``expires_at`` is introduced RIGHT HERE (ADR-058 §6, NOT a later ALTER): NULL = never expires
(normal chat); ``created_at + TEMPORARY_IMAGE_TTL_SECONDS`` for a temporary chat (ADR-056), after
which the image is logically unreachable by the fetch query (privacy does not depend on the GC).

Expand-only: only CREATE TABLE + indexes, no backfill, no changes to existing tables, no new enum
types. FKs: ``user_id`` → ``users(id) ON DELETE CASCADE`` (the sole authorization anchor in the
GET); ``session_id`` → ``chat_sessions(id) ON DELETE SET NULL`` (the image outlives its session;
NULL for a temporary chat). ``message_step_id`` / ``tool_call_id`` are NOT FKs (mirrors
``chat_steps.message_step_id``; ``tool_call_id`` is the debit idempotency key protected by a partial
UNIQUE index). Two extra indexes: ``(user_id, created_at DESC)`` for per-user listing and the
partial ``(expires_at) WHERE expires_at IS NOT NULL`` for the sweep scan (ADR-058 §6).

Chain: 0001 -> ... -> 0015 -> 0016 (single head). ``down_revision`` is the FULL revision id of
migration 0015 (``0015_dialog_mode`` — read from that file's ``revision`` field, NOT its filename),
otherwise the Alembic chain breaks. The ``revision`` id stays <= 32 chars
(``alembic_version.version_num`` is VARCHAR(32)): ``0016_generated_images`` (21 chars).

Revision ID: 0016_generated_images
Revises: 0015_dialog_mode
Create Date: 2026-07-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0016_generated_images"
down_revision: str | None = "0015_dialog_mode"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "generated_images",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("chat_sessions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("message_step_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("tool_call_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column("content_type", sa.Text(), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        # ADR-058 §6: introduced immediately (NOT a later ALTER). NULL = never expires.
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("size >= 0", name="ck_generated_images_size_nonneg"),
    )
    op.create_index(
        "ix_generated_images_user_created",
        "generated_images",
        ["user_id", sa.text("created_at DESC")],
    )
    # Partial-unique on tool_call_id — blocks a duplicate row on continuation replay + double debit
    # (ADR-025/ADR-058 §1). Rows with a NULL tool_call_id are unconstrained.
    op.create_index(
        "ux_generated_images_tool_call",
        "generated_images",
        ["tool_call_id"],
        unique=True,
        postgresql_where=sa.text("tool_call_id IS NOT NULL"),
    )
    # Fast scan of expired rows for the opportunistic sweep (ADR-058 §6).
    op.create_index(
        "ix_generated_images_expires",
        "generated_images",
        ["expires_at"],
        postgresql_where=sa.text("expires_at IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_generated_images_expires", table_name="generated_images")
    op.drop_index("ux_generated_images_tool_call", table_name="generated_images")
    op.drop_index("ix_generated_images_user_created", table_name="generated_images")
    op.drop_table("generated_images")
