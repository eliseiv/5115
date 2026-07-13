"""Integration: multi-provider BYOK generation + stale-model fallback (ADR-044 §5 / §Связанное).

Through the real /chat/run app with the real PostgreSQL container. BOTH LLM clients are faked at
the singleton boundary (no real network; tests pass with placeholder API keys):
- the Anthropic singleton is the conftest ``fake_anthropic`` (already patched by the ``client``
  fixture),
- the OpenAI singleton is faked here (``_fake_openai``) and records the ``model`` handed to it.

The instance is forced to ``LLM_PROVIDER=openai`` by mutating the cached Settings singleton
(restored per test), mirroring the ADR-034 session-model tests. Scenarios:
- §5: BYOK with an ``sk-ant-`` key on an OpenAI instance → generation routes to the ANTHROPIC
  client with the user's key; a foreign session model (``gpt-*``) is NOT forwarded → the anthropic
  BYOK default is used.
- §Связанное: a credits chat whose session model is ``claude-*`` after the instance switched to
  ``LLM_PROVIDER=openai`` → resume passes ``model=None`` (no 502); the stored model is NOT
  rewritten; the same fallback applies to the tool_result continuation.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.chat.llm_client as llm_mod
from app.config import get_settings
from tests.conftest import FakeAnthropicClient, auth_headers, seed_user
from tests.fake_client_tool import FAKE_CLIENT_TOOL, register_fake_client_tool


@pytest.fixture(autouse=True)
def _register_fake_client_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    # ADR-063: register a test-only client-side example tool (no shipped client-side tool remains).
    register_fake_client_tool(monkeypatch)


class FakeOpenAIClient:
    """In-memory OpenAI LLMClient double recording the model handed to create_message.

    Mirrors FakeAnthropicClient's recording seam but in OpenAI wire shape — enough for the
    orchestrator's generation loop (text-only turns). No network; validate_key honors valid_keys.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.responses: list[Any] = []
        self.valid_keys: set[str] = set()

    def text_result(self, text_value: str = "openai answer") -> Any:
        from app.chat.llm_client import LLMResult, LLMUsage

        usage = LLMUsage(
            input_tokens=10,
            output_tokens=5,
            model="gpt-4o",
            cache_read_tokens=0,
            cache_write_tokens=0,
        )
        return LLMResult(
            stop_reason="end_turn",
            content_blocks=[{"role": "assistant", "content": text_value}],
            usage=usage,
            text=text_value,
            tool_uses=[],
        )

    def tool_result(self, tool_name: str, args: dict[str, Any], tool_id: str | None = None) -> Any:
        """A turn with one OpenAI-shape tool_call (call_... id), domain tool_uses (ADR-033 §4)."""
        from app.chat.llm_client import LLMResult, LLMUsage

        tid = tool_id or f"call_{uuid.uuid4().hex[:24]}"
        usage = LLMUsage(
            input_tokens=10,
            output_tokens=5,
            model="gpt-4o",
            cache_read_tokens=0,
            cache_write_tokens=0,
        )
        assistant_msg = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": tid,
                    "type": "function",
                    "function": {
                        "name": tool_name.replace(".", "_"),
                        "arguments": json.dumps(args),
                    },
                }
            ],
        }
        return LLMResult(
            stop_reason="tool_use",
            content_blocks=[assistant_msg],
            usage=usage,
            text="",
            tool_uses=[{"id": tid, "name": tool_name, "input": args}],
        )

    async def create_message(self, **kwargs: Any) -> Any:
        self.calls.append({"model": kwargs.get("model"), "api_key": kwargs.get("api_key")})
        if not self.responses:
            return self.text_result()
        return self.responses.pop(0)

    async def validate_key(self, api_key: str) -> Any:
        from app.chat.llm_client import KeyValidation

        return KeyValidation.valid if api_key in self.valid_keys else KeyValidation.invalid


@pytest.fixture
def fake_openai() -> FakeOpenAIClient:
    return FakeOpenAIClient()


@pytest.fixture
def openai_instance(
    fake_openai: FakeOpenAIClient, monkeypatch: pytest.MonkeyPatch
) -> Iterator[FakeOpenAIClient]:
    """Force LLM_PROVIDER=openai + patch the OpenAI singleton with the recording fake.

    Mutates the cached Settings singleton in place (restored after) and sets per-provider
    allowlists so both providers resolve a deterministic default model. The OpenAI singleton is
    patched so ``get_llm_client()`` (active credits client) and ``llm_client_for('openai')`` both
    return the fake. The Anthropic singleton stays the conftest ``fake_anthropic`` (client fixture).
    """
    s = get_settings()
    orig = (
        s.llm_provider,
        s.anthropic_models_raw,
        s.openai_models_raw,
        s.anthropic_model,
        s.openai_model,
        s.byok_default_model,
        s.openai_byok_default_model,
    )
    s.llm_provider = "openai"
    s.anthropic_models_raw = json.dumps({"claude-sonnet-4-6": "Sonnet"})
    s.openai_models_raw = json.dumps({"gpt-4o": "GPT-4o"})
    s.anthropic_model = "claude-sonnet-4-5"
    s.openai_model = "gpt-4o"
    s.byok_default_model = "claude-sonnet-4-6"
    s.openai_byok_default_model = "gpt-4o"
    monkeypatch.setattr(llm_mod, "_openai_singleton", fake_openai)
    yield fake_openai
    (
        s.llm_provider,
        s.anthropic_models_raw,
        s.openai_models_raw,
        s.anthropic_model,
        s.openai_model,
        s.byok_default_model,
        s.openai_byok_default_model,
    ) = orig


