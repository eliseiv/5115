"""initial schema: 9 tables, enums, pgcrypto (03-data-model.md)

Revision ID: 0001_initial
Revises:
Create Date: 2026-05-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    subscription_status = postgresql.ENUM("active", "expired", "none", name="subscription_status")
    ledger_tx_type = postgresql.ENUM("credit", "debit", name="ledger_tx_type")
    byok_key_status = postgresql.ENUM("valid", "invalid", "missing", name="byok_key_status")
    chat_mode = postgresql.ENUM("credits", "byok", name="chat_mode")
    chat_role = postgresql.ENUM("user", "assistant", "tool", name="chat_role")
    tool_call_status = postgresql.ENUM("pending", "completed", "errored", name="tool_call_status")
    for enum in (
        subscription_status,
        ledger_tx_type,
        byok_key_status,
        chat_mode,
        chat_role,
        tool_call_status,
    ):
        enum.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "users",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("trial_used", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )

    op.create_table(
        "subscriptions",
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "status",
            postgresql.ENUM(
                "active", "expired", "none", name="subscription_status", create_type=False
            ),
            nullable=False,
            server_default=sa.text("'none'"),
        ),
        sa.Column("plan", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_subscriptions_expires_at", "subscriptions", ["expires_at"])

    op.create_table(
        "wallets",
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("balance", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("balance >= 0", name="ck_wallets_balance_nonneg"),
    )

    op.create_table(
        "ledger_transactions",
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
            "type",
            postgresql.ENUM("credit", "debit", name="ledger_tx_type", create_type=False),
            nullable=False,
        ),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column(
            "meta", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("amount > 0", name="ck_ledger_amount_positive"),
    )
    op.create_index(
        "ux_ledger_idempotency",
        "ledger_transactions",
        ["user_id", "idempotency_key"],
        unique=True,
    )
    op.create_index(
        "ix_ledger_user_created",
        "ledger_transactions",
        ["user_id", sa.text("created_at DESC")],
    )

    op.create_table(
        "byok_keys",
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("encrypted_key", sa.LargeBinary(), nullable=False),
        sa.Column("encrypted_dek", sa.LargeBinary(), nullable=False),
        sa.Column("nonce", sa.LargeBinary(), nullable=False),
        sa.Column(
            "key_status",
            postgresql.ENUM(
                "valid", "invalid", "missing", name="byok_key_status", create_type=False
            ),
            nullable=False,
            server_default=sa.text("'missing'"),
        ),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    op.create_table(
        "chat_sessions",
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
        sa.Column("project_id", sa.Text(), nullable=False),
        sa.Column(
            "mode",
            postgresql.ENUM("credits", "byok", name="chat_mode", create_type=False),
            nullable=False,
        ),
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
        "ix_sessions_user_updated",
        "chat_sessions",
        ["user_id", sa.text("updated_at DESC")],
    )

    op.create_table(
        "chat_steps",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("chat_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("message_step_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "role",
            postgresql.ENUM("user", "assistant", "tool", name="chat_role", create_type=False),
            nullable=False,
        ),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("usage", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_steps_session_created", "chat_steps", ["session_id", "created_at"])
    op.create_index("ix_steps_message_step", "chat_steps", ["message_step_id"])

    op.create_table(
        "tool_calls",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("chat_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("message_step_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tool_name", sa.Text(), nullable=False),
        sa.Column("args", postgresql.JSONB(), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(
                "pending", "completed", "errored", name="tool_call_status", create_type=False
            ),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column("result", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_tool_calls_session", "tool_calls", ["session_id", "created_at"])

    op.create_table(
        "audit_logs",
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
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_audit_user_created", "audit_logs", ["user_id", sa.text("created_at DESC")])
    op.create_index("ix_audit_event_type", "audit_logs", ["event_type", sa.text("created_at DESC")])


def downgrade() -> None:
    op.drop_table("audit_logs")
    op.drop_table("tool_calls")
    op.drop_table("chat_steps")
    op.drop_table("chat_sessions")
    op.drop_table("byok_keys")
    op.drop_table("ledger_transactions")
    op.drop_table("wallets")
    op.drop_table("subscriptions")
    op.drop_table("users")
    for enum_name in (
        "tool_call_status",
        "chat_role",
        "chat_mode",
        "byok_key_status",
        "ledger_tx_type",
        "subscription_status",
    ):
        op.execute(f"DROP TYPE IF EXISTS {enum_name}")
