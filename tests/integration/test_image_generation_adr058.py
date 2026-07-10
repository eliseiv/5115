"""Integration tests for ADR-058 — image.generate in the chat tool-loop + GET /v1/images + sweep.

Real PostgreSQL container; the active provider (anthropic in the test env) is faked at the client
boundary (``fake_anthropic``) and the image generator is faked at the ``image_client`` singleton
boundary (conftest ``FakeImageGenerator``, wired by the ``client`` fixture) — NO real OpenAI /
network (hermetic; the suite passes with placeholder keys). image.generate is provider-INDEPENDENT
(ADR-058 §3): the model «calls» it, the backend PRE-generates bytes, then debits + INSERTs.

Coverage (ADR-058):
- §1/§7 happy path: bytes land in generated_images; chat_steps/tool_result carry ONLY the id +
  metadata (never bytes/base64); ChatResponse.images[0].imageId present.
- §4 billing (revised): the image is debited in EVERY mode (our server key). credits → 1 +
  N*IMAGE_CREDITS_COST; byok → 0 (text) + N*COST; trial → free text but a zero-balance image blocks
  without consuming the trial.
- §7 multiple images per turn: append-all, each with its own debit.
- §5 insufficient credits: blocked + image_credits_empty (HTTP 200), zero rows, ledger rolled back,
  no dangling tool_use without tool_result.
- §4 idempotency: a replayed tool_call_id duplicates neither the ledger row nor the image row.
- §2 GET /v1/images: owner → 200 + bytes + nosniff/no-store; foreign / expired / missing → 404 with
  a byte-identical body (existence not revealed).
- §6 TTL: temporary chat → expires_at == created_at + TTL (DB clock); normal chat → NULL.
- §3 degrade: content_policy / image_generation_failed tool_result, the turn survives, the image
  debit is NOT charged but the turn credit IS.
- §7/TD-035 privacy: the prompt never reaches audit_logs.payload nor serverTools[].summary.
- §6 temporary chat: zero chat_* rows, but a generated_images row with session_id NULL + expires_at.
- §6 sweep: expired rows deleted in a bounded batch; Redis down → fail-open; throttled by the lock.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
import redis.asyncio as redis
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.chat.image_sweep as sweep_mod
from app.config import get_settings

# The fixed PNG the FakeImageGenerator returns (byte-identical to what GET must serve).
from tests.conftest import (
    _FIXED_PNG,  # noqa: PLC2701
    FakeAnthropicClient,
    FakeImageGenerator,
    auth_headers,
    seed_user,
)

_SECRET_PROMPT = "SECRET_IMG_PROMPT_TOKEN_a_watercolor_fox"


async def _scalar(maker: async_sessionmaker[AsyncSession], sql: str, **params: object) -> Any:
    async with maker() as s:
        return await s.scalar(text(sql), params)


async def _count(maker: async_sessionmaker[AsyncSession], sql: str, **params: object) -> int:
    async with maker() as s:
        return int(await s.scalar(text(sql), params) or 0)


async def _debit_amounts(maker: async_sessionmaker[AsyncSession], uid: uuid.UUID) -> list[int]:
    """All ledger DEBIT amounts for a user, sorted (assert exact SUMS, not just the count)."""
    async with maker() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT amount FROM ledger_transactions "
                    "WHERE user_id=:u AND type='debit' ORDER BY amount"
                ),
                {"u": str(uid)},
            )
        ).all()
    return [int(r[0]) for r in rows]


async def _run(client: AsyncClient, uid: uuid.UUID, **extra: Any) -> Any:
    payload: dict[str, Any] = {
        "userId": str(uid),
        "message": "draw me a fox",
        "mode": "credits",
    }
    payload.update(extra)
    return await client.post("/v1/chat/run", json=payload, headers=auth_headers(uid))


# ============================ happy path (§1/§7) ============================
@pytest.mark.asyncio
async def test_image_generate_happy_path_stores_bytes_and_returns_ref(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=100)
    fake_anthropic.responses = [
        fake_anthropic.tool_result(
            "image.generate", {"prompt": _SECRET_PROMPT}, tool_id="toolu_img01"
        ),
        fake_anthropic.text_result("Here is your fox."),
    ]
    r = await _run(client, uid)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "assistant_message", body
    assert body["assistantMessage"] == "Here is your fox."

    # ChatResponse.images carries exactly one ref with id + metadata (no prompt, no bytes).
    images = body["images"]
    assert len(images) == 1
    assert set(images[0]) == {"imageId", "contentType", "size"}
    assert images[0]["contentType"] == "image/png"
    assert images[0]["size"] == len(_FIXED_PNG)
    image_id = images[0]["imageId"]

    # The bytes are persisted in generated_images (exactly what the fake produced).
    async with db_sessionmaker() as s:
        row = (
            await s.execute(
                text(
                    "SELECT content, content_type, size, prompt, session_id, expires_at "
                    "FROM generated_images WHERE id=:id"
                ),
                {"id": image_id},
            )
        ).one()
    content, content_type, size, prompt, session_id, expires_at = row
    assert bytes(content) == _FIXED_PNG
    assert content_type == "image/png"
    assert size == len(_FIXED_PNG)
    assert prompt == _SECRET_PROMPT  # stored server-side (regeneration/debug), never returned
    assert session_id is not None  # normal chat → bound to the session
    assert expires_at is None  # normal chat → never expires (§6)

    # The persisted tool step + tool_result carry ONLY id/contentType/size — NEVER bytes/base64.
    async with db_sessionmaker() as s:
        payload = await s.scalar(
            text(
                "SELECT payload FROM chat_steps WHERE session_id=:sid AND role='tool' "
                "ORDER BY seq LIMIT 1"
            ),
            {"sid": body["sessionId"]},
        )
    assert payload["toolName"] == "image.generate"
    assert payload["error"] is None
    assert payload["result"] == {
        "imageId": image_id,
        "contentType": "image/png",
        "size": len(_FIXED_PNG),
    }
    # Defensive: no base64 of the PNG anywhere in the step payload.
    import base64 as _b64
    import json as _json

    step_text = _json.dumps(payload)
    assert _b64.b64encode(_FIXED_PNG).decode() not in step_text


# ============================ billing (§4) ============================
@pytest.mark.asyncio
async def test_credits_billing_is_one_plus_image_cost(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    cost = get_settings().image_credits_cost
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=100)
    fake_anthropic.responses = [
        fake_anthropic.tool_result("image.generate", {"prompt": "a"}, tool_id="toolu_b1"),
        fake_anthropic.text_result("done"),
    ]
    r = await _run(client, uid)
    assert r.json()["status"] == "assistant_message", r.text
    bal = await _scalar(db_sessionmaker, "SELECT balance FROM wallets WHERE user_id=:u", u=str(uid))
    # 1 (turn) + 1 * IMAGE_CREDITS_COST (image).
    assert int(bal) == 100 - (1 + cost)
    # TWO distinct ledger events with the EXACT amounts: the 1-credit text turn AND the image cost
    # (matches backend's real-DB run: ledger amounts == [1, 5] at IMAGE_CREDITS_COST=5).
    assert await _debit_amounts(db_sessionmaker, uid) == sorted([1, cost])


@pytest.mark.asyncio
async def test_byok_text_free_but_image_still_debited(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    # ADR-058 §4 (revised): the image is ALWAYS generated on OUR server OPENAI_API_KEY, so it is
    # debited in EVERY mode. byok → text is free (user's own key) but N×IMAGE_CREDITS_COST is still
    # charged for the image → total 0 (text) + COST. BYOK does NOT exempt images.
    cost = get_settings().image_credits_cost
    async with db_sessionmaker() as s:
        uid = await seed_user(
            s, subscription="active", balance=100, byok_enabled=True, byok_status="valid"
        )
    fake_anthropic.responses = [
        fake_anthropic.tool_result("image.generate", {"prompt": "a"}, tool_id="toolu_byok1"),
        fake_anthropic.text_result("done"),
    ]
    r = await _run(client, uid, mode="byok")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "assistant_message", r.text
    # Exactly ONE debit whose amount is the image COST — NO 1-credit text debit under byok
    # (matches backend's real-DB run: ledger amounts == [5] at IMAGE_CREDITS_COST=5).
    assert await _debit_amounts(db_sessionmaker, uid) == [cost]
    bal = await _scalar(db_sessionmaker, "SELECT balance FROM wallets WHERE user_id=:u", u=str(uid))
    assert int(bal) == 100 - cost  # only the image cost, not the text turn
    imgs = await _count(
        db_sessionmaker, "SELECT count(*) FROM generated_images WHERE user_id=:u", u=str(uid)
    )
    assert imgs == 1


@pytest.mark.asyncio
async def test_trial_zero_balance_image_blocks_without_consuming_trial(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    # ADR-058 §4 (revised, variant (a)): trial gives a free TEXT turn, but the image needs a
    # non-zero credit balance (it runs on our key). A trial user with zero balance asking for an
    # image is blocked (image_credits_empty) BEFORE the turn is finalized, so the trial is NOT
    # consumed (users.trial_used stays false) — the user can retry text-only.
    async with db_sessionmaker() as s:
        uid = await seed_user(s, trial_used=False)  # no subscription, no wallet (zero balance)
    fake_anthropic.responses = [
        fake_anthropic.tool_result("image.generate", {"prompt": "a"}, tool_id="toolu_trial1"),
        fake_anthropic.text_result("should not be reached"),
    ]
    r = await _run(client, uid)
    assert r.status_code == 200, r.text  # ADR-004: blocked is HTTP 200
    body = r.json()
    assert body["status"] == "blocked", body
    assert body["blockReason"] == "image_credits_empty"
    # Bytes NOT saved; the trial is preserved (flip happens after the tool-loop, §4).
    imgs = await _count(
        db_sessionmaker, "SELECT count(*) FROM generated_images WHERE user_id=:u", u=str(uid)
    )
    assert imgs == 0
    # The ledger is EMPTY — not a single row of any type (the failed image consume rolled back).
    ledger_rows = await _count(
        db_sessionmaker, "SELECT count(*) FROM ledger_transactions WHERE user_id=:u", u=str(uid)
    )
    assert ledger_rows == 0
    trial_used = await _scalar(
        db_sessionmaker, "SELECT trial_used FROM users WHERE id=:u", u=str(uid)
    )
    assert trial_used is False  # trial NOT consumed — the user can retry text-only


@pytest.mark.asyncio
async def test_trial_retry_text_only_after_image_block_consumes_trial(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    # Proves the earlier image-block consumed NOTHING: after a zero-balance trial user is blocked on
    # an image (image_credits_empty), a follow-up TEXT-ONLY turn still burns the lifetime trial
    # (users.trial_used false→true) and returns assistant_message. If the first block had wrongly
    # flipped the trial, this second free turn would be impossible.
    async with db_sessionmaker() as s:
        uid = await seed_user(s, trial_used=False)  # zero balance, no subscription

    # Turn 1: request an image → blocked, trial untouched.
    fake_anthropic.responses = [
        fake_anthropic.tool_result("image.generate", {"prompt": "a"}, tool_id="toolu_tr_a"),
        fake_anthropic.text_result("unreached"),
    ]
    r1 = await _run(client, uid, message="draw me a fox")
    assert r1.json()["status"] == "blocked", r1.text
    assert r1.json()["blockReason"] == "image_credits_empty"
    before = await _scalar(db_sessionmaker, "SELECT trial_used FROM users WHERE id=:u", u=str(uid))
    assert before is False  # first block consumed nothing

    # Turn 2: text-only (model returns plain text, no image tool) → free trial turn is spent.
    fake_anthropic.responses = [fake_anthropic.text_result("here is a description instead")]
    r2 = await _run(client, uid, message="describe a fox in words")
    assert r2.status_code == 200, r2.text
    assert r2.json()["status"] == "assistant_message", r2.text
    after = await _scalar(db_sessionmaker, "SELECT trial_used FROM users WHERE id=:u", u=str(uid))
    assert after is True  # the trial was still available and is now spent on the text turn
    # The text trial turn has no debit (ADR-002: the flip is the "payment").
    assert await _debit_amounts(db_sessionmaker, uid) == []


@pytest.mark.asyncio
async def test_multiple_images_byok_each_own_tool_call_and_ledger_row(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    # byok + TWO images in one turn: text free, but N×IMAGE_CREDITS_COST charged — each image its
    # OWN tool_call_id and its OWN ledger row (idempotency keyed by tool_call_id, ADR-058 §4/§1).
    cost = get_settings().image_credits_cost
    async with db_sessionmaker() as s:
        uid = await seed_user(
            s, subscription="active", balance=100, byok_enabled=True, byok_status="valid"
        )
    fake_anthropic.responses = [
        fake_anthropic.parallel_tool_result(
            [("image.generate", {"prompt": "fox"}), ("image.generate", {"prompt": "owl"})],
            tool_ids=["toolu_mb1", "toolu_mb2"],
        ),
        fake_anthropic.text_result("two animals"),
    ]
    r = await _run(client, uid, mode="byok")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "assistant_message", r.text
    # Two images persisted, each with a DISTINCT tool_call_id.
    async with db_sessionmaker() as s:
        tool_call_ids = (
            await s.execute(
                text("SELECT tool_call_id FROM generated_images WHERE user_id=:u"),
                {"u": str(uid)},
            )
        ).all()
    assert len(tool_call_ids) == 2
    assert len({row[0] for row in tool_call_ids}) == 2  # distinct keys
    # Two image debits, EACH exactly the image cost; NO 1-credit text debit under byok.
    assert await _debit_amounts(db_sessionmaker, uid) == [cost, cost]
    bal = await _scalar(db_sessionmaker, "SELECT balance FROM wallets WHERE user_id=:u", u=str(uid))
    assert int(bal) == 100 - 2 * cost


# ============================ multiple images per turn (§7) ============================
@pytest.mark.asyncio
async def test_multiple_images_one_turn_append_all_each_debited(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    cost = get_settings().image_credits_cost
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=100)
    # One assistant turn with TWO image.generate blocks (ADR-025 parallel tool use).
    fake_anthropic.responses = [
        fake_anthropic.parallel_tool_result(
            [("image.generate", {"prompt": "fox"}), ("image.generate", {"prompt": "owl"})],
            tool_ids=["toolu_m1", "toolu_m2"],
        ),
        fake_anthropic.text_result("two animals"),
    ]
    r = await _run(client, uid)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "assistant_message", body
    # append-all: BOTH images surface in images[] (not last-wins).
    assert len(body["images"]) == 2
    ids = {img["imageId"] for img in body["images"]}
    assert len(ids) == 2

    imgs = await _count(
        db_sessionmaker, "SELECT count(*) FROM generated_images WHERE user_id=:u", u=str(uid)
    )
    assert imgs == 2
    # 1 turn debit + 2 image debits.
    debits = await _count(
        db_sessionmaker,
        "SELECT count(*) FROM ledger_transactions WHERE user_id=:u AND type='debit'",
        u=str(uid),
    )
    assert debits == 3
    bal = await _scalar(db_sessionmaker, "SELECT balance FROM wallets WHERE user_id=:u", u=str(uid))
    assert int(bal) == 100 - (1 + 2 * cost)


# ============================ insufficient credits (§5) ============================
@pytest.mark.asyncio
async def test_insufficient_image_credits_blocks_and_saves_nothing(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    cost = get_settings().image_credits_cost
    # Balance below the image cost → the image debit fails; the whole turn rolls back and blocks.
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=cost - 1)
    fake_anthropic.responses = [
        fake_anthropic.tool_result("image.generate", {"prompt": "a"}, tool_id="toolu_ins1"),
        fake_anthropic.text_result("should not be reached"),
    ]
    r = await _run(client, uid)
    assert r.status_code == 200, r.text  # ADR-004: blocked is HTTP 200
    body = r.json()
    assert body["status"] == "blocked", body
    assert body["blockReason"] == "image_credits_empty"

    # Zero image rows (bytes NOT saved, §5).
    imgs = await _count(
        db_sessionmaker, "SELECT count(*) FROM generated_images WHERE user_id=:u", u=str(uid)
    )
    assert imgs == 0
    # Ledger rolled back: no debit persisted, balance unchanged.
    debits = await _count(
        db_sessionmaker,
        "SELECT count(*) FROM ledger_transactions WHERE user_id=:u AND type='debit'",
        u=str(uid),
    )
    assert debits == 0
    bal = await _scalar(db_sessionmaker, "SELECT balance FROM wallets WHERE user_id=:u", u=str(uid))
    assert int(bal) == cost - 1
    # No dangling tool_use without a tool_result: the whole turn was rolled back → no tool_calls,
    # no tool steps for this (never-persisted) session.
    tool_calls = await _count(
        db_sessionmaker,
        "SELECT count(*) FROM tool_calls WHERE session_id IN "
        "(SELECT id FROM chat_sessions WHERE user_id=:u)",
        u=str(uid),
    )
    assert tool_calls == 0
    tool_steps = await _count(
        db_sessionmaker,
        "SELECT count(*) FROM chat_steps WHERE role='tool' AND session_id IN "
        "(SELECT id FROM chat_sessions WHERE user_id=:u)",
        u=str(uid),
    )
    assert tool_steps == 0


# ============================ idempotency (§4) ============================
@pytest.mark.asyncio
async def test_image_debit_and_row_idempotent_by_tool_call_id(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    # The image debit and the row are keyed by the DOMAIN tool_call_id (uuid4). A second image row
    # for the SAME tool_call_id must not duplicate (partial-unique index + ON CONFLICT DO NOTHING),
    # and a wallet.consume with the same idempotency key must not double-debit (ADR-005/025).
    cost = get_settings().image_credits_cost
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=100)
    fake_anthropic.responses = [
        fake_anthropic.tool_result("image.generate", {"prompt": "a"}, tool_id="toolu_idem1"),
        fake_anthropic.text_result("done"),
    ]
    r = await _run(client, uid)
    body = r.json()
    assert body["status"] == "assistant_message", body
    image_id = body["images"][0]["imageId"]

    # Fetch the domain tool_call_id used as the debit / row idempotency key.
    async with db_sessionmaker() as s:
        tool_call_id = await s.scalar(
            text("SELECT tool_call_id FROM generated_images WHERE id=:id"), {"id": image_id}
        )
        # The idempotency key of the image ledger row is the tool_call_id.
        idem = await s.scalar(
            text(
                "SELECT idempotency_key FROM ledger_transactions "
                "WHERE user_id=:u AND idempotency_key=:k"
            ),
            {"u": str(uid), "k": str(tool_call_id)},
        )
    assert idem == str(tool_call_id)

    # Replaying a wallet.consume with the SAME key does not double-charge; a second INSERT with the
    # same tool_call_id is a no-op (ON CONFLICT DO NOTHING) — simulate the replay directly.
    async with db_sessionmaker() as s:
        dup = await s.execute(
            text(
                "INSERT INTO generated_images (user_id, tool_call_id, content, content_type, size) "
                "VALUES (:u, :tc, :c, 'image/png', :sz) "
                "ON CONFLICT (tool_call_id) WHERE tool_call_id IS NOT NULL DO NOTHING "
                "RETURNING id"
            ),
            {"u": str(uid), "tc": str(tool_call_id), "c": _FIXED_PNG, "sz": len(_FIXED_PNG)},
        )
        await s.commit()
    assert dup.first() is None  # conflict → no duplicate row inserted

    rows = await _count(
        db_sessionmaker,
        "SELECT count(*) FROM generated_images WHERE tool_call_id=:tc",
        tc=str(tool_call_id),
    )
    assert rows == 1
    # Exactly one image debit for this key.
    image_debits = await _count(
        db_sessionmaker,
        "SELECT count(*) FROM ledger_transactions WHERE idempotency_key=:k AND type='debit'",
        k=str(tool_call_id),
    )
    assert image_debits == 1
    bal = await _scalar(db_sessionmaker, "SELECT balance FROM wallets WHERE user_id=:u", u=str(uid))
    assert int(bal) == 100 - (1 + cost)


# ============================ GET /v1/images (§2) ============================
async def _generate_one_image(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    uid: uuid.UUID,
    **run_extra: Any,
) -> str:
    fake_anthropic.responses = [
        fake_anthropic.tool_result(
            "image.generate", {"prompt": "a"}, tool_id=f"toolu_{uuid.uuid4().hex[:12]}"
        ),
        fake_anthropic.text_result("done"),
    ]
    r = await _run(client, uid, **run_extra)
    assert r.status_code == 200, r.text
    return r.json()["images"][0]["imageId"]


@pytest.mark.asyncio
async def test_get_image_owner_200_with_bytes_and_security_headers(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=100)
    image_id = await _generate_one_image(client, db_sessionmaker, fake_anthropic, uid)

    resp = await client.get(f"/v1/images/{image_id}", headers=auth_headers(uid))
    assert resp.status_code == 200, resp.text
    assert resp.content == _FIXED_PNG
    assert resp.headers["content-type"].startswith("image/png")
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["cache-control"] == "private, no-store"


@pytest.mark.asyncio
async def test_get_image_foreign_expired_missing_all_404_identical_body(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        owner = await seed_user(s, subscription="active", balance=100)
        other = await seed_user(s, subscription="active", balance=100)
    image_id = await _generate_one_image(client, db_sessionmaker, fake_anthropic, owner)

    # (1) foreign user → 404.
    foreign = await client.get(f"/v1/images/{image_id}", headers=auth_headers(other))
    assert foreign.status_code == 404

    # (2) missing id → 404.
    missing = await client.get(f"/v1/images/{uuid.uuid4()}", headers=auth_headers(owner))
    assert missing.status_code == 404

    # (3) expired: push expires_at into the past → 404 by the freshness condition (NOT by deletion).
    async with db_sessionmaker() as s:
        await s.execute(
            text("UPDATE generated_images SET expires_at = now() - interval '1 hour' WHERE id=:id"),
            {"id": image_id},
        )
        await s.commit()
    expired = await client.get(f"/v1/images/{image_id}", headers=auth_headers(owner))
    assert expired.status_code == 404
    # The row still physically exists (logical inaccessibility ≠ deletion, §2).
    still_there = await _count(
        db_sessionmaker, "SELECT count(*) FROM generated_images WHERE id=:id", id=image_id
    )
    assert still_there == 1

    # Bodies are byte-identical across all three 404s (existence not revealed).
    assert foreign.content == missing.content == expired.content


# ============================ TTL (§6) ============================
@pytest.mark.asyncio
async def test_temporary_chat_image_expires_at_is_created_plus_ttl(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    ttl = get_settings().temporary_image_ttl_seconds
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=100)
    fake_anthropic.responses = [
        fake_anthropic.tool_result("image.generate", {"prompt": "a"}, tool_id="toolu_ttl1"),
        fake_anthropic.text_result("done"),
    ]
    r = await _run(client, uid, temporary=True, history=[])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "assistant_message", body
    image_id = body["images"][0]["imageId"]

    # expires_at == created_at + TTL, compared on the DB side (one clock), tolerance < 1s.
    async with db_sessionmaker() as s:
        drift = await s.scalar(
            text(
                "SELECT abs(extract(epoch FROM (expires_at - created_at)) - :ttl) "
                "FROM generated_images WHERE id=:id"
            ),
            {"ttl": ttl, "id": image_id},
        )
    assert drift is not None
    assert float(drift) < 1.0


@pytest.mark.asyncio
async def test_normal_chat_image_expires_at_null(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=100)
    image_id = await _generate_one_image(client, db_sessionmaker, fake_anthropic, uid)
    expires_at = await _scalar(
        db_sessionmaker, "SELECT expires_at FROM generated_images WHERE id=:id", id=image_id
    )
    assert expires_at is None


# ============================ degrade (§3) ============================
@pytest.mark.asyncio
async def test_content_policy_degrades_turn_survives_no_image_debit(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    fake_image_generator: FakeImageGenerator,
) -> None:
    fake_image_generator.mode = "content_policy"
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=100)
    fake_anthropic.responses = [
        fake_anthropic.tool_result("image.generate", {"prompt": "a"}, tool_id="toolu_cp1"),
        fake_anthropic.text_result("I could not draw that; let me describe it instead."),
    ]
    r = await _run(client, uid)
    assert r.status_code == 200, r.text
    body = r.json()
    # The turn SURVIVES (not 502, not blocked).
    assert body["status"] == "assistant_message", body
    assert body.get("images") in (None, [])
    # The tool_result records the content_policy code.
    sid = body["sessionId"]
    async with db_sessionmaker() as s:
        payload = await s.scalar(
            text(
                "SELECT payload FROM chat_steps WHERE session_id=:sid AND role='tool' "
                "ORDER BY seq LIMIT 1"
            ),
            {"sid": sid},
        )
    assert payload["error"]["code"] == "content_policy"
    # No image row; the image debit was NOT charged, but the TURN credit WAS (§3).
    imgs = await _count(
        db_sessionmaker, "SELECT count(*) FROM generated_images WHERE user_id=:u", u=str(uid)
    )
    assert imgs == 0
    bal = await _scalar(db_sessionmaker, "SELECT balance FROM wallets WHERE user_id=:u", u=str(uid))
    assert int(bal) == 99  # only the 1-credit turn debit


@pytest.mark.asyncio
async def test_generation_failure_degrades_turn_survives(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    fake_image_generator: FakeImageGenerator,
) -> None:
    fake_image_generator.mode = "failure"
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=100)
    fake_anthropic.responses = [
        fake_anthropic.tool_result("image.generate", {"prompt": "a"}, tool_id="toolu_gf1"),
        fake_anthropic.text_result("image service is down; here is text."),
    ]
    r = await _run(client, uid)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "assistant_message", body
    sid = body["sessionId"]
    async with db_sessionmaker() as s:
        payload = await s.scalar(
            text(
                "SELECT payload FROM chat_steps WHERE session_id=:sid AND role='tool' "
                "ORDER BY seq LIMIT 1"
            ),
            {"sid": sid},
        )
    assert payload["error"]["code"] == "image_generation_failed"
    imgs = await _count(
        db_sessionmaker, "SELECT count(*) FROM generated_images WHERE user_id=:u", u=str(uid)
    )
    assert imgs == 0
    # Turn credit still charged.
    bal = await _scalar(db_sessionmaker, "SELECT balance FROM wallets WHERE user_id=:u", u=str(uid))
    assert int(bal) == 99


# ============================ TD-035 privacy (§7) ============================
@pytest.mark.asyncio
async def test_prompt_never_leaks_to_audit_or_server_tool_summary(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=100)
    fake_anthropic.responses = [
        fake_anthropic.tool_result(
            "image.generate", {"prompt": _SECRET_PROMPT}, tool_id="toolu_td035"
        ),
        fake_anthropic.text_result("done"),
    ]
    r = await _run(client, uid)
    body = r.json()
    assert body["status"] == "assistant_message", body

    # (1) audit_logs.payload carries NONE of the prompt (TD-035).
    audit_text = await _scalar(
        db_sessionmaker,
        "SELECT string_agg(payload::text, '||') FROM audit_logs WHERE user_id=:u",
        u=str(uid),
    )
    assert audit_text is not None
    assert _SECRET_PROMPT not in audit_text

    # (2) serverTools[].summary is content-free ("ok"), never the prompt.
    server = body["serverTools"]
    img_tool = next(st for st in server if st["toolName"] == "image.generate")
    assert img_tool["status"] == "completed"
    assert _SECRET_PROMPT not in (img_tool.get("summary") or "")


# ============================ temporary chat persistence (§6) ============================
@pytest.mark.asyncio
async def test_temporary_chat_persists_only_the_image_row(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=100)
    fake_anthropic.responses = [
        fake_anthropic.tool_result("image.generate", {"prompt": "a"}, tool_id="toolu_temp1"),
        fake_anthropic.text_result("ephemeral fox"),
    ]
    r = await _run(client, uid, temporary=True, history=[])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "assistant_message", body
    assert len(body["images"]) == 1

    # Zero chat_* rows (ADR-056 §1).
    assert (
        await _count(
            db_sessionmaker, "SELECT count(*) FROM chat_sessions WHERE user_id=:u", u=str(uid)
        )
        == 0
    )
    assert await _count(db_sessionmaker, "SELECT count(*) FROM chat_steps") == 0
    assert await _count(db_sessionmaker, "SELECT count(*) FROM tool_calls") == 0

    # But the image row EXISTS with session_id NULL + a non-null expires_at (§6).
    async with db_sessionmaker() as s:
        row = (
            await s.execute(
                text("SELECT session_id, expires_at FROM generated_images WHERE user_id=:u"),
                {"u": str(uid)},
            )
        ).one()
    session_id, expires_at = row
    assert session_id is None
    assert expires_at is not None


# ============================ opportunistic sweep (§6) ============================
class _FakeRedis:
    """Minimal Redis double supporting SET NX EX (the sweep lock/throttle key)."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def set(self, key: str, value: str, *, nx: bool = False, ex: int | None = None) -> Any:
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True


