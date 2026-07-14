"""Unit tests for ADR-065 — the `actionPrompt` schema validator and its size limit.

The HTTP layer redacts a `ValueError` from `ChatRunRequest._check_sizes` into a generic 422
envelope, so the EXACT normative messages (§5.2 / §5.3) are asserted here against the model
directly; the integration suite asserts the resulting 422 status + no upstream call.

Covers the normative check ORDER of ADR-065 §5 п.0: size-guard (on the RAW value, before strip) →
lenient-drop → turn-validity. The decisive case is «whitespace-only, over the limit»: the guard runs
first, so the verdict is "actionPrompt exceeds size limit", NOT «empty turn».
"""

from __future__ import annotations

import uuid

import pydantic
import pytest

from app.config import get_settings
from app.schemas.chat import ChatRunRequest, ChatToolResultRequest

_LIMIT = 16 * 1024


def _run(**overrides: object) -> ChatRunRequest:
    body: dict[str, object] = {"userId": str(uuid.uuid4()), "mode": "credits"}
    body.update(overrides)
    return ChatRunRequest.model_validate(body)


# ============================ §6: the limit itself ============================
def test_size_limit_action_prompt_default_is_16kb() -> None:
    """ADR-065 §6: the new config defaults to 16 KB (half of SIZE_LIMIT_MESSAGE = 32 KB)."""
    settings = get_settings()
    assert settings.size_limit_action_prompt == _LIMIT
    assert settings.size_limit_action_prompt * 2 == settings.size_limit_message


# ============================ §5.2: turn-validity rule ============================
def test_empty_message_with_action_prompt_is_valid() -> None:
    """§5.2 consequence (product requirement): message="" + a non-empty actionPrompt is VALID."""
    req = _run(message="", actionPrompt="Объясни проще")
    assert req.message == ""
    assert req.actionPrompt == "Объясни проще"


def test_no_message_no_attachment_no_action_prompt_is_422_with_new_text() -> None:
    """§5.2 revises ADR-039 §1: the error text now names actionPrompt as a third content source."""
    with pytest.raises(
        pydantic.ValidationError,
        match=r"message, actionPrompt or at least one attachment is required",
    ):
        _run(message="")


@pytest.mark.parametrize("prompt", ["", "   ", "\n\t "])
def test_whitespace_only_action_prompt_alone_is_422_empty_turn(prompt: str) -> None:
    """§5.1 lenient-drop: a whitespace-only actionPrompt gives the turn NO content → §5.2 422."""
    with pytest.raises(
        pydantic.ValidationError,
        match=r"message, actionPrompt or at least one attachment is required",
    ):
        _run(message="", actionPrompt=prompt)


def test_whitespace_only_action_prompt_with_message_is_valid() -> None:
    """§5.1: a whitespace-only actionPrompt is NOT a 422 by itself — the message carries it."""
    req = _run(message="hi", actionPrompt="   ")
    assert req.actionPrompt == "   "  # kept verbatim on the model; dropped in the orchestrator


# ==================== §5.3 + §5 п.0: size-guard, RAW value, FIRST ====================
def test_oversize_action_prompt_is_422_exceeds_size_limit() -> None:
    with pytest.raises(pydantic.ValidationError, match=r"actionPrompt exceeds size limit"):
        _run(message="hi", actionPrompt="x" * (_LIMIT + 1))


def test_action_prompt_exactly_at_limit_is_valid() -> None:
    """The guard is `>` — exactly `size_limit_action_prompt` bytes passes."""
    req = _run(message="hi", actionPrompt="x" * _LIMIT)
    assert req.actionPrompt is not None and len(req.actionPrompt.encode("utf-8")) == _LIMIT


