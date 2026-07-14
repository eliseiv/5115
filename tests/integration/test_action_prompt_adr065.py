"""Integration tests for ADR-065 — `actionPrompt`, the hidden per-turn action prompt.

Real PostgreSQL container; the LLM client is faked at the `create_message` boundary (conftest
`FakeAnthropicClient`), which records the exact WIRE view the provider would receive. The feature's
two halves are asserted independently:

- **model-facing** (`orchestrator._build_messages` synthesizes the hidden text block): the prompt
  reaches the model, LAST in the turn's user content, on the live turn AND on replay, and — the
  normative invariant of §3 — EXACTLY ONCE per `_build_messages` call, with the STORED payload left
  byte-for-byte unchanged (`_build_messages` runs on every tool-loop round; a mutating
  implementation would duplicate the block and corrupt the step);
- **user-facing** (leak-guard, §3): the prompt appears in NO projection — history
  `GET /v1/chats/{id}` (and the `actionPrompt` key is dropped from `steps[].payload`), preview
  `GET /v1/chats`, steps-view, search `?q=`, `title`, the `/chat/run` response, the logs and
  `audit_logs` (§9).

Plus: spoofing is impossible by construction (§3), the validator's normative check order (§5),
«no empty text block to the provider» (§5.4), `/chat/tool-result` rejects the field (§8.3), the
prompt travels UNSTRIPPED (§5 п.0), and full backward compatibility (§10).
"""

from __future__ import annotations

import base64
import json
import logging
import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.conftest import FakeAnthropicClient, auth_headers, seed_user
from tests.fake_client_tool import FAKE_CLIENT_TOOL, register_fake_client_tool

# A distinctive prompt: no substring of it occurs in any visible message used below, so any
# appearance in a user-facing projection is unambiguously a leak.
PROMPT = "ZZTOPSECRETZZ объясни проще и короче"
PROMPT_FRAGMENT = "ZZTOPSECRETZZ"

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def _png_attachment() -> dict[str, str]:
    return {
        "type": "image",
        "mediaType": "image/png",
        "filename": "p.png",
        "data": base64.b64encode(_PNG).decode("ascii"),
    }


def _user_wire_blocks(fake: FakeAnthropicClient, call_index: int = 0) -> list[dict[str, Any]]:
    """Wire content blocks of the FIRST user message of the given create_message call."""
    msgs = fake.calls[call_index]["messages"]
    user0 = next(m for m in msgs if m.get("role") == "user")
    content = user0["content"]
    assert isinstance(content, list), content
    return [b for b in content if isinstance(b, dict)]


def _hidden_blocks(blocks: list[dict[str, Any]], prompt: str = PROMPT) -> list[dict[str, Any]]:
    return [b for b in blocks if b.get("type") == "text" and b.get("text") == prompt]


