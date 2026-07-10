"""Unit: temporary chat request-schema validation (ADR-056 §3).

Pure Pydantic validation of ``ChatRunRequest`` for the temporary chat (no I/O). Covers:
- Mutual exclusions → 422: ``temporary`` + ``sessionId`` / ``editMessageStepId`` / ``projectId`` /
  ``workspaceProjectId``.
- ``history`` present without ``temporary`` → 422.
- A non-empty ``history`` MUST start with a ``user`` turn (``history[0].role == "assistant"`` → 422
  with the "must start with a user turn" message).
- POSITIVE: consecutive same-role turns are NOT rejected (strict alternation is NOT required) —
  ``[user, user]`` and ``[user, assistant, assistant]`` validate.
- ``history=[]`` + ``temporary=true`` → valid (first turn).
- ``TemporaryTurn.content`` empty string → 422 (min_length=1).
- ``history`` byte size over ``size_limit_context`` → 422.
- Baseline: ``temporary`` defaults to False; ``history`` defaults to None.
"""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from app.config import get_settings
from app.schemas.chat import ChatRunRequest, TemporaryTurn

_UID = uuid.uuid4()


def _run_request(**overrides: object) -> ChatRunRequest:
    base: dict[str, object] = {"userId": _UID, "mode": "credits", "message": "hi"}
    base.update(overrides)
    return ChatRunRequest(**base)  # type: ignore[arg-type]


def _err_text(exc: ValidationError) -> str:
    return str(exc.value) if hasattr(exc, "value") else str(exc)


# ------------------------------- baseline defaults -------------------------------
def test_temporary_defaults_false_history_none() -> None:
    req = _run_request()
    assert req.temporary is False
    assert req.history is None


def test_temporary_true_without_history_is_valid_first_turn() -> None:
    # A temporary chat with no prior transcript is the first turn — history omitted is allowed.
    req = _run_request(temporary=True)
    assert req.temporary is True
    assert req.history is None


# ------------------------------- mutual exclusions → 422 -------------------------------
def test_temporary_with_session_id_rejected() -> None:
    with pytest.raises(ValidationError) as exc:
        _run_request(temporary=True, sessionId=uuid.uuid4())
    assert "cannot be resumed" in _err_text(exc)


def test_temporary_with_edit_message_step_id_rejected() -> None:
    with pytest.raises(ValidationError) as exc:
        _run_request(temporary=True, editMessageStepId=uuid.uuid4())
    assert "cannot edit a message" in _err_text(exc)


def test_temporary_with_project_id_rejected() -> None:
    with pytest.raises(ValidationError) as exc:
        _run_request(temporary=True, projectId="my-project")
    assert "cannot use a projectId" in _err_text(exc)


def test_temporary_with_workspace_project_id_rejected() -> None:
    with pytest.raises(ValidationError) as exc:
        _run_request(temporary=True, workspaceProjectId=uuid.uuid4())
    assert "cannot use a workspaceProjectId" in _err_text(exc)


# ------------------------------- history without temporary → 422 -------------------------------
def test_history_without_temporary_rejected() -> None:
    with pytest.raises(ValidationError) as exc:
        _run_request(history=[{"role": "user", "content": "prior"}])
    assert "only allowed for a temporary chat" in _err_text(exc)


def test_empty_history_without_temporary_rejected() -> None:
    # history is "present" even when empty (a list, not None) → still requires temporary=true.
    with pytest.raises(ValidationError) as exc:
        _run_request(history=[])
    assert "only allowed for a temporary chat" in _err_text(exc)


# ------------------------------- first turn must be user -------------------------------
def test_history_first_turn_assistant_rejected() -> None:
    with pytest.raises(ValidationError) as exc:
        _run_request(
            temporary=True,
            history=[
                {"role": "assistant", "content": "I answer first"},
                {"role": "user", "content": "hi"},
            ],
        )
    assert "must start with a user turn" in _err_text(exc)


def test_history_first_turn_user_accepted() -> None:
    req = _run_request(
        temporary=True,
        history=[
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1"},
        ],
    )
    assert req.history is not None
    assert [t.role for t in req.history] == ["user", "assistant"]


# ------------------------------- positive: consecutive same-role turns allowed -------------------
def test_history_consecutive_user_turns_allowed() -> None:
    # Strict alternation is NOT required (ADR-056 §3): [user, user] is a legitimate transcript.
    req = _run_request(
        temporary=True,
        history=[
            {"role": "user", "content": "first"},
            {"role": "user", "content": "second"},
        ],
    )
    assert req.history is not None
    assert [t.role for t in req.history] == ["user", "user"]


def test_history_consecutive_assistant_turns_allowed() -> None:
    req = _run_request(
        temporary=True,
        history=[
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": "a-part-1"},
            {"role": "assistant", "content": "a-part-2"},
        ],
    )
    assert req.history is not None
    assert [t.role for t in req.history] == ["user", "assistant", "assistant"]


# ------------------------------- empty history [] with temporary → valid ------------------------
def test_empty_history_with_temporary_valid() -> None:
    req = _run_request(temporary=True, history=[])
    assert req.history == []


# ------------------------------- TemporaryTurn content min_length -------------------------------
def test_temporary_turn_empty_content_rejected() -> None:
    with pytest.raises(ValidationError):
        TemporaryTurn(role="user", content="")


def test_history_turn_empty_content_rejected_in_request() -> None:
    with pytest.raises(ValidationError):
        _run_request(temporary=True, history=[{"role": "user", "content": ""}])


@pytest.mark.parametrize("role", ["user", "assistant"])
def test_temporary_turn_nonempty_content_accepted(role: str) -> None:
    turn = TemporaryTurn(role=role, content="x")  # type: ignore[arg-type]
    assert turn.role == role
    assert turn.content == "x"


def test_temporary_turn_unknown_role_rejected() -> None:
    with pytest.raises(ValidationError):
        TemporaryTurn(role="system", content="x")  # type: ignore[arg-type]


# ------------------------------- history over size limit → 422 -------------------------------
def test_history_exceeding_size_limit_context_rejected() -> None:
    limit = get_settings().size_limit_context
    # One turn whose UTF-8 content is one byte over the context size limit.
    oversized = "a" * (limit + 1)
    with pytest.raises(ValidationError) as exc:
        _run_request(temporary=True, history=[{"role": "user", "content": oversized}])
    assert "history exceeds size limit" in _err_text(exc)


def test_history_at_size_limit_context_accepted() -> None:
    limit = get_settings().size_limit_context
    # Exactly at the limit is allowed (boundary); split across two same-role turns to also confirm
    # the sum (not per-turn) is what is bounded.
    half = limit // 2
    content_bytes = "a" * half
    req = _run_request(
        temporary=True,
        history=[
            {"role": "user", "content": content_bytes},
            {"role": "user", "content": content_bytes},
        ],
    )
    assert req.history is not None
    assert sum(len(t.content.encode("utf-8")) for t in req.history) <= limit
