"""Integration: alembic migration 0010 (chat_sessions.model nullable column, ADR-034 §3).

Hermetic and ISOLATED: spins its OWN throwaway PostgreSQL container so the ADD/DROP COLUMN cannot
affect the shared session container other tests rely on (mirrors test_migration_0009). Verifies:
- single head: the migration graph has exactly one head (no fork) and 0010 is on the chain;
- upgrade adds a NULLABLE `model` column with NO backfill — an existing row stays model=NULL;
- a row inserted after upgrade without specifying `model` is NULL (= instance default);
- downgrade drops the column.

SYNC tests (no pytest-asyncio): alembic's env.py drives migrations under asyncio.run, which cannot
nest inside a running test loop (mirrors test_migration_0009 + the conftest _migrated fixture).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

_PREV_REV = "0009_notifications_default_false"
_THIS_REV = "0010_chat_sessions_model"


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


def _columns(url: str, table: str) -> dict[str, Any]:
    async def _run() -> dict[str, Any]:
        engine = create_async_engine(url, future=True, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                cols = await conn.run_sync(lambda sc: inspect(sc).get_columns(table))
                return {c["name"]: c for c in cols}
        finally:
            await engine.dispose()

    return asyncio.run(_run())


async def _run_async(url: str, fn: Any) -> Any:
    engine = create_async_engine(url, future=True, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            return await fn(conn)
    finally:
        await engine.dispose()


def _reset_to_prev(cfg: Any, url: str) -> None:
    from alembic import command

    async def _drop_all(conn: Any) -> None:
        await conn.execute(text("DROP SCHEMA public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))

    asyncio.run(_run_async(url, _drop_all))
    command.upgrade(cfg, _PREV_REV)


def _insert_user(url: str) -> uuid.UUID:
    uid = uuid.uuid4()

    async def _seed(conn: Any) -> None:
        await conn.execute(
            text("INSERT INTO users (id, trial_used) VALUES (:id, false)"), {"id": str(uid)}
        )

    asyncio.run(_run_async(url, _seed))
    return uid


def _insert_session(url: str, uid: uuid.UUID) -> uuid.UUID:
    """Insert a chat_sessions row at the PRE-0010 schema (no model column)."""
    sid = uuid.uuid4()

    async def _seed(conn: Any) -> None:
        await conn.execute(
            text(
                "INSERT INTO chat_sessions (id, user_id, mode, assistant_mode) "
                "VALUES (:sid, :uid, 'credits', 'chat')"
            ),
            {"sid": str(sid), "uid": str(uid)},
        )

    asyncio.run(_run_async(url, _seed))
    return sid


# --------------------------- single head (no fork at/after 0010) ---------------------------
def test_0010_single_head() -> None:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(Config("alembic.ini"))
    heads = script.get_heads()
    assert len(heads) == 1, f"expected a single migration head (no fork), got {heads}"

    head = heads[0]
    ancestry = {rev.revision for rev in script.walk_revisions("base", head)}
    assert _THIS_REV in ancestry, f"{_THIS_REV} is not an ancestor of head {head}: {ancestry}"


# --------------------------- upgrade adds nullable column, no backfill ---------------------------
def test_0010_upgrade_adds_nullable_model_no_backfill(isolated_pg: str) -> None:
    from alembic import command

    cfg = _alembic_config(isolated_pg)
    _reset_to_prev(cfg, isolated_pg)

    # Before 0010: chat_sessions has NO `model` column.
    assert "model" not in _columns(isolated_pg, "chat_sessions")

    # An existing session row created before the column exists.
    uid = _insert_user(isolated_pg)
    sid = _insert_session(isolated_pg, uid)

    command.upgrade(cfg, _THIS_REV)

    cols = _columns(isolated_pg, "chat_sessions")
    assert "model" in cols
    # The column is nullable.
    assert cols["model"]["nullable"] is True

    # NO backfill: the pre-existing row's model is NULL after the add-column.
    async def _read(conn: Any) -> Any:
        return await conn.scalar(
            text("SELECT model FROM chat_sessions WHERE id=:sid"), {"sid": str(sid)}
        )

    assert asyncio.run(_run_async(isolated_pg, _read)) is None

    # A new row inserted WITHOUT specifying model is NULL (= instance default).
    sid2 = uuid.uuid4()

    async def _insert(conn: Any) -> None:
        await conn.execute(
            text(
                "INSERT INTO chat_sessions (id, user_id, mode, assistant_mode) "
                "VALUES (:sid, :uid, 'credits', 'chat')"
            ),
            {"sid": str(sid2), "uid": str(uid)},
        )

    asyncio.run(_run_async(isolated_pg, _insert))

    async def _read2(conn: Any) -> Any:
        return await conn.scalar(
            text("SELECT model FROM chat_sessions WHERE id=:sid"), {"sid": str(sid2)}
        )

    assert asyncio.run(_run_async(isolated_pg, _read2)) is None


# --------------------------- downgrade drops the column / re-up clean ---------------------------
def test_0010_downgrade_drops_column_and_reupgrade_clean(isolated_pg: str) -> None:
    from alembic import command

    cfg = _alembic_config(isolated_pg)
    _reset_to_prev(cfg, isolated_pg)

    command.upgrade(cfg, _THIS_REV)
    assert "model" in _columns(isolated_pg, "chat_sessions")

    command.downgrade(cfg, _PREV_REV)
    assert "model" not in _columns(isolated_pg, "chat_sessions")

    # Re-upgrade is clean and idempotent.
    command.upgrade(cfg, _THIS_REV)
    assert "model" in _columns(isolated_pg, "chat_sessions")
