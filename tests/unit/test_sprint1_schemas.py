"""Unit tests for Sprint-1 (figma-gap) request/response schema validation.

Pure Pydantic — no I/O. Covers contract-level validation that the API layer relies on
(chats/profile/preferences/02-api-contracts.md): length limits, extra='forbid', at-least-one
field, codeDefaults size + secret rejection, empty-string displayName normalization.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.chats import ChatPatchRequest
from app.schemas.preferences import PreferencesPatchRequest
from app.schemas.profile import ProfileUpdateRequest


# --------------------------- ChatPatchRequest ---------------------------
def test_chat_patch_title_over_200_rejected() -> None:
    with pytest.raises(ValidationError):
        ChatPatchRequest(title="x" * 201)


def test_chat_patch_title_at_200_ok() -> None:
    req = ChatPatchRequest(title="x" * 200)
    assert req.title is not None and len(req.title) == 200


def test_chat_patch_extra_forbidden() -> None:
    with pytest.raises(ValidationError):
        ChatPatchRequest(title="ok", bogus="nope")  # type: ignore[call-arg]


def test_chat_patch_requires_at_least_one_field() -> None:
    # Neither title (field set) nor isPinned → rejected (chats/02).
    with pytest.raises(ValidationError):
        ChatPatchRequest()


def test_chat_patch_pin_only_ok() -> None:
    req = ChatPatchRequest(isPinned=True)
    assert req.isPinned is True
    assert "title" not in req.model_fields_set


def test_chat_patch_explicit_null_title_is_a_set_field() -> None:
    # An explicit null title counts as a provided field (model_fields_set) → accepted as
    # "clear the title"; the validator only rejects when NO field at all was provided.
    req = ChatPatchRequest(title=None)
    assert "title" in req.model_fields_set
    assert req.title is None


# --------------------------- ProfileUpdateRequest ---------------------------
def test_profile_display_name_over_80_rejected() -> None:
    with pytest.raises(ValidationError):
        ProfileUpdateRequest(displayName="n" * 81)


def test_profile_display_name_empty_normalizes_to_none() -> None:
    assert ProfileUpdateRequest(displayName="").normalized() is None
    assert ProfileUpdateRequest(displayName="   ").normalized() is None


def test_profile_display_name_trimmed() -> None:
    assert ProfileUpdateRequest(displayName="  Alice  ").normalized() == "Alice"


def test_profile_extra_forbidden() -> None:
    with pytest.raises(ValidationError):
        ProfileUpdateRequest(displayName="ok", extra="x")  # type: ignore[call-arg]


# --------------------------- PreferencesPatchRequest ---------------------------
def test_preferences_patch_requires_at_least_one_field() -> None:
    with pytest.raises(ValidationError):
        PreferencesPatchRequest()


def test_preferences_invalid_assistant_mode_rejected() -> None:
    with pytest.raises(ValidationError):
        PreferencesPatchRequest(defaultAssistantMode="banana")  # type: ignore[arg-type]


def test_preferences_partial_patch_ok() -> None:
    req = PreferencesPatchRequest(notificationsEnabled=False)
    assert req.notificationsEnabled is False
    assert req.defaultAssistantMode is None
    assert req.codeDefaults is None


def test_preferences_code_defaults_over_limit_rejected() -> None:
    big = {"k": "v" * (9 * 1024)}  # > 8KB serialized
    with pytest.raises(ValidationError):
        PreferencesPatchRequest(codeDefaults=big)


def test_preferences_code_defaults_with_secret_key_rejected() -> None:
    with pytest.raises(ValidationError):
        PreferencesPatchRequest(codeDefaults={"apiKey": "sk-ant-xxx"})
    with pytest.raises(ValidationError):
        PreferencesPatchRequest(codeDefaults={"nested": {"password": "p"}})


def test_preferences_code_defaults_benign_ok() -> None:
    req = PreferencesPatchRequest(codeDefaults={"language": "python", "indent": 4})
    assert req.codeDefaults == {"language": "python", "indent": 4}