async def _session_model(maker: async_sessionmaker[AsyncSession], session_id: str) -> str | None:
    async with maker() as s:
        return await s.scalar(
            text("SELECT model FROM chat_sessions WHERE id=:sid"), {"sid": session_id}
        )


async def _set_byok_provider(
    maker: async_sessionmaker[AsyncSession], uid: uuid.UUID, provider: str | None
) -> None:
    async with maker() as s:
        await s.execute(
            text("UPDATE byok_keys SET provider=:p WHERE user_id=:u"),
            {"p": provider, "u": str(uid)},
        )
        await s.commit()


# ---------------------------------------------------------------------------------------------
# §5 — BYOK sk-ant- key on an OpenAI instance → generation via the ANTHROPIC client + user's key.
# ---------------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_byok_ant_key_on_openai_instance_routes_to_anthropic(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    openai_instance: FakeOpenAIClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(
            s, subscription="active", balance=0, byok_enabled=True, byok_status="valid"
        )
    # The seeded key is sk-ant-user-key; mark its stored provider=anthropic (post-0013 row).
    await _set_byok_provider(db_sessionmaker, uid, "anthropic")

    fake_anthropic.responses = [fake_anthropic.text_result("byok answer")]
    r = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "projectId": "p", "message": "hi", "mode": "byok"},
        headers=auth_headers(uid),
    )
    assert r.status_code == 200
    assert r.json()["status"] == "assistant_message"

    # Generation went to the ANTHROPIC client with the USER's key — NOT the active OpenAI client.
    assert fake_anthropic.calls[-1]["api_key"] == "sk-ant-user-key"
    assert openai_instance.calls == []  # the openai (active) client was never called for byok
    # No foreign session model: the anthropic BYOK default is used (model=None → client default,
    # or the byok default). The orchestrator forwards None when no session model is set.
    assert fake_anthropic.calls[-1]["model"] in (None, "claude-sonnet-4-6")


@pytest.mark.asyncio
async def test_byok_legacy_null_provider_fallback_detect_on_openai_instance(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    openai_instance: FakeOpenAIClient,
) -> None:
    """Legacy NULL-provider byok row on an OpenAI instance → fallback-detect from the plaintext
    (sk-ant-…) → routes to anthropic, not 502 (ADR-044 §4/§5/TD-029)."""
    async with db_sessionmaker() as s:
        uid = await seed_user(
            s, subscription="active", balance=0, byok_enabled=True, byok_status="valid"
        )
    # seed_user leaves provider NULL — the legacy row shape.
    assert await _session_model(db_sessionmaker, str(uid)) is None  # sanity: no session yet

    fake_anthropic.responses = [fake_anthropic.text_result("legacy byok")]
    r = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "projectId": "p", "message": "hi", "mode": "byok"},
        headers=auth_headers(uid),
    )
    assert r.status_code == 200
    assert r.json()["status"] == "assistant_message"
    assert fake_anthropic.calls[-1]["api_key"] == "sk-ant-user-key"
    assert openai_instance.calls == []


@pytest.mark.asyncio
async def test_byok_foreign_session_model_not_forwarded(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    openai_instance: FakeOpenAIClient,
) -> None:
    """A session model of the OTHER provider (gpt-*) is never forwarded to the anthropic byok
    client → the anthropic BYOK default is used instead (ADR-044 §5.3)."""
    async with db_sessionmaker() as s:
        uid = await seed_user(
            s, subscription="active", balance=0, byok_enabled=True, byok_status="valid"
        )
        # Create a byok session pinned to gpt-4o (the active OpenAI provider's model).
        sid = uuid.uuid4()
        await s.execute(
            text(
                "INSERT INTO chat_sessions (id, user_id, project_id, mode, model) "
                "VALUES (:id, :uid, 'p', 'byok', 'gpt-4o')"
            ),
            {"id": str(sid), "uid": str(uid)},
        )
        await s.commit()
    await _set_byok_provider(db_sessionmaker, uid, "anthropic")

    fake_anthropic.responses = [fake_anthropic.text_result("byok answer")]
    r = await client.post(
        "/v1/chat/run",
        json={
            "userId": str(uid),
            "sessionId": str(sid),
            "projectId": "p",
            "message": "resume",
            "mode": "byok",
        },
        headers=auth_headers(uid),
    )
    assert r.status_code == 200
    # gpt-4o is NOT in the anthropic allowlist → not forwarded → anthropic BYOK default model.
    assert fake_anthropic.calls[-1]["model"] == "claude-sonnet-4-6"
    # The stored session model is NOT rewritten (expand-only).
    assert await _session_model(db_sessionmaker, str(sid)) == "gpt-4o"