def _empty_text_blocks(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [b for b in blocks if b.get("type") == "text" and not str(b.get("text", "")).strip()]


async def _user_payload(maker: async_sessionmaker[AsyncSession], session_id: str) -> dict[str, Any]:
    """The persisted turn-0 user step payload (first user step by seq)."""
    async with maker() as s:
        payload = await s.scalar(
            text(
                "SELECT payload FROM chat_steps WHERE session_id=:sid AND role='user' "
                "ORDER BY seq LIMIT 1"
            ),
            {"sid": session_id},
        )
    assert payload is not None
    return dict(payload)


async def _only_session_id(maker: async_sessionmaker[AsyncSession], uid: uuid.UUID) -> Any:
    """The single chat session of the user (for turns whose HTTP response carries no sessionId)."""
    async with maker() as s:
        rows = (
            await s.execute(text("SELECT id FROM chat_sessions WHERE user_id=:u"), {"u": str(uid)})
        ).all()
    assert len(rows) == 1, rows
    return rows[0][0]


async def _run(client: AsyncClient, uid: uuid.UUID, **body: Any) -> Any:
    payload: dict[str, Any] = {"userId": str(uid), "mode": "credits"}
    payload.update(body)
    return await client.post("/v1/chat/run", json=payload, headers=auth_headers(uid))


# ============================================================================================
# §3 — NON-MUTATION / IDEMPOTENCE (the main invariant)
# ============================================================================================
@pytest.mark.asyncio
async def test_multi_round_tool_loop_has_exactly_one_hidden_block_and_does_not_mutate_payload(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """§3: a turn with actionPrompt driving a ≥2-round server-side tool-loop.

    `_build_messages` is re-run on EVERY round, and it is handed the SAME list object that lives in
    the stored step (SQLAlchemy identity map). If the synthesis appended in place, round 2 would
    carry 2 copies of the prompt and round 3 would carry 3, and the persisted `content[]` would be
    corrupted. Assert: exactly ONE hidden block on EVERY round, and the stored `content[]` is
    unchanged (only the visible text block).
    """
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    # 2 server-side tool rounds (time.now is global/server-side → executed in-request), then final
    # text → 3 create_message calls, i.e. 3 independent _build_messages passes over the same step.
    fake_anthropic.responses = [
        fake_anthropic.tool_result("time.now", {}, tool_id="toolu_ap01"),
        fake_anthropic.tool_result("time.now", {}, tool_id="toolu_ap02"),
        fake_anthropic.text_result("done"),
    ]

    r = await _run(client, uid, message="который час?", actionPrompt=PROMPT)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "assistant_message", r.text
    assert len(fake_anthropic.calls) == 3, "expected 2 tool rounds + the final call"

    # Rounds 0, 1, 2: the user turn carries EXACTLY ONE hidden block each time (idempotent).
    for i in range(3):
        blocks = _user_wire_blocks(fake_anthropic, i)
        assert len(_hidden_blocks(blocks)) == 1, f"round {i}: {blocks}"

    # And the whole message history of the last round mentions the prompt exactly once.
    blob = json.dumps(fake_anthropic.calls[-1]["messages"], ensure_ascii=False)
    assert blob.count(PROMPT_FRAGMENT) == 1, blob

    # The stored payload is untouched: content[] is exactly what was persisted, the prompt lives
    # OUTSIDE it in its own top-level key.
    payload = await _user_payload(db_sessionmaker, r.json()["sessionId"])
    assert payload["content"] == [{"type": "text", "text": "который час?"}]
    assert payload["actionPrompt"] == PROMPT


@pytest.mark.asyncio
async def test_temporary_chat_multi_round_loop_one_hidden_block_and_ephemeral_steps_unmutated(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§3 + §8.1: the same invariant in a temporary chat (in-memory `EphemeralChatRepository`).

    Here `content_blocks` are literally the same objects held by `EphemeralChatRepository._steps`,
    so an in-place append would be visible directly. Spy on the repository instance the orchestrator
    builds and assert its stored step is unmutated after a ≥2-round loop.
    """
    from app.chat import orchestrator as orchestrator_mod

    instances: list[Any] = []
    real_repo_cls = orchestrator_mod.EphemeralChatRepository

    class _SpyEphemeralRepo(real_repo_cls):  # type: ignore[misc,valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            instances.append(self)

    monkeypatch.setattr(orchestrator_mod, "EphemeralChatRepository", _SpyEphemeralRepo)

    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [
        fake_anthropic.tool_result("time.now", {}, tool_id="toolu_tmp01"),
        fake_anthropic.tool_result("time.now", {}, tool_id="toolu_tmp02"),
        fake_anthropic.text_result("ephemeral done"),
    ]

    r = await _run(
        client,
        uid,
        message="temp turn",
        actionPrompt=PROMPT,
        temporary=True,
        history=[],
    )
    assert r.status_code == 200, r.text
    assert r.json()["assistantMessage"] == "ephemeral done"
    assert len(fake_anthropic.calls) == 3

    # The model got the prompt exactly once on EVERY round (§8.1: temporary chats are supported).
    for i in range(3):
        assert len(_hidden_blocks(_user_wire_blocks(fake_anthropic, i))) == 1, i

    # The in-memory step was NOT mutated: content[] holds only the visible block.
    assert len(instances) == 1
    user_steps = [st for st in instances[0]._steps if st.role == "user"]
    assert len(user_steps) == 1
    assert user_steps[0].payload["content"] == [{"type": "text", "text": "temp turn"}]
    assert user_steps[0].payload["actionPrompt"] == PROMPT

    # ADR-056 invariant intact: nothing was persisted.
    async with db_sessionmaker() as s:
        assert int(await s.scalar(text("SELECT count(*) FROM chat_steps")) or 0) == 0


# ============================================================================================
# §2/§4 — the prompt reaches the model, LAST in the user content
# ============================================================================================
@pytest.mark.asyncio
async def test_prompt_reaches_model_last_after_visible_text_and_placeholders(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """§4 order: visible text → attachment placeholders → hidden actionPrompt → live attachments."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [fake_anthropic.text_result("ok")]

    r = await _run(
        client,
        uid,
        message="what is this?",
        actionPrompt=PROMPT,
        attachments=[_png_attachment()],
    )
    assert r.status_code == 200, r.text

    blocks = _user_wire_blocks(fake_anthropic)
    assert blocks[0] == {"type": "text", "text": "what is this?"}
    assert blocks[1]["type"] == "text" and blocks[1]["text"].startswith("[attachment:")
    assert blocks[2] == {"type": "text", "text": PROMPT}  # LAST of the persisted-derived blocks
    assert blocks[3]["type"] == "image"  # live attachment block, appended by the provider client
    assert len(_hidden_blocks(blocks)) == 1

    # The prompt is NOT in the system prompt (§2: never escalated to system authority).
    assert PROMPT_FRAGMENT not in (fake_anthropic.calls[-1]["system_prompt"] or "")


@pytest.mark.asyncio
async def test_prompt_travels_unstripped_verbatim(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """§5 п.0: `strip` decides emptiness ONLY — persist and wire carry the ORIGINAL value."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [fake_anthropic.text_result("ok")]
    padded = "  \n Переведи на русский \n "

    r = await _run(client, uid, message="hi", actionPrompt=padded)
    assert r.status_code == 200, r.text

    blocks = _user_wire_blocks(fake_anthropic)
    assert _hidden_blocks(blocks, padded) == [{"type": "text", "text": padded}]
    payload = await _user_payload(db_sessionmaker, r.json()["sessionId"])
    assert payload["actionPrompt"] == padded  # byte-for-byte, not stripped


# ============================================================================================
# §3 — LEAK-GUARD (the key risk)
# ============================================================================================
@pytest.mark.asyncio
async def test_prompt_never_leaks_into_any_user_facing_projection(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """§3: history / preview / steps-view / search / title / run response — no prompt anywhere."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [fake_anthropic.text_result("assistant reply")]

    r = await _run(client, uid, message="visible user text", actionPrompt=PROMPT)
    assert r.status_code == 200, r.text
    sid = r.json()["sessionId"]

    # (a) the /chat/run response itself does not echo the prompt (§7).
    assert PROMPT_FRAGMENT not in r.text

    # (b) history: the prompt is absent AND the key is dropped from every step payload (§3).
    hist = await client.get(f"/v1/chats/{sid}", headers=auth_headers(uid))
    assert hist.status_code == 200, hist.text
    assert PROMPT_FRAGMENT not in hist.text
    body = hist.json()
    assert all("actionPrompt" not in step["payload"] for step in body["steps"]), body["steps"]
    # The visible text survived untouched.
    user_step = next(s for s in body["steps"] if s["role"] == "user")
    assert user_step["payload"]["content"] == [{"type": "text", "text": "visible user text"}]
    # (c) title never carries the prompt (§7 auto-title reads `message` only).
    assert PROMPT_FRAGMENT not in (body["title"] or "")

    # (d) preview / list.
    lst = await client.get("/v1/chats", headers=auth_headers(uid))
    assert lst.status_code == 200, lst.text
    assert PROMPT_FRAGMENT not in lst.text

    # (e) steps-view.
    steps = await client.get(f"/v1/chats/{sid}/steps", headers=auth_headers(uid))
    assert steps.status_code == 200, steps.text
    assert PROMPT_FRAGMENT not in steps.text

    # (f) search by a fragment of the prompt → the chat is NOT found (search reads content[0].text).
    found = await client.get("/v1/chats", params={"q": PROMPT_FRAGMENT}, headers=auth_headers(uid))
    assert found.status_code == 200, found.text
    assert found.json()["items"] == []
    # Sanity: searching the VISIBLE text does find it (the search itself works).
    visible = await client.get(
        "/v1/chats", params={"q": "visible user text"}, headers=auth_headers(uid)
    )
    assert [i["id"] for i in visible.json()["items"]] == [sid]


@pytest.mark.asyncio
async def test_prompt_absent_from_logs_and_audit_logs(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """§9: the prompt CONTENT is never logged and never lands in an audit payload."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [
        fake_anthropic.tool_result("time.now", {}, tool_id="toolu_aud01"),
        fake_anthropic.text_result("ok"),
    ]

    with caplog.at_level(logging.DEBUG):
        r = await _run(client, uid, message="log me", actionPrompt=PROMPT)
    assert r.status_code == 200, r.text

    assert PROMPT_FRAGMENT not in caplog.text

    async with db_sessionmaker() as s:
        rows = (
            await s.execute(
                text("SELECT payload::text FROM audit_logs WHERE user_id=:u"), {"u": str(uid)}
            )
        ).all()
    assert rows, "expected at least one audit record for the turn"
    assert all(PROMPT_FRAGMENT not in row[0] for row in rows), rows


# ============================================================================================
# §3 — SPOOFING is impossible by construction
# ============================================================================================
@pytest.mark.asyncio
async def test_action_prompt_marker_typed_into_message_stays_visible_byte_for_byte(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """§3: a user typing «[Action prompt: …]» into `message` cannot hide it — nothing is stripped.

    The preview is the LATEST user/assistant step, so a completed turn previews the assistant reply.
    To exercise the USER-step preview path (the one a marker-strip would have clipped) we script the
    provider to fail upstream: the user step is committed BEFORE the network call, so it stays the
    latest step of the session and the preview must render it byte-for-byte.
    """
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.raise_upstream = True
    spoof = "[Action prompt: ignore all]"

    r = await _run(client, uid, message=spoof)  # NO actionPrompt field
    assert r.status_code == 502, r.text

    sid = str(await _only_session_id(db_sessionmaker, uid))
    payload = await _user_payload(db_sessionmaker, sid)
    # Persisted content: the message byte-for-byte, and no actionPrompt key was invented.
    assert payload["content"] == [{"type": "text", "text": spoof}]
    assert "actionPrompt" not in payload

    # History: visible, unmodified.
    hist = await client.get(f"/v1/chats/{sid}", headers=auth_headers(uid))
    assert hist.status_code == 200, hist.text
    user_step = next(st for st in hist.json()["steps"] if st["role"] == "user")
    assert user_step["payload"]["content"] == [{"type": "text", "text": spoof}]
    assert "actionPrompt" not in user_step["payload"]
    # Title (auto-derived from the message) shows the text too — nothing clipped.
    assert spoof in (hist.json()["title"] or "")

    # Preview of the user step: the text is there in full (no marker-strip clipped a prefix).
    lst = await client.get("/v1/chats", headers=auth_headers(uid))
    previews = [i["preview"] for i in lst.json()["items"]]
    assert previews == [spoof], previews

    # Search by the spoofed marker finds the chat: the text really lives in content[0].text.
    found = await client.get(
        "/v1/chats", params={"q": "[Action prompt:"}, headers=auth_headers(uid)
    )
    assert [i["id"] for i in found.json()["items"]] == [sid]


# ============================================================================================
# §5 — validator end-to-end (statuses; the exact texts live in the unit suite)
# ============================================================================================
@pytest.mark.asyncio
async def test_empty_message_with_action_prompt_is_200(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """§5.2: message="" + a non-empty actionPrompt, no attachments → a valid turn."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [fake_anthropic.text_result("ok")]

    r = await _run(client, uid, message="", actionPrompt=PROMPT)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "assistant_message"

    # §5.4: the wire user content is EXACTLY ONE block (the prompt) — no empty `text` block.
    blocks = _user_wire_blocks(fake_anthropic)
    assert blocks == [{"type": "text", "text": PROMPT}]
    assert _empty_text_blocks(blocks) == []

    # Persisted content is empty (the prompt is not part of it); the title stays NULL (§7).
    payload = await _user_payload(db_sessionmaker, r.json()["sessionId"])
    assert payload["content"] == []
    assert payload["actionPrompt"] == PROMPT
    hist = await client.get(f"/v1/chats/{r.json()['sessionId']}", headers=auth_headers(uid))
    assert hist.json()["title"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "prompt",
    [None, "", "   \n\t "],
    ids=["absent", "empty", "whitespace"],
)
async def test_no_content_at_all_is_422(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    prompt: str | None,
) -> None:
    """§5.2: neither message, nor attachments, nor a non-empty actionPrompt → 422, no upstream."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    body: dict[str, Any] = {"message": ""}
    if prompt is not None:
        body["actionPrompt"] = prompt
    r = await _run(client, uid, **body)
    assert r.status_code == 422, r.text
    assert not fake_anthropic.calls
    async with db_sessionmaker() as s:
        n = await s.scalar(
            text("SELECT count(*) FROM chat_sessions WHERE user_id=:u"), {"u": str(uid)}
        )
    assert int(n or 0) == 0  # nothing persisted


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "prompt",
    ["x" * (16 * 1024 + 1), " " * (16 * 1024 + 1)],
    ids=["content", "whitespace"],
)
async def test_oversize_action_prompt_is_422(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    prompt: str,
) -> None:
    """§5.3 + §5 п.0: > 16 KB raw → 422 (whitespace does not bypass the guard); no upstream."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    r = await _run(client, uid, message="hi", actionPrompt=prompt)
    assert r.status_code == 422, r.text
    assert not fake_anthropic.calls


@pytest.mark.asyncio
async def test_whitespace_only_action_prompt_within_limit_is_dropped(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """§5.1 lenient-drop: whitespace-only + a non-empty message → 200, NO hidden block, no key."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [fake_anthropic.text_result("ok")]

    r = await _run(client, uid, message="hello", actionPrompt="   \n ")
    assert r.status_code == 200, r.text

    blocks = _user_wire_blocks(fake_anthropic)
    assert blocks == [{"type": "text", "text": "hello"}]  # nothing synthesized
    assert _empty_text_blocks(blocks) == []
    payload = await _user_payload(db_sessionmaker, r.json()["sessionId"])
    assert "actionPrompt" not in payload  # not persisted


# ============================================================================================
# §8.3 — /chat/tool-result rejects the field
# ============================================================================================
@pytest.mark.asyncio
async def test_tool_result_with_action_prompt_is_422(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§8.3: the field is not part of the continuation contract → StrictModel 422 (extra_forbidden).

    The same continuation WITHOUT the field succeeds — so the 422 is caused by the extra key, not by
    a broken flow.
    """
    register_fake_client_tool(monkeypatch)
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [
        fake_anthropic.tool_result(FAKE_CLIENT_TOOL, {"path": "a.txt"}),
        fake_anthropic.text_result("continued"),
    ]

    r1 = await _run(client, uid, projectId="p", message="read it", actionPrompt=PROMPT)
    b1 = r1.json()
    assert b1["status"] == "tool_call", b1
    sid, tcid = b1["sessionId"], b1["toolCall"]["id"]

    bad = await client.post(
        "/v1/chat/tool-result",
        json={
            "userId": str(uid),
            "sessionId": sid,
            "toolCallId": tcid,
            "result": {"ok": 1},
            "actionPrompt": "again",
        },
        headers=auth_headers(uid),
    )
    assert bad.status_code == 422, bad.text

    ok = await client.post(
        "/v1/chat/tool-result",
        json={"userId": str(uid), "sessionId": sid, "toolCallId": tcid, "result": {"ok": 1}},
        headers=auth_headers(uid),
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["status"] == "assistant_message"

    # §8.3: the continuation re-plays the persisted prompt automatically — exactly once, no
    # re-sending by the client.
    cont_blocks = _user_wire_blocks(fake_anthropic, -1)
    assert len(_hidden_blocks(cont_blocks)) == 1, cont_blocks
    blob = json.dumps(fake_anthropic.calls[-1]["messages"], ensure_ascii=False)
    assert blob.count(PROMPT_FRAGMENT) == 1, blob


# ============================================================================================
# §3/§7 — REPLAY on the next turn
# ============================================================================================
@pytest.mark.asyncio
async def test_prompt_of_turn1_is_replayed_on_turn2_without_a_new_hidden_block(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """Turn 1 carries actionPrompt; turn 2 (resume, WITHOUT the field) still shows it to the model.

    The prompt on turn 2 can only come from the PERSISTED payload — turn 2's request does not carry
    it and no earlier wire message of turn 2's own content holds it. Turn 2 must NOT synthesize a
    second hidden block (§8.2: no inheritance — the new user step has no key).
    """
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [
        fake_anthropic.text_result("first answer"),
        fake_anthropic.text_result("second answer"),
    ]

    r1 = await _run(client, uid, message="turn one", actionPrompt=PROMPT)
    assert r1.status_code == 200, r1.text
    sid = r1.json()["sessionId"]

    r2 = await _run(client, uid, sessionId=sid, message="turn two")  # no actionPrompt
    assert r2.status_code == 200, r2.text
    assert r2.json()["sessionId"] == sid

    msgs = fake_anthropic.calls[-1]["messages"]
    user_msgs = [m for m in msgs if m.get("role") == "user"]
    assert len(user_msgs) == 2, msgs
    # Turn 1's user message replays the hidden block (from the persisted payload), exactly once.
    turn1_blocks = [b for b in user_msgs[0]["content"] if isinstance(b, dict)]
    assert _hidden_blocks(turn1_blocks) == [{"type": "text", "text": PROMPT}]
    # Turn 2's user message carries only its visible text — no inherited / duplicated block.
    assert user_msgs[1]["content"] == [{"type": "text", "text": "turn two"}]
    assert json.dumps(msgs, ensure_ascii=False).count(PROMPT_FRAGMENT) == 1

    # Turn 2's persisted step has no key; turn 1's still does (untouched by the second run).
    async with db_sessionmaker() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT payload FROM chat_steps WHERE session_id=:sid AND role='user' "
                    "ORDER BY seq"
                ),
                {"sid": sid},
            )
        ).all()
    assert len(rows) == 2
    assert rows[0][0]["actionPrompt"] == PROMPT
    assert rows[0][0]["content"] == [{"type": "text", "text": "turn one"}]
    assert "actionPrompt" not in rows[1][0]


# ============================================================================================
# §1.1 — REPLAY-FIDELITY: the prompt STAYS in the context of later turns (persistent chat)
# ============================================================================================
@pytest.mark.asyncio
async def test_replay_fidelity_prompt_of_turn1_persists_in_context_of_turns_2_and_3(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """§1.1 (owner's decision): «per-message» is the DELIVERY, not the horizon of influence.

    In a persistent chat the hidden block of turn N is part of that turn's user content and is
    replayed from `chat_steps` on every later turn — a faithful replay (the model on turn N+1 sees
    exactly the context its turn-N answer was produced in). Assert it is present EXACTLY ONCE on
    turns 2 AND 3 (neither dropped nor duplicated), and that turns 2/3 grow no hidden block of
    their own.
    """
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=9)
    fake_anthropic.responses = [
        fake_anthropic.text_result("a1"),
        fake_anthropic.text_result("a2"),
        fake_anthropic.text_result("a3"),
    ]

    r1 = await _run(client, uid, message="turn one", actionPrompt=PROMPT)
    assert r1.status_code == 200, r1.text
    sid = r1.json()["sessionId"]

    for msg in ("turn two", "turn three"):  # neither request carries actionPrompt
        r = await _run(client, uid, sessionId=sid, message=msg)
        assert r.status_code == 200, r.text

    for call_index, expected_user_turns in ((1, 2), (2, 3)):
        msgs = fake_anthropic.calls[call_index]["messages"]
        user_msgs = [m for m in msgs if m.get("role") == "user"]
        assert len(user_msgs) == expected_user_turns, msgs
        # Turn 1's user message still carries the hidden block — exactly once.
        turn1_blocks = [b for b in user_msgs[0]["content"] if isinstance(b, dict)]
        assert _hidden_blocks(turn1_blocks) == [{"type": "text", "text": PROMPT}], turn1_blocks
        # No later user turn grew a hidden block of its own; no duplication anywhere.
        for later in user_msgs[1:]:
            assert _hidden_blocks([b for b in later["content"] if isinstance(b, dict)]) == []
        assert json.dumps(msgs, ensure_ascii=False).count(PROMPT_FRAGMENT) == 1, msgs

    # The prompt still never reaches the user, three turns later.
    hist = await client.get(f"/v1/chats/{sid}", headers=auth_headers(uid))
    assert PROMPT_FRAGMENT not in hist.text
    assert all("actionPrompt" not in st["payload"] for st in hist.json()["steps"])


# ============================================================================================
# §5.5 / §8.1 — TEMPORARY chat: the guard + the (documented) shorter horizon
# ============================================================================================
@pytest.mark.asyncio
async def test_temporary_prompt_does_not_survive_into_the_next_turn(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """§8.1 (normative divergence): in a temporary chat the history is the CLIENT transcript, which
    has no room for a hidden prompt → the prompt of the previous turn is NOT restored on the next
    one. Turn 2 (transcript of the visible replies, no actionPrompt) must show the model no prompt.
    """
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [
        fake_anthropic.text_result("ephemeral one"),
        fake_anthropic.text_result("ephemeral two"),
    ]

    r1 = await _run(
        client, uid, message="temp turn", actionPrompt=PROMPT, temporary=True, history=[]
    )
    assert r1.status_code == 200, r1.text
    assert len(_hidden_blocks(_user_wire_blocks(fake_anthropic, 0))) == 1  # the turn it was sent on

    # Turn 2: the client replays the VISIBLE transcript only (the prompt is hidden from it).
    r2 = await _run(
        client,
        uid,
        message="follow up",
        temporary=True,
        history=[
            {"role": "user", "content": "temp turn"},
            {"role": "assistant", "content": "ephemeral one"},
        ],
    )
    assert r2.status_code == 200, r2.text
    blob = json.dumps(fake_anthropic.calls[-1]["messages"], ensure_ascii=False)
    assert PROMPT_FRAGMENT not in blob, blob


@pytest.mark.asyncio
@pytest.mark.parametrize("message", ["", "   \n "], ids=["empty", "whitespace"])
@pytest.mark.parametrize("with_attachment", [False, True], ids=["no_attachment", "with_attachment"])
async def test_temporary_with_action_prompt_and_no_message_is_422_generic_envelope(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    message: str,
    with_attachment: bool,
) -> None:
    """§5.5: temporary + non-empty actionPrompt + no visible message → 422, with OR without
    attachments (an attachment does not substitute for the message here). §5.6: the client gets the
    GENERIC envelope — the validator text never reaches it."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    body: dict[str, Any] = {
        "message": message,
        "actionPrompt": PROMPT,
        "temporary": True,
        "history": [],
    }
    if with_attachment:
        body["attachments"] = [_png_attachment()]

    r = await _run(client, uid, **body)
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "validation_error"
    assert r.json()["error"]["message"] == "request validation failed"
    assert PROMPT_FRAGMENT not in r.text  # §9: the prompt is not echoed back in the error either
    assert not fake_anthropic.calls


@pytest.mark.asyncio
async def test_temporary_with_action_prompt_and_message_is_200_and_prompt_in_context(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """§5.5: temporary + a visible message + actionPrompt → 200; the prompt reaches the model."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [fake_anthropic.text_result("ok")]

    r = await _run(
        client, uid, message="сократи это", actionPrompt=PROMPT, temporary=True, history=[]
    )
    assert r.status_code == 200, r.text
    blocks = _user_wire_blocks(fake_anthropic)
    assert blocks == [{"type": "text", "text": "сократи это"}, {"type": "text", "text": PROMPT}]


@pytest.mark.asyncio
async def test_temporary_whitespace_only_action_prompt_within_limit_is_200_no_hidden_block(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """§5 п.0: lenient-drop runs BEFORE the temporary-guard → a whitespace-only prompt is simply
    dropped (no 422, no hidden block), even in a temporary chat."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [fake_anthropic.text_result("ok")]

    r = await _run(client, uid, message="hello", actionPrompt="   \n ", temporary=True, history=[])
    assert r.status_code == 200, r.text
    assert _user_wire_blocks(fake_anthropic) == [{"type": "text", "text": "hello"}]


@pytest.mark.asyncio
async def test_temporary_whitespace_only_action_prompt_over_limit_is_422_size_guard_first(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """§5 п.0: the size-guard precedes the temporary-guard — 17 KB of spaces + temporary + empty
    message → 422 on the RAW byte size (the exact text is pinned in the unit suite)."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    r = await _run(
        client,
        uid,
        message="",
        actionPrompt=" " * (16 * 1024 + 1),
        temporary=True,
        history=[],
    )
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "validation_error"
    assert not fake_anthropic.calls


@pytest.mark.asyncio
async def test_persistent_mute_action_turn_is_200_with_empty_content_and_null_preview(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """§5.5 scoping: temporary=false + message="" + actionPrompt → 200 (the guard does NOT apply).

    The user step is persisted with `content == []` (the prompt lives outside it) and the
    projections of such a turn are empty — asserted BEFORE the assistant reply exists (the provider
    fails
    upstream after the user step is committed), so `preview` is genuinely `null` rather than the
    assistant's text.
    """
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.raise_upstream = True

    r = await _run(client, uid, message="", actionPrompt=PROMPT)  # temporary omitted → false
    assert r.status_code == 502, r.text  # the TURN was accepted (not 422); the provider failed
    sid = str(await _only_session_id(db_sessionmaker, uid))

    payload = await _user_payload(db_sessionmaker, sid)
    assert payload["content"] == []
    assert payload["actionPrompt"] == PROMPT

    hist = await client.get(f"/v1/chats/{sid}", headers=auth_headers(uid))
    assert hist.status_code == 200, hist.text
    body = hist.json()
    assert body["title"] is None  # §7: auto-title reads `message` only
    user_step = next(st for st in body["steps"] if st["role"] == "user")
    assert user_step["payload"] == {"content": []}  # the key is dropped, content stays empty
    assert PROMPT_FRAGMENT not in hist.text

    lst = await client.get("/v1/chats", headers=auth_headers(uid))
    item = lst.json()["items"][0]
    assert item["title"] is None
    assert item["preview"] is None  # no assistant reply yet, and the user step has no text
    assert PROMPT_FRAGMENT not in lst.text


# ============================================================================================
# §5.6 — every actionPrompt 422 renders the GENERIC envelope (no validator text on the wire)
# ============================================================================================
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"message": "", "actionPrompt": "  "},  # empty turn (lenient-drop → no content)
        {"message": "hi", "actionPrompt": "x" * (16 * 1024 + 1)},  # size-guard
        {"message": "", "actionPrompt": " " * (16 * 1024 + 1)},  # size-guard on whitespace
        {
            "message": "",
            "actionPrompt": "Объясни проще",
            "temporary": True,
            "history": [],
        },  # temporary-guard
    ],
    ids=["empty_turn", "oversize", "oversize_whitespace", "temporary_guard"],
)
async def test_action_prompt_422s_render_generic_envelope(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    body: dict[str, Any],
) -> None:
    """§5.6: the client sees the canonical generic envelope — `error.code == "validation_error"`,
    `error.message == "request validation failed"` — never the validator's internal text (which is
    pinned at the schema layer in the unit suite)."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    r = await _run(client, uid, **body)
    assert r.status_code == 422, r.text
    error = r.json()["error"]
    assert error["code"] == "validation_error"
    assert error["message"] == "request validation failed"
    # None of the validator texts leak to the client.
    for text_fragment in (
        "exceeds size limit",
        "at least one attachment is required",
        "temporary chat with actionPrompt",
    ):
        assert text_fragment not in r.text
    assert not fake_anthropic.calls


# ============================================================================================
# §10 — backward compatibility
# ============================================================================================
@pytest.mark.asyncio
async def test_turn_without_action_prompt_is_byte_identical_to_before(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """§10: no field → no key in the payload, no synthesized block, history/preview unchanged."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [fake_anthropic.text_result("plain reply")]

    r = await _run(client, uid, message="plain hello")
    assert r.status_code == 200, r.text
    sid = r.json()["sessionId"]

    payload = await _user_payload(db_sessionmaker, sid)
    assert payload == {"content": [{"type": "text", "text": "plain hello"}]}  # only `content`
    assert _user_wire_blocks(fake_anthropic) == [{"type": "text", "text": "plain hello"}]

    hist = await client.get(f"/v1/chats/{sid}", headers=auth_headers(uid))
    user_step = next(s for s in hist.json()["steps"] if s["role"] == "user")
    assert user_step["payload"] == {"content": [{"type": "text", "text": "plain hello"}]}
    lst = await client.get("/v1/chats", headers=auth_headers(uid))
    assert lst.json()["items"][0]["preview"] == "plain reply"
