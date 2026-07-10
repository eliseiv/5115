"""Integration tests for ADR-025 — parallel client-side tool calls + max_tokens truncation.

Normative coverage of docs/modules/chat-orchestrator/09-testing.md §«Integration — Параллельные
tool-вызовы + max_tokens (ADR-025)» and docs/09-e2e-testing.md (E2E-TOOL-6/7 invariants exercised
hermetically here). Real PostgreSQL container; Anthropic faked at the client boundary via the
shared FakeAnthropicClient (BUG-4 invariant: provider ids are realistic ``toolu_...``, never UUID).

Two independent fixes:
- Fix B — parallel client-side tool calls: ``toolCalls[]`` surfaces ALL client-side tool_use of a
  turn; the turn barrier gates continuation until every client-side result is collected; mixed
  (server-side + client-side) turns; idempotency; backward compatibility; billing unchanged.
- Fix A — max_tokens: stop_reason="max_tokens" → status=blocked, blockReason=max_tokens, no
  toolCall(s), usage/messageStepId/stepId present, NO debit / NO trial flip; config defaults;
  policy-blocked regression contrast.

Scenarios map 1:1 to the task brief (1..10).
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.conftest import FakeAnthropicClient, auth_headers, seed_user

_WRITE_A = {"path": "index.html", "content": "<h1>a</h1>", "encoding": "utf8", "overwrite": True}
_WRITE_B = {"path": "style.css", "content": "body{}", "encoding": "utf8", "overwrite": True}


def _history_step_by_id(history: dict, step_id: str) -> dict | None:
    for step in history["steps"]:
        if step["id"] == step_id:
            return step
    return None


def _tool_use_blocks(step: dict) -> list[dict]:
    content = step.get("payload", {}).get("content", [])
    return [b for b in content if isinstance(b, dict) and b.get("type") == "tool_use"]


async def _count(maker: async_sessionmaker[AsyncSession], sql: str, uid: object) -> int:
    async with maker() as s:
        return int(await s.scalar(text(sql), {"u": str(uid)}) or 0)


async def _balance(maker: async_sessionmaker[AsyncSession], uid: object) -> int | None:
    async with maker() as s:
        bal = await s.scalar(
            text("SELECT balance FROM wallets WHERE user_id = :u"), {"u": str(uid)}
        )
    return None if bal is None else int(bal)


async def _trial_used(maker: async_sessionmaker[AsyncSession], uid: object) -> bool:
    async with maker() as s:
        return bool(
            await s.scalar(text("SELECT trial_used FROM users WHERE id = :u"), {"u": str(uid)})
        )


# ============================================================================================
# Scenario 1 — ALL client-side tool_use surfaced in toolCalls[] (parallel). Two files.write in
# one turn → status=tool_call, toolCalls[] has BOTH (in block order), toolCall == toolCalls[0].
# FAILS on the old (first_client_out) implementation that surfaced only one.
# ============================================================================================
@pytest.mark.asyncio
async def test_parallel_tool_use_all_calls_surfaced_in_tool_calls(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [
        fake_anthropic.parallel_tool_result(
            [("files.write", _WRITE_A), ("files.write", _WRITE_B)],
            tool_ids=["toolu_01ParA", "toolu_01ParB"],
        ),
    ]

    r = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "projectId": "p", "message": "build", "mode": "credits"},
        headers=auth_headers(uid),
    )
    body = r.json()
    assert body["status"] == "tool_call", body
    # ALL client-side calls present, in block order.
    assert body["toolCalls"] is not None
    assert len(body["toolCalls"]) == 2, body["toolCalls"]
    names = [tc["name"] for tc in body["toolCalls"]]
    args = [tc["args"] for tc in body["toolCalls"]]
    assert names == ["files.write", "files.write"]
    assert args == [_WRITE_A, _WRITE_B]
    # Two distinct domain ids (not provider ids).
    ids = [tc["id"] for tc in body["toolCalls"]]
    assert ids[0] != ids[1]
    assert all(not i.startswith("toolu_") for i in ids)
    # Backward-compat singular toolCall == toolCalls[0].
    assert body["toolCall"] == body["toolCalls"][0]
    # stepId one per turn; all toolCalls belong to it (carrier assistant step has both tool_use).
    sid = body["sessionId"]
    hist = (await client.get(f"/v1/chats/{sid}", headers=auth_headers(uid))).json()
    carrier = _history_step_by_id(hist, body["stepId"])
    assert carrier is not None and carrier["role"] == "assistant"
    assert len(_tool_use_blocks(carrier)) == 2


# ============================================================================================
# Scenario 2 — turn barrier: one of two results → status=tool_call with the REMAINING call,
# Anthropic NOT called, NO debit. Second result → continuation (one Anthropic call). Also the
# batch [r1, r2] closes the barrier at once.
# ============================================================================================
@pytest.mark.asyncio
async def test_turn_barrier_partial_then_complete_then_continuation(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [
        fake_anthropic.parallel_tool_result(
            [("files.write", _WRITE_A), ("files.write", _WRITE_B)],
            tool_ids=["toolu_01BarA", "toolu_01BarB"],
        ),
        fake_anthropic.text_result("done"),
    ]

    run = (
        await client.post(
            "/v1/chat/run",
            json={"userId": str(uid), "projectId": "p", "message": "build", "mode": "credits"},
            headers=auth_headers(uid),
        )
    ).json()
    assert run["status"] == "tool_call", run
    sid = run["sessionId"]
    id_a, id_b = run["toolCalls"][0]["id"], run["toolCalls"][1]["id"]
    calls_after_run = len(fake_anthropic.calls)  # 1 (the run generation)

    # Send ONE of two results → barrier still open.
    tr1 = (
        await client.post(
            "/v1/chat/tool-result",
            json={"userId": str(uid), "sessionId": sid, "toolCallId": id_a, "result": {"ok": 1}},
            headers=auth_headers(uid),
        )
    ).json()
    assert tr1["status"] == "tool_call", tr1
    # Remaining toolCalls[] = the not-yet-completed call (id_b).
    remaining_ids = [tc["id"] for tc in tr1["toolCalls"]]
    assert remaining_ids == [id_b]
    assert tr1["toolCall"]["id"] == id_b
    # Anthropic NOT called for the partial result, credit NOT debited.
    assert len(fake_anthropic.calls) == calls_after_run
    assert (
        await _count(
            db_sessionmaker, "SELECT count(*) FROM ledger_transactions WHERE user_id=:u", uid
        )
        == 0
    )

    # Send the second result → barrier closes → continuation (ONE Anthropic call) → final.
    tr2 = (
        await client.post(
            "/v1/chat/tool-result",
            json={"userId": str(uid), "sessionId": sid, "toolCallId": id_b, "result": {"ok": 1}},
            headers=auth_headers(uid),
        )
    ).json()
    assert tr2["status"] == "assistant_message", tr2
    assert len(fake_anthropic.calls) == calls_after_run + 1  # exactly one continuation call
    # Exactly one debit on the final assistant_message.
    assert (
        await _count(
            db_sessionmaker,
            "SELECT count(*) FROM ledger_transactions WHERE user_id=:u AND type='debit'",
            uid,
        )
        == 1
    )


@pytest.mark.asyncio
async def test_turn_barrier_batch_closes_at_once(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [
        fake_anthropic.parallel_tool_result(
            [("files.write", _WRITE_A), ("files.write", _WRITE_B)],
            tool_ids=["toolu_01BatchA", "toolu_01BatchB"],
        ),
        fake_anthropic.text_result("done"),
    ]
    run = (
        await client.post(
            "/v1/chat/run",
            json={"userId": str(uid), "projectId": "p", "message": "build", "mode": "credits"},
            headers=auth_headers(uid),
        )
    ).json()
    sid = run["sessionId"]
    id_a, id_b = run["toolCalls"][0]["id"], run["toolCalls"][1]["id"]
    calls_after_run = len(fake_anthropic.calls)

    # Batch [r1, r2] in ONE request → barrier closed immediately → continuation → final.
    tr = (
        await client.post(
            "/v1/chat/tool-result",
            json={
                "userId": str(uid),
                "sessionId": sid,
                "results": [
                    {"toolCallId": id_a, "result": {"ok": 1}},
                    {"toolCallId": id_b, "result": {"ok": 2}},
                ],
            },
            headers=auth_headers(uid),
        )
    ).json()
    assert tr["status"] == "assistant_message", tr
    assert len(fake_anthropic.calls) == calls_after_run + 1  # single continuation


# ============================================================================================
# Scenario 3 — mixed turn (site.write_file server-side + files.write client-side in one turn):
# site.* executed on the backend, toolCalls[] carries ONLY client-side; continuation after the
# client tool_result, gathering both server-side + client-side tool_result.
# ============================================================================================
@pytest.mark.asyncio
async def test_mixed_turn_server_side_executed_client_side_surfaced(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    site_args = {
        "path": "index.html",
        "content": "<h1>x</h1>",
        "contentType": "text/html",
        "encoding": "utf8",
    }
    fake_anthropic.responses = [
        fake_anthropic.parallel_tool_result(
            [("site.write_file", site_args), ("files.write", _WRITE_A)],
            tool_ids=["toolu_01MixSite", "toolu_01MixClient"],
        ),
        fake_anthropic.text_result("ready"),
    ]

    run = (
        await client.post(
            "/v1/chat/run",
            json={"userId": str(uid), "projectId": "site-proj", "message": "go", "mode": "credits"},
            headers=auth_headers(uid),
        )
    ).json()
    assert run["status"] == "tool_call", run
    sid = run["sessionId"]
    # Only the client-side files.write is surfaced; site.write_file is not.
    assert [tc["name"] for tc in run["toolCalls"]] == ["files.write"]
    client_id = run["toolCalls"][0]["id"]

    # site.write_file executed on the backend → site_files row persisted.
    async with db_sessionmaker() as s:
        site_files = int(await s.scalar(text("SELECT count(*) FROM site_files")) or 0)
    assert site_files == 1
    # Send the client result → barrier closes (server-side already completed) → continuation.
    tr = (
        await client.post(
            "/v1/chat/tool-result",
            json={
                "userId": str(uid),
                "sessionId": sid,
                "toolCallId": client_id,
                "result": {"ok": 1},
            },
            headers=auth_headers(uid),
        )
    ).json()
    assert tr["status"] == "assistant_message", tr


# ============================================================================================
# Scenario 4 — idempotency: repeated completed toolCallId (batch/single) does not overwrite,
# continuation not duplicated, debit not repeated; duplicate toolCallId in one batch → 422;
# cross-session toolCallId → 404; cross-turn in a batch → 422.
# ============================================================================================
@pytest.mark.asyncio
async def test_idempotent_replay_no_duplicate_continuation_or_debit(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [
        fake_anthropic.parallel_tool_result(
            [("files.write", _WRITE_A), ("files.write", _WRITE_B)],
            tool_ids=["toolu_01IdemA", "toolu_01IdemB"],
        ),
        fake_anthropic.text_result("done"),
    ]
    run = (
        await client.post(
            "/v1/chat/run",
            json={"userId": str(uid), "projectId": "p", "message": "build", "mode": "credits"},
            headers=auth_headers(uid),
        )
    ).json()
    sid = run["sessionId"]
    id_a, id_b = run["toolCalls"][0]["id"], run["toolCalls"][1]["id"]

    tr = (
        await client.post(
            "/v1/chat/tool-result",
            json={
                "userId": str(uid),
                "sessionId": sid,
                "results": [
                    {"toolCallId": id_a, "result": {"ok": 1}},
                    {"toolCallId": id_b, "result": {"ok": 2}},
                ],
            },
            headers=auth_headers(uid),
        )
    ).json()
    assert tr["status"] == "assistant_message", tr
    calls_after_first = len(fake_anthropic.calls)

    # Replay the SAME batch → idempotent: same step, no new Anthropic call, no extra debit.
    tr2 = (
        await client.post(
            "/v1/chat/tool-result",
            json={
                "userId": str(uid),
                "sessionId": sid,
                "results": [
                    {"toolCallId": id_a, "result": {"ok": 1}},
                    {"toolCallId": id_b, "result": {"ok": 2}},
                ],
            },
            headers=auth_headers(uid),
        )
    ).json()
    assert tr2["status"] == "assistant_message", tr2
    assert tr2["stepId"] == tr["stepId"]
    assert tr2["messageStepId"] == tr["messageStepId"]
    assert len(fake_anthropic.calls) == calls_after_first  # no extra continuation

    # Single-form replay of one completed id → also idempotent (same saved step).
    tr3 = (
        await client.post(
            "/v1/chat/tool-result",
            json={"userId": str(uid), "sessionId": sid, "toolCallId": id_a, "result": {"ok": 1}},
            headers=auth_headers(uid),
        )
    ).json()
    assert tr3["status"] == "assistant_message", tr3
    assert tr3["stepId"] == tr["stepId"]
    assert len(fake_anthropic.calls) == calls_after_first

    # Exactly one debit despite all the replays.
    assert (
        await _count(
            db_sessionmaker,
            "SELECT count(*) FROM ledger_transactions WHERE user_id=:u AND type='debit'",
            uid,
        )
        == 1
    )


@pytest.mark.asyncio
async def test_duplicate_tool_call_id_in_batch_is_422(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [
        fake_anthropic.parallel_tool_result(
            [("files.write", _WRITE_A), ("files.write", _WRITE_B)],
            tool_ids=["toolu_01DupA", "toolu_01DupB"],
        ),
    ]
    run = (
        await client.post(
            "/v1/chat/run",
            json={"userId": str(uid), "projectId": "p", "message": "build", "mode": "credits"},
            headers=auth_headers(uid),
        )
    ).json()
    sid = run["sessionId"]
    id_a = run["toolCalls"][0]["id"]
    r = await client.post(
        "/v1/chat/tool-result",
        json={
            "userId": str(uid),
            "sessionId": sid,
            "results": [
                {"toolCallId": id_a, "result": {"ok": 1}},
                {"toolCallId": id_a, "result": {"ok": 2}},
            ],
        },
        headers=auth_headers(uid),
    )
    assert r.status_code == 422, r.text


@pytest.mark.asyncio
async def test_cross_session_tool_call_id_is_404(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [
        fake_anthropic.parallel_tool_result(
            [("files.write", _WRITE_A)], tool_ids=["toolu_01XSess"]
        ),
    ]
    run = (
        await client.post(
            "/v1/chat/run",
            json={"userId": str(uid), "projectId": "p", "message": "build", "mode": "credits"},
            headers=auth_headers(uid),
        )
    ).json()
    real_tcid = run["toolCalls"][0]["id"]
    # A foreign sessionId for the real toolCallId → 404 (ownership/single-session invariant).
    import uuid as _uuid

    r = await client.post(
        "/v1/chat/tool-result",
        json={
            "userId": str(uid),
            "sessionId": str(_uuid.uuid4()),
            "toolCallId": real_tcid,
            "result": {"ok": 1},
        },
        headers=auth_headers(uid),
    )
    assert r.status_code == 404, r.text


@pytest.mark.asyncio
async def test_cross_turn_tool_call_id_in_batch_is_422(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """Two separate turns in one session; a batch mixing toolCallIds from different turns → 422
    (all batch items must belong to one turn / one message_step_id)."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    # Turn 1 → a tool_call; do not complete it. Then a SECOND run on the same session → turn 2.
    fake_anthropic.responses = [
        fake_anthropic.parallel_tool_result([("files.write", _WRITE_A)], tool_ids=["toolu_01T1"]),
        fake_anthropic.parallel_tool_result([("files.write", _WRITE_B)], tool_ids=["toolu_01T2"]),
    ]
    run1 = (
        await client.post(
            "/v1/chat/run",
            json={"userId": str(uid), "projectId": "p", "message": "one", "mode": "credits"},
            headers=auth_headers(uid),
        )
    ).json()
    sid = run1["sessionId"]
    id_turn1 = run1["toolCalls"][0]["id"]
    run2 = (
        await client.post(
            "/v1/chat/run",
            json={
                "userId": str(uid),
                "sessionId": sid,
                "projectId": "p",
                "message": "two",
                "mode": "credits",
            },
            headers=auth_headers(uid),
        )
    ).json()
    id_turn2 = run2["toolCalls"][0]["id"]

    r = await client.post(
        "/v1/chat/tool-result",
        json={
            "userId": str(uid),
            "sessionId": sid,
            "results": [
                {"toolCallId": id_turn1, "result": {"ok": 1}},
                {"toolCallId": id_turn2, "result": {"ok": 2}},
            ],
        },
        headers=auth_headers(uid),
    )
    assert r.status_code == 422, r.text


