"""Unit: ChatRunRequest.model field validation (ADR-034 §3).

Pure, no I/O. The `model` field is optional (None ok). When PRESENT it must be a non-empty string
after strip — a blank/whitespace value is a 422 (symmetric to projectId). Allowlist membership is
NOT validated here (it needs settings.allowed_models() and runs in the orchestrator at session
creation); the schema only enforces presence/blank.
"""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from app.schemas.chat import ChatRunRequest

_UID = uuid.UUID("11111111-2222-3333-4444-555555555555")


def _run_payload(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {"userId": str(_UID), "message": "hi", "mode": "credits"}
    base.update(overrides)
    return base


def test_model_absent_is_valid_and_none() -> None:
    req = ChatRunRequest.model_validate(_run_payload())
    assert req.model is None


def test_model_present_valid_string() -> None:
    req = ChatRunRequest.model_validate(_run_payload(model="gpt-4o"))
    assert req.model == "gpt-4o"


def test_model_explicit_null_is_valid_and_none() -> None:
    req = ChatRunRequest.model_validate(_run_payload(model=None))
    assert req.model is None


@pytest.mark.parametrize("blank", ["", "   ", "\t", "\n", " \t \n "])
def test_model_blank_rejected_422(blank: str) -> None:
    # ADR-034 §3: present-but-blank model is rejected (not silently coerced to NULL/default).
    with pytest.raises(ValidationError, match="non-empty"):
        ChatRunRequest.model_validate(_run_payload(model=blank))


def test_model_not_validated_against_allowlist_at_schema_level() -> None:
    # The schema does NOT know the instance allowlist; an unknown id passes schema validation
    # (the 422 for an unavailable model is raised in the orchestrator at session creation).
    req = ChatRunRequest.model_validate(_run_payload(model="totally-unknown-model"))
    assert req.model == "totally-unknown-model"