def test_limit_is_measured_in_bytes_not_characters() -> None:
    """Like SIZE_LIMIT_MESSAGE/CONTEXT the guard measures UTF-8 BYTES: 2-byte chars hit sooner."""
    prompt = "я" * (_LIMIT // 2 + 1)  # 2 bytes each → just over the byte limit, under it in chars
    assert len(prompt) < _LIMIT < len(prompt.encode("utf-8"))
    with pytest.raises(pydantic.ValidationError, match=r"actionPrompt exceeds size limit"):
        _run(message="hi", actionPrompt=prompt)


def test_whitespace_only_over_limit_is_size_limit_error_not_empty_turn() -> None:
    """§5 п.0 (the decisive ordering case): 17 KB of spaces → "actionPrompt exceeds size limit".

    The size-guard runs BEFORE the lenient-drop and measures the RAW (unstripped) value, so
    whitespace can never bypass the byte ceiling and the verdict never depends on invisible chars.
    A stripped-first implementation would have answered «empty turn» here.
    """
    with pytest.raises(pydantic.ValidationError, match=r"actionPrompt exceeds size limit"):
        _run(message="", actionPrompt=" " * (_LIMIT + 1))


def test_whitespace_only_over_limit_wins_over_empty_turn_even_with_no_other_content() -> None:
    """Same input as above — assert the OTHER message is NOT the one raised (order is normative)."""
    with pytest.raises(pydantic.ValidationError) as exc:
        _run(message="", actionPrompt="\n" * (_LIMIT + 1))
    text = str(exc.value)
    assert "actionPrompt exceeds size limit" in text
    assert "at least one attachment is required" not in text


# ============ §5.5: temporary-guard (temporary + actionPrompt needs a message) ============
@pytest.mark.parametrize("message", ["", "   \n\t "])
def test_temporary_with_action_prompt_and_no_message_is_422(message: str) -> None:
    """§5.5: temporary=true + a non-empty actionPrompt REQUIRES a non-empty message.

    A «mute» action turn cannot be represented in the client transcript of the next request
    (`TemporaryTurn.content` has min_length=1 and carries only VISIBLE text) → the temporary chat
    would become uncontinuable. Rejected on input with the normative text.
    """
    with pytest.raises(
        pydantic.ValidationError,
        match=r"temporary chat with actionPrompt requires a non-empty message",
    ):
        _run(message=message, actionPrompt="Объясни проще", temporary=True, history=[])


def test_temporary_with_action_prompt_and_attachments_but_no_message_is_422() -> None:
    """§5.5: attachments do NOT substitute for the message under the temporary-guard."""
    attachment = {
        "type": "text",
        "mediaType": "text/plain",
        "filename": "n.txt",
        "data": "Ym9keQ==",  # b64("body")
    }
    with pytest.raises(
        pydantic.ValidationError,
        match=r"temporary chat with actionPrompt requires a non-empty message",
    ):
        _run(
            message="",
            actionPrompt="Сократи",
            temporary=True,
            history=[],
            attachments=[attachment],
        )


def test_temporary_with_action_prompt_and_message_is_valid() -> None:
    """§5.5: the guard only bans the MUTE combination — with a visible message the turn is valid."""
    req = _run(message="сократи это", actionPrompt="Сократи", temporary=True, history=[])
    assert req.temporary is True
    assert req.actionPrompt == "Сократи"


def test_temporary_with_whitespace_only_action_prompt_and_message_is_valid() -> None:
    """§5 п.0 order: lenient-drop runs BEFORE the temp-guard → a dropped prompt cannot fire it."""
    req = _run(message="hi", actionPrompt="   ", temporary=True, history=[])
    assert req.temporary is True


def test_temporary_whitespace_only_action_prompt_over_limit_is_size_limit_not_temp_guard() -> None:
    """§5 п.0 order: the size-guard runs FIRST — before both the lenient-drop and the temp-guard."""
    with pytest.raises(pydantic.ValidationError) as exc:
        _run(message="", actionPrompt=" " * (_LIMIT + 1), temporary=True, history=[])
    text = str(exc.value)
    assert "actionPrompt exceeds size limit" in text
    assert "temporary chat with actionPrompt" not in text


def test_persistent_chat_with_empty_message_and_action_prompt_is_valid() -> None:
    """§5.5: the guard is scoped to temporary=true — the same combination is VALID persistently."""
    req = _run(message="", actionPrompt="Объясни проще")
    assert req.temporary is False
    assert req.actionPrompt == "Объясни проще"


# ============================ §10: backward compatibility ============================
def test_action_prompt_defaults_to_none() -> None:
    req = _run(message="hello")
    assert req.actionPrompt is None


def test_message_size_limit_still_enforced_alongside_action_prompt() -> None:
    """The pre-existing message guard is untouched by the new checks."""
    big = "x" * (get_settings().size_limit_message + 1)
    with pytest.raises(pydantic.ValidationError, match=r"message exceeds size limit"):
        _run(message=big, actionPrompt="p")


# ==================== §8.3: /chat/tool-result rejects the field ====================
def test_tool_result_request_rejects_action_prompt_extra_forbidden() -> None:
    """§8.3: ChatToolResultRequest is a StrictModel → an actionPrompt key is an unknown field."""
    with pytest.raises(pydantic.ValidationError) as exc:
        ChatToolResultRequest.model_validate(
            {
                "userId": str(uuid.uuid4()),
                "sessionId": str(uuid.uuid4()),
                "toolCallId": str(uuid.uuid4()),
                "result": {"ok": 1},
                "actionPrompt": "sneaky",
            }
        )
    errors = exc.value.errors()
    assert any(
        e["type"] == "extra_forbidden" and e["loc"] == ("actionPrompt",) for e in errors
    ), errors