# ============================================================================================
# Scenario 5 — backward compatibility: single request form ≡ batch of one; singular toolCall of
# the response == toolCalls[0].
# ============================================================================================
@pytest.mark.asyncio
async def test_single_form_equivalent_to_batch_of_one(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    # Single client tool call this turn → single-form tool-result must drive continuation.
    fake_anthropic.responses = [
        fake_anthropic.tool_result("files.read", {"path": "a.txt"}, tool_id="toolu_01Single"),
        fake_anthropic.text_result("final"),
    ]
    run = (
        await client.post(
            "/v1/chat/run",
            json={"userId": str(uid), "projectId": "p", "message": "go", "mode": "credits"},
            headers=auth_headers(uid),
        )
    ).json()
    assert run["status"] == "tool_call", run
    # Singular toolCall == toolCalls[0].
    assert run["toolCall"] == run["toolCalls"][0]
    assert len(run["toolCalls"]) == 1
    sid = run["sessionId"]
    tcid = run["toolCall"]["id"]

    # Deprecated single form closes the (one-call) barrier → continuation → final.
    tr = (
        await client.post(
            "/v1/chat/tool-result",
            json={"userId": str(uid), "sessionId": sid, "toolCallId": tcid, "result": {"ok": 1}},
            headers=auth_headers(uid),
        )
    ).json()
    assert tr["status"] == "assistant_message", tr


# ============================================================================================
# Scenario 6 — billing: a turn with several parallel calls + batch results debits EXACTLY 1
# credit on the final assistant_message (idempotent by messageStepId).
# ============================================================================================
@pytest.mark.asyncio
async def test_parallel_turn_batch_results_bills_exactly_one_credit(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [
        fake_anthropic.parallel_tool_result(
            [("files.write", _WRITE_A), ("files.write", _WRITE_B)],
            tool_ids=["toolu_01BillA", "toolu_01BillB"],
        ),
        fake_anthropic.text_result("done"),
    ]
    run = (
        await client.post(
            "/v1/chat/run",
            json={"userId": str(uid), "projectId": "p", "message": "build", "mode": "credits"},
            headers=auth_headers(uid),
        )
    ).json()
    sid = run["sessionId"]
    id_a, id_b = run["toolCalls"][0]["id"], run["toolCalls"][1]["id"]
    tr = (
        await client.post(
            "/v1/chat/tool-result",
            json={
                "userId": str(uid),
                "sessionId": sid,
                "results": [
                    {"toolCallId": id_a, "result": {"ok": 1}},
                    {"toolCallId": id_b, "result": {"ok": 2}},
                ],
            },
            headers=auth_headers(uid),
        )
    ).json()
    assert tr["status"] == "assistant_message", tr
    # Exactly one debit (5 → 4); idempotent by messageStepId — one ledger debit row.
    assert await _balance(db_sessionmaker, uid) == 4
    assert (
        await _count(
            db_sessionmaker,
            "SELECT count(*) FROM ledger_transactions WHERE user_id=:u AND type='debit'",
            uid,
        )
        == 1
    )


# ============================================================================================
# Scenario 7 — max_tokens: stop_reason=max_tokens (text + incomplete tool_use) → status=blocked,
# blockReason=max_tokens, NO toolCall/toolCalls, usage/messageStepId/stepId present (not null),
# assistantMessage = partial text, credit NOT debited, trial NOT flipped (mode=credits, active).
# FAILS on the old implementation (assistant_message + toolCall=null).
# ============================================================================================
@pytest.mark.asyncio
async def test_max_tokens_truncation_blocked_no_debit_ids_present(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [
        fake_anthropic.max_tokens_result(
            text="Here is the start of the landing page...",
            truncated_tool=("files.write", {"path": "index.html"}),  # incomplete: no content
            tool_id="toolu_01Trunc",
            output_tokens=16000,
        ),
    ]
    r = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "projectId": "p", "message": "make a landing", "mode": "credits"},
        headers=auth_headers(uid),
    )
    assert r.status_code == 200, r.text  # blocked is HTTP 200 (ADR-004)
    body = r.json()
    assert body["status"] == "blocked", body
    assert body["blockReason"] == "max_tokens"
    # No tool calls surfaced (incomplete tool_use must not leak).
    assert body.get("toolCall") is None
    assert body.get("toolCalls") is None
    # Unlike policy-blocked: usage + message_step_id + step_id present (not null).
    assert body["usage"] is not None
    assert body["usage"]["outputTokens"] == 16000
    assert body["messageStepId"] is not None
    assert body["stepId"] is not None
    # Partial text surfaced.
    assert body["assistantMessage"] == "Here is the start of the landing page..."
    # Credit NOT debited, trial NOT flipped.
    assert await _balance(db_sessionmaker, uid) == 5
    assert (
        await _count(
            db_sessionmaker,
            "SELECT count(*) FROM ledger_transactions WHERE user_id=:u AND type='debit'",
            uid,
        )
        == 0
    )


@pytest.mark.asyncio
async def test_max_tokens_truncation_does_not_flip_trial(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    # Trial user (no subscription, trial_used=false, mode=credits) — policy allows; truncation
    # must NOT consume the lifetime trial (no successful final assistant_message).
    async with db_sessionmaker() as s:
        uid = await seed_user(s, trial_used=False)
    fake_anthropic.responses = [
        fake_anthropic.max_tokens_result(text="partial", output_tokens=16000),
    ]
    r = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "projectId": "p", "message": "go", "mode": "credits"},
        headers=auth_headers(uid),
    )
    body = r.json()
    assert body["status"] == "blocked" and body["blockReason"] == "max_tokens", body
    assert await _trial_used(db_sessionmaker, uid) is False


