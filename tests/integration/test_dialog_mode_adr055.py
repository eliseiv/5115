"""Integration: session-fixed, provider-gated dialog_mode (ADR-055 / ADR-059).

Through the real /chat/run + /v1/preferences app with the real PostgreSQL container. BOTH LLM
clients are faked at the singleton boundary (no real network; tests pass with placeholder API keys):
- the Anthropic singleton is the conftest ``fake_anthropic`` (patched by the ``client`` fixture),
- the OpenAI singleton is faked here (``FakeOpenAIClient``) and records the ``model`` + dialog-mode
  ``options`` handed to it.

The ``openai_instance`` fixture forces ``LLM_PROVIDER=openai`` by mutating the cached Settings
singleton (restored per test), mirroring the ADR-034/ADR-044 tests. Coverage:
- session-fixed resolve at creation: request → user_preferences.default_dialog_mode → 'smart';
- resume IGNORES the request field (read from chat_sessions.dialog_mode);
- 422 unsupported_dialog_mode for an unknown value AND for the provider-gated modes on anthropic
  (asserting the MACHINE code in the body, not just the status);
- 'smart' works on anthropic; deep_thinking FORCES DEEP_THINKING_MODEL over the user's model;
- PATCH preferences may store 'search' on anthropic (gate is at session creation, not on save) yet a
  later run without dialogMode is 422 — the gate-vs-preference boundary.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.chat.llm_client as llm_mod
from app.config import get_settings
from tests.conftest import FakeAnthropicClient, auth_headers, seed_user


class FakeOpenAIClient:
    """In-memory OpenAI LLMClient double recording the model + dialog-mode options per call.

    Enough for the orchestrator's generation loop (text-only turns). No network; validate_key
    honors valid_keys. ``calls[*]`` records the session-fixed ``model`` the orchestrator forwarded
    and the ``dialog_mode`` carried by GenerationOptions (ADR-055 §5) so the gate/forcing semantics
    are assertable at the boundary.
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

    async def create_message(self, **kwargs: Any) -> Any:
        options = kwargs.get("options")
        self.calls.append(
            {
                "model": kwargs.get("model"),
                "api_key": kwargs.get("api_key"),
                "dialog_mode": getattr(options, "dialog_mode", None),
            }
        )
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

    Mutates the cached Settings singleton in place (restored after). A DISTINCTIVE
    ``deep_thinking_model`` ('o4-mini-deep') is set so the deep_thinking model-forcing assertion is
    unambiguous (it is NOT the user's selectable model and NOT the plain instance default).
    """
    s = get_settings()
    orig = (
        s.llm_provider,
        s.anthropic_models_raw,
        s.openai_models_raw,
        s.anthropic_model,
        s.openai_model,
        s.deep_thinking_model,
    )
    s.llm_provider = "openai"
    s.anthropic_models_raw = json.dumps({"claude-sonnet-4-6": "Sonnet"})
    s.openai_models_raw = json.dumps({"gpt-4o": "GPT-4o"})
    s.anthropic_model = "claude-sonnet-4-5"
    s.openai_model = "gpt-4o"
    s.deep_thinking_model = "o4-mini-deep"
    monkeypatch.setattr(llm_mod, "_openai_singleton", fake_openai)
    yield fake_openai
    (
        s.llm_provider,
        s.anthropic_models_raw,
        s.openai_models_raw,
        s.anthropic_model,
        s.openai_model,
        s.deep_thinking_model,
    ) = orig


async def _session_dialog_mode(
    maker: async_sessionmaker[AsyncSession], session_id: str
) -> str | None:
    async with maker() as s:
        return await s.scalar(
            text("SELECT dialog_mode FROM chat_sessions WHERE id=:sid"), {"sid": session_id}
        )


# =============================== session-fixed resolution ===============================
@pytest.mark.asyncio
async def test_create_without_dialog_mode_defaults_to_smart_anthropic(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    # No dialogMode, no preference row → 'smart' (request → default → smart). Works on anthropic.
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=100)
    r = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "message": "hi", "mode": "credits"},
        headers=auth_headers(uid),
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "assistant_message"
    sid = r.json()["sessionId"]
    assert await _session_dialog_mode(db_sessionmaker, sid) == "smart"


@pytest.mark.asyncio
async def test_create_explicit_dialog_mode_search_openai(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    openai_instance: FakeOpenAIClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=100)
    r = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "message": "hi", "mode": "credits", "dialogMode": "search"},
        headers=auth_headers(uid),
    )
    assert r.status_code == 200, r.text
    sid = r.json()["sessionId"]
    assert await _session_dialog_mode(db_sessionmaker, sid) == "search"
    # The session dialog mode reached the client via GenerationOptions.
    assert openai_instance.calls[-1]["dialog_mode"] == "search"


@pytest.mark.asyncio
async def test_create_uses_preference_default_when_absent_openai(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    openai_instance: FakeOpenAIClient,
) -> None:
    # request → user_preferences.default_dialog_mode: a stored default of 'study_learn' is used when
    # the request omits dialogMode (openai instance so the gated mode is allowed).
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=100)
    pr = await client.patch(
        "/v1/preferences",
        json={"defaultDialogMode": "study_learn"},
        headers=auth_headers(uid),
    )
    assert pr.status_code == 200, pr.text
    r = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "message": "hi", "mode": "credits"},
        headers=auth_headers(uid),
    )
    assert r.status_code == 200, r.text
    sid = r.json()["sessionId"]
    assert await _session_dialog_mode(db_sessionmaker, sid) == "study_learn"


# =============================== resume ignores the request field ===============================
@pytest.mark.asyncio
async def test_resume_ignores_request_dialog_mode(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    # Create a 'smart' session on anthropic, then resume passing a gated mode: it must be IGNORED
    # (no 422, session stays 'smart' — validation runs only at creation, ADR-055 §2).
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=100)
    r1 = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "message": "first", "mode": "credits", "dialogMode": "smart"},
        headers=auth_headers(uid),
    )
    assert r1.status_code == 200, r1.text
    sid = r1.json()["sessionId"]

    r2 = await client.post(
        "/v1/chat/run",
        json={
            "userId": str(uid),
            "sessionId": sid,
            "message": "second",
            "mode": "credits",
            "dialogMode": "deep_thinking",  # gated + would 422 on create — ignored on resume
        },
        headers=auth_headers(uid),
    )
    assert r2.status_code == 200, r2.text  # NOT 422 — resume ignores the field
    assert r2.json()["status"] == "assistant_message"
    # Both the response's session and the stored row are unchanged (still 'smart').
    assert await _session_dialog_mode(db_sessionmaker, sid) == "smart"


# =============================== 422 unsupported_dialog_mode ===============================
@pytest.mark.asyncio
async def test_unknown_dialog_mode_is_422_with_machine_code(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=100)
    r = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "message": "hi", "mode": "credits", "dialogMode": "bogus"},
        headers=auth_headers(uid),
    )
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "unsupported_dialog_mode"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["deep_thinking", "study_learn", "search"])
async def test_provider_gated_modes_are_422_on_anthropic(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    mode: str,
) -> None:
    # ADR-055 §4: the three advanced modes require OpenAI. On an anthropic instance (conftest
    # default) they are rejected with the dedicated machine code (not a generic 422, not `blocked`).
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=100)
    r = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "message": "hi", "mode": "credits", "dialogMode": mode},
        headers=auth_headers(uid),
    )
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "unsupported_dialog_mode"
    # No session row was created (the gate raises before get_or_create_session).
    async with db_sessionmaker() as s:
        n = await s.scalar(
            text("SELECT count(*) FROM chat_sessions WHERE user_id=:u"), {"u": str(uid)}
        )
    assert int(n or 0) == 0


@pytest.mark.asyncio
async def test_smart_works_on_anthropic(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=100)
    r = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "message": "hi", "mode": "credits", "dialogMode": "smart"},
        headers=auth_headers(uid),
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "assistant_message"
    sid = r.json()["sessionId"]
    assert await _session_dialog_mode(db_sessionmaker, sid) == "smart"


# ==================== deep_thinking forces DEEP_THINKING_MODEL ====================
@pytest.mark.asyncio
async def test_deep_thinking_forces_instance_reasoning_model_openai(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    openai_instance: FakeOpenAIClient,
) -> None:
    # ADR-055 §5 / ADR-059 §4: deep_thinking overrides the user's selected model with the instance
    # DEEP_THINKING_MODEL. The user picks gpt-4o (in allowlist) but generation uses o4-mini-deep.
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=100)
    r = await client.post(
        "/v1/chat/run",
        json={
            "userId": str(uid),
            "message": "think hard",
            "mode": "credits",
            "model": "gpt-4o",
            "dialogMode": "deep_thinking",
        },
        headers=auth_headers(uid),
    )
    assert r.status_code == 200, r.text
    sid = r.json()["sessionId"]
    assert await _session_dialog_mode(db_sessionmaker, sid) == "deep_thinking"
    # The client was called with the FORCED instance reasoning model, NOT the user's gpt-4o.
    assert openai_instance.calls[-1]["model"] == "o4-mini-deep"
    assert openai_instance.calls[-1]["dialog_mode"] == "deep_thinking"
    # The session model the user picked is still stored verbatim (forcing is generation-time only).
    async with db_sessionmaker() as s:
        stored = await s.scalar(text("SELECT model FROM chat_sessions WHERE id=:sid"), {"sid": sid})
    assert stored == "gpt-4o"


# ==================== gate vs preference (the key boundary) ====================
@pytest.mark.asyncio
async def test_patch_search_preference_allowed_on_anthropic_but_run_gated(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    # ADR-055 §4 / preferences-02: PATCH validates ONLY enum membership → storing 'search' on an
    # anthropic instance is allowed (200). The provider-gate fires later, at session creation: a
    # /chat/run WITHOUT dialogMode resolves to the stored 'search' default → 422 on this instance.
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=100)

    pr = await client.patch(
        "/v1/preferences",
        json={"defaultDialogMode": "search"},
        headers=auth_headers(uid),
    )
    assert pr.status_code == 200, pr.text
    assert pr.json()["defaultDialogMode"] == "search"  # stored despite the anthropic instance

    r = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "message": "hi", "mode": "credits"},
        headers=auth_headers(uid),
    )
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "unsupported_dialog_mode"
