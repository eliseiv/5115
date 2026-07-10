"""auth_identities: external identity providers (Sign in with Apple) — ADR-043 §4

Expand-only (04-data-model.md §21, 03-data-model.md §21). Adds the ``auth_identities`` table for
external identity-provider links (Sign in with Apple on start, extensible to email/google/...).
``users``/``auth_devices``/``auth_refresh_tokens`` are NOT touched (identity stays
``users.id == sub``, ADR-007); device-based register/token/refresh/jwks are unchanged.

``UNIQUE(provider, subject)`` is the cross-device resolution point (one Apple account = one
``userId``) and the race-safety anchor; ``ix_auth_identities_user`` powers the reverse
"does this userId already have an Apple identity" lookup (account-linking, ADR-043 §5).

Chain: 0001 -> ... -> 0011 -> 0012 (single head). down_revision is the FULL revision id of 0011
(``0011_workspaces``), NOT the short ``0011`` — the short form would break the Alembic chain.

Revision ID: 0012_auth_identities
Revises: 0011_workspaces
Create Date: 2026-06-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012_auth_identities"
down_revision: str | None = "0011_workspaces"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 21. auth_identities — external identity-provider link (ADR-043 §4). One row per
    # (provider, subject); UNIQUE(provider, subject) anchors cross-device resolution + race safety
    # (ON CONFLICT (provider, subject) DO NOTHING + re-read on the service side).
    op.create_table(
        "auth_identities",
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
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("email", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ux_auth_identities_provider_subject",
        "auth_identities",
        ["provider", "subject"],
        unique=True,
    )
    op.create_index("ix_auth_identities_user", "auth_identities", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_auth_identities_user", table_name="auth_identities")
    op.drop_index("ux_auth_identities_provider_subject", table_name="auth_identities")
    op.drop_table("auth_identities")
