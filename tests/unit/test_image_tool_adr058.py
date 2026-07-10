"""Unit tests for ADR-058 image.generate — key-gate, arg schema, strict-set, degrade classification.

Pure, no DB / no network. Four concerns:

- **key-gate (§3):** ``image.generate`` is offered to the LLM (neutral / anthropic / openai defs)
  IFF ``OPENAI_API_KEY`` is non-empty after ``strip()`` — a NEW fourth offered-set gate composing by
  logical AND with ``include_server_side`` / ``include_client_side`` / ``dialog_mode``. The machine
  ``tool_catalog()`` is NOT key-gated (lists every registered tool).
- **arg schema (§3):** ``ImageGenerateArgs`` — valid case + every rejection (bad size/quality, empty
  or over-cap prompt, unknown field).
- **strict set (§3):** ``image.generate`` is deliberately NOT a strict OpenAI tool.
- **degrade classification (§3):** ``ImageContentPolicyError`` (the subclass) is caught FIRST →
  ``content_policy`` / INFO; the base ``ImageGenerationError`` → ``image_generation_failed`` /
  WARNING + metric. The prompt (and any key) NEVER reach the log records (TD-035).

``OPENAI_API_KEY`` is forced non-empty for the whole suite (conftest). The key-gate tests flip it
via ``monkeypatch.setenv`` + ``get_settings.cache_clear()``; an autouse teardown clears the cache so
the forced default is re-read for every other test.
"""

from __future__ import annotations

import inspect
import logging
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.chat.image_client import ImageContentPolicyError, ImageGenerationError
from app.chat.orchestrator import ChatOrchestrator, _BillingPlan, _ImagePregen
from app.chat.tools import (
    _OPENAI_STRICT_TOOLS,
    IMAGE_PROMPT_MAX_LENGTH,
    TOOL_IMAGE_GENERATE,
    ImageGenerateArgs,
    anthropic_tool_definitions,
    neutral_tool_definitions,
    openai_tool_definitions,
    tool_catalog,
    validate_tool_args,
)
from app.config import get_settings
from app.observability.metrics import image_generation_errors_total


@pytest.fixture(autouse=True)
def _restore_settings_cache() -> Iterator[None]:
    """After each test re-read the (conftest-forced) env: a key-gate test that flips OPENAI_API_KEY
    via monkeypatch must not leak its cached Settings into the next test."""
    yield
    get_settings.cache_clear()