# ---------------------------------------------------------------------------------------------
# §Связанное — stale-model credits fallback after an LLM_PROVIDER switch (no 502).
# ---------------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_credits_stale_model_resume_falls_back_to_none(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    openai_instance: FakeOpenAIClient,
) -> None:
    """A credits chat fixed to claude-* on a now-OpenAI instance → resume passes model=None to the
    active OpenAI client (fallback), not the foreign claude model → no 502 (ADR-044 §Связанное)."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=100)
        sid = uuid.uuid4()
        # Session was fixed to a claude-* model (created when the instance was anthropic).
        await s.execute(
            text(
                "INSERT INTO chat_sessions (id, user_id, project_id, mode, model) "
                "VALUES (:id, :uid, 'p', 'credits', 'claude-sonnet-4-6')"
            ),
            {"id": str(sid), "uid": str(uid)},
        )
        await s.commit()

    openai_instance.responses = [openai_instance.text_result("resumed")]
    r = await client.post(
        "/v1/chat/run",
        json={
            "userId": str(uid),
            "sessionId": str(sid),
            "projectId": "p",
            "message": "resume",
            "mode": "credits",
        },
        headers=auth_headers(uid),
    )
    assert r.status_code == 200
    assert r.json()["status"] == "assistant_message"
    # The active OpenAI client was called with model=None (stale claude-* → fallback), NOT 502.
    assert openai_instance.calls[-1]["model"] is None
    # The stored model is NOT rewritten.
    assert await _session_model(db_sessionmaker, str(sid)) == "claude-sonnet-4-6"


@pytest.mark.asyncio
async def test_credits_stale_model_tool_result_continuation_falls_back(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    openai_instance: FakeOpenAIClient,
) -> None:
    """The stale-model fallback ALSO applies to the tool_result continuation, not just resume
    (ADR-044 §Связанное: both `run` resume and `tool_result` continuation)."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=100)
        sid = uuid.uuid4()
        await s.execute(
            text(
                "INSERT INTO chat_sessions (id, user_id, project_id, mode, model) "
                "VALUES (:id, :uid, 'p', 'credits', 'claude-sonnet-4-6')"
            ),
            {"id": str(sid), "uid": str(uid)},
        )
        await s.commit()

    # First turn (resume of the claude-pinned session): a client-side tool call, then continuation.
    openai_instance.responses = [
        openai_instance.tool_result(FAKE_CLIENT_TOOL, {"path": "a.txt"}),
        openai_instance.text_result("continued"),
    ]
    r1 = await client.post(
        "/v1/chat/run",
        json={
            "userId": str(uid),
            "sessionId": str(sid),
            "projectId": "p",
            "message": "go",
            "mode": "credits",
        },
        headers=auth_headers(uid),
    )
    assert r1.status_code == 200
    body1 = r1.json()
    assert body1["status"] == "tool_call"
    # Resume run already used the fallback (model=None) for the stale claude model.
    assert openai_instance.calls[-1]["model"] is None
    tcid = body1["toolCall"]["id"]

    r2 = await client.post(
        "/v1/chat/tool-result",
        json={"userId": str(uid), "sessionId": str(sid), "toolCallId": tcid, "result": {"ok": 1}},
        headers=auth_headers(uid),
    )
    assert r2.status_code == 200
    assert r2.json()["status"] == "assistant_message"
    # The continuation call ALSO used model=None (fallback), not the foreign claude model.
    assert openai_instance.calls[-1]["model"] is None
    assert await _session_model(db_sessionmaker, str(sid)) == "claude-sonnet-4-6"  # not rewritten


@pytest.mark.asyncio
async def test_credits_active_model_still_forwarded(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    openai_instance: FakeOpenAIClient,
) -> None:
    """Control: a session model that IS in the active (openai) allowlist is forwarded verbatim."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=100)
        sid = uuid.uuid4()
        await s.execute(
            text(
                "INSERT INTO chat_sessions (id, user_id, project_id, mode, model) "
                "VALUES (:id, :uid, 'p', 'credits', 'gpt-4o')"
            ),
            {"id": str(sid), "uid": str(uid)},
        )
        await s.commit()

    openai_instance.responses = [openai_instance.text_result("ok")]
    r = await client.post(
        "/v1/chat/run",
        json={
            "userId": str(uid),
            "sessionId": str(sid),
            "projectId": "p",
            "message": "resume",
            "mode": "credits",
        },
        headers=auth_headers(uid),
    )
    assert r.status_code == 200
    assert openai_instance.calls[-1]["model"] == "gpt-4o"
