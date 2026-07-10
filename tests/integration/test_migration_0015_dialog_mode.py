"""Integration: alembic migration 0015 (dialog_mode enum + two columns, ADR-055).

Hermetic and ISOLATED: spins its OWN throwaway PostgreSQL container so the CREATE TYPE / ADD COLUMN
cannot affect the shared session container other tests rely on (mirrors test_migration_0010).
Verifies:
- single head: the migration graph has exactly one head (no fork) and 0015 is on the chain;
- upgrade creates the ``dialog_mode`` enum with EXACTLY the 4 ADR-055 values;
- ``chat_sessions.dialog_mode`` is NOT NULL with server_default 'smart'; an EXISTING pre-0015 row
  is backfilled to 'smart' (server_default covers it, no data-migration);
- ``user_preferences.default_dialog_mode`` is NOT NULL with server_default 'smart'; a pre-existing
  row is backfilled to 'smart';
- a NEW row inserted without specifying the column gets 'smart';
- downgrade drops BOTH columns and the enum type; re-upgrade is clean.

SYNC tests (no pytest-asyncio): alembic's env.py drives migrations under asyncio.run, which cannot
nest inside a running test loop (mirrors test_migration_0010 + the conftest _migrated fixture).
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

_PREV_REV = "0014_cp_webhook_events"
_THIS_REV = "0015_dialog_mode"
_EXPECTED_VALUES = {"smart", "deep_thinking", "study_learn", "search"}


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


def _enum_values(url: str, enum_name: str) -> set[str]:
    async def _read(conn: Any) -> set[str]:
        rows = await conn.execute(
            text(
                "SELECT e.enumlabel FROM pg_enum e "
                "JOIN pg_type t ON t.oid = e.enumtypid WHERE t.typname = :n"
            ),
            {"n": enum_name},
        )
        return {r[0] for r in rows}

    return asyncio.run(_run_async(url, _read))


def _insert_user(url: str) -> uuid.UUID:
    uid = uuid.uuid4()

    async def _seed(conn: Any) -> None:
        await conn.execute(
            text("INSERT INTO users (id, trial_used) VALUES (:id, false)"), {"id": str(uid)}
        )

    asyncio.run(_run_async(url, _seed))
    return uid


def _insert_session(url: str, uid: uuid.UUID) -> uuid.UUID:
    """Insert a chat_sessions row at the PRE-0015 schema (no dialog_mode column)."""
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


def _insert_preferences(url: str, uid: uuid.UUID) -> None:
    """Insert a user_preferences row at the PRE-0015 schema (no default_dialog_mode column)."""

    async def _seed(conn: Any) -> None:
        await conn.execute(
            text("INSERT INTO user_preferences (user_id) VALUES (:uid)"),
            {"uid": str(uid)},
        )

    asyncio.run(_run_async(url, _seed))


def _scalar(url: str, sql: str, params: dict[str, Any]) -> Any:
    async def _read(conn: Any) -> Any:
        return await conn.scalar(text(sql), params)

    return asyncio.run(_run_async(url, _read))


# --------------------------- single head (no fork at/after 0015) ---------------------------
def test_0015_single_head() -> None:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(Config("alembic.ini"))
    heads = script.get_heads()
    assert len(heads) == 1, f"expected a single migration head (no fork), got {heads}"

    head = heads[0]
    ancestry = {rev.revision for rev in script.walk_revisions("base", head)}
    assert _THIS_REV in ancestry, f"{_THIS_REV} is not an ancestor of head {head}: {ancestry}"


# --------------------------- upgrade: enum + columns + backfill ---------------------------
def test_0015_upgrade_creates_enum_columns_and_backfills(isolated_pg: str) -> None:
    from alembic import command

    cfg = _alembic_config(isolated_pg)
    _reset_to_prev(cfg, isolated_pg)

    # Before 0015: neither column exists, and the enum type is absent.
    assert "dialog_mode" not in _columns(isolated_pg, "chat_sessions")
    assert "default_dialog_mode" not in _columns(isolated_pg, "user_preferences")
    assert _enum_values(isolated_pg, "dialog_mode") == set()

    # Pre-existing rows created before the columns exist (must be backfilled by server_default).
    uid = _insert_user(isolated_pg)
    sid = _insert_session(isolated_pg, uid)
    _insert_preferences(isolated_pg, uid)

    command.upgrade(cfg, _THIS_REV)

    # (1) enum created WHOLE with exactly the 4 ADR-055 values.
    assert _enum_values(isolated_pg, "dialog_mode") == _EXPECTED_VALUES

    # (2) chat_sessions.dialog_mode: present, NOT NULL, server_default 'smart'.
    sess_cols = _columns(isolated_pg, "chat_sessions")
    assert "dialog_mode" in sess_cols
    assert sess_cols["dialog_mode"]["nullable"] is False
    assert "smart" in str(sess_cols["dialog_mode"]["default"])

    # (3) user_preferences.default_dialog_mode: present, NOT NULL, server_default 'smart'.
    pref_cols = _columns(isolated_pg, "user_preferences")
    assert "default_dialog_mode" in pref_cols
    assert pref_cols["default_dialog_mode"]["nullable"] is False
    assert "smart" in str(pref_cols["default_dialog_mode"]["default"])

    # (4) the pre-existing rows are backfilled to 'smart' (no NULLs, no data-migration needed).
    assert (
        _scalar(
            isolated_pg,
            "SELECT dialog_mode FROM chat_sessions WHERE id=:sid",
            {"sid": str(sid)},
        )
        == "smart"
    )
    assert (
        _scalar(
            isolated_pg,
            "SELECT default_dialog_mode FROM user_preferences WHERE user_id=:uid",
            {"uid": str(uid)},
        )
        == "smart"
    )


def test_0015_new_row_defaults_to_smart(isolated_pg: str) -> None:
    from alembic import command

    cfg = _alembic_config(isolated_pg)
    _reset_to_prev(cfg, isolated_pg)
    command.upgrade(cfg, _THIS_REV)

    uid = _insert_user(isolated_pg)
    # A new chat_sessions row inserted WITHOUT specifying dialog_mode → 'smart'.
    sid = uuid.uuid4()

    async def _insert(conn: Any) -> None:
        await conn.execute(
            text(
                "INSERT INTO chat_sessions (id, user_id, mode, assistant_mode) "
                "VALUES (:sid, :uid, 'credits', 'chat')"
            ),
            {"sid": str(sid), "uid": str(uid)},
        )

    asyncio.run(_run_async(isolated_pg, _insert))
    assert (
        _scalar(
            isolated_pg, "SELECT dialog_mode FROM chat_sessions WHERE id=:sid", {"sid": str(sid)}
        )
        == "smart"
    )


# --------------------- downgrade drops columns + enum / re-up clean ---------------------
def test_0015_downgrade_drops_columns_and_enum_and_reupgrade_clean(isolated_pg: str) -> None:
    from alembic import command

    cfg = _alembic_config(isolated_pg)
    _reset_to_prev(cfg, isolated_pg)

    command.upgrade(cfg, _THIS_REV)
    assert "dialog_mode" in _columns(isolated_pg, "chat_sessions")
    assert "default_dialog_mode" in _columns(isolated_pg, "user_preferences")
    assert _enum_values(isolated_pg, "dialog_mode") == _EXPECTED_VALUES

    command.downgrade(cfg, _PREV_REV)
    assert "dialog_mode" not in _columns(isolated_pg, "chat_sessions")
    assert "default_dialog_mode" not in _columns(isolated_pg, "user_preferences")
    assert _enum_values(isolated_pg, "dialog_mode") == set()  # enum type dropped

    # Re-upgrade is clean and idempotent.
    command.upgrade(cfg, _THIS_REV)
    assert "dialog_mode" in _columns(isolated_pg, "chat_sessions")
    assert _enum_values(isolated_pg, "dialog_mode") == _EXPECTED_VALUES
