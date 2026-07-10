"""Unit: dialogMode / defaultDialogMode schema validation (ADR-055 §6, §3).

Pure Pydantic validation (no I/O). Covers:
- ``ChatRunRequest.dialogMode`` is a free ``str | None`` (NOT a Literal): a blank / whitespace-only
  value → 422 (ValidationError, symmetric to ``model``); an arbitrary NON-empty string passes the
  SCHEMA (membership + provider-gate are the orchestrator's job → the machine code
  ``unsupported_dialog_mode``, not a generic 422); an absent field → None.
- ``PreferencesPatchRequest`` / ``PreferencesResponse.defaultDialogMode`` validate ENUM membership
  (Literal): a value outside the set → 422; a valid value is accepted.
"""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from app.schemas.chat import ChatRunRequest
from app.schemas.preferences import PreferencesPatchRequest, PreferencesResponse

_UID = uuid.uuid4()


def _run_request(**overrides: object) -> ChatRunRequest:
    base: dict[str, object] = {"userId": _UID, "mode": "credits", "message": "hi"}
    base.update(overrides)
    return ChatRunRequest(**base)  # type: ignore[arg-type]


# ------------------------------- ChatRunRequest.dialogMode -------------------------------
def test_dialog_mode_absent_is_none() -> None:
    assert _run_request().dialogMode is None


def test_dialog_mode_empty_string_rejected() -> None:
    with pytest.raises(ValidationError):
        _run_request(dialogMode="")


def test_dialog_mode_whitespace_only_rejected() -> None:
    with pytest.raises(ValidationError):
        _run_request(dialogMode="   ")


@pytest.mark.parametrize("value", ["smart", "deep_thinking", "study_learn", "search"])
def test_dialog_mode_known_values_pass_schema(value: str) -> None:
    assert _run_request(dialogMode=value).dialogMode == value


def test_dialog_mode_arbitrary_nonempty_string_passes_schema() -> None:
    # NOT a Literal: an unknown non-empty value must pass the schema so the orchestrator can reject
    # it with the dedicated machine code (unsupported_dialog_mode), not a generic Pydantic 422.
    assert _run_request(dialogMode="bogus").dialogMode == "bogus"


def test_dialog_mode_value_is_not_stripped_by_schema() -> None:
    # The schema only rejects blank; a non-blank value is forwarded verbatim (membership check owns
    # normalization semantics downstream). Leading/trailing content around real text stays intact.
    assert _run_request(dialogMode="search").dialogMode == "search"


# ------------------------------- PreferencesPatchRequest -------------------------------
@pytest.mark.parametrize("value", ["smart", "deep_thinking", "study_learn", "search"])
def test_patch_default_dialog_mode_valid_accepted(value: str) -> None:
    assert PreferencesPatchRequest(defaultDialogMode=value).defaultDialogMode == value  # type: ignore[arg-type]


@pytest.mark.parametrize("value", ["bogus", "", "SMART", "deep-thinking"])
def test_patch_default_dialog_mode_invalid_rejected(value: str) -> None:
    with pytest.raises(ValidationError):
        PreferencesPatchRequest(defaultDialogMode=value)  # type: ignore[arg-type]


def test_patch_default_dialog_mode_absent_is_none() -> None:
    # Membership is only checked when present; absent → None (partial update semantics), but at
    # least one field must be present, so pair it with another field.
    assert PreferencesPatchRequest(notificationsEnabled=True).defaultDialogMode is None


# ------------------------------- PreferencesResponse -------------------------------
@pytest.mark.parametrize("value", ["smart", "deep_thinking", "study_learn", "search"])
def test_response_default_dialog_mode_valid_accepted(value: str) -> None:
    resp = PreferencesResponse(
        defaultAssistantMode="chat",
        defaultDialogMode=value,  # type: ignore[arg-type]
        notificationsEnabled=False,
        codeDefaults={},
    )
    assert resp.defaultDialogMode == value


def test_response_default_dialog_mode_invalid_rejected() -> None:
    with pytest.raises(ValidationError):
        PreferencesResponse(
            defaultAssistantMode="chat",
            defaultDialogMode="bogus",  # type: ignore[arg-type]
            notificationsEnabled=False,
            codeDefaults={},
        )
