"""website-builder: projects + site_files (expand-only) (03-data-model.md, ADR-010)

Adds the two website-builder tables and their indexes. Expand-only: no change to the existing
9 tables. pgcrypto (gen_random_uuid) is already enabled by 0001_initial.

Revision ID: 0003_website_builder
Revises: 0002_provider_tool_use_id
Create Date: 2026-06-01
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_website_builder"
down_revision: str | None = "0002_provider_tool_use_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "projects",
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
        sa.Column("external_project_id", sa.Text(), nullable=False),
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
        "ux_projects_user_external",
        "projects",
        ["user_id", "external_project_id"],
        unique=True,
    )
    op.create_index(
        "ix_projects_user",
        "projects",
        ["user_id", sa.text("updated_at DESC")],
    )

    op.create_table(
        "site_files",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column("content_type", sa.Text(), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("size >= 0", name="ck_site_files_size_nonneg"),
    )
    op.create_index(
        "ux_site_files_project_path",
        "site_files",
        ["project_id", "path"],
        unique=True,
    )
    op.create_index("ix_site_files_project", "site_files", ["project_id"])


def downgrade() -> None:
    op.drop_table("site_files")
    op.drop_table("projects")
