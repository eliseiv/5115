"""embedded auth-issuer: auth_devices + auth_refresh_tokens (ADR-018)

Expand-only (03-data-model.md tables 18-19, modules/auth/04-data-model.md). Adds the two
device-based identity tables for the embedded RS256 issuer. ``users`` is NOT touched
(identity stays ``users.id == sub``, ADR-007).

Chain: 0001 -> 0002 -> 0003 -> 0004 -> 0005 (single head). down_revision is the FULL
revision id of 0004 (``0004_figma_gap_sprint1``), NOT the short ``0004`` — using the short
form would break the Alembic chain (no such revision).

Revision ID: 0005_embedded_auth_issuer
Revises: 0004_figma_gap_sprint1
Create Date: 2026-06-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_embedded_auth_issuer"
down_revision: str | None = "0004_figma_gap_sprint1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 18. auth_devices — deviceId -> userId mapping (find-or-create). device_id is the PK
    # (one device == one identity); concurrent register of the same device_id is resolved by
    # ON CONFLICT (device_id) DO NOTHING + re-read on the service side.
    op.create_table(
        "auth_devices",
        sa.Column("device_id", sa.Text(), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_auth_devices_user", "auth_devices", ["user_id"])

    # 19. auth_refresh_tokens — opaque refresh tokens stored ONLY as sha256(token_hash).
    # single-use rotation (used_at) + chain revocation (revoked_at) for reuse/theft detection.
    op.create_table(
        "auth_refresh_tokens",
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
            "device_id",
            sa.Text(),
            sa.ForeignKey("auth_devices.device_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ux_refresh_token_hash", "auth_refresh_tokens", ["token_hash"], unique=True)
    op.create_index("ix_refresh_user_device", "auth_refresh_tokens", ["user_id", "device_id"])


def downgrade() -> None:
    op.drop_index("ix_refresh_user_device", table_name="auth_refresh_tokens")
    op.drop_index("ux_refresh_token_hash", table_name="auth_refresh_tokens")
    op.drop_table("auth_refresh_tokens")
    op.drop_index("ix_auth_devices_user", table_name="auth_devices")
    op.drop_table("auth_devices")
