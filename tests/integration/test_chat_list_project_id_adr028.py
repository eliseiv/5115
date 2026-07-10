"""Integration tests for ADR-028 Решение 1 — projectId in GET /v1/chats list items.

Real PostgreSQL container (testcontainers). The chats list is read-only over the
orchestrator-owned chat_sessions table, so sessions are seeded directly via SQL (the
orchestrator writes them in production), mirroring tests/integration/test_chats.py.

ADR-028 Решение 1 (additive, non-breaking):
- ChatListItemSchema gains `projectId` (= chat_sessions.project_id, free string, ADR-022).
- `null` projectId = «чистый чат» (session created without a project).
- `workspaceProjectId` is untouched: still ALWAYS null (Sprint-2 column not created yet).
- The two project fields are independent and both present in every item.
- Existing fields (id/title/preview/assistantMode/isPinned/workspaceProjectId/updatedAt),
  sorting and pagination are unchanged (additivity regression).
"""

from __future__ import annotations

import datetime
import uuid

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
    project_id: str | None = "p",
) -> uuid.UUID:
    """Insert a chat_sessions row. project_id=None seeds a «чистый чат» (NULL project)."""
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


def _item_by_title(body: dict, title: str) -> dict:
    for item in body["items"]:
        if item["title"] == title:
            return item
    raise AssertionError(f"item with title {title!r} not found in {body['items']}")


# ============================================================================================
# Scenario 1 — projectId echoes chat_sessions.project_id; null for a project-less session;
# workspaceProjectId ALWAYS null. Both fields present and independent in every item.
# ============================================================================================
@pytest.mark.asyncio
async def test_list_items_carry_project_id_and_null_for_clean_chat(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    base = _now()
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
        # A website-builder session (project set) and a «чистый чат» (no project).
        await _seed_session(
            s,
            user_id=uid,
            title="with-project",
            project_id="my-ios-project",
            updated_at=base - datetime.timedelta(hours=1),
        )
        await _seed_session(
            s,
            user_id=uid,
            title="clean-chat",
            project_id=None,
            updated_at=base - datetime.timedelta(hours=2),
        )
        await s.commit()

    r = await client.get("/v1/chats", headers=auth_headers(uid))
    assert r.status_code == 200, r.text
    body = r.json()

    with_project = _item_by_title(body, "with-project")
    clean = _item_by_title(body, "clean-chat")

    # projectId echoes the stored free string for the project session, null for the clean chat.
    assert with_project["projectId"] == "my-ios-project"
    assert clean["projectId"] is None

    # workspaceProjectId is untouched by ADR-028 — ALWAYS null (Sprint-2 column not created).
    assert with_project["workspaceProjectId"] is None
    assert clean["workspaceProjectId"] is None

    # Both project fields are present (independent) in every item.
    for item in body["items"]:
        assert "projectId" in item
        assert "workspaceProjectId" in item


@pytest.mark.asyncio
async def test_project_id_is_a_free_string_round_tripped_verbatim(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    # ADR-022/ADR-028: projectId is an opaque free string — round-tripped byte-for-byte, no
    # parsing into a UUID (contrast workspaceProjectId which is a UUID|null).
    weird = "Project_42.v2-區/слой"
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
        await _seed_session(s, user_id=uid, title="weird", project_id=weird)
        await s.commit()
    r = await client.get("/v1/chats", headers=auth_headers(uid))
    assert r.status_code == 200, r.text
    assert _item_by_title(r.json(), "weird")["projectId"] == weird


# ============================================================================================
# Scenario — additivity regression: the existing item fields, sort (pinned-first then recency)
# and pagination are unchanged; the new projectId rides along on every page item.
# ============================================================================================
@pytest.mark.asyncio
async def test_existing_fields_and_sort_unchanged_with_project_id_present(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    base = _now()
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
        await _seed_session(
            s,
            user_id=uid,
            title="old",
            project_id="po",
            updated_at=base - datetime.timedelta(hours=2),
        )
        await _seed_session(
            s,
            user_id=uid,
            title="new",
            project_id=None,
            updated_at=base - datetime.timedelta(hours=1),
        )
        await _seed_session(
            s,
            user_id=uid,
            title="pinned",
            is_pinned=True,
            project_id="pp",
            updated_at=base - datetime.timedelta(hours=5),
        )
        await s.commit()

    r = await client.get("/v1/chats", headers=auth_headers(uid))
    body = r.json()
    # Sort invariant unchanged (ADR-028 is additive): pinned first, then by recency.
    assert [it["title"] for it in body["items"]] == ["pinned", "new", "old"]
    # Every legacy field still present alongside the new projectId.
    for item in body["items"]:
        assert set(item.keys()) >= {
            "id",
            "title",
            "preview",
            "assistantMode",
            "isPinned",
            "projectId",
            "workspaceProjectId",
            "updatedAt",
        }
    # Per-item projectId mapping is correct (incl. null for the clean «new» chat).
    assert _item_by_title(body, "pinned")["projectId"] == "pp"
    assert _item_by_title(body, "old")["projectId"] == "po"
    assert _item_by_title(body, "new")["projectId"] is None


@pytest.mark.asyncio
async def test_project_id_survives_cursor_pagination(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    same_ts = _now()
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
        for i in range(3):
            await _seed_session(
                s, user_id=uid, title=f"c{i}", project_id=f"proj-{i}", updated_at=same_ts
            )
        await s.commit()

    r1 = await client.get("/v1/chats?limit=2", headers=auth_headers(uid))
    body1 = r1.json()
    assert len(body1["items"]) == 2
    assert body1["nextCursor"] is not None
    r2 = await client.get(
        f"/v1/chats?limit=2&cursor={body1['nextCursor']}", headers=auth_headers(uid)
    )
    body2 = r2.json()
    # Every item across both pages carries a projectId matching its seeded title index.
    for item in body1["items"] + body2["items"]:
        idx = item["title"][1:]
        assert item["projectId"] == f"proj-{idx}"
        assert item["workspaceProjectId"] is None