# ============================================================================================
# Scenario 8 — config defaults: ANTHROPIC_MAX_TOKENS=16000, ANTHROPIC_TIMEOUT_SECONDS=120 by
# model_fields default (a local .env may override the live get_settings()).
# ============================================================================================
def test_config_defaults_max_tokens_and_timeout() -> None:
    from app.config import Settings

    fields = Settings.model_fields
    assert fields["anthropic_max_tokens"].default == 16000
    assert fields["anthropic_timeout_seconds"].default == 120.0


# ============================================================================================
# Scenario 9 — policy-blocked regression: credits_empty still has messageStepId/stepId=null and
# NO usage (contrast with max_tokens-blocked).
# ============================================================================================
@pytest.mark.asyncio
async def test_policy_blocked_credits_empty_null_ids_no_usage(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    # Active subscription + balance 0 + mode=credits → credits_empty BEFORE generation.
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=0)
    r = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "projectId": "p", "message": "go", "mode": "credits"},
        headers=auth_headers(uid),
    )
    body = r.json()
    assert r.status_code == 200
    assert body["status"] == "blocked", body
    assert body["blockReason"] == "credits_empty"
    # Contrast with max_tokens: both ids null, no usage.
    assert body["messageStepId"] is None
    assert body["stepId"] is None
    assert body.get("usage") is None
    # Block happened before generation — Anthropic not called.
    assert fake_anthropic.calls == []


