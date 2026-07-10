"""byok_keys.provider: multi-provider BYOK — ADR-044 §4

Expand-only (04-data-model.md). Adds a single nullable ``provider`` column to ``byok_keys`` so a
BYOK key's provider (detected from the key prefix, ADR-044 §1) is stored at ``set`` time and read
back WITHOUT decrypting the key (so GET /v1/byok can report ``activeModel`` without touching
plaintext, ADR-003/ADR-044 §4).

NULL = provider unknown/not recorded: legacy rows from before this migration (no backfill — like
0009/0010), or a ``set`` with an unrecognized key format (ADR-044 §3.1). For a NULL row the
service falls back to detecting the provider on the fly from the decrypted key on generation
(ADR-044 §5/§6); the fresh ``provider`` is written on the next ``set``. TEXT (not an enum) for
extensibility without ALTER TYPE — allowed values {anthropic, openai} are enforced by the
application (detector), not a DB constraint (symmetric with ``auth_identities.provider``).

Chain: 0001 -> ... -> 0012 -> 0013 (single head). down_revision is the FULL revision id of 0012
(``0012_auth_identities``), NOT the short ``0012`` — the short form would break the Alembic chain.

Revision ID: 0013_byok_provider
Revises: 0012_auth_identities
Create Date: 2026-06-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013_byok_provider"
down_revision: str | None = "0012_auth_identities"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ADR-044 §4: + nullable provider column. Expand-only, no backfill — legacy rows stay NULL and
    # use the fallback detector on use.
    op.add_column("byok_keys", sa.Column("provider", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("byok_keys", "provider")
