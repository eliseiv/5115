"""Integration: alembic migration 0007 (chat_sessions.project_id DROP NOT NULL, ADR-022 §3).

Hermetic and ISOLATED: spins its OWN throwaway PostgreSQL container so upgrade/downgrade of the
column constraint cannot affect the shared session container other tests rely on. Verifies:
- single head: the migration graph has exactly one head (no fork from 0007);
- upgrade relaxes NOT NULL so a session row with project_id NULL can be inserted;
- downgrade is fail-loud when NULL rows exist (Postgres rejects SET NOT NULL);
- on a clean DB upgrade → downgrade → re-upgrade is clean and idempotent.

SYNC tests (no pytest-asyncio): alembic's env.py drives migrations under asyncio.run, which cannot
nest inside a running test loop (mirrors test_migration_0006_chat_steps_seq + the conftest
_migrated fixture).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

_PREV_REV = "0006_chat_steps_seq"
_THIS_REV = "0007_project_id_nullable"


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


def _columns(url: str, table: str) -> dict[str, dict[str, Any]]:
    async def _run() -> dict[str, dict[str, Any]]:
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
    # A prior test may have left project_id NULL rows at rev 0007 (the fail-loud downgrade test);
    # the 0007->0006 step would then fail. Drop the whole schema first (DROP base is unconditional),
    # so the reset is robust to whatever state the shared module container is in.
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


# --------------------------- single head (no fork at/after 0007) ---------------------------
def test_0007_single_head() -> None:
    """The migration graph stays linear (exactly one head) and 0007 is not a fork point.

    The original assertion pinned the head to 0007, but later sprints legitimately chain new
    revisions onto it (0008_adapty_webhook_events extends the single linear history, ADR-029).
    The invariant this test protects is "no fork" — exactly one head — and that 0007 is an
    ancestor of that head (i.e. 0008 builds on 0007, not in parallel). It must NOT re-pin to a
    moving tip every time a migration is added.
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(Config("alembic.ini"))
    heads = script.get_heads()
    assert len(heads) == 1, f"expected a single migration head (no fork), got {heads}"

    # 0007 must be on the path from the single head back to base (linear chain through 0007).
    head = heads[0]
    ancestry = {rev.revision for rev in script.walk_revisions("base", head)}
    assert _THIS_REV in ancestry, f"{_THIS_REV} is not an ancestor of head {head}: {ancestry}"


# --------------------------- upgrade relaxes NOT NULL ---------------------------
def test_0007_upgrade_makes_project_id_nullable(isolated_pg: str) -> None:
    from alembic import command

    cfg = _alembic_config(isolated_pg)
    _reset_to_prev(cfg, isolated_pg)
    # Before 0007: project_id is NOT NULL.
    assert _columns(isolated_pg, "chat_sessions")["project_id"]["nullable"] is False

    command.upgrade(cfg, _THIS_REV)
    assert _columns(isolated_pg, "chat_sessions")["project_id"]["nullable"] is True

    # A «чистый чат» session row with project_id NULL is now insertable.
    uid = _insert_user(isolated_pg)
    sid = uuid.uuid4()

    async def _insert_null_project(conn: Any) -> None:
        await conn.execute(
            text(
                "INSERT INTO chat_sessions (id, user_id, project_id, mode) "
                "VALUES (:id, :uid, NULL, 'credits')"
            ),
            {"id": str(sid), "uid": str(uid)},
        )

    asyncio.run(_run_async(isolated_pg, _insert_null_project))

    async def _read(conn: Any) -> Any:
        return await conn.scalar(
            text("SELECT project_id FROM chat_sessions WHERE id=:id"), {"id": str(sid)}
        )

    assert asyncio.run(_run_async(isolated_pg, _read)) is None


# --------------------------- downgrade fails loud on NULL rows ---------------------------
def test_0007_downgrade_fails_loud_with_null_rows(isolated_pg: str) -> None:
    from alembic import command

    cfg = _alembic_config(isolated_pg)
    _reset_to_prev(cfg, isolated_pg)
    command.upgrade(cfg, _THIS_REV)

    uid = _insert_user(isolated_pg)
    sid = uuid.uuid4()

    async def _insert_null_project(conn: Any) -> None:
        await conn.execute(
            text(
                "INSERT INTO chat_sessions (id, user_id, project_id, mode) "
                "VALUES (:id, :uid, NULL, 'credits')"
            ),
            {"id": str(sid), "uid": str(uid)},
        )

    asyncio.run(_run_async(isolated_pg, _insert_null_project))

    # downgrade SET NOT NULL must FAIL (fail-loud, not silent data loss) while a NULL row exists.
    with pytest.raises(DBAPIError):
        command.downgrade(cfg, _PREV_REV)

    # The column is still nullable (the failed ALTER did not partially apply).
    assert _columns(isolated_pg, "chat_sessions")["project_id"]["nullable"] is True


# --------------------------- clean up/down/re-up on empty DB ---------------------------
def test_0007_upgrade_downgrade_reupgrade_clean(isolated_pg: str) -> None:
    from alembic import command

    cfg = _alembic_config(isolated_pg)
    _reset_to_prev(cfg, isolated_pg)

    command.upgrade(cfg, _THIS_REV)
    assert _columns(isolated_pg, "chat_sessions")["project_id"]["nullable"] is True

    # No NULL rows → downgrade restores NOT NULL cleanly.
    command.downgrade(cfg, _PREV_REV)
    assert _columns(isolated_pg, "chat_sessions")["project_id"]["nullable"] is False

    # Re-upgrade is clean and idempotent.
    command.upgrade(cfg, _THIS_REV)
    assert _columns(isolated_pg, "chat_sessions")["project_id"]["nullable"] is True