class _BrokenRedis:
    async def set(self, *args: Any, **kwargs: Any) -> Any:
        raise redis.RedisError("redis is down")


async def _insert_expired(
    maker: async_sessionmaker[AsyncSession], uid: uuid.UUID, *, count: int, past: bool
) -> None:
    ts = "now() - interval '1 hour'" if past else "now() + interval '1 hour'"
    async with maker() as s:
        for _ in range(count):
            await s.execute(
                text(
                    "INSERT INTO generated_images "
                    "(user_id, content, content_type, size, expires_at) "
                    f"VALUES (:u, :c, 'image/png', :sz, {ts})"
                ),
                {"u": str(uid), "c": _FIXED_PNG, "sz": len(_FIXED_PNG)},
            )
        await s.commit()


@pytest.mark.asyncio
async def test_sweep_deletes_one_bounded_batch(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A single sweep deletes at most IMAGE_SWEEP_BATCH_SIZE expired rows (bounded batch), leaving
    # the rest — proves the LIMIT batching. Non-expired rows are never touched.
    monkeypatch.setattr(get_settings(), "image_sweep_batch_size", 2)
    monkeypatch.setattr(sweep_mod, "get_redis", lambda: _FakeRedis())
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=0)
    await _insert_expired(db_sessionmaker, uid, count=5, past=True)
    await _insert_expired(db_sessionmaker, uid, count=3, past=False)  # future → never swept

    await sweep_mod.maybe_sweep_expired_images(db_session)

    remaining_expired = await _count(
        db_sessionmaker,
        "SELECT count(*) FROM generated_images WHERE user_id=:u AND expires_at <= now()",
        u=str(uid),
    )
    assert remaining_expired == 3  # 5 - batch(2)
    remaining_future = await _count(
        db_sessionmaker,
        "SELECT count(*) FROM generated_images WHERE user_id=:u AND expires_at > now()",
        u=str(uid),
    )
    assert remaining_future == 3  # future rows untouched


