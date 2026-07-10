"""Integration tests for GET /v1/images — owner image list, keyset pagination (ADR-058 §8).

Real PostgreSQL container (per 06-testing-strategy.md). No OpenAI / network: image rows are
inserted DIRECTLY into ``generated_images`` via the test sessionmaker (the list endpoint reads
metadata only — it never touches the generator). ``generated_images`` is in conftest ``_TABLES``,
so state is truncated between tests (no leakage of count/pagination assertions).

Coverage (ADR-058 §8):
- empty list → 200 {items: [], nextCursor: null};
- owner isolation (a foreign user's images never appear);
- freshness (expired hidden; NULL / future visible);
- keyset pagination over (created_at DESC, id DESC) with created_at TIES → no dupes / no gaps;
- nextCursor non-null until the last page, null strictly on the last;
- cursor round-trip (nextCursor resumes correctly);
- broken cursor → 422 (not 500); limit=0 / limit=101 → 422;
- newest-first ordering;
- item shape == {imageId, contentType, size, createdAt, sessionId} (no prompt / content bytes);
  sessionId may be null (temporary chat) and mirrors a bound session when set;
- auth: no JWT → 401;
- sweep fail-open: Redis unavailable during the opportunistic sweep still yields 200.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.chat.image_sweep as sweep_mod
from tests.conftest import (
    _FIXED_PNG,  # noqa: PLC2701
    auth_headers,
    seed_user,
)


def _now() -> datetime.datetime:
    return datetime.datetime.now(tz=datetime.UTC)


async def _insert_image(
    maker: async_sessionmaker[AsyncSession],
    uid: uuid.UUID,
    *,
    created_at: datetime.datetime,
    expires_at: datetime.datetime | None = None,
    session_id: uuid.UUID | None = None,
    content_type: str = "image/png",
    size: int | None = None,
    prompt: str | None = "secret-server-side-prompt",
) -> str:
    """Direct insert of one image row with a controlled created_at / expires_at. Returns its id."""
    image_id = uuid.uuid4()
    async with maker() as s:
        await s.execute(
            text(
                "INSERT INTO generated_images "
                "(id, user_id, session_id, content, content_type, size, prompt, created_at, "
                "expires_at) "
                "VALUES (:id, :u, :sid, :c, :ct, :sz, :p, :cre, :exp)"
            ),
            {
                "id": str(image_id),
                "u": str(uid),
                "sid": str(session_id) if session_id is not None else None,
                "c": _FIXED_PNG,
                "ct": content_type,
                "sz": size if size is not None else len(_FIXED_PNG),
                "p": prompt,
                "cre": created_at,
                "exp": expires_at,
            },
        )
        await s.commit()
    return str(image_id)


async def _insert_session(maker: async_sessionmaker[AsyncSession], uid: uuid.UUID) -> uuid.UUID:
    sid = uuid.uuid4()
    async with maker() as s:
        await s.execute(
            text(
                "INSERT INTO chat_sessions (id, user_id, project_id, mode) "
                "VALUES (:id, :uid, 'proj', 'credits')"
            ),
            {"id": str(sid), "uid": str(uid)},
        )
        await s.commit()
    return sid


async def _list(client: AsyncClient, uid: uuid.UUID, **params: Any) -> Any:
    return await client.get("/v1/images", params=params, headers=auth_headers(uid))


# ============================ empty list ============================
@pytest.mark.asyncio
async def test_empty_list_returns_200_empty_items_null_cursor(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=100)
    r = await _list(client, uid)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {"items": [], "nextCursor": None}


# ============================ owner isolation ============================
@pytest.mark.asyncio
async def test_owner_isolation_each_user_sees_only_their_own(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as s:
        owner = await seed_user(s, subscription="active", balance=100)
        other = await seed_user(s, subscription="active", balance=100)
    now = _now()
    owner_ids = {
        await _insert_image(db_sessionmaker, owner, created_at=now - datetime.timedelta(seconds=i))
        for i in range(3)
    }
    other_ids = {
        await _insert_image(db_sessionmaker, other, created_at=now - datetime.timedelta(seconds=i))
        for i in range(2)
    }

    r_owner = await _list(client, owner, limit=100)
    assert r_owner.status_code == 200, r_owner.text
    got_owner = {item["imageId"] for item in r_owner.json()["items"]}
    assert got_owner == owner_ids
    assert got_owner.isdisjoint(other_ids)

    r_other = await _list(client, other, limit=100)
    got_other = {item["imageId"] for item in r_other.json()["items"]}
    assert got_other == other_ids
    assert got_other.isdisjoint(owner_ids)


# ============================ freshness ============================
@pytest.mark.asyncio
async def test_freshness_expired_hidden_null_and_future_visible(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=100)
    now = _now()
    # NULL expires_at (never expires) → visible.
    never_id = await _insert_image(db_sessionmaker, uid, created_at=now, expires_at=None)
    # Future expires_at → visible.
    future_id = await _insert_image(
        db_sessionmaker,
        uid,
        created_at=now - datetime.timedelta(seconds=1),
        expires_at=now + datetime.timedelta(hours=1),
    )
    # Past expires_at → hidden (logically inaccessible, not deleted).
    expired_id = await _insert_image(
        db_sessionmaker,
        uid,
        created_at=now - datetime.timedelta(seconds=2),
        expires_at=now - datetime.timedelta(hours=1),
    )

    r = await _list(client, uid, limit=100)
    assert r.status_code == 200, r.text
    got = {item["imageId"] for item in r.json()["items"]}
    assert never_id in got
    assert future_id in got
    assert expired_id not in got

    # The expired row still physically exists (freshness ≠ deletion).
    async with db_sessionmaker() as s:
        still_there = int(
            await s.scalar(
                text("SELECT count(*) FROM generated_images WHERE id=:id"), {"id": expired_id}
            )
            or 0
        )
    assert still_there == 1


# ==================== pagination: no dupes / no gaps (tie-break) ====================
def _parse(item: dict[str, Any]) -> tuple[datetime.datetime, uuid.UUID]:
    return datetime.datetime.fromisoformat(item["createdAt"]), uuid.UUID(item["imageId"])


@pytest.mark.asyncio
async def test_pagination_covers_all_without_dupes_or_gaps_with_created_at_ties(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=100)
    base = _now()
    # 10 images across 5 distinct created_at values, TWO images per timestamp → forces the
    # (created_at DESC, id DESC) tie-break to keep pages disjoint and complete.
    all_ids: set[str] = set()
    for group in range(5):
        ts = base - datetime.timedelta(seconds=group)
        for _ in range(2):
            all_ids.add(await _insert_image(db_sessionmaker, uid, created_at=ts))
    assert len(all_ids) == 10

    # Walk every page with limit=3 (< 10) following nextCursor.
    collected: list[dict[str, Any]] = []
    cursor: str | None = None
    pages = 0
    while True:
        params: dict[str, Any] = {"limit": 3}
        if cursor is not None:
            params["cursor"] = cursor
        r = await _list(client, uid, **params)
        assert r.status_code == 200, r.text
        body = r.json()
        page_items = body["items"]
        collected.extend(page_items)
        pages += 1
        assert pages <= 10, "pagination did not terminate"
        cursor = body["nextCursor"]
        if cursor is None:
            # Last page: fewer than or equal to limit, and no further cursor.
            break
        # Not the last page yet → a full page was returned.
        assert len(page_items) == 3

    got_ids = [item["imageId"] for item in collected]
    # No duplicates, no gaps: the multiset of ids equals the full set exactly.
    assert len(got_ids) == len(all_ids)
    assert set(got_ids) == all_ids

    # Global order across all pages is monotonically non-increasing on (created_at, id) DESC.
    keys = [_parse(item) for item in collected]
    for a, b in zip(keys, keys[1:], strict=False):
        assert a > b, f"order violated: {a} !> {b}"


# ============================ nextCursor semantics ============================
@pytest.mark.asyncio
async def test_next_cursor_null_strictly_on_last_page(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=100)
    now = _now()
    for i in range(5):
        await _insert_image(db_sessionmaker, uid, created_at=now - datetime.timedelta(seconds=i))

    # Page 1 of 5 with limit=2 → more pages remain → non-null cursor.
    r1 = await _list(client, uid, limit=2)
    assert r1.status_code == 200, r1.text
    b1 = r1.json()
    assert len(b1["items"]) == 2
    assert b1["nextCursor"] is not None

    # Page 2 → still one more remaining → non-null.
    r2 = await _list(client, uid, limit=2, cursor=b1["nextCursor"])
    b2 = r2.json()
    assert len(b2["items"]) == 2
    assert b2["nextCursor"] is not None

    # Page 3 → last row (1 item) → cursor null strictly on the last page.
    r3 = await _list(client, uid, limit=2, cursor=b2["nextCursor"])
    b3 = r3.json()
    assert len(b3["items"]) == 1
    assert b3["nextCursor"] is None


@pytest.mark.asyncio
async def test_next_cursor_null_when_exactly_one_full_page(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    # Exactly `limit` rows → the single page is the last one → cursor must be null (limit+1 fetch
    # finds no extra row).
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=100)
    now = _now()
    for i in range(3):
        await _insert_image(db_sessionmaker, uid, created_at=now - datetime.timedelta(seconds=i))
    r = await _list(client, uid, limit=3)
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["items"]) == 3
    assert body["nextCursor"] is None


# ============================ cursor round-trip ============================
@pytest.mark.asyncio
async def test_cursor_round_trip_resumes_without_overlap(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=100)
    now = _now()
    for i in range(6):
        await _insert_image(db_sessionmaker, uid, created_at=now - datetime.timedelta(seconds=i))

    r1 = await _list(client, uid, limit=2)
    b1 = r1.json()
    page1 = {item["imageId"] for item in b1["items"]}
    assert b1["nextCursor"] is not None

    # The opaque cursor from the response is accepted verbatim and continues the scan.
    r2 = await _list(client, uid, limit=2, cursor=b1["nextCursor"])
    assert r2.status_code == 200, r2.text
    b2 = r2.json()
    page2 = {item["imageId"] for item in b2["items"]}
    assert len(page2) == 2
    # No overlap between consecutive pages (strict keyset continuation).
    assert page1.isdisjoint(page2)


# ============================ invalid cursor / limit → 422 ============================
@pytest.mark.asyncio
async def test_broken_cursor_returns_422_not_500(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=100)
    for bad in ("!!!not-base64!!!", "Zm9vYmFy", "%%%%", "a b c"):
        r = await _list(client, uid, cursor=bad)
        assert r.status_code == 422, f"cursor={bad!r} → {r.status_code}: {r.text}"


@pytest.mark.asyncio
async def test_limit_out_of_range_returns_422(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=100)
    r0 = await _list(client, uid, limit=0)
    assert r0.status_code == 422, r0.text
    r101 = await _list(client, uid, limit=101)
    assert r101.status_code == 422, r101.text


# ============================ ordering (newest first) ============================
@pytest.mark.asyncio
async def test_order_newest_first(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=100)
    now = _now()
    oldest = await _insert_image(db_sessionmaker, uid, created_at=now - datetime.timedelta(hours=2))
    middle = await _insert_image(db_sessionmaker, uid, created_at=now - datetime.timedelta(hours=1))
    newest = await _insert_image(db_sessionmaker, uid, created_at=now)

    r = await _list(client, uid, limit=100)
    assert r.status_code == 200, r.text
    ordered = [item["imageId"] for item in r.json()["items"]]
    assert ordered == [newest, middle, oldest]


# ============================ item shape ============================
@pytest.mark.asyncio
async def test_item_shape_exact_fields_no_prompt_no_content(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=100)
    sid = await _insert_session(db_sessionmaker, uid)
    now = _now()
    # One image bound to a session (sessionId set) and one temporary (sessionId NULL).
    bound_id = await _insert_image(
        db_sessionmaker,
        uid,
        created_at=now,
        session_id=sid,
        content_type="image/jpeg",
        size=1234,
    )
    temp_id = await _insert_image(
        db_sessionmaker, uid, created_at=now - datetime.timedelta(seconds=1), session_id=None
    )

    r = await _list(client, uid, limit=100)
    assert r.status_code == 200, r.text
    items = {item["imageId"]: item for item in r.json()["items"]}

    for item in items.values():
        # Exactly the five contract fields — no prompt, no content bytes leak.
        assert set(item.keys()) == {"imageId", "contentType", "size", "createdAt", "sessionId"}
        assert "prompt" not in item
        assert "content" not in item

    bound = items[bound_id]
    assert bound["sessionId"] == str(sid)
    assert bound["contentType"] == "image/jpeg"
    assert bound["size"] == 1234

    temp = items[temp_id]
    assert temp["sessionId"] is None  # temporary chat → session_id NULL


# ============================ auth ============================
@pytest.mark.asyncio
async def test_no_jwt_returns_401(client: AsyncClient) -> None:
    r = await client.get("/v1/images")
    assert r.status_code == 401, r.text


# ============================ sweep fail-open ============================
class _BrokenRedis:
    async def set(self, *args: Any, **kwargs: Any) -> Any:
        import redis.asyncio as redis

        raise redis.RedisError("redis is down")


@pytest.mark.asyncio
async def test_sweep_redis_unavailable_still_returns_200(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The opportunistic sweep runs before the list query. When Redis is unavailable it fails open
    # inside maybe_sweep_expired_images (WARNING + return), so the list endpoint MUST still 200 and
    # return the owner's images. Force the broken-Redis path deterministically.
    monkeypatch.setattr(sweep_mod, "get_redis", lambda: _BrokenRedis())
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=100)
    img_id = await _insert_image(db_sessionmaker, uid, created_at=_now())

    r = await _list(client, uid, limit=100)
    assert r.status_code == 200, r.text
    got = {item["imageId"] for item in r.json()["items"]}
    assert img_id in got
