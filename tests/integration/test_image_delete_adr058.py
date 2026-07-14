"""Integration tests for ADR-058 §9 — DELETE /v1/images/{imageId} (hard-delete from gallery).

Real PostgreSQL container; the active provider (anthropic in the test env) is faked at the client
boundary (``fake_anthropic``) and the image generator is faked at the ``image_client`` singleton
boundary (conftest ``FakeImageGenerator``) — NO real OpenAI / network (hermetic; passes with
placeholder keys). Reuses helpers/fixtures from ``test_image_generation_adr058`` (same suite).

Coverage (ADR-058 §9 — normative bullets from the "## Решение" §9):
- owner deletes own FRESH image → 204 + security headers; subsequent GET → 404; row physically gone.
- repeat DELETE of the same id → 404 (deliberately NOT idempotent-204: never reveal existence).
- FOREIGN user → 404 AND owner's row untouched (owner can still GET 200 / DELETE 204).
- EXPIRED own image (temporary-chat TTL) → 404 by the freshness predicate; row NOT deleted by this
  DELETE (sweep is force-disabled here so the survival proves the DELETE predicate, not the GC).
- MISSING id → 404.
- security headers (nosniff + no-store) on BOTH 204 and 404.
- no/invalid JWT → 401.
- no-refund + history untouched: chat_steps/tool_calls and the wallet balance are unchanged.

NOT covered here (documented, not faked): rate-limit → 429. ``enforce_other_limits`` is force-
overridden to always-allow by the ``client`` fixture (tests/conftest.py) for determinism, so there
is no test infrastructure to exhaust the ``_rate_limit`` contour. ADR-058 §9 lists 429 as the same
GET/list contour (``enforce_other_limits``); it is exercised by the rate-limit unit tests, not here.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.chat.image_sweep as sweep_mod
from app.config import get_settings
from tests.conftest import (
    FakeAnthropicClient,
    auth_headers,
    seed_user,
)
from tests.integration.test_image_generation_adr058 import (
    _BrokenRedis,  # noqa: PLC2701
    _count,  # noqa: PLC2701
    _generate_one_image,  # noqa: PLC2701
    _scalar,  # noqa: PLC2701
)


def _assert_security_headers(resp: object) -> None:
    # httpx lower-cases header names. Both invariants must hold on 204 AND 404 (ADR-058 §9 / §2).
    assert resp.headers["x-content-type-options"] == "nosniff"  # type: ignore[attr-defined]
    assert resp.headers["cache-control"] == "private, no-store"  # type: ignore[attr-defined]


# ============================ owner deletes fresh image (§9: 204) ============================
@pytest.mark.asyncio
async def test_owner_deletes_fresh_image_204_then_get_404_and_row_gone(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=100)
    image_id = await _generate_one_image(client, db_sessionmaker, fake_anthropic, uid)
    # Sanity: the row is really there before we delete it.
    assert (
        await _count(
            db_sessionmaker, "SELECT count(*) FROM generated_images WHERE id=:id", id=image_id
        )
        == 1
    )

    resp = await client.delete(f"/v1/images/{image_id}", headers=auth_headers(uid))
    assert resp.status_code == 204, resp.text
    assert resp.content == b""  # 204 No Content — empty body (ADR-058 §9)
    _assert_security_headers(resp)

    # The now-dangling id → GET returns 404 (existence no longer observable).
    get = await client.get(f"/v1/images/{image_id}", headers=auth_headers(uid))
    assert get.status_code == 404

    # Hard-delete: the row (and its BYTEA content) is physically gone.
    assert (
        await _count(
            db_sessionmaker, "SELECT count(*) FROM generated_images WHERE id=:id", id=image_id
        )
        == 0
    )


# ==================== repeat DELETE → 404, NOT 204 (§9 idempotency) ====================
@pytest.mark.asyncio
async def test_repeat_delete_of_deleted_image_returns_404_not_204(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=100)
    image_id = await _generate_one_image(client, db_sessionmaker, fake_anthropic, uid)

    first = await client.delete(f"/v1/images/{image_id}", headers=auth_headers(uid))
    assert first.status_code == 204, first.text

    # Deliberately NOT idempotent-204: a 204 on a missing row would confirm it once existed.
    second = await client.delete(f"/v1/images/{image_id}", headers=auth_headers(uid))
    assert second.status_code == 404
    _assert_security_headers(second)


# ============ foreign user → 404, owner's row untouched (§9 isolation) ============
@pytest.mark.asyncio
async def test_foreign_delete_404_and_owner_row_untouched(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        owner = await seed_user(s, subscription="active", balance=100)
        other = await seed_user(s, subscription="active", balance=100)
    image_id = await _generate_one_image(client, db_sessionmaker, fake_anthropic, owner)

    # Foreign user tries to delete owner's image → 404 (never 403 — do not reveal existence).
    foreign = await client.delete(f"/v1/images/{image_id}", headers=auth_headers(other))
    assert foreign.status_code == 404
    _assert_security_headers(foreign)

    # The owner's row is NOT deleted (0 rows matched the foreign owner-condition).
    assert (
        await _count(
            db_sessionmaker,
            "SELECT count(*) FROM generated_images WHERE id=:id AND user_id=:u",
            id=image_id,
            u=str(owner),
        )
        == 1
    )
    # The owner still fully controls it: can GET (200) and can DELETE (204).
    owner_get = await client.get(f"/v1/images/{image_id}", headers=auth_headers(owner))
    assert owner_get.status_code == 200
    owner_del = await client.delete(f"/v1/images/{image_id}", headers=auth_headers(owner))
    assert owner_del.status_code == 204, owner_del.text


# ========== expired own image → 404, row NOT deleted (§9 freshness parity) ==========
@pytest.mark.asyncio
async def test_expired_own_image_404_and_row_not_deleted_by_delete(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ADR-058 §9: the freshness predicate ``expires_at > now()`` in the DELETE statement does NOT
    # match an expired row → 0 rows → 404 (parity with GET §2: DELETE never returns 204 on what GET
    # returns 404). To isolate the DELETE PREDICATE from the opportunistic sweep (which could
    # physically GC the expired row first and also yield 404), we force the sweep to a guaranteed
    # no-op via a broken Redis (fail-open). Thus the row's SURVIVAL below proves the DELETE
    # predicate rejected it — not the sweep. (If the sweep were active it could delete the row; per
    # ADR-058 §9 that is ALSO a legitimate 404 — the observable contract is identical.)
    monkeypatch.setattr(sweep_mod, "get_redis", lambda: _BrokenRedis())
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=100)
    image_id = await _generate_one_image(client, db_sessionmaker, fake_anthropic, uid)

    # Push expires_at into the past (simulates a temporary-chat image past its TTL, ADR-058 §6).
    async with db_sessionmaker() as s:
        await s.execute(
            text("UPDATE generated_images SET expires_at = now() - interval '1 hour' WHERE id=:id"),
            {"id": image_id},
        )
        await s.commit()

    resp = await client.delete(f"/v1/images/{image_id}", headers=auth_headers(uid))
    assert resp.status_code == 404  # expired is logically inaccessible → 404 (not 204, not 403)
    _assert_security_headers(resp)

    # The row still physically exists — the DELETE freshness predicate rejected it (sweep disabled).
    assert (
        await _count(
            db_sessionmaker, "SELECT count(*) FROM generated_images WHERE id=:id", id=image_id
        )
        == 1
    )


# ============================ missing id → 404 + headers (§9) ============================
@pytest.mark.asyncio
async def test_delete_missing_image_returns_404_with_security_headers(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=100)
    resp = await client.delete(f"/v1/images/{uuid.uuid4()}", headers=auth_headers(uid))
    assert resp.status_code == 404
    _assert_security_headers(resp)  # headers present on the 404 path too


# ============================ auth: no / invalid JWT → 401 (§9 codes) ============================
@pytest.mark.asyncio
async def test_delete_without_jwt_401(
    client: AsyncClient,
) -> None:
    # No Authorization header → 401 (auth runs before ownership/row logic).
    resp = await client.delete(f"/v1/images/{uuid.uuid4()}")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_delete_with_invalid_jwt_401(
    client: AsyncClient,
) -> None:
    resp = await client.delete(
        f"/v1/images/{uuid.uuid4()}",
        headers={"Authorization": "Bearer not-a-real-jwt"},
    )
    assert resp.status_code == 401


# ============ no-refund + history untouched (§9 billing/history) ============
@pytest.mark.asyncio
async def test_delete_does_not_refund_credits_or_touch_chat_history(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    # ADR-058 §9: deleting the artifact from the gallery does NOT refund IMAGE_CREDITS_COST
    # (no-refund-on-delete, symmetric to no-refund-on-edit) and does NOT touch chat history
    # (chat_steps / tool_calls) — the turn stays in history with a now-dangling image reference.
    cost = get_settings().image_credits_cost
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=100)
    image_id = await _generate_one_image(client, db_sessionmaker, fake_anthropic, uid)

    # After a normal-chat image turn: 1 (turn) + cost (image) were debited; history rows exist.
    bal_before = int(
        await _scalar(db_sessionmaker, "SELECT balance FROM wallets WHERE user_id=:u", u=str(uid))
    )
    assert bal_before == 100 - (1 + cost)
    steps_before = await _count(
        db_sessionmaker,
        "SELECT count(*) FROM chat_steps WHERE session_id IN "
        "(SELECT id FROM chat_sessions WHERE user_id=:u)",
        u=str(uid),
    )
    calls_before = await _count(
        db_sessionmaker,
        "SELECT count(*) FROM tool_calls WHERE session_id IN "
        "(SELECT id FROM chat_sessions WHERE user_id=:u)",
        u=str(uid),
    )
    assert steps_before > 0  # the image turn really produced history rows
    assert calls_before > 0  # ...including a tool_calls row for image.generate

    resp = await client.delete(f"/v1/images/{image_id}", headers=auth_headers(uid))
    assert resp.status_code == 204, resp.text

    # No refund: the balance is unchanged after deletion.
    bal_after = int(
        await _scalar(db_sessionmaker, "SELECT balance FROM wallets WHERE user_id=:u", u=str(uid))
    )
    assert bal_after == bal_before
    # History untouched: chat_steps and tool_calls counts are unchanged.
    steps_after = await _count(
        db_sessionmaker,
        "SELECT count(*) FROM chat_steps WHERE session_id IN "
        "(SELECT id FROM chat_sessions WHERE user_id=:u)",
        u=str(uid),
    )
    calls_after = await _count(
        db_sessionmaker,
        "SELECT count(*) FROM tool_calls WHERE session_id IN "
        "(SELECT id FROM chat_sessions WHERE user_id=:u)",
        u=str(uid),
    )
    assert steps_after == steps_before
    assert calls_after == calls_before
