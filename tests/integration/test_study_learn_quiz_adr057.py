"""Integration tests for ADR-057 — Study & Learn quiz.generate in the chat tool-loop.

Real PostgreSQL container; BOTH LLM clients are faked at the singleton boundary (no real network;
the suite passes with placeholder API keys):
- the Anthropic singleton is the conftest ``fake_anthropic`` (patched by the ``client`` fixture),
- the OpenAI singleton is faked here (``FakeOpenAIClient``) — study_learn is provider-gated to
  OpenAI (ADR-055 §4 / ADR-059), so the quiz path runs on the OpenAI instance.

quiz.generate is a GLOBAL server-side tool (ADR-057 §4), executed in the tool-loop like time.now:
«исполнение» = validate the strict args + echo the dict; a VALID quiz is lifted into
``ChatResponse.quiz`` (last-wins); an INVALID quiz DEGRADES to a ``tool_result`` error the model
fixes in the same turn (graceful degrade, ADR-057 §3) — never a 422 that drops the turn. Coverage:

- happy path: valid quiz → ``ChatResponse.quiz`` filled, ``assistantMessage`` present, and
  ``serverTools`` records quiz.generate (status completed);
- history: the tool step is persisted with tool_use/tool_result parity;
- degrade (one test per violation class: options count / length / correctIndex range) → NOT 422,
  errored tool step, turn continues;
- retry: invalid then valid in one turn → the CORRECT quiz surfaces;
- never valid: model persists → quiz is None when it eventually stops; 502 when it exhausts rounds;
- TD-035 privacy: neither ``audit_logs.payload`` nor the degrade ``tool_result`` text carries the
  learning content (question / options / explanation);
- temporary chat: temporary=true + study_learn → quiz works, zero rows in
  chat_sessions/chat_steps/tool_calls;
- provider gate: study_learn on an anthropic instance → 422 unsupported_dialog_mode.
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
from tests.conftest import auth_headers, seed_user

# Distinctive learning-content tokens — used to prove (TD-035) the quiz text never leaks into the
# audit payload or the degrade tool_result message.
_Q = "SECRET_QUESTION_TOKEN_what_is_the_capital_of_France"
_OPTS = ["SECRET_OPT_paris", "SECRET_OPT_london", "SECRET_OPT_berlin"]
_EXPL = "SECRET_EXPLANATION_TOKEN_paris_is_the_capital"


def _valid_quiz(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "question": _Q,
        "options": list(_OPTS),
        "correctIndex": 0,
        "explanation": _EXPL,
    }
    base.update(overrides)
    return base


class FakeOpenAIClient:
    """In-memory OpenAI LLMClient double: scriptable text / tool_call turns (no network).

    ``responses`` is popped in order; when empty, ``always_response`` (if set) is returned on every
    further call (to exercise the round-exhaustion guard), else a default end_turn text result.
    ``tool_result`` builds an OpenAI-wire assistant turn with one function tool_call plus the
    domain-shaped ``tool_uses`` the orchestrator dispatches on (ADR-033 §4).
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.responses: list[Any] = []
        self.always_response: Any = None
        self.valid_keys: set[str] = set()

    def text_result(self, text_value: str = "Here is your quiz and explanation.") -> Any:
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

    def tool_result(
        self,
        tool_name: str,
        args: dict[str, Any],
        *,
        text_value: str = "",
        tool_id: str | None = None,
    ) -> Any:
        from app.chat.llm_client import LLMResult, LLMUsage

        tid = tool_id or f"call_{uuid.uuid4().hex[:24]}"
        usage = LLMUsage(
            input_tokens=10,
            output_tokens=5,
            model="gpt-4o",
            cache_read_tokens=0,
            cache_write_tokens=0,
        )
        assistant_msg: dict[str, Any] = {
            "role": "assistant",
            "content": text_value or None,
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
            text=text_value,
            tool_uses=[{"id": tid, "name": tool_name, "input": args}],
        )

    async def create_message(self, **kwargs: Any) -> Any:
        options = kwargs.get("options")
        self.calls.append(
            {
                "model": kwargs.get("model"),
                "api_key": kwargs.get("api_key"),
                "dialog_mode": getattr(options, "dialog_mode", None),
                "tools": kwargs.get("tools", []),
            }
        )
        if self.responses:
            return self.responses.pop(0)
        if self.always_response is not None:
            return self.always_response
        return self.text_result()

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
    """Force LLM_PROVIDER=openai + patch the OpenAI singleton with the recording fake (restored)."""
    s = get_settings()
    orig = (
        s.llm_provider,
        s.anthropic_models_raw,
        s.openai_models_raw,
        s.anthropic_model,
        s.openai_model,
    )
    s.llm_provider = "openai"
    s.anthropic_models_raw = json.dumps({"claude-sonnet-4-6": "Sonnet"})
    s.openai_models_raw = json.dumps({"gpt-4o": "GPT-4o"})
    s.anthropic_model = "claude-sonnet-4-5"
    s.openai_model = "gpt-4o"
    monkeypatch.setattr(llm_mod, "_openai_singleton", fake_openai)
    yield fake_openai
    (
        s.llm_provider,
        s.anthropic_models_raw,
        s.openai_models_raw,
        s.anthropic_model,
        s.openai_model,
    ) = orig


