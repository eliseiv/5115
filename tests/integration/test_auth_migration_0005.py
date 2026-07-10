"""Integration: alembic migration 0005 (embedded auth-issuer tables) apply + rollback (ADR-018).

Hermetic and ISOLATED: spins its OWN throwaway PostgreSQL container so upgrade/downgrade cannot
corrupt the shared session container other tests rely on. Verifies the single-head chain
0001→0002→0003→0004→0005 applies cleanly, creates auth_devices / auth_refresh_tokens, and that
downgrade -1 drops exactly those two tables (reversible), then re-upgrade restores them.

These tests are SYNC (no pytest-asyncio): alembic's env.py drives migrations under
``asyncio.run`` itself, which cannot be nested inside a running test event loop (mirrors the sync
``_migrated`` fixture in conftest). Async inspection is run in its own fresh loop via asyncio.run.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest
from sqlalchemy import inspect
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


def test_migration_0005_apply_creates_auth_tables(isolated_pg: str) -> None:
    from alembic import command

    cfg = _alembic_config(isolated_pg)
    command.upgrade(cfg, "head")
    tables = _table_names(isolated_pg)
    assert "auth_devices" in tables
    assert "auth_refresh_tokens" in tables
    # Sanity: the prior chain tables are present too (full 0001→0005 ran).
    assert "users" in tables


def test_migration_0005_downgrade_drops_only_auth_tables(isolated_pg: str) -> None:
    from alembic import command

    cfg = _alembic_config(isolated_pg)
    # Pin to the EXPLICIT 0005 revision, not "head": head now advances past 0005 (e.g. 0006
    # chat_steps.seq, ADR-021), so a relative `downgrade -1` would only undo the newest migration
    # and leave the auth tables in place. This test validates the 0005 <-> 0004 boundary
    # specifically, so it targets those exact revisions and is immune to future head changes.
    command.upgrade(cfg, "0005_embedded_auth_issuer")
    assert {"auth_devices", "auth_refresh_tokens"} <= _table_names(isolated_pg)

    # Roll back exactly to 0004: the two auth tables disappear, users stays.
    command.downgrade(cfg, "0004_figma_gap_sprint1")
    after_down = _table_names(isolated_pg)
    assert "auth_devices" not in after_down
    assert "auth_refresh_tokens" not in after_down
    assert "users" in after_down

    # Re-upgrade to 0005 restores them (reversible).
    command.upgrade(cfg, "0005_embedded_auth_issuer")
    after_up = _table_names(isolated_pg)
    assert {"auth_devices", "auth_refresh_tokens"} <= after_up


def test_migration_0005_columns_present(isolated_pg: str) -> None:
    from alembic import command

    cfg = _alembic_config(isolated_pg)
    command.upgrade(cfg, "head")
    devices = _columns(isolated_pg, "auth_devices")
    assert {"device_id", "user_id", "created_at", "last_seen_at"} <= devices
    refresh = _columns(isolated_pg, "auth_refresh_tokens")
    assert {
        "id",
        "user_id",
        "device_id",
        "token_hash",
        "expires_at",
        "used_at",
        "revoked_at",
        "created_at",
    } <= refresh
