"""Integration: alembic migration 0013 (byok_keys.provider, multi-provider BYOK) — ADR-044 §4.

Hermetic and ISOLATED: spins its OWN throwaway PostgreSQL container so upgrade/downgrade cannot
corrupt the shared session container. Verifies the chain is SINGLE-HEAD at 0013, that the upgrade
adds a nullable ``provider`` TEXT column to ``byok_keys`` (expand-only, no backfill), that a legacy
row keeps ``provider IS NULL`` and a fresh row can store ``anthropic``/``openai``, and that the
downgrade drops exactly that column (reversible) leaving ``byok_keys`` and the rest intact.

SYNC (no pytest-asyncio): alembic's env.py drives migrations under asyncio.run itself, which cannot
nest inside a running test loop (mirrors test_auth_migration_0012 and conftest._migrated).
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool


@pytest.fixture(scope="module")
def isolated_pg() -> Iterator[str]:
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine", driver="asyncpg") as pg:
        yield pg.get_connection_url()


def _alembic_config(url: str):
    from alembic.config import Config

    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def _table_names(url: str) -> set[str]:
    async def _run() -> set[str]:
        engine = create_async_engine(url, future=True, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                return set(await conn.run_sync(lambda sc: inspect(sc).get_table_names()))
        finally:
            await engine.dispose()

    return asyncio.run(_run())


def _columns(url: str, table: str) -> dict[str, bool]:
    """Return {column_name: nullable} for a table."""

    async def _run() -> dict[str, bool]:
        engine = create_async_engine(url, future=True, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                cols = await conn.run_sync(lambda sc: inspect(sc).get_columns(table))
                return {c["name"]: bool(c["nullable"]) for c in cols}
        finally:
            await engine.dispose()

    return asyncio.run(_run())


def test_migrations_single_head() -> None:
    """The chain has exactly ONE head and 0013 exists with its FULL revision id (ADR-044 §4).

    Do NOT assert the head equals 0013: the head advances with every new migration (0014+). The
    single-head invariant plus the presence of 0013's full (non-truncated) id is what this test
    guards — independent of which revision is currently latest.
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config("alembic.ini")
    script = ScriptDirectory.from_config(cfg)
    heads = script.get_heads()
    assert len(heads) == 1, f"expected single head, got {heads}"
    assert script.get_revision("0013_byok_provider").revision == "0013_byok_provider"


def test_migration_0013_adds_nullable_provider_column(isolated_pg: str) -> None:
    from alembic import command

    cfg = _alembic_config(isolated_pg)
    command.upgrade(cfg, "head")
    cols = _columns(isolated_pg, "byok_keys")
    assert "provider" in cols
    # Expand-only: the new column is NULLABLE (legacy rows stay NULL, no backfill).
    assert cols["provider"] is True
    # Pre-existing columns are intact.
    assert {"user_id", "encrypted_key", "encrypted_dek", "nonce", "key_status", "enabled"} <= set(
        cols
    )


def test_provider_column_stores_values_and_defaults_null(isolated_pg: str) -> None:
    from alembic import command

    cfg = _alembic_config(isolated_pg)
    command.upgrade(cfg, "head")

    async def _run() -> None:
        engine = create_async_engine(isolated_pg, future=True, poolclass=NullPool)
        try:
            async with engine.begin() as conn:
                uid_legacy = (
                    await conn.execute(
                        text("INSERT INTO users (id) VALUES (gen_random_uuid()) RETURNING id")
                    )
                ).scalar_one()
                uid_fresh = (
                    await conn.execute(
                        text("INSERT INTO users (id) VALUES (gen_random_uuid()) RETURNING id")
                    )
                ).scalar_one()
                # Legacy row: provider omitted → NULL.
                await conn.execute(
                    text(
                        "INSERT INTO byok_keys "
                        "(user_id, encrypted_key, encrypted_dek, nonce, key_status, enabled) "
                        "VALUES (:u, :k, :d, :n, 'valid', false)"
                    ),
                    {"u": str(uid_legacy), "k": b"x", "d": b"y", "n": b"z"},
                )
                # Fresh row: provider stored explicitly.
                await conn.execute(
                    text(
                        "INSERT INTO byok_keys "
                        "(user_id, encrypted_key, encrypted_dek, nonce, key_status, enabled, "
                        "provider) VALUES (:u, :k, :d, :n, 'valid', false, 'anthropic')"
                    ),
                    {"u": str(uid_fresh), "k": b"x", "d": b"y", "n": b"z"},
                )
            async with engine.connect() as conn:
                legacy = await conn.scalar(
                    text("SELECT provider FROM byok_keys WHERE user_id=:u"), {"u": str(uid_legacy)}
                )
                fresh = await conn.scalar(
                    text("SELECT provider FROM byok_keys WHERE user_id=:u"), {"u": str(uid_fresh)}
                )
                assert legacy is None  # legacy NULL (no backfill)
                assert fresh == "anthropic"
        finally:
            await engine.dispose()

    asyncio.run(_run())


def test_migration_0013_downgrade_drops_only_provider_column(isolated_pg: str) -> None:
    from alembic import command

    cfg = _alembic_config(isolated_pg)
    command.upgrade(cfg, "0013_byok_provider")
    assert "provider" in _columns(isolated_pg, "byok_keys")

    # Roll back exactly to 0012: the provider column disappears, byok_keys (and users) stay.
    command.downgrade(cfg, "0012_auth_identities")
    after_down = _columns(isolated_pg, "byok_keys")
    assert "provider" not in after_down
    assert {"user_id", "encrypted_key", "key_status", "enabled"} <= set(after_down)
    assert "byok_keys" in _table_names(isolated_pg)

    # Re-upgrade restores it (reversible).
    command.upgrade(cfg, "0013_byok_provider")
    assert "provider" in _columns(isolated_pg, "byok_keys")
