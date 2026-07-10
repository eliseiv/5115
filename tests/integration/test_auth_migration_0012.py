"""Integration: alembic migration 0012 (auth_identities, Sign in with Apple) — ADR-043 §4.

Hermetic and ISOLATED: spins its OWN throwaway PostgreSQL container so upgrade/downgrade cannot
corrupt the shared session container. Verifies the chain is SINGLE-HEAD up to 0012, that the
upgrade creates auth_identities with the expected columns and a UNIQUE(provider, subject) index,
that the constraint actually rejects a duplicate (provider, subject), and that downgrade drops
exactly that table (reversible) while users / auth_devices stay.

SYNC (no pytest-asyncio): alembic's env.py drives migrations under asyncio.run itself, which
cannot nest inside a running test loop (mirrors test_auth_migration_0005 and conftest._migrated).
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


def _columns(url: str, table: str) -> set[str]:
    async def _run() -> set[str]:
        engine = create_async_engine(url, future=True, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                cols = await conn.run_sync(lambda sc: inspect(sc).get_columns(table))
                return {c["name"] for c in cols}
        finally:
            await engine.dispose()

    return asyncio.run(_run())


def _index_names(url: str, table: str) -> set[str]:
    async def _run() -> set[str]:
        engine = create_async_engine(url, future=True, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                idx = await conn.run_sync(lambda sc: inspect(sc).get_indexes(table))
                return {i["name"] for i in idx}
        finally:
            await engine.dispose()

    return asyncio.run(_run())


def test_migrations_single_head() -> None:
    # The chain must have exactly ONE head (0012 extends 0011 with the full revision id). A second
    # head would mean a broken/branched chain (a real defect, blame:code).
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config("alembic.ini")
    script = ScriptDirectory.from_config(cfg)
    heads = script.get_heads()
    assert len(heads) == 1, f"expected single head, got {heads}"


def test_migration_0012_apply_creates_auth_identities(isolated_pg: str) -> None:
    from alembic import command

    cfg = _alembic_config(isolated_pg)
    command.upgrade(cfg, "head")
    tables = _table_names(isolated_pg)
    assert "auth_identities" in tables
    # The prior chain is intact (users / auth_devices from earlier migrations).
    assert {"users", "auth_devices"} <= tables


def test_migration_0012_columns_and_unique_index(isolated_pg: str) -> None:
    from alembic import command

    cfg = _alembic_config(isolated_pg)
    command.upgrade(cfg, "head")
    cols = _columns(isolated_pg, "auth_identities")
    assert {"id", "user_id", "provider", "subject", "email", "created_at"} <= cols
    indexes = _index_names(isolated_pg, "auth_identities")
    assert "ux_auth_identities_provider_subject" in indexes
    assert "ix_auth_identities_user" in indexes


def test_unique_provider_subject_rejects_duplicate(isolated_pg: str) -> None:
    from alembic import command

    cfg = _alembic_config(isolated_pg)
    command.upgrade(cfg, "head")

    async def _run() -> None:
        # SQLAlchemy's async engine wraps asyncpg's UniqueViolationError in IntegrityError.
        from sqlalchemy.exc import IntegrityError

        engine = create_async_engine(isolated_pg, future=True, poolclass=NullPool)
        try:
            # Seed two users and the first identity in a committed transaction.
            async with engine.begin() as conn:
                user1 = (
                    await conn.execute(
                        text("INSERT INTO users (id) VALUES (gen_random_uuid()) RETURNING id")
                    )
                ).scalar_one()
                user2 = (
                    await conn.execute(
                        text("INSERT INTO users (id) VALUES (gen_random_uuid()) RETURNING id")
                    )
                ).scalar_one()
                await conn.execute(
                    text(
                        "INSERT INTO auth_identities (user_id, provider, subject) "
                        "VALUES (:u, 'apple', 'dup-subject')"
                    ),
                    {"u": str(user1)},
                )
            # A duplicate (provider, subject) in a SEPARATE transaction must violate the UNIQUE
            # index; the failed transaction rolls back on context exit (no poisoned outer commit).
            with pytest.raises(IntegrityError):
                async with engine.begin() as conn:
                    await conn.execute(
                        text(
                            "INSERT INTO auth_identities (user_id, provider, subject) "
                            "VALUES (:u, 'apple', 'dup-subject')"
                        ),
                        {"u": str(user2)},
                    )
        finally:
            await engine.dispose()

    asyncio.run(_run())


def test_migration_0012_downgrade_drops_only_auth_identities(isolated_pg: str) -> None:
    from alembic import command

    cfg = _alembic_config(isolated_pg)
    command.upgrade(cfg, "0012_auth_identities")
    assert "auth_identities" in _table_names(isolated_pg)

    # Roll back exactly to 0011: auth_identities disappears, users / auth_devices stay.
    command.downgrade(cfg, "0011_workspaces")
    after_down = _table_names(isolated_pg)
    assert "auth_identities" not in after_down
    assert {"users", "auth_devices"} <= after_down

    # Re-upgrade restores it (reversible).
    command.upgrade(cfg, "0012_auth_identities")
    assert "auth_identities" in _table_names(isolated_pg)