async def _scalar(maker: async_sessionmaker[AsyncSession], sql: str, **params: object) -> object:
    async with maker() as s:
        return await s.scalar(text(sql), params)


async def _run_study_learn(client: AsyncClient, uid: uuid.UUID, **extra: Any) -> Any:
    payload: dict[str, Any] = {
        "userId": str(uid),
        "message": "teach me French geography",
        "mode": "credits",
        "dialogMode": "study_learn",
    }
    payload.update(extra)
    return await client.post("/v1/chat/run", json=payload, headers=auth_headers(uid))


# ============================ happy path ============================
@pytest.mark.asyncio
async def test_valid_quiz_fills_response_and_records_server_tool(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    openai_instance: FakeOpenAIClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    openai_instance.responses = [
        openai_instance.tool_result("quiz.generate", _valid_quiz()),
        openai_instance.text_result("Paris is the capital of France."),
    ]
    r = await _run_study_learn(client, uid)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "assistant_message", body
    assert body["assistantMessage"] == "Paris is the capital of France."
    # ADR-057 §5: the quiz is surfaced, validated, with the correct index.
    quiz = body["quiz"]
    assert quiz is not None
    assert quiz["question"] == _Q
    assert quiz["options"] == _OPTS
    assert quiz["correctIndex"] == 0
    assert quiz["explanation"] == _EXPL
    # serverTools records the server-side quiz.generate execution (completed), not a client call.
    server = body["serverTools"]
    assert any(st["toolName"] == "quiz.generate" and st["status"] == "completed" for st in server)
    assert body.get("toolCalls") in (None, [])
    # study_learn reached the client as a generation option.
    assert openai_instance.calls[0]["dialog_mode"] == "study_learn"
    # quiz.generate WAS in the offered tool-set for this study_learn turn.
    offered = {t["name"] for t in openai_instance.calls[0]["tools"]}
    assert "quiz.generate" in offered


@pytest.mark.asyncio
async def test_quiz_tool_step_persisted_with_use_result_parity(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    openai_instance: FakeOpenAIClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    openai_instance.responses = [
        openai_instance.tool_result("quiz.generate", _valid_quiz(), tool_id="call_quizhist01"),
        openai_instance.text_result("done"),
    ]
    r = await _run_study_learn(client, uid)
    sid = r.json()["sessionId"]

    # The tool_call row is completed server-side and keeps the raw provider id (ADR-008).
    async with db_sessionmaker() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT tool_name, status, provider_tool_use_id "
                    "FROM tool_calls WHERE session_id=:sid"
                ),
                {"sid": sid},
            )
        ).all()
    assert len(rows) == 1
    assert rows[0][0] == "quiz.generate"
    assert rows[0][1] == "completed"
    assert rows[0][2] == "call_quizhist01"

    # A tool step exists carrying the result and the providerToolUseId (tool_use/tool_result pair).
    async with db_sessionmaker() as s:
        payload = await s.scalar(
            text(
                "SELECT payload FROM chat_steps WHERE session_id=:sid AND role='tool' "
                "ORDER BY seq LIMIT 1"
            ),
            {"sid": sid},
        )
    assert payload is not None
    assert payload["toolName"] == "quiz.generate"
    assert payload["providerToolUseId"] == "call_quizhist01"
    assert payload["error"] is None
    assert payload["result"]["correctIndex"] == 0