# ============================================================================================
# Scenario 10 — sync invariant: toolCalls[i].name (dot) / .id (domain) == the tool_use block of
# step stepId in GET /v1/chats/{id} == /v1/tools name.
# ============================================================================================
@pytest.mark.asyncio
async def test_sync_invariant_tool_calls_match_history_and_tools_catalog(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [
        fake_anthropic.parallel_tool_result(
            [("files.write", _WRITE_A), ("files.read", {"path": "b.txt"})],
            tool_ids=["toolu_01SyncA", "toolu_01SyncB"],
        ),
    ]
    run = (
        await client.post(
            "/v1/chat/run",
            json={"userId": str(uid), "projectId": "p", "message": "go", "mode": "credits"},
            headers=auth_headers(uid),
        )
    ).json()
    assert run["status"] == "tool_call", run
    sid = run["sessionId"]
    step_id = run["stepId"]

    # /v1/tools advertises the same dot names.
    tools = (await client.get("/v1/tools", headers=auth_headers(uid))).json()["tools"]
    tool_names = {t["name"] for t in tools}
    for tc in run["toolCalls"]:
        assert tc["name"] in tool_names

    # History: the carrier step's tool_use blocks match toolCalls[] name+id (domain), in order.
    hist = (await client.get(f"/v1/chats/{sid}", headers=auth_headers(uid))).json()
    carrier = _history_step_by_id(hist, step_id)
    assert carrier is not None
    hist_tu = _tool_use_blocks(carrier)
    assert [b["name"] for b in hist_tu] == [tc["name"] for tc in run["toolCalls"]]
    assert [b["id"] for b in hist_tu] == [tc["id"] for tc in run["toolCalls"]]
    # No provider id leaks.
    assert "toolu_" not in (await client.get(f"/v1/chats/{sid}", headers=auth_headers(uid))).text