def _set_openai_key(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", value)
    get_settings.cache_clear()  # Settings are lru_cached — force a rebuild from the new env.


def _neutral_names(**kwargs: Any) -> set[str]:
    return {d["name"] for d in neutral_tool_definitions(**kwargs)}


def _anthropic_names(**kwargs: Any) -> set[str]:
    # anthropic defs carry the underscore wire name (image_generate); map back for readability.
    return {d["name"] for d in anthropic_tool_definitions(**kwargs)}


def _openai_names(**kwargs: Any) -> set[str]:
    return {d["name"] for d in openai_tool_definitions(**kwargs)}


# ============================ key-gate (§3) ============================
@pytest.mark.parametrize("empty", ["", "   ", "\t", "\n", "  \t\n "])
def test_image_generate_absent_when_openai_key_empty_or_whitespace(
    monkeypatch: pytest.MonkeyPatch, empty: str
) -> None:
    # ADR-058 §3: an empty / whitespace-only OPENAI_API_KEY (non-empty after strip() is the rule)
    # drops image.generate from EVERY offered set — the tool cannot be served without a key.
    _set_openai_key(monkeypatch, empty)
    assert TOOL_IMAGE_GENERATE not in _neutral_names()
    assert "image_generate" not in _anthropic_names()
    assert "image_generate" not in _openai_names()


def test_image_generate_present_when_openai_key_set(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_openai_key(monkeypatch, "sk-openai-abc123")
    assert TOOL_IMAGE_GENERATE in _neutral_names()
    assert "image_generate" in _anthropic_names()
    assert "image_generate" in _openai_names()


def test_key_gate_composes_with_include_client_side_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ADR-056: a temporary chat drops CLIENT-side tools; image.generate is GLOBAL server-side, so
    # the key-gate (satisfied) keeps it even with include_client_side=False.
    _set_openai_key(monkeypatch, "sk-openai-abc123")
    names = _neutral_names(include_client_side=False, include_server_side=False)
    assert TOOL_IMAGE_GENERATE in names
    # And with an empty key the same call drops it (the gates compose by logical AND).
    _set_openai_key(monkeypatch, "")
    assert TOOL_IMAGE_GENERATE not in _neutral_names(
        include_client_side=False, include_server_side=False
    )


def test_key_gate_independent_of_dialog_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    # ADR-058 §3: image.generate is NOT dialog-mode-gated (unlike quiz.generate). It is offered in
    # every mode as long as the key is set — e.g. dialog_mode="smart" keeps it.
    _set_openai_key(monkeypatch, "sk-openai-abc123")
    assert TOOL_IMAGE_GENERATE in _neutral_names(dialog_mode="smart")
    assert TOOL_IMAGE_GENERATE in _neutral_names(dialog_mode="study_learn")
    assert TOOL_IMAGE_GENERATE in _neutral_names(dialog_mode=None)


def test_tool_catalog_not_key_gated(monkeypatch: pytest.MonkeyPatch) -> None:
    # ADR-058 §3: the machine-readable /v1/tools catalog lists EVERY registered tool regardless of
    # the key-gate (the gate constrains only the LLM offered-set). image.generate is present even
    # with an empty OPENAI_API_KEY.
    _set_openai_key(monkeypatch, "")
    names = {t["name"] for t in tool_catalog()}
    assert TOOL_IMAGE_GENERATE in names
    entry = next(t for t in tool_catalog() if t["name"] == TOOL_IMAGE_GENERATE)
    assert entry["execution"] == "server"
    assert entry["mutating"] is True  # writes bytes + tariffed (ADR-058 §1/§4)


# ============================ ImageGenerateArgs schema (§3) ============================
def test_image_args_valid_full() -> None:
    out = validate_tool_args(
        TOOL_IMAGE_GENERATE,
        {"prompt": "a red bicycle on a beach", "size": "1024x1536", "quality": "high"},
    )
    assert out == {"prompt": "a red bicycle on a beach", "size": "1024x1536", "quality": "high"}


def test_image_args_valid_prompt_only_defaults_none() -> None:
    # size/quality are OPTIONAL → None (the instance default is resolved later from config).
    out = validate_tool_args(TOOL_IMAGE_GENERATE, {"prompt": "a cat"})
    assert out == {"prompt": "a cat", "size": None, "quality": None}


@pytest.mark.parametrize("bad_size", ["512x512", "1024X1024", "huge", ""])
def test_image_args_rejects_bad_size(bad_size: str) -> None:
    with pytest.raises(ValueError):
        validate_tool_args(TOOL_IMAGE_GENERATE, {"prompt": "x", "size": bad_size})


@pytest.mark.parametrize("bad_quality", ["ultra", "HIGH", "best", ""])
def test_image_args_rejects_bad_quality(bad_quality: str) -> None:
    with pytest.raises(ValueError):
        validate_tool_args(TOOL_IMAGE_GENERATE, {"prompt": "x", "quality": bad_quality})


def test_image_args_rejects_empty_prompt() -> None:
    with pytest.raises(ValueError):
        validate_tool_args(TOOL_IMAGE_GENERATE, {"prompt": ""})


def test_image_args_rejects_over_cap_prompt() -> None:
    over = "a" * (IMAGE_PROMPT_MAX_LENGTH + 1)
    with pytest.raises(ValueError):
        validate_tool_args(TOOL_IMAGE_GENERATE, {"prompt": over})
    # The cap boundary itself is accepted.
    ok = validate_tool_args(TOOL_IMAGE_GENERATE, {"prompt": "a" * IMAGE_PROMPT_MAX_LENGTH})
    assert len(ok["prompt"]) == IMAGE_PROMPT_MAX_LENGTH


def test_image_args_rejects_unknown_field() -> None:
    # extra='forbid': the model cannot smuggle e.g. an n / user_id / model override.
    with pytest.raises(ValueError):
        validate_tool_args(TOOL_IMAGE_GENERATE, {"prompt": "x", "n": 4})


def test_image_args_accepts_valid_size_quality_sets() -> None:
    # Both allowlists are honored end-to-end via the model.
    for size in ("1024x1024", "1024x1536", "1536x1024", "auto"):
        ImageGenerateArgs.model_validate({"prompt": "x", "size": size})
    for quality in ("low", "medium", "high", "auto"):
        ImageGenerateArgs.model_validate({"prompt": "x", "quality": quality})


# ============================ strict-set (§3) ============================
def test_image_generate_is_not_an_openai_strict_tool() -> None:
    # ADR-058 §3: image.generate's optional fields serialize to anyOf and its values are already
    # enforced by pydantic, so strict adds nothing — it is deliberately absent from the strict set
    # and its OpenAI schema is sent UNCHANGED (strict=False).
    assert TOOL_IMAGE_GENERATE not in _OPENAI_STRICT_TOOLS
    defs = {d["name"]: d for d in openai_tool_definitions()}
    assert defs["image_generate"]["strict"] is False


# ============================ degrade classification (§3) ============================
def _make_orchestrator() -> tuple[ChatOrchestrator, MagicMock, MagicMock]:
    """Build a ChatOrchestrator whose persistence deps are mocks (no DB on the degrade path).

    The degrade branch of ``_execute_image_generate_tool`` only calls ``repo.complete_tool_call`` /
    ``repo.add_step`` / ``audit.record`` (all awaited) and ``_fk_session_id`` — it never touches the
    session directly, so mocks fully isolate the classification + logging + metric code as a unit.
    """
    repo = MagicMock(complete_tool_call=AsyncMock(), add_step=AsyncMock())
    audit = MagicMock(record=AsyncMock())
    orch = ChatOrchestrator(
        session=MagicMock(),
        repo=repo,
        wallet=MagicMock(),
        byok=MagicMock(),
        audit=audit,
        anthropic_client=MagicMock(),
        site_tools=MagicMock(),
        preferences=MagicMock(),
        global_tools=MagicMock(),
        workspaces=MagicMock(),
    )
    return orch, repo, audit


async def _run_degrade(
    orch: ChatOrchestrator, error: ImageGenerationError, *, prompt: str
) -> tuple[list[Any], Any]:
    server_tools: list[Any] = []
    # ADR-058 §4 was revised mid-sprint (image debit is now mode-independent); the ``billing`` param
    # of ``_execute_image_generate_tool`` is being kept-or-dropped by that refactor. Pass the full
    # kwarg set but FILTER to the method's CURRENT signature so this degrade unit test is robust to
    # whether ``billing`` is still a parameter — the degrade path returns before any debit, so the
    # value is irrelevant to the assertions below.
    all_kwargs: dict[str, Any] = {
        "user_id": uuid.uuid4(),
        "session_id": uuid.uuid4(),
        "message_step_id": uuid.uuid4(),
        "tool_call_id": uuid.uuid4(),
        "args": {"prompt": prompt, "size": None, "quality": None},
        "provider_tool_use_id": "toolu_img01",
        "pregen": _ImagePregen(data=None, error=error),
        "billing": _BillingPlan(debit_credits=True, mark_trial=False),
        "temporary": False,
        "server_tools": server_tools,
        "images_acc": [],
    }
    params = inspect.signature(orch._execute_image_generate_tool).parameters
    kwargs = {k: v for k, v in all_kwargs.items() if k in params}
    result = await orch._execute_image_generate_tool(**kwargs)
    return server_tools, result


def _metric(result_label: str) -> float:
    return image_generation_errors_total.labels(result=result_label)._value.get()


@contextmanager
def _capture_orchestrator_logs() -> Iterator[list[logging.LogRecord]]:
    """Capture ``app.chat.orchestrator`` records via a handler attached DIRECTLY to that logger.

    Deliberately NOT pytest's ``caplog`` and hardened against TWO pieces of process-global logging
    state left by earlier tests in a full run:
    - ``migrations/env.py`` calls ``logging.config.fileConfig(...)`` (default
      ``disable_existing_loggers=True``) when the ``_migrated`` fixture runs alembic in-process — it
      sets ``app.chat.orchestrator``'s ``.disabled = True`` — after ANY DB-backed test the logger is
      silenced and ``logger.log()`` emits NOTHING (``isEnabledFor`` returns False). We flip
      ``.disabled`` back off for the capture window (and restore it). This is a TEST-ENV artifact
      (production never runs migrations in the serving process), not a code bug.
    - ``configure_logging`` (run at ``create_app``) resets the ROOT level/handlers, so we attach our
      OWN handler to the SPECIFIC logger and force its level rather than rely on root propagation.
    The result is deterministic in isolation AND after any integration test in the full suite.
    """
    logger = logging.getLogger("app.chat.orchestrator")
    records: list[logging.LogRecord] = []

    class _ListHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _ListHandler()
    orig_level = logger.level
    orig_disabled = logger.disabled
    logger.disabled = False  # undo alembic fileConfig's disable_existing_loggers side-effect
    logger.setLevel(logging.INFO)  # ensure INFO records are emitted to our handler
    logger.addHandler(handler)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(orig_level)
        logger.disabled = orig_disabled


_SECRET_PROMPT = "SECRET_IMG_PROMPT_TOKEN_a_forbidden_scene"


@pytest.mark.asyncio
async def test_content_policy_error_degrades_first_info_no_metric_bump_for_failed() -> None:
    # ADR-058 §3: ImageContentPolicyError (⊂ ImageGenerationError) is classified FIRST → code
    # content_policy, INFO log, content_policy metric; the generation_failed metric is untouched.
    # Metric asserts use DELTAS (Prometheus counters are process-global) so the full-suite order
    # cannot break them; logs are captured on the specific logger (immune to root-handler churn).
    orch, repo, _audit = _make_orchestrator()
    before_cp = _metric("content_policy")
    before_gf = _metric("generation_failed")
    with _capture_orchestrator_logs() as recs:
        server_tools, result = await _run_degrade(
            orch, ImageContentPolicyError("rejected"), prompt=_SECRET_PROMPT
        )
    # Turn survives (degrade, not a block): returns None, no ChatRunOut.
    assert result is None
    # serverTools records the errored image.generate with the content-free machine code.
    assert len(server_tools) == 1
    assert server_tools[0].tool_name == TOOL_IMAGE_GENERATE
    assert server_tools[0].status == "errored"
    assert server_tools[0].summary == "content_policy"
    # tool_result payload carries the content_policy code, never bytes/prompt.
    payload = repo.complete_tool_call.call_args.kwargs["result"]
    assert payload["error"]["code"] == "content_policy"
    # metric: content_policy +1, generation_failed unchanged (delta, not absolute).
    assert _metric("content_policy") == before_cp + 1
    assert _metric("generation_failed") == before_gf
    # The single record is INFO and names the content-policy event.
    assert len(recs) == 1
    assert recs[0].levelno == logging.INFO
    assert recs[0].getMessage() == "image_content_policy_refused"
    # TD-035: the prompt is NOWHERE in the log record (message or structured fields).
    _assert_no_prompt_leak(recs, payload)


@pytest.mark.asyncio
async def test_generation_error_degrades_warning_with_metric() -> None:
    # ADR-058 §3: the base ImageGenerationError → code image_generation_failed, WARNING log +
    # generation_failed metric; the content_policy metric is untouched.
    orch, repo, _audit = _make_orchestrator()
    before_cp = _metric("content_policy")
    before_gf = _metric("generation_failed")
    with _capture_orchestrator_logs() as recs:
        server_tools, result = await _run_degrade(
            orch, ImageGenerationError("boom"), prompt=_SECRET_PROMPT
        )
    assert result is None
    assert server_tools[0].status == "errored"
    assert server_tools[0].summary == "image_generation_failed"
    payload = repo.complete_tool_call.call_args.kwargs["result"]
    assert payload["error"]["code"] == "image_generation_failed"
    # metric: generation_failed +1, content_policy unchanged (delta, not absolute).
    assert _metric("generation_failed") == before_gf + 1
    assert _metric("content_policy") == before_cp
    assert len(recs) == 1
    assert recs[0].levelno == logging.WARNING
    assert recs[0].getMessage() == "image_generation_failed"
    # The structured fields carry the error CLASS + toolCallId + status, never the prompt.
    fields = getattr(recs[0], "extra_fields", {})
    assert fields.get("errorClass") == "ImageGenerationError"
    assert fields.get("status") == "errored"
    _assert_no_prompt_leak(recs, payload)


def _assert_no_prompt_leak(recs: list[logging.LogRecord], payload: dict[str, Any]) -> None:
    """TD-035: the prompt must never surface in the log records nor the tool_result payload."""
    for r in recs:
        assert _SECRET_PROMPT not in r.getMessage()
        fields = getattr(r, "extra_fields", {})
        assert _SECRET_PROMPT not in str(fields), f"prompt leaked into log fields: {fields}"
        for value in fields.values():
            assert _SECRET_PROMPT not in str(value)
    assert _SECRET_PROMPT not in str(payload)
