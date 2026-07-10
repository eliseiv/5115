"""Integration tests for ADR-023 — sync ids (`messageStepId`/`stepId`) in `ChatResponse`.

Normative coverage of the sync invariant per docs/modules/chat-orchestrator/09-testing.md
§«Integration — sync ids в ChatResponse (ADR-023)». Real PostgreSQL container; Anthropic faked at
the client boundary via the shared FakeAnthropicClient (same fake used by the tool-loop tests).
The fake returns realistic ``toolu_...`` provider ids (BUG-4 invariant), never UUID-shaped.

Scenarios (the 6 normative requirements):
1. ``/chat/run`` and ``/chat/tool-result`` with status=assistant_message / tool_call →
   ``messageStepId`` and ``stepId`` are NON-EMPTY (neither null).
2. ``stepId`` is byte-for-byte == ``ChatStepSchema.id`` of the carrier step in
   ``GET /v1/chats/{id}`` (assistant_message → final assistant step; tool_call → the assistant
   step carrying the ``tool_use`` block). Likewise ``messageStepId`` == that step's
   ``ChatStepSchema.messageStepId``.
3. ``messageStepId`` is stable within a turn: the value from ``/chat/run`` == the value in the
   subsequent ``/chat/tool-result`` of the same turn.
4. status=blocked → ``messageStepId`` is null AND ``stepId`` is null.
5. status=tool_call → ``stepId`` != ``toolCall.id`` AND ``messageStepId`` != ``toolCall.id``.
6. Idempotent replay of ``/chat/tool-result`` (same toolCallId) → same ``messageStepId``/``stepId``
   as the first response.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.conftest import FakeAnthropicClient, auth_headers, seed_user


def _history_step_by_id(history: dict, step_id: str) -> dict | None:
    """Return the ChatStepSchema (from GET /v1/chats/{id}) whose `id` equals step_id, or None."""
    for step in history["steps"]:
        if step["id"] == step_id:
            return step
    return None


def _has_tool_use_block(step: dict) -> bool:
    content = step.get("payload", {}).get("content", [])
    return any(isinstance(b, dict) and b.get("type") == "tool_use" for b in content)


# --------------------------------------------------------------------------------------------
# Scenario 1 + 2 + 5 (run → tool_call): non-empty ids; stepId == carrier (tool_use) step id;
# messageStepId == that step's messageStepId; stepId/messageStepId != toolCall.id.
# --------------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_run_tool_call_ids_nonempty_match_history_and_differ_from_toolcall(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    # Realistic toolu_ provider id (BUG-4 invariant): the domain toolCall.id is a separate UUID.
    fake_anthropic.responses = [
        fake_anthropic.tool_result("files.read", {"path": "a.txt"}, tool_id="toolu_01RunToolCall"),
    ]

    r = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "projectId": "p", "message": "go", "mode": "credits"},
        headers=auth_headers(uid),
    )
    body = r.json()
    assert body["status"] == "tool_call", body

    # Scenario 1: both ids non-empty (not null).
    assert body["messageStepId"] is not None
    assert body["stepId"] is not None
    sid = body["sessionId"]
    tool_call_id = body["toolCall"]["id"]

    # Scenario 5: neither id equals the domain toolCall.id (different identifiers — ADR-008).
    assert body["stepId"] != tool_call_id
    assert body["messageStepId"] != tool_call_id

    # Scenario 2: stepId must be byte-for-byte the carrier step's id in GET /v1/chats/{id}, and
    # that carrier step (status=tool_call) is the assistant step holding the tool_use block.
    hist = (await client.get(f"/v1/chats/{sid}", headers=auth_headers(uid))).json()
    carrier = _history_step_by_id(hist, body["stepId"])
    assert carrier is not None, f"stepId {body['stepId']} not found in history steps"
    assert carrier["role"] == "assistant"
    assert _has_tool_use_block(carrier), "carrier step for tool_call must hold the tool_use block"
    # messageStepId of the response must equal the carrier step's messageStepId in history.
    assert carrier["messageStepId"] == body["messageStepId"]


# --------------------------------------------------------------------------------------------
# Scenario 1 + 2 (run → assistant_message): non-empty ids; stepId == final assistant step id;
# messageStepId == that step's messageStepId.
# --------------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_run_assistant_message_ids_nonempty_match_final_assistant_step(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [fake_anthropic.text_result("the answer")]

    r = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "projectId": "p", "message": "hi", "mode": "credits"},
        headers=auth_headers(uid),
    )
    body = r.json()
    assert body["status"] == "assistant_message", body
    assert body["messageStepId"] is not None
    assert body["stepId"] is not None
    sid = body["sessionId"]

    hist = (await client.get(f"/v1/chats/{sid}", headers=auth_headers(uid))).json()
    carrier = _history_step_by_id(hist, body["stepId"])
    assert carrier is not None, f"stepId {body['stepId']} not found in history steps"
    # assistant_message carrier = the final assistant step (the one this response represents).
    assert carrier["role"] == "assistant"
    # It is the LAST assistant step in the history (final step of the turn — no later assistant).
    assistant_steps = [st for st in hist["steps"] if st["role"] == "assistant"]
    assert assistant_steps[-1]["id"] == body["stepId"]
    assert carrier["messageStepId"] == body["messageStepId"]


# --------------------------------------------------------------------------------------------
# Scenario 1 + 2 + 3 + 6 (run → tool_call → tool-result → assistant_message):
#  - tool-result assistant_message ids non-empty + stepId matches its history step;
#  - messageStepId stable across run → tool-result of the SAME turn (scenario 3);
#  - idempotent replay returns the SAME messageStepId/stepId (scenario 6).
# --------------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_tool_result_ids_match_history_stable_messagestep_and_idempotent_replay(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [
        fake_anthropic.tool_result("files.read", {"path": "a.txt"}, tool_id="toolu_01TurnAbc"),
        fake_anthropic.text_result("final answer"),
    ]

    # run → tool_call
    r1 = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "projectId": "p", "message": "go", "mode": "credits"},
        headers=auth_headers(uid),
    )
    b1 = r1.json()
    assert b1["status"] == "tool_call", b1
    sess = b1["sessionId"]
    tcid = b1["toolCall"]["id"]
    run_message_step_id = b1["messageStepId"]

    # tool-result → assistant_message (final of the turn)
    r2 = await client.post(
        "/v1/chat/tool-result",
        json={"userId": str(uid), "sessionId": sess, "toolCallId": tcid, "result": {"ok": 1}},
        headers=auth_headers(uid),
    )
    b2 = r2.json()
    assert b2["status"] == "assistant_message", b2

    # Scenario 1: non-empty ids on the tool-result response.
    assert b2["messageStepId"] is not None
    assert b2["stepId"] is not None

    # Scenario 3: messageStepId is stable within the turn (run value == tool-result value).
    assert b2["messageStepId"] == run_message_step_id

    # Scenario 2: stepId of the tool-result response == its carrier step id in GET history (the
    # final assistant step), and messageStepId matches that step's messageStepId.
    hist = (await client.get(f"/v1/chats/{sess}", headers=auth_headers(uid))).json()
    carrier = _history_step_by_id(hist, b2["stepId"])
    assert carrier is not None, f"stepId {b2['stepId']} not found in history"
    assert carrier["role"] == "assistant"
    assistant_steps = [st for st in hist["steps"] if st["role"] == "assistant"]
    assert assistant_steps[-1]["id"] == b2["stepId"]  # the final assistant step of the turn
    assert carrier["messageStepId"] == b2["messageStepId"]

    # Scenario 6: idempotent replay of the SAME tool-result → identical sync ids.
    r3 = await client.post(
        "/v1/chat/tool-result",
        json={"userId": str(uid), "sessionId": sess, "toolCallId": tcid, "result": {"ok": 1}},
        headers=auth_headers(uid),
    )
    b3 = r3.json()
    assert b3["status"] == "assistant_message", b3
    assert b3["messageStepId"] == b2["messageStepId"]
    assert b3["stepId"] == b2["stepId"]


# --------------------------------------------------------------------------------------------
# Scenario 4: status=blocked → both ids null (no step / turn is created — block before generation).
# --------------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_blocked_response_has_null_sync_ids(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    # No subscription + trial already used + mode=credits → policy blocks (trial_used) BEFORE any
    # generation. Anthropic must not be called.
    async with db_sessionmaker() as s:
        uid = await seed_user(s, trial_used=True)

    r = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "projectId": "p", "message": "go", "mode": "credits"},
        headers=auth_headers(uid),
    )
    body = r.json()
    assert r.status_code == 200  # blocked is HTTP 200 (ADR-004)
    assert body["status"] == "blocked", body
    assert body["blockReason"] == "trial_used"
    # Scenario 4: both sync ids null (key for null is present in the JSON with value None).
    assert "messageStepId" in body and body["messageStepId"] is None
    assert "stepId" in body and body["stepId"] is None
    # Sanity: the block happened before generation — Anthropic was never called.
    assert fake_anthropic.calls == []
