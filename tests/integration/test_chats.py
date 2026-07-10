"""Integration tests for /v1/chats* (chats module, 09-testing.md).

DB is the real container (testcontainers). Owner isolation is enforced at the repo (WHERE
user_id = sub); a foreign/missing chat is always 404 (BR-CH-1). steps-view and history must
never leak the raw provider tool_use.id (toolu_..., ADR-008).

Chats CRUD is read-only over the orchestrator-owned step tables, so we seed chat_sessions /
chat_steps / tool_calls directly via SQL (the orchestrator writes them in production).
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.conftest import auth_headers, seed_user


def _now() -> datetime.datetime:
    return datetime.datetime.now(tz=datetime.UTC)


async def _seed_session(
    s: AsyncSession,
    *,
    user_id: uuid.UUID,
    session_id: uuid.UUID | None = None,
    title: str | None = None,
    is_pinned: bool = False,
    assistant_mode: str = "chat",
    mode: str = "credits",
    updated_at: datetime.datetime | None = None,
    project_id: str = "p",
) -> uuid.UUID:
    sid = session_id or uuid.uuid4()
    ts = updated_at or _now()
    await s.execute(
        text(
            "INSERT INTO chat_sessions "
            "(id, user_id, project_id, mode, title, assistant_mode, is_pinned, "
            "created_at, updated_at) "
            "VALUES (:id, :uid, :pid, :mode, :title, :am, :pin, :cre, :upd)"
        ),
        {
            "id": str(sid),
            "uid": str(user_id),
            "pid": project_id,
            "mode": mode,
            "title": title,
            "am": assistant_mode,
            "pin": is_pinned,
            "cre": ts,
            "upd": ts,
        },
    )
    return sid


async def _seed_step(
    s: AsyncSession,
    *,
    session_id: uuid.UUID,
    message_step_id: uuid.UUID,
    role: str,
    payload: dict[str, Any],
    usage: dict[str, Any] | None = None,
    created_at: datetime.datetime | None = None,
) -> uuid.UUID:
    step_id = uuid.uuid4()
    await s.execute(
        text(
            "INSERT INTO chat_steps "
            "(id, session_id, message_step_id, role, payload, usage, created_at) "
            "VALUES (:id, :sid, :msid, :role, CAST(:payload AS JSONB), "
            "CAST(:usage AS JSONB), :cre)"
        ),
        {
            "id": str(step_id),
            "sid": str(session_id),
            "msid": str(message_step_id),
            "role": role,
            "payload": __import__("json").dumps(payload),
            "usage": __import__("json").dumps(usage) if usage is not None else None,
            "cre": created_at or _now(),
        },
    )
    return step_id


async def _seed_tool_call(
    s: AsyncSession,
    *,
    session_id: uuid.UUID,
    message_step_id: uuid.UUID,
    tool_name: str,
    provider_tool_use_id: str,
    tool_call_id: uuid.UUID | None = None,
    args: dict[str, Any] | None = None,
) -> uuid.UUID:
    tcid = tool_call_id or uuid.uuid4()
    await s.execute(
        text(
            "INSERT INTO tool_calls "
            "(id, session_id, message_step_id, tool_name, provider_tool_use_id, args, status) "
            "VALUES (:id, :sid, :msid, :tn, :ptu, CAST(:args AS JSONB), 'completed')"
        ),
        {
            "id": str(tcid),
            "sid": str(session_id),
            "msid": str(message_step_id),
            "tn": tool_name,
            "ptu": provider_tool_use_id,
            "args": __import__("json").dumps(args or {}),
        },
    )
    return tcid


def _user_payload(textval: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": textval}]}


# --- GET /v1/chats: list / sort / search / isolation ---
@pytest.mark.asyncio
async def test_list_pinned_first_then_recency(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    base = _now()
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
        # older unpinned, newer unpinned, pinned (older than both) — pinned must surface first.
        await _seed_session(
            s, user_id=uid, title="old", updated_at=base - datetime.timedelta(hours=2)
        )
        await _seed_session(
            s, user_id=uid, title="new", updated_at=base - datetime.timedelta(hours=1)
        )
        await _seed_session(
            s,
            user_id=uid,
            title="pinned",
            is_pinned=True,
            updated_at=base - datetime.timedelta(hours=5),
        )
        await s.commit()

    r = await client.get("/v1/chats", headers=auth_headers(uid))
    assert r.status_code == 200
    titles = [it["title"] for it in r.json()["items"]]
    assert titles == ["pinned", "new", "old"]


@pytest.mark.asyncio
async def test_list_cursor_pagination_tie_break_by_id(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    same_ts = _now()
    ids: list[uuid.UUID] = []
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
        for i in range(3):
            sid = await _seed_session(s, user_id=uid, title=f"c{i}", updated_at=same_ts)
            ids.append(sid)
        await s.commit()

    # limit=2 with identical updated_at → tie-break by id DESC; cursor returns the rest with
    # no duplicates and no omissions.
    r1 = await client.get("/v1/chats?limit=2", headers=auth_headers(uid))
    body1 = r1.json()
    assert len(body1["items"]) == 2
    assert body1["nextCursor"] is not None

    r2 = await client.get(
        f"/v1/chats?limit=2&cursor={body1['nextCursor']}", headers=auth_headers(uid)
    )
    body2 = r2.json()
    assert len(body2["items"]) == 1
    seen = [it["id"] for it in body1["items"]] + [it["id"] for it in body2["items"]]
    assert sorted(seen) == sorted(str(i) for i in ids)
    assert len(set(seen)) == 3  # no duplicates across pages


@pytest.mark.asyncio
async def test_search_matches_title_and_first_user_message(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
        # matches by title only
        await _seed_session(s, user_id=uid, title="Banana recipe")
        # matches by first user message text only
        sid2 = await _seed_session(s, user_id=uid, title="Untitled")
        await _seed_step(
            s,
            session_id=sid2,
            message_step_id=uuid.uuid4(),
            role="user",
            payload=_user_payload("how to grow a pineapple"),
        )
        # no match
        await _seed_session(s, user_id=uid, title="grocery list")
        await s.commit()

    r1 = await client.get("/v1/chats?q=banana", headers=auth_headers(uid))
    assert [it["title"] for it in r1.json()["items"]] == ["Banana recipe"]

    r2 = await client.get("/v1/chats?q=pineapple", headers=auth_headers(uid))
    assert [it["id"] for it in r2.json()["items"]] == [str(sid2)]


@pytest.mark.asyncio
async def test_list_owner_isolation(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as s:
        owner = await seed_user(s)
        other = await seed_user(s)
        await _seed_session(s, user_id=other, title="other-chat")
        await s.commit()
    r = await client.get("/v1/chats", headers=auth_headers(owner))
    assert r.json()["items"] == []


# --- GET /v1/chats/{id}: history / isolation / no raw id ---
@pytest.mark.asyncio
async def test_get_history_foreign_chat_404(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as s:
        owner = await seed_user(s)
        other = await seed_user(s)
        sid = await _seed_session(s, user_id=other, title="secret")
        await s.commit()
    # foreign chat is indistinguishable from missing → 404 (never reveal existence).
    r = await client.get(f"/v1/chats/{sid}", headers=auth_headers(owner))
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_get_history_omits_provider_tool_use_id(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
        sid = await _seed_session(s, user_id=uid, title="t")
        msid = uuid.uuid4()
        await _seed_step(
            s, session_id=sid, message_step_id=msid, role="user", payload=_user_payload("hi")
        )
        # a tool step persists providerToolUseId internally; it must be stripped from history.
        await _seed_step(
            s,
            session_id=sid,
            message_step_id=msid,
            role="tool",
            payload={
                "toolCallId": str(uuid.uuid4()),
                "providerToolUseId": "toolu_SECRET123",
                "toolName": "files.read",
                "result": {"ok": True},
            },
        )
        await s.commit()

    r = await client.get(f"/v1/chats/{sid}", headers=auth_headers(uid))
    assert r.status_code == 200
    blob = r.text
    assert "providerToolUseId" not in blob
    assert "toolu_SECRET123" not in blob


# --- GET /v1/chats/{id}/steps: domain names, no raw id ---
@pytest.mark.asyncio
async def test_steps_view_uses_domain_tool_names_no_raw_id(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
        sid = await _seed_session(s, user_id=uid, title="t")
        msid = uuid.uuid4()
        raw_id = "toolu_RAW999"
        # assistant step with a tool_use block carrying the raw provider id.
        await _seed_step(
            s,
            session_id=sid,
            message_step_id=msid,
            role="assistant",
            payload={
                "content": [
                    {"type": "text", "text": "let me read that"},
                    {"type": "tool_use", "id": raw_id, "name": "fs_read", "input": {}},
                ]
            },
        )
        await _seed_tool_call(
            s,
            session_id=sid,
            message_step_id=msid,
            tool_name="files.read",
            provider_tool_use_id=raw_id,
        )
        await s.commit()

    r = await client.get(f"/v1/chats/{sid}/steps", headers=auth_headers(uid))
    assert r.status_code == 200
    body = r.json()
    assert body["messageStepId"] == str(msid)
    tool_names = [st["toolName"] for st in body["steps"] if st["kind"] == "tool_call"]
    assert "files.read" in tool_names  # domain dotted name resolved
    assert raw_id not in r.text  # raw provider id never leaks


@pytest.mark.asyncio
async def test_steps_view_foreign_chat_404(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as s:
        owner = await seed_user(s)
        other = await seed_user(s)
        sid = await _seed_session(s, user_id=other, title="t")
        await s.commit()
    r = await client.get(f"/v1/chats/{sid}/steps", headers=auth_headers(owner))
    assert r.status_code == 404


# --- PATCH /v1/chats/{id}: rename / pin / validation / isolation ---
@pytest.mark.asyncio
async def test_patch_rename_and_pin(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
        sid = await _seed_session(s, user_id=uid, title="old")
        await s.commit()

    r = await client.patch(
        f"/v1/chats/{sid}",
        json={"title": "new title", "isPinned": True},
        headers=auth_headers(uid),
    )
    assert r.status_code == 200
    assert r.json()["title"] == "new title"
    assert r.json()["isPinned"] is True


@pytest.mark.asyncio
async def test_patch_extra_field_forbidden_422(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
        sid = await _seed_session(s, user_id=uid)
        await s.commit()
    r = await client.patch(
        f"/v1/chats/{sid}",
        json={"title": "x", "unexpected": 1},
        headers=auth_headers(uid),
    )
    assert r.status_code == 422  # StrictModel extra='forbid'


@pytest.mark.asyncio
async def test_patch_title_too_long_422(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
        sid = await _seed_session(s, user_id=uid)
        await s.commit()
    r = await client.patch(f"/v1/chats/{sid}", json={"title": "x" * 201}, headers=auth_headers(uid))
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_patch_foreign_chat_404(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as s:
        owner = await seed_user(s)
        other = await seed_user(s)
        sid = await _seed_session(s, user_id=other)
        await s.commit()
    r = await client.patch(f"/v1/chats/{sid}", json={"isPinned": True}, headers=auth_headers(owner))
    assert r.status_code == 404


# --- DELETE /v1/chats/{id}: cascade / idempotency / isolation ---
@pytest.mark.asyncio
async def test_delete_cascades_steps_and_tool_calls(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
        sid = await _seed_session(s, user_id=uid)
        msid = uuid.uuid4()
        await _seed_step(
            s, session_id=sid, message_step_id=msid, role="user", payload=_user_payload("hi")
        )
        await _seed_tool_call(
            s,
            session_id=sid,
            message_step_id=msid,
            tool_name="files.read",
            provider_tool_use_id="toolu_x",
        )
        await s.commit()

    r = await client.delete(f"/v1/chats/{sid}", headers=auth_headers(uid))
    assert r.status_code == 200
    assert r.json()["deleted"] is True

    async with db_sessionmaker() as s:
        steps = await s.scalar(
            text("SELECT count(*) FROM chat_steps WHERE session_id=:s"), {"s": str(sid)}
        )
        tcs = await s.scalar(
            text("SELECT count(*) FROM tool_calls WHERE session_id=:s"), {"s": str(sid)}
        )
    assert int(steps) == 0  # FK cascade
    assert int(tcs) == 0


@pytest.mark.asyncio
async def test_delete_repeated_is_404(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
        sid = await _seed_session(s, user_id=uid)
        await s.commit()
    first = await client.delete(f"/v1/chats/{sid}", headers=auth_headers(uid))
    assert first.status_code == 200
    second = await client.delete(f"/v1/chats/{sid}", headers=auth_headers(uid))
    assert second.status_code == 404  # already deleted → 404


@pytest.mark.asyncio
async def test_delete_foreign_chat_404(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as s:
        owner = await seed_user(s)
        other = await seed_user(s)
        sid = await _seed_session(s, user_id=other)
        await s.commit()
    r = await client.delete(f"/v1/chats/{sid}", headers=auth_headers(owner))
    assert r.status_code == 404
    # the foreign chat must still exist (not deleted).
    async with db_sessionmaker() as s:
        cnt = await s.scalar(
            text("SELECT count(*) FROM chat_sessions WHERE id=:s"), {"s": str(sid)}
        )
    assert int(cnt) == 1


# --------------------------- auth ---------------------------
@pytest.mark.asyncio
async def test_list_requires_auth(client: AsyncClient) -> None:
    r = await client.get("/v1/chats")
    assert r.status_code == 401
