"""Unit: AdminGrantRequest schema validation (admin/09-testing.md).

amount > 0, non-empty reason, bounded idempotencyKey, extra='forbid'.
"""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from app.schemas.admin import AdminGrantRequest


def _base() -> dict[str, object]:
    return {
        "userId": str(uuid.uuid4()),
        "amount": 10,
        "idempotencyKey": "k-1",
        "reason": "support compensation",
    }


def test_valid_grant() -> None:
    req = AdminGrantRequest.model_validate(_base())
    assert req.amount == 10
    assert req.reason == "support compensation"


@pytest.mark.parametrize("amount", [0, -1, -100])
def test_nonpositive_amount_rejected(amount: int) -> None:
    payload = _base() | {"amount": amount}
    with pytest.raises(ValidationError):
        AdminGrantRequest.model_validate(payload)


def test_empty_reason_rejected() -> None:
    payload = _base() | {"reason": ""}
    with pytest.raises(ValidationError):
        AdminGrantRequest.model_validate(payload)


def test_missing_reason_rejected() -> None:
    payload = _base()
    del payload["reason"]
    with pytest.raises(ValidationError):
        AdminGrantRequest.model_validate(payload)


def test_empty_idempotency_key_rejected() -> None:
    payload = _base() | {"idempotencyKey": ""}
    with pytest.raises(ValidationError):
        AdminGrantRequest.model_validate(payload)


def test_extra_field_rejected() -> None:
    payload = _base() | {"adminToken": "leak"}
    with pytest.raises(ValidationError):
        AdminGrantRequest.model_validate(payload)