# ============================ graceful degrade (NOT 422) ============================
@pytest.mark.parametrize(
    "bad_quiz",
    [
        pytest.param(_valid_quiz(options=["only-one"], correctIndex=0), id="too_few_options"),
        pytest.param(
            _valid_quiz(options=[f"opt-{i}" for i in range(11)], correctIndex=0),
            id="too_many_options",
        ),
        pytest.param(_valid_quiz(question="q" * 1001), id="question_too_long"),
        pytest.param(
            _valid_quiz(options=["a", "b"], correctIndex=5), id="correct_index_out_of_range"
        ),
        # ADR-057 §3: a JSON `true` correctIndex (bool) is rejected on the RAW input (mode="before"
        # guard) and degrades like any other invalid quiz — NOT silently coerced to index 1.
        pytest.param(_valid_quiz(correctIndex=True), id="correct_index_boolean"),
    ],
)
@pytest.mark.asyncio
async def test_invalid_quiz_degrades_without_422(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    openai_instance: FakeOpenAIClient,
    bad_quiz: dict[str, Any],
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    openai_instance.responses = [
        openai_instance.tool_result("quiz.generate", bad_quiz),
        openai_instance.text_result("Let me try again with a proper quiz."),
    ]
    r = await _run_study_learn(client, uid)
    # ADR-057 §3: the invalid quiz does NOT 422 the turn — it completes normally.
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "assistant_message", body
    # No valid quiz was produced this turn → quiz is null.
    assert body["quiz"] is None
    # The server-side quiz.generate execution is recorded as errored.
    assert any(
        st["toolName"] == "quiz.generate" and st["status"] == "errored"
        for st in body["serverTools"]
    )
    # The persisted tool step is errored with the machine code (turn survived).
    sid = body["sessionId"]
    async with db_sessionmaker() as s:
        payload = await s.scalar(
            text(
                "SELECT payload FROM chat_steps WHERE session_id=:sid AND role='tool' "
                "ORDER BY seq LIMIT 1"
            ),
            {"sid": sid},
        )
    assert payload["error"]["code"] == "invalid_quiz"


# ============================ retry within the same turn ============================
@pytest.mark.asyncio
async def test_invalid_then_valid_quiz_surfaces_corrected_quiz(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    openai_instance: FakeOpenAIClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    openai_instance.responses = [
        # Round 1: invalid (out-of-range index) → degrade. Round 2: corrected. Round 3: final text.
        openai_instance.tool_result(
            "quiz.generate", _valid_quiz(options=["a", "b"], correctIndex=9)
        ),
        openai_instance.tool_result("quiz.generate", _valid_quiz(correctIndex=1)),
        openai_instance.text_result("Corrected quiz above."),
    ]
    r = await _run_study_learn(client, uid)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "assistant_message"
    # last-wins: the CORRECTED quiz (correctIndex=1, full options) is surfaced.
    assert body["quiz"] is not None
    assert body["quiz"]["correctIndex"] == 1
    assert body["quiz"]["options"] == _OPTS
    # Both rounds recorded: one errored, one completed.
    statuses = sorted(
        st["status"] for st in body["serverTools"] if st["toolName"] == "quiz.generate"
    )
    assert statuses == ["completed", "errored"]


# ============================ never valid ============================
@pytest.mark.asyncio
async def test_never_valid_quiz_finishes_with_null_quiz(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    openai_instance: FakeOpenAIClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    # Three invalid quiz rounds, then the model gives up and returns plain text → quiz is null.
    openai_instance.responses = [
        openai_instance.tool_result("quiz.generate", _valid_quiz(options=["x"], correctIndex=0)),
        openai_instance.tool_result("quiz.generate", _valid_quiz(options=["x"], correctIndex=0)),
        openai_instance.tool_result("quiz.generate", _valid_quiz(options=["x"], correctIndex=0)),
        openai_instance.text_result("Sorry, here is a plain explanation without a quiz."),
    ]
    r = await _run_study_learn(client, uid)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "assistant_message"
    assert body["quiz"] is None
    errored = [st for st in body["serverTools"] if st["status"] == "errored"]
    assert len(errored) == 3


@pytest.mark.asyncio
async def test_relentlessly_invalid_quiz_exhausts_rounds_502(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    openai_instance: FakeOpenAIClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ADR-057 §3 boundary: a model that NEVER yields a valid quiz eventually hits the shared
    # MAX_SERVER_TOOL_ROUNDS guard (ADR-011 §2) → controlled 502, no billing. Rounds lowered so the
    # test is fast; always_response makes every round an invalid quiz.
    monkeypatch.setattr(get_settings(), "max_server_tool_rounds", 2)
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    openai_instance.always_response = openai_instance.tool_result(
        "quiz.generate", _valid_quiz(options=["x"], correctIndex=0)
    )
    r = await _run_study_learn(client, uid)
    assert r.status_code == 502, r.text
    # No billing on the controlled failure (no final assistant_message).
    bal = await _scalar(db_sessionmaker, "SELECT balance FROM wallets WHERE user_id=:u", u=str(uid))
    assert int(bal) == 5


# ============================ TD-035 privacy ============================
@pytest.mark.asyncio
async def test_quiz_learning_content_never_leaks_to_audit_or_error(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    openai_instance: FakeOpenAIClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    # Invalid quiz carrying the distinctive content tokens → degrade builds a content-free error.
    openai_instance.responses = [
        openai_instance.tool_result(
            "quiz.generate", _valid_quiz(options=["only-one"], correctIndex=0)
        ),
        openai_instance.text_result("retry"),
    ]
    r = await _run_study_learn(client, uid)
    assert r.status_code == 200, r.text
    sid = r.json()["sessionId"]

    # (1) audit_logs.payload carries NONE of the learning content (TD-035).
    async with db_sessionmaker() as s:
        audit_text = await s.scalar(
            text("SELECT string_agg(payload::text, '||') FROM audit_logs WHERE user_id=:u"),
            {"u": str(uid)},
        )
    assert audit_text is not None
    for token in (_Q, *_OPTS, _EXPL):
        assert token not in audit_text, f"leaked into audit: {token}"

    # (2) the degrade tool_result error message is content-free (built from loc+type only).
    async with db_sessionmaker() as s:
        payload = await s.scalar(
            text(
                "SELECT payload FROM chat_steps WHERE session_id=:sid AND role='tool' "
                "ORDER BY seq LIMIT 1"
            ),
            {"sid": sid},
        )
    err_message = payload["error"]["message"]
    for token in (_Q, *_OPTS, _EXPL):
        assert token not in err_message, f"leaked into tool_result error: {token}"


@pytest.mark.asyncio
async def test_boolean_correct_index_degrades_content_free(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    openai_instance: FakeOpenAIClient,
) -> None:
    # ADR-057 §3 + TD-035: a bool correctIndex (JSON `true`) is rejected on the RAW input
    # (mode="before" guard) and degrades — NOT a 422, NOT silently coerced to 1 — and the resulting
    # invalid_quiz tool_result error stays content-free (no question/options/explanation).
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    openai_instance.responses = [
        openai_instance.tool_result("quiz.generate", _valid_quiz(correctIndex=True)),
        openai_instance.text_result("retry with an integer index"),
    ]
    r = await _run_study_learn(client, uid)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "assistant_message"
    # The bool was NOT accepted as index 1 → no valid quiz this round.
    assert body["quiz"] is None
    sid = body["sessionId"]
    async with db_sessionmaker() as s:
        payload = await s.scalar(
            text(
                "SELECT payload FROM chat_steps WHERE session_id=:sid AND role='tool' "
                "ORDER BY seq LIMIT 1"
            ),
            {"sid": sid},
        )
    assert payload["error"]["code"] == "invalid_quiz"
    err_message = payload["error"]["message"]
    for token in (_Q, *_OPTS, _EXPL):
        assert token not in err_message, f"leaked into tool_result error: {token}"


# ============================ temporary chat ============================
@pytest.mark.asyncio
async def test_study_learn_quiz_in_temporary_chat_persists_nothing(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    openai_instance: FakeOpenAIClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    openai_instance.responses = [
        openai_instance.tool_result("quiz.generate", _valid_quiz()),
        openai_instance.text_result("ephemeral quiz explanation"),
    ]
    r = await _run_study_learn(client, uid, temporary=True, history=[])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "assistant_message"
    # Quiz still works in a temporary chat (global server-side tool, in-request execution, ADR-056).
    assert body["quiz"] is not None
    assert body["quiz"]["correctIndex"] == 0

    # No persistence at all (ADR-056 §1).
    sessions = await _scalar(
        db_sessionmaker, "SELECT count(*) FROM chat_sessions WHERE user_id=:u", u=str(uid)
    )
    steps = await _scalar(db_sessionmaker, "SELECT count(*) FROM chat_steps")
    tool_calls = await _scalar(db_sessionmaker, "SELECT count(*) FROM tool_calls")
    assert int(sessions or 0) == 0
    assert int(steps or 0) == 0
    assert int(tool_calls or 0) == 0


# ============================ provider gate (regression, ADR-055 §4) ============================
@pytest.mark.asyncio
async def test_study_learn_on_anthropic_instance_is_422(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    # The `client` fixture runs the anthropic posture (conftest). study_learn is OpenAI-gated →
    # 422 unsupported_dialog_mode. (The exhaustive gate matrix lives in test_dialog_mode_adr055;
    # this is the quiz-local regression guard so the quiz path cannot silently run on anthropic.)
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    r = await _run_study_learn(client, uid)
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "unsupported_dialog_mode"
