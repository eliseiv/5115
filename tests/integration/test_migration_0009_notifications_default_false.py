"""Integration: alembic migration 0009 (user_preferences.notifications_enabled default, ADR-032).

Hermetic and ISOLATED: spins its OWN throwaway PostgreSQL container so the default-only ALTER of
the column cannot affect the shared session container other tests rely on. Verifies:
- single head: the migration graph has exactly one head (no fork) and 0009 is on the chain;
- upgrade flips server_default true -> false (privacy-by-default for new / no-row users);
- downgrade restores server_default true;
- NO backfill: an existing row with an EXPLICIT notifications_enabled=true is NOT touched by the
  upgrade (the column default change must not overwrite stored user choices, ADR-032 §1);
- a row INSERTed after upgrade without specifying the column gets the new default false.

SYNC tests (no pytest-asyncio): alembic's env.py drives migrations under asyncio.run, which cannot
nest inside a running test loop (mirrors test_migration_0007_project_id_nullable + the conftest
_migrated fixture).
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

_PREV_REV = "0008_adapty_webhook_events"
_THIS_REV = "0009_notifications_default_false"


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


def _column_default(url: str, table: str, column: str) -> str | None:
    """Return the raw server default string for a column (e.g. 'true' / 'false'), or None."""

    async def _run() -> str | None:
        engine = create_async_engine(url, future=True, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                cols = await conn.run_sync(lambda sc: inspect(sc).get_columns(table))
                by_name = {c["name"]: c for c in cols}
                return by_name[column].get("default")
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
    # Drop the whole schema first (unconditional) so the reset is robust to whatever state a
    # prior test left the shared module container in, then migrate up to the revision before 0009.
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


def _normalize(default: str | None) -> str | None:
    """Postgres reports boolean defaults as 'true'/'false' (possibly with whitespace)."""
    return default.strip().lower() if isinstance(default, str) else default


# --------------------------- single head (no fork at/after 0009) ---------------------------
def test_0009_single_head() -> None:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(Config("alembic.ini"))
    heads = script.get_heads()
    assert len(heads) == 1, f"expected a single migration head (no fork), got {heads}"

    head = heads[0]
    ancestry = {rev.revision for rev in script.walk_revisions("base", head)}
    assert _THIS_REV in ancestry, f"{_THIS_REV} is not an ancestor of head {head}: {ancestry}"


# --------------------------- upgrade flips default true -> false ---------------------------
def test_0009_upgrade_sets_default_false(isolated_pg: str) -> None:
    from alembic import command

    cfg = _alembic_config(isolated_pg)
    _reset_to_prev(cfg, isolated_pg)
    # Before 0009: server_default is true.
    assert (
        _normalize(_column_default(isolated_pg, "user_preferences", "notifications_enabled"))
        == "true"
    )

    command.upgrade(cfg, _THIS_REV)
    assert (
        _normalize(_column_default(isolated_pg, "user_preferences", "notifications_enabled"))
        == "false"
    )

    # A row inserted WITHOUT specifying the column now gets the new default (false).
    uid = _insert_user(isolated_pg)

    async def _insert_default(conn: Any) -> None:
        await conn.execute(
            text("INSERT INTO user_preferences (user_id) VALUES (:uid)"),
            {"uid": str(uid)},
        )

    asyncio.run(_run_async(isolated_pg, _insert_default))

    async def _read(conn: Any) -> Any:
        return await conn.scalar(
            text("SELECT notifications_enabled FROM user_preferences WHERE user_id=:uid"),
            {"uid": str(uid)},
        )

    assert asyncio.run(_run_async(isolated_pg, _read)) is False


# --------------------------- NO backfill: explicit true row is preserved ---------------------
def test_0009_upgrade_does_not_backfill_existing_true_row(isolated_pg: str) -> None:
    from alembic import command

    cfg = _alembic_config(isolated_pg)
    _reset_to_prev(cfg, isolated_pg)

    # Before upgrade: a user explicitly opted IN (notifications_enabled = true).
    uid = _insert_user(isolated_pg)

    async def _insert_true(conn: Any) -> None:
        await conn.execute(
            text(
                "INSERT INTO user_preferences (user_id, notifications_enabled) "
                "VALUES (:uid, true)"
            ),
            {"uid": str(uid)},
        )

    asyncio.run(_run_async(isolated_pg, _insert_true))

    command.upgrade(cfg, _THIS_REV)

    # ADR-032 §1: the default-only ALTER must NOT touch existing rows (no UPDATE / backfill).
    async def _read(conn: Any) -> Any:
        return await conn.scalar(
            text("SELECT notifications_enabled FROM user_preferences WHERE user_id=:uid"),
            {"uid": str(uid)},
        )

    assert asyncio.run(_run_async(isolated_pg, _read)) is True


# --------------------------- clean up/down/re-up on empty DB ---------------------------
def test_0009_upgrade_downgrade_reupgrade_clean(isolated_pg: str) -> None:
    from alembic import command

    cfg = _alembic_config(isolated_pg)
    _reset_to_prev(cfg, isolated_pg)

    command.upgrade(cfg, _THIS_REV)
    assert (
        _normalize(_column_default(isolated_pg, "user_preferences", "notifications_enabled"))
        == "false"
    )

    # downgrade restores the previous contract default (true).
    command.downgrade(cfg, _PREV_REV)
    assert (
        _normalize(_column_default(isolated_pg, "user_preferences", "notifications_enabled"))
        == "true"
    )

    # Re-upgrade is clean and idempotent.
    command.upgrade(cfg, _THIS_REV)
    assert (
        _normalize(_column_default(isolated_pg, "user_preferences", "notifications_enabled"))
        == "false"
    )
