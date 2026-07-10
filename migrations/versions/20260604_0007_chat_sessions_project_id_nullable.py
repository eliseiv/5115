"""chat_sessions.project_id DROP NOT NULL — optional project / tool gating (ADR-022)

Positioning (ADR-022): the service is primarily a Claude chat aggregator; website-builder
(server-side ``site.*`` tools) is an OPTIONAL feature. ``projectId`` becomes optional in
``/v1/chat/run`` and ``chat_sessions.project_id`` becomes nullable: a session created without a
``projectId`` stores ``project_id = NULL`` («чистый чат») and ``site.*`` tools are NOT offered to
Claude (gating handled in the orchestrator; this migration only relaxes the column).

Expand-only: ``ALTER COLUMN project_id DROP NOT NULL``. No backfill, no data loss, indexes
unchanged. Existing rows keep their value; only new sessions without a project store NULL.

downgrade restores NOT NULL. It only succeeds if there are no NULL rows (i.e. no «чистый чат»
sessions were created after the upgrade) — acceptable for an immediate rollback. If NULL rows
exist the ALTER fails loudly rather than silently dropping data.

Chain: 0001 -> 0002 -> 0003 -> 0004 -> 0005 -> 0006 -> 0007 (single head). down_revision is the
FULL revision id of 0006 (``0006_chat_steps_seq``), NOT the short ``0006``.

Revision ID: 0007_project_id_nullable
Revises: 0006_chat_steps_seq
Create Date: 2026-06-04
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0007_project_id_nullable"
down_revision: str | None = "0006_chat_steps_seq"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Expand-only: relax the NOT NULL constraint so «чистый чат» sessions can store NULL.
    # No backfill, no index changes (ADR-022 §3).
    op.execute("ALTER TABLE chat_sessions ALTER COLUMN project_id DROP NOT NULL")


def downgrade() -> None:
    # Restore NOT NULL. Succeeds only if no NULL rows exist (no chat-only sessions created after
    # upgrade); otherwise Postgres raises, which is the correct fail-loud behavior for a rollback.
    op.execute("ALTER TABLE chat_sessions ALTER COLUMN project_id SET NOT NULL")
