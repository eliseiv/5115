"""workspaces: workspace_projects + workspace_files + chat_sessions.workspace_project_id (ADR-036)

Adds the two workspace tables and the nullable ``chat_sessions.workspace_project_id`` binding in a
single expand-only migration (ADR-036 §2/§4; modules/workspaces/07-implementation-phases.md). The
feature is self-contained (own BYTEA ``workspace_files`` table, NOT the deferred ``attachments`` —
TD-015), so both sub-phases (3A core + 3B files) ship together.

Backward-compatible / additive:
- ``workspace_projects`` / ``workspace_files`` are new tables (no impact on existing rows);
- ``chat_sessions.workspace_project_id`` is nullable with NO backfill — existing chats stay NULL
  (= chat without a workspace), so a deploy without any workspace usage keeps the exact current
  behavior. NOT to be confused with ``chat_sessions.project_id`` (Text, website-builder, ADR-022).
- ``workspace_project_id`` FK is ON DELETE SET NULL (deleting a workspace keeps its chats as
  «чистые»); ``workspace_files`` FK is ON DELETE CASCADE (knowledge is meaningless without the
  project) — ADR-036 §5.

Chain: 0001 -> ... -> 0010 -> 0011 (single head). down_revision is the FULL revision id of 0010
(``0010_chat_sessions_model``). downgrade reverses in dependency order.

Revision ID: 0011_workspaces
Revises: 0010_chat_sessions_model
Create Date: 2026-06-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011_workspaces"
down_revision: str | None = "0010_chat_sessions_model"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- workspace_projects (ADR-036 §2) ---
    op.create_table(
        "workspace_projects",
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
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("instructions", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_workspace_projects_user_updated",
        "workspace_projects",
        ["user_id", "updated_at"],
    )

    # --- workspace_files (ADR-036 §4, TD-027 BYTEA) ---
    op.create_table(
        "workspace_files",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "workspace_project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspace_projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column("media_type", sa.Text(), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("extracted_text", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("size >= 0", name="ck_workspace_files_size_nonneg"),
    )
    op.create_index(
        "ix_workspace_files_project",
        "workspace_files",
        ["workspace_project_id"],
    )

    # --- chat_sessions.workspace_project_id (ADR-036 §2, SET NULL on workspace delete) ---
    op.add_column(
        "chat_sessions",
        sa.Column(
            "workspace_project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspace_projects.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_sessions_workspace",
        "chat_sessions",
        ["workspace_project_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_sessions_workspace", table_name="chat_sessions")
    op.drop_column("chat_sessions", "workspace_project_id")
    op.drop_table("workspace_files")
    op.drop_table("workspace_projects")
