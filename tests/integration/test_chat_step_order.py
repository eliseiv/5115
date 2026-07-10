"""Integration tests for ADR-021 deterministic step order via chat_steps.seq (BUG-5).

Real PostgreSQL container. These cover the NORMATIVE ADR-021 test requirement (Consequences
§Тестовое требование, scenarios 1 & 4):

- Root cause: in the server-side tool-loop tool_use + tool_result are written in ONE transaction
  → identical transaction-time created_at. The old order key (created_at, id) tie-breaks on a
  random UUID v4 → ~50% the tool_result row sorts BEFORE its tool_use → orphan tool_result →
  Anthropic 400. We craft the UUIDs so the (created_at, id) order is WRONG (tool_result first) and
  assert that list_steps (ordered by seq) still yields tool_use → tool_result. A control assertion
  confirms the OLD (created_at, id) sort would have produced the broken order — i.e. seq is what
  fixes it, not luck.
- list_steps reconstruction: equal created_at across many steps → seq insertion order is preserved.

We exercise the real ChatRepository against the real DB (no Anthropic needed): the bug is purely
in persistence ordering, so this is the most direct, deterministic level to test it.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.chat.repository import ChatRepository
from app.models import ChatStep
from tests.conftest import seed_user


async def _make_session(s: AsyncSession, user_id: uuid.UUID) -> uuid.UUID:
    sid = uuid.uuid4()
    await s.execute(
        text(
            "INSERT INTO chat_sessions (id, user_id, project_id, mode) "
            "VALUES (:id, :uid, 'p', 'credits')"
        ),
        {"id": str(sid), "uid": str(user_id)},
    )
    await s.flush()
    return sid


async def _insert_step_with_fixed_id_and_ts(
    s: AsyncSession,
    *,
    step_id: uuid.UUID,
    session_id: uuid.UUID,
    message_step_id: uuid.UUID,
    role: str,
    payload_json: str,
    created_at_sql: str,
) -> None:
    """Insert a chat_steps row with explicit id + created_at (seq is DB-assigned by INSERT order).

    created_at_sql is a literal SQL expression (e.g. a fixed timestamptz) so several rows can share
    the EXACT same created_at, reproducing the same-transaction transaction-time collision.
    """
    await s.execute(
        text(
            "INSERT INTO chat_steps (id, session_id, message_step_id, role, payload, created_at) "
            f"VALUES (:id, :sid, :msid, :role, CAST(:payload AS JSONB), {created_at_sql})"
        ),
        {
            "id": str(step_id),
            "sid": str(session_id),
            "msid": str(message_step_id),
            "role": role,
            "payload": payload_json,
        },
    )


@pytest.mark.asyncio
async def test_list_steps_tool_use_before_tool_result_despite_uuid_tiebreak(
    db_session: AsyncSession,
) -> None:
    """Root cause (KEY): same created_at + adversarial UUIDs → seq still orders tool_use first."""
    uid = await seed_user(db_session, balance=0)
    sid = await _make_session(db_session, uid)
    repo = ChatRepository(db_session)
    msid = uuid.uuid4()

    # Adversarial ids: the tool_use (inserted FIRST → smaller seq) gets the LARGER UUID, the
    # tool_result (inserted SECOND → larger seq) gets the SMALLER UUID. Under the OLD (created_at,
    # id) order with equal created_at, the smaller-id tool_result would sort BEFORE its tool_use →
    # orphan tool_result. Under seq order, insertion order wins → tool_use first.
    tool_use_id = uuid.UUID("ffffffff-ffff-4fff-8fff-ffffffffffff")  # larger
    tool_result_id = uuid.UUID("00000000-0000-4000-8000-000000000001")  # smaller
    assert tool_result_id < tool_use_id

    fixed_ts = "TIMESTAMPTZ '2026-06-04 12:00:00+00'"
    # Insert in the SAME transaction in the natural loop order: tool_use first, then tool_result.
    await _insert_step_with_fixed_id_and_ts(
        db_session,
        step_id=tool_use_id,
        session_id=sid,
        message_step_id=msid,
        role="assistant",
        payload_json='{"content":[{"type":"tool_use","id":"toolu_a","name":"site_list","input":{}}]}',
        created_at_sql=fixed_ts,
    )
    await _insert_step_with_fixed_id_and_ts(
        db_session,
        step_id=tool_result_id,
        session_id=sid,
        message_step_id=msid,
        role="tool",
        payload_json='{"toolCallId":"tc","providerToolUseId":"toolu_a","result":{"ok":1}}',
        created_at_sql=fixed_ts,
    )
    await db_session.flush()

    # --- seq order (production path): tool_use BEFORE tool_result ---
    steps = await repo.list_steps(sid)
    assert [st.role for st in steps] == ["assistant", "tool"]
    assert steps[0].id == tool_use_id
    assert steps[1].id == tool_result_id
    assert steps[0].seq < steps[1].seq  # monotonic insertion order

    # --- control: the OLD (created_at, id) sort WOULD have been broken (tool_result first) ---
    old_order = list(
        await db_session.scalars(
            text("SELECT id FROM chat_steps WHERE session_id = :sid " "ORDER BY created_at, id"),
            {"sid": str(sid)},
        )
    )
    # equal created_at → id tie-break → smaller tool_result_id sorts first → WRONG (orphan).
    assert [str(x) for x in old_order] == [
        str(tool_result_id),
        str(tool_use_id),
    ], "control: the legacy (created_at, id) order must be the BROKEN order, proving seq is the fix"


@pytest.mark.asyncio
async def test_list_steps_preserves_insertion_order_with_equal_created_at(
    db_session: AsyncSession,
) -> None:
    """scenario 4: many steps with identical created_at → seq reconstructs exact insertion order."""
    uid = await seed_user(db_session, balance=0)
    sid = await _make_session(db_session, uid)
    repo = ChatRepository(db_session)
    msid = uuid.uuid4()

    fixed_ts = "TIMESTAMPTZ '2026-06-04 12:00:00+00'"
    # Insert user -> assistant(tool_use) -> tool(tool_result) -> assistant(final), all same ts,
    # with DESCENDING ids so any id-based tie-break would reverse them.
    rows = [
        (uuid.UUID("ffffffff-ffff-4fff-8fff-000000000004"), "user", '{"content":[]}'),
        (
            uuid.UUID("ffffffff-ffff-4fff-8fff-000000000003"),
            "assistant",
            '{"content":[{"type":"tool_use","id":"toolu_a","name":"site_list","input":{}}]}',
        ),
        (
            uuid.UUID("ffffffff-ffff-4fff-8fff-000000000002"),
            "tool",
            '{"toolCallId":"tc","providerToolUseId":"toolu_a","result":{}}',
        ),
        (
            uuid.UUID("ffffffff-ffff-4fff-8fff-000000000001"),
            "assistant",
            '{"content":[{"type":"text","text":"done"}]}',
        ),
    ]
    for step_id, role, payload in rows:
        await _insert_step_with_fixed_id_and_ts(
            db_session,
            step_id=step_id,
            session_id=sid,
            message_step_id=msid,
            role=role,
            payload_json=payload,
            created_at_sql=fixed_ts,
        )
    await db_session.flush()

    steps = await repo.list_steps(sid)
    assert [st.role for st in steps] == ["user", "assistant", "tool", "assistant"]
    assert [st.id for st in steps] == [r[0] for r in rows]
    # seq strictly increases in insertion order.
    seqs = [st.seq for st in steps]
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == 4


@pytest.mark.asyncio
async def test_add_step_assigns_monotonic_seq(db_session: AsyncSession) -> None:
    """seq is DB-assigned (Identity always) and monotonic across add_step calls in one tx."""
    uid = await seed_user(db_session, balance=0)
    sid = await _make_session(db_session, uid)
    repo = ChatRepository(db_session)
    msid = uuid.uuid4()

    s1 = await repo.add_step(
        session_id=sid, message_step_id=msid, role="user", payload={"content": []}
    )
    s2 = await repo.add_step(
        session_id=sid,
        message_step_id=msid,
        role="assistant",
        payload={"content": [{"type": "text", "text": "hi"}]},
    )
    assert s1.seq is not None and s2.seq is not None
    assert s2.seq > s1.seq  # second insert gets a strictly greater identity value


@pytest.mark.asyncio
async def test_seq_is_db_assigned_not_settable_in_code(db_session: AsyncSession) -> None:
    """ChatStep.seq is GENERATED ALWAYS — the DB assigns it; code never sets it (invariant)."""
    uid = await seed_user(db_session, balance=0)
    sid = await _make_session(db_session, uid)
    # Attempting to insert an explicit seq into a GENERATED ALWAYS identity must error in Postgres.
    step = ChatStep(
        session_id=sid,
        message_step_id=uuid.uuid4(),
        role="user",
        payload={"content": []},
    )
    db_session.add(step)
    await db_session.flush()
    # seq was populated by the DB even though code never set it.
    assert step.seq is not None and step.seq > 0