@pytest.mark.asyncio
async def test_sweep_fail_open_when_redis_down(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sweep_mod, "get_redis", lambda: _BrokenRedis())
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=0)
    await _insert_expired(db_sessionmaker, uid, count=3, past=True)

    # Redis down → the sweep is a no-op and NEVER raises (fail-open); expired rows remain.
    await sweep_mod.maybe_sweep_expired_images(db_session)

    remaining = await _count(
        db_sessionmaker, "SELECT count(*) FROM generated_images WHERE user_id=:u", u=str(uid)
    )
    assert remaining == 3


@pytest.mark.asyncio
async def test_sweep_throttled_by_lock_within_interval(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A shared FakeRedis holds the lock/throttle key across both calls: the FIRST sweep acquires it
    # and deletes a batch, the SECOND (within the interval) finds the key set → no-op.
    shared = _FakeRedis()
    monkeypatch.setattr(get_settings(), "image_sweep_batch_size", 2)
    monkeypatch.setattr(sweep_mod, "get_redis", lambda: shared)
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=0)
    await _insert_expired(db_sessionmaker, uid, count=5, past=True)

    await sweep_mod.maybe_sweep_expired_images(db_session)
    after_first = await _count(
        db_sessionmaker, "SELECT count(*) FROM generated_images WHERE user_id=:u", u=str(uid)
    )
    assert after_first == 3  # 5 - batch(2)

    # Second call is throttled by the still-held lock → deletes nothing more.
    await sweep_mod.maybe_sweep_expired_images(db_session)
    after_second = await _count(
        db_sessionmaker, "SELECT count(*) FROM generated_images WHERE user_id=:u", u=str(uid)
    )
    assert after_second == 3
