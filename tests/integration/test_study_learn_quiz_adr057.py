"""Integration tests for the Study & Learn quiz.generate pool in the chat tool-loop.

ADR-057 introduced quiz.generate; **ADR-062 redesigns it into a POOL** of 3..10 questions delivered
in ONE call (``{questions: [...]}``), and makes ``assistantMessage`` deterministically ``null``
whenever a quiz is present (no spoiler / no duplicated question in the free text). These tests run
against a real PostgreSQL container; BOTH LLM clients are faked at the singleton boundary (no real
network; the suite passes with placeholder API keys):
- the Anthropic singleton is the conftest ``fake_anthropic`` (patched by the ``client`` fixture),
- the OpenAI singleton is faked here (``FakeOpenAIClient``) — study_learn is provider-gated to
  OpenAI (ADR-055 §4 / ADR-059), so the quiz path runs on the OpenAI instance.

quiz.generate is a GLOBAL server-side tool (ADR-062 §1), executed in the tool-loop like time.now:
«исполнение» = validate the strict POOL args + echo the dict back; a VALID pool is lifted into
``ChatResponse.quiz.questions[]`` (last-wins); an INVALID pool DEGRADES all-or-nothing to a
``tool_result`` error the model fixes in the same turn (graceful degrade, ADR-062 §7) — never a 422
that drops the turn. Coverage:

- happy path: valid pool of 3 (and 10) → ``ChatResponse.quiz.questions`` filled, each with
  options/correctIndex/explanation; ``serverTools`` records quiz.generate (completed);
- ADR-062 §3: ``assistantMessage`` is null when a quiz is present (assistant_message AND tool_call
  status); a non-quiz turn keeps its ``assistantMessage``;
- pool count bounds: 2 → degrade, 11 → degrade, 3 and 10 → valid;
- all-or-nothing: ONE bad question (out-of-range/negative/bool index, too few options, over-length)
  degrades the WHOLE pool → NOT 422, errored tool step, turn continues;
- retry: invalid then valid pool in one turn → the CORRECTED pool surfaces (last-wins);
- never valid: model persists → quiz is None when it eventually stops; 502 when it exhausts rounds;
- ADR-062 §4: correctIndex/explanation reach the client (local check); no continuation endpoint;
- TD-035 privacy: neither ``audit_logs.payload`` nor the degrade ``tool_result`` text carries the
  learning content; the nested loc (``questions.N.correctIndex``) leaks only indices/field/code;
- temporary chat: temporary=true + study_learn → pool works, zero rows persisted;
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


def _card(**overrides: Any) -> dict[str, Any]:
    """One quiz question card carrying the distinctive secret tokens (nested pool element)."""
    base: dict[str, Any] = {
        "question": _Q,
        "options": list(_OPTS),
        "correctIndex": 0,
        "explanation": _EXPL,
    }
    base.update(overrides)
    return base


def _pool(n: int = 3, *, bad: dict[str, Any] | None = None) -> dict[str, Any]:
    """A valid pool of ``n`` secret-token cards; ``bad`` REPLACES the last card (all-or-nothing)."""
    cards = [_card() for _ in range(n)]
    if bad is not None:
        cards[-1] = bad
    return {"questions": cards}


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


# ============================ happy path (pool) ============================
@pytest.mark.parametrize("pool_size", [3, 10])
@pytest.mark.asyncio
async def test_valid_pool_fills_response_and_records_server_tool(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    openai_instance: FakeOpenAIClient,
    pool_size: int,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    openai_instance.responses = [
        openai_instance.tool_result("quiz.generate", _pool(pool_size)),
        openai_instance.text_result("Paris is the capital of France."),
    ]
    r = await _run_study_learn(client, uid)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "assistant_message", body
    # ADR-062 §3 (hard): a quiz is present → assistantMessage is deterministically null
    # (spoiler-safe, no duplicated question in free text), REGARDLESS of the model's text.
    assert body["assistantMessage"] is None
    # ADR-062 §2: the whole pool is surfaced under quiz.questions[], each fully populated.
    quiz = body["quiz"]
    assert quiz is not None
    assert len(quiz["questions"]) == pool_size
    for q in quiz["questions"]:
        assert q["question"] == _Q
        assert q["options"] == _OPTS
        assert q["correctIndex"] == 0
        assert q["explanation"] == _EXPL
    # serverTools records the server-side quiz.generate execution (completed), not a client call.
    server = body["serverTools"]
    assert any(st["toolName"] == "quiz.generate" and st["status"] == "completed" for st in server)
    assert body.get("toolCalls") in (None, [])
    # study_learn reached the client as a generation option, and quiz.generate WAS offered.
    assert openai_instance.calls[0]["dialog_mode"] == "study_learn"
    offered = {t["name"] for t in openai_instance.calls[0]["tools"]}
    assert "quiz.generate" in offered


@pytest.mark.asyncio
async def test_pool_tool_step_persisted_with_use_result_parity(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    openai_instance: FakeOpenAIClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    openai_instance.responses = [
        openai_instance.tool_result("quiz.generate", _pool(3), tool_id="call_quizhist01"),
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
    # The echoed pool round-trips through the tool step.
    assert len(payload["result"]["questions"]) == 3
    assert payload["result"]["questions"][0]["correctIndex"] == 0


# ============================ assistantMessage null-guard (ADR-062 §3) ============================
@pytest.mark.asyncio
async def test_non_quiz_turn_keeps_assistant_message(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    openai_instance: FakeOpenAIClient,
) -> None:
    # ADR-062 §3: the null-guard fires ONLY when a quiz is present. A study_learn turn WITHOUT a
    # quiz.generate call keeps its assistantMessage (the guard is not a blanket study_learn muzzle).
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    openai_instance.responses = [
        openai_instance.text_result("Here is a plain explanation, no quiz this time."),
    ]
    r = await _run_study_learn(client, uid)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "assistant_message"
    assert body["quiz"] is None
    assert body["assistantMessage"] == "Here is a plain explanation, no quiz this time."


@pytest.mark.asyncio
async def test_quiz_with_client_tool_zeroes_accompanying_text_on_tool_call(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    openai_instance: FakeOpenAIClient,
) -> None:
    # ADR-062 §3 (overrides ADR-024 for the quiz turn): when a quiz.generate round precedes a
    # client-side tool_use IN THE SAME TURN, the tool_call response carries the quiz AND the model's
    # accompanying text is dropped (assistantMessage=null) — the single _to_response assembly point
    # covers the tool_call status too, not just assistant_message.
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    openai_instance.responses = [
        # Round 1: server-side quiz.generate (executed in-loop, added to quiz_acc).
        openai_instance.tool_result("quiz.generate", _pool(3)),
        # Round 2: a client-side files.read WITH accompanying text → tool_call hand-off to iOS.
        openai_instance.tool_result(
            "files.read",
            {"path": "notes.md"},
            text_value="Here is the quiz and also let me read your file.",
        ),
    ]
    r = await _run_study_learn(client, uid)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "tool_call", body
    # The quiz surfaced on the tool_call response…
    assert body["quiz"] is not None
    assert len(body["quiz"]["questions"]) == 3
    # …and the accompanying text was zeroed (would otherwise spoil/duplicate — ADR-062 §3).
    assert body["assistantMessage"] is None
    # The client-side tool is still handed off.
    assert any(tc["name"] == "files.read" for tc in body["toolCalls"])


# ============================ pool count bounds (degrade, ADR-062 §7) ============================
@pytest.mark.parametrize("count", [2, 11])
@pytest.mark.asyncio
async def test_pool_count_out_of_bounds_degrades_without_422(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    openai_instance: FakeOpenAIClient,
    count: int,
) -> None:
    # ADR-062 §7: a pool of 2 (below 3) or 11 (above 10) degrades all-or-nothing — NOT a 422; the
    # model gets an invalid_quiz tool_result and the turn continues.
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    openai_instance.responses = [
        openai_instance.tool_result("quiz.generate", _pool(count)),
        openai_instance.text_result("Let me try again with the right number of questions."),
    ]
    r = await _run_study_learn(client, uid)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "assistant_message", body
    assert body["quiz"] is None
    assert any(
        st["toolName"] == "quiz.generate" and st["status"] == "errored"
        for st in body["serverTools"]
    )
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


# =============== all-or-nothing: one bad question degrades the whole pool ===============
@pytest.mark.parametrize(
    "bad_card",
    [
        pytest.param(_card(options=["only-one"], correctIndex=0), id="too_few_options"),
        pytest.param(
            _card(options=[f"opt-{i}" for i in range(11)], correctIndex=0), id="too_many_options"
        ),
        pytest.param(_card(question="q" * 1001), id="question_too_long"),
        pytest.param(_card(options=["a", "b"], correctIndex=5), id="correct_index_out_of_range"),
        pytest.param(_card(correctIndex=-1), id="correct_index_negative"),
        # ADR-062 §7 + ADR-057 §3: a JSON `true` correctIndex (bool) is rejected on the RAW input
        # (mode="before" guard) and degrades — NOT silently coerced to index 1.
        pytest.param(_card(correctIndex=True), id="correct_index_boolean"),
    ],
)
@pytest.mark.asyncio
async def test_single_bad_question_degrades_whole_pool_without_422(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    openai_instance: FakeOpenAIClient,
    bad_card: dict[str, Any],
) -> None:
    # ADR-062 §7: ONE bad question among two valid ones invalidates the ENTIRE pool (no partial
    # acceptance) and degrades — NOT a 422; the turn completes normally with quiz=null.
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    openai_instance.responses = [
        openai_instance.tool_result("quiz.generate", _pool(3, bad=bad_card)),
        openai_instance.text_result("Let me try again with a proper quiz."),
    ]
    r = await _run_study_learn(client, uid)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "assistant_message", body
    assert body["quiz"] is None
    assert any(
        st["toolName"] == "quiz.generate" and st["status"] == "errored"
        for st in body["serverTools"]
    )
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
async def test_invalid_then_valid_pool_surfaces_corrected_pool(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    openai_instance: FakeOpenAIClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    openai_instance.responses = [
        # Round 1: invalid (one question out-of-range) → degrade. Round 2: corrected pool. Round 3:
        # final text.
        openai_instance.tool_result(
            "quiz.generate", _pool(3, bad=_card(options=["a", "b"], correctIndex=9))
        ),
        openai_instance.tool_result("quiz.generate", _pool(4, bad=_card(correctIndex=1))),
        openai_instance.text_result("Corrected quiz above."),
    ]
    r = await _run_study_learn(client, uid)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "assistant_message"
    # last-wins: the CORRECTED pool (4 questions, last one correctIndex=1) is surfaced.
    assert body["quiz"] is not None
    assert len(body["quiz"]["questions"]) == 4
    assert body["quiz"]["questions"][-1]["correctIndex"] == 1
    # Both rounds recorded: one errored, one completed.
    statuses = sorted(
        st["status"] for st in body["serverTools"] if st["toolName"] == "quiz.generate"
    )
    assert statuses == ["completed", "errored"]


# ============================ never valid ============================
@pytest.mark.asyncio
async def test_never_valid_pool_finishes_with_null_quiz(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    openai_instance: FakeOpenAIClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    # Three invalid pool rounds (each a too-small pool), then the model gives up → quiz is null.
    openai_instance.responses = [
        openai_instance.tool_result("quiz.generate", _pool(2)),
        openai_instance.tool_result("quiz.generate", _pool(2)),
        openai_instance.tool_result("quiz.generate", _pool(2)),
        openai_instance.text_result("Sorry, here is a plain explanation without a quiz."),
    ]
    r = await _run_study_learn(client, uid)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "assistant_message"
    assert body["quiz"] is None
    # No quiz present → the plain-text explanation is preserved (ADR-062 §3 guard did not fire).
    assert body["assistantMessage"] == "Sorry, here is a plain explanation without a quiz."
    errored = [st for st in body["serverTools"] if st["status"] == "errored"]
    assert len(errored) == 3


@pytest.mark.asyncio
async def test_relentlessly_invalid_pool_exhausts_rounds_502(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    openai_instance: FakeOpenAIClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ADR-062 §7 boundary: a model that NEVER yields a valid pool eventually hits the shared
    # MAX_SERVER_TOOL_ROUNDS guard (ADR-011 §2) → controlled 502, no billing. Rounds lowered so the
    # test is fast; always_response makes every round an invalid pool.
    monkeypatch.setattr(get_settings(), "max_server_tool_rounds", 2)
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    openai_instance.always_response = openai_instance.tool_result("quiz.generate", _pool(2))
    r = await _run_study_learn(client, uid)
    assert r.status_code == 502, r.text
    # No billing on the controlled failure (no final assistant_message).
    bal = await _scalar(db_sessionmaker, "SELECT balance FROM wallets WHERE user_id=:u", u=str(uid))
    assert int(bal) == 5


# ============================ no continuation endpoint (ADR-062 §4) ============================
@pytest.mark.asyncio
async def test_client_side_check_no_answer_submission_endpoint(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    openai_instance: FakeOpenAIClient,
) -> None:
    # ADR-062 §4: correctIndex/explanation reach the client (local check); there is NO endpoint to
    # submit quiz answers back — the client scores locally, nothing is posted to the server.
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    openai_instance.responses = [
        openai_instance.tool_result("quiz.generate", _pool(3)),
        openai_instance.text_result("done"),
    ]
    r = await _run_study_learn(client, uid)
    body = r.json()
    # The client-side answer key (correctIndex + explanation) is present for every question.
    for q in body["quiz"]["questions"]:
        assert "correctIndex" in q
        assert "explanation" in q
    # No answer-submission endpoint exists (regression guard against introducing continuation).
    app = client._transport.app  # type: ignore[attr-defined]
    routes = {getattr(route, "path", "") for route in app.routes}
    assert not any("quiz" in path and "answer" in path for path in routes)
    assert "/v1/chat/quiz-result" not in routes
    assert "/v1/quiz/answers" not in routes


# ============================ TD-035 privacy ============================
@pytest.mark.asyncio
async def test_pool_learning_content_never_leaks_to_audit_or_error(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    openai_instance: FakeOpenAIClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    # Invalid pool: a bool correctIndex in the LAST question → nested loc questions.2.correctIndex.
    openai_instance.responses = [
        openai_instance.tool_result("quiz.generate", _pool(3, bad=_card(correctIndex=True))),
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

    # (2) the degrade tool_result error message is content-free (built from loc+type only) yet DOES
    # carry the nested field locus (indices/field names/error code — never the submitted values).
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
    # The nested loc names the offending question index + field, but no value.
    assert "questions.2.correctIndex" in err_message


# ============================ temporary chat ============================
@pytest.mark.asyncio
async def test_study_learn_pool_in_temporary_chat_persists_nothing(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    openai_instance: FakeOpenAIClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    openai_instance.responses = [
        openai_instance.tool_result("quiz.generate", _pool(3)),
        openai_instance.text_result("ephemeral quiz explanation"),
    ]
    r = await _run_study_learn(client, uid, temporary=True, history=[])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "assistant_message"
    # Pool still works in a temporary chat (global server-side tool, in-request execution, ADR-056).
    assert body["quiz"] is not None
    assert len(body["quiz"]["questions"]) == 3
    assert body["quiz"]["questions"][0]["correctIndex"] == 0
    # ADR-062 §3 still holds in a temporary chat.
    assert body["assistantMessage"] is None

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
