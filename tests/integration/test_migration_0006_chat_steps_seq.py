"""Integration: alembic migration 0006 (chat_steps.seq, ADR-021/BUG-5) backfill + reversibility.

Hermetic and ISOLATED: spins its OWN throwaway PostgreSQL container so upgrade/downgrade of the
seq column / index swap cannot corrupt the shared session container other tests rely on. Verifies:
- backfill preserves the prior historical order (ROW_NUMBER over created_at, id) for rows that
  share an equal created_at (the same-transaction collision the fix targets);
- after upgrade `seq` is NOT NULL, monotonic, and the identity sequence is advanced above max(seq)
  so new inserts do not collide with backfilled values;
- index swap: ix_steps_session_seq exists, ix_steps_session_created is gone, ix_steps_message_step
  is untouched;
- downgrade drops seq + restores ix_steps_session_created; re-upgrade is clean.

SYNC tests (no pytest-asyncio): alembic's env.py drives migrations under asyncio.run, which cannot
nest inside a running test loop (mirrors test_auth_migration_0005 + the conftest _migrated fixture).
"""

from __future__ import annotations

import asyncio
import datetime
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

_PREV_REV = "0005_embedded_auth_issuer"
_THIS_REV = "0006_chat_steps_seq"


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


async def _run_async(url: str, fn: Any) -> Any:
    engine = create_async_engine(url, future=True, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            return await fn(conn)
    finally:
        await engine.dispose()


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


def _seed_pre_0006_steps(url: str) -> list[uuid.UUID]:
    """Seed a user/session + chat_steps with a SHARED created_at and DESCENDING ids.

    Mirrors the same-transaction collision: several rows share one created_at, and their ids are in
    DESCENDING order so the backfill's (created_at, id) ROW_NUMBER differs from physical/insert
    order. Returns the ids in the EXPECTED post-backfill seq order (ascending by (created_at, id)).
    """
    uid = uuid.uuid4()
    sid = uuid.uuid4()
    msid = uuid.uuid4()
    fixed_ts = datetime.datetime(2026, 6, 4, 12, 0, 0, tzinfo=datetime.UTC)
    # ids chosen so ascending-id order = the list below; we INSERT them in REVERSE to prove the
    # backfill sorts by (created_at, id), not by physical insert order.
    ids_in_expected_order = [
        uuid.UUID("00000000-0000-4000-8000-000000000001"),
        uuid.UUID("00000000-0000-4000-8000-000000000002"),
        uuid.UUID("00000000-0000-4000-8000-000000000003"),
        uuid.UUID("00000000-0000-4000-8000-000000000004"),
    ]

    async def _seed(conn: Any) -> None:
        await conn.execute(
            text("INSERT INTO users (id, trial_used) VALUES (:id, false)"), {"id": str(uid)}
        )
        await conn.execute(
            text(
                "INSERT INTO chat_sessions (id, user_id, project_id, mode) "
                "VALUES (:id, :uid, 'p', 'credits')"
            ),
            {"id": str(sid), "uid": str(uid)},
        )
        # Insert in REVERSE id order, all with the SAME created_at.
        for step_id in reversed(ids_in_expected_order):
            await conn.execute(
                text(
                    "INSERT INTO chat_steps "
                    "(id, session_id, message_step_id, role, payload, created_at) "
                    "VALUES (:id, :sid, :msid, 'user', CAST('{}' AS JSONB), :ts)"
                ),
                {"id": str(step_id), "sid": str(sid), "msid": str(msid), "ts": fixed_ts},
            )

    asyncio.run(_run_async(url, _seed))
    return ids_in_expected_order


def _seq_by_id(url: str) -> dict[str, int]:
    async def _run() -> dict[str, int]:
        engine = create_async_engine(url, future=True, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                res = await conn.execute(text("SELECT id, seq FROM chat_steps"))
                return {str(r[0]): r[1] for r in res.fetchall()}
        finally:
            await engine.dispose()

    return asyncio.run(_run())


def _reset_to_prev(cfg: Any) -> None:
    """Reset the shared isolated container to a clean schema at 0005 (no seq, no seeded rows)."""
    from alembic import command

    command.downgrade(cfg, "base")
    command.upgrade(cfg, _PREV_REV)


def test_0006_backfill_preserves_historical_order_and_monotonic_not_null(isolated_pg: str) -> None:
    from alembic import command

    cfg = _alembic_config(isolated_pg)
    # Bring schema to 0005 (no seq column yet), seed historical rows, THEN apply 0006.
    _reset_to_prev(cfg)
    assert "seq" not in _columns(isolated_pg, "chat_steps")
    expected_order = _seed_pre_0006_steps(isolated_pg)

    command.upgrade(cfg, _THIS_REV)

    # seq column exists and is NOT NULL.
    cols = _columns(isolated_pg, "chat_steps")
    assert "seq" in cols
    assert cols["seq"]["nullable"] is False

    # Backfill order: seq must be ascending by (created_at, id) — the prior historical order.
    seqs = _seq_by_id(isolated_pg)
    ordered_seqs = [seqs[str(i)] for i in expected_order]
    assert ordered_seqs == sorted(
        ordered_seqs
    ), "backfill seq not monotonic in (created_at,id) order"
    assert len(set(ordered_seqs)) == len(ordered_seqs), "backfill seq not unique"
    # Strictly increasing in the expected (created_at, id) order.
    assert all(a < b for a, b in zip(ordered_seqs, ordered_seqs[1:], strict=False))


def test_0006_new_insert_after_backfill_does_not_collide(isolated_pg: str) -> None:
    """The identity sequence is advanced above max(seq); a new INSERT gets a fresh greater seq."""
    from alembic import command

    cfg = _alembic_config(isolated_pg)
    _reset_to_prev(cfg)
    _seed_pre_0006_steps(isolated_pg)
    command.upgrade(cfg, _THIS_REV)

    seqs_before = _seq_by_id(isolated_pg)
    max_before = max(seqs_before.values())

    new_id = uuid.uuid4()

    async def _insert_new(conn: Any) -> None:
        sid = await conn.scalar(text("SELECT id FROM chat_sessions LIMIT 1"))
        await conn.execute(
            text(
                "INSERT INTO chat_steps (id, session_id, message_step_id, role, payload) "
                "VALUES (:id, :sid, :msid, 'assistant', CAST('{}' AS JSONB))"
            ),
            {"id": str(new_id), "sid": str(sid), "msid": str(uuid.uuid4())},
        )

    asyncio.run(_run_async(isolated_pg, _insert_new))

    seqs_after = _seq_by_id(isolated_pg)
    assert seqs_after[str(new_id)] > max_before, "new insert collided with backfilled seq range"


def test_0006_index_swap(isolated_pg: str) -> None:
    from alembic import command

    cfg = _alembic_config(isolated_pg)
    _reset_to_prev(cfg)
    command.upgrade(cfg, _THIS_REV)
    idx = _index_names(isolated_pg, "chat_steps")
    assert "ix_steps_session_seq" in idx
    assert "ix_steps_session_created" not in idx
    assert "ix_steps_message_step" in idx  # untouched


def test_0006_downgrade_then_reupgrade_clean(isolated_pg: str) -> None:
    from alembic import command

    cfg = _alembic_config(isolated_pg)
    _reset_to_prev(cfg)
    command.upgrade(cfg, _THIS_REV)
    assert "seq" in _columns(isolated_pg, "chat_steps")

    # Downgrade one revision (0006 → 0005): seq dropped, old index restored.
    command.downgrade(cfg, _PREV_REV)
    cols = _columns(isolated_pg, "chat_steps")
    assert "seq" not in cols
    idx = _index_names(isolated_pg, "chat_steps")
    assert "ix_steps_session_created" in idx
    assert "ix_steps_session_seq" not in idx
    assert "ix_steps_message_step" in idx

    # Re-upgrade restores seq + the new index (reversible).
    command.upgrade(cfg, _THIS_REV)
    cols2 = _columns(isolated_pg, "chat_steps")
    assert "seq" in cols2 and cols2["seq"]["nullable"] is False
    idx2 = _index_names(isolated_pg, "chat_steps")
    assert "ix_steps_session_seq" in idx2
    assert "ix_steps_session_created" not in idx2


def test_0006_empty_table_upgrade_clean(isolated_pg: str) -> None:
    """COALESCE in setval handles an empty chat_steps: upgrade on a fresh DB must not error."""
    from alembic import command

    cfg = _alembic_config(isolated_pg)
    # Fresh chain to 0005 then 0006 with NO seeded steps.
    _reset_to_prev(cfg)
    command.upgrade(cfg, _THIS_REV)
    cols = _columns(isolated_pg, "chat_steps")
    assert "seq" in cols and cols["seq"]["nullable"] is False
