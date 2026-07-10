"""Unit: AdminSubscriptionGrantRequest schema validation (ADR-048 §1, admin/09-testing.md).

Exactly one of expiresAt/days; expiresAt tz-aware and strictly future; days>0; credits>=0;
bounded idempotencyKey; extra='forbid'. The model_validator encodes the lazy-expiry contract.
"""

from __future__ import annotations

import datetime
import uuid

import pytest
from pydantic import ValidationError

from app.schemas.admin import AdminSubscriptionGrantRequest


def _future() -> str:
    return (datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(days=10)).isoformat()


def _base_days() -> dict[str, object]:
    return {"userId": str(uuid.uuid4()), "days": 30, "idempotencyKey": "k-1"}


def _base_expires() -> dict[str, object]:
    return {"userId": str(uuid.uuid4()), "expiresAt": _future(), "idempotencyKey": "k-1"}


def test_valid_days() -> None:
    req = AdminSubscriptionGrantRequest.model_validate(_base_days())
    assert req.days == 30
    assert req.expiresAt is None
    assert req.plan == "manual_grant"  # default
    assert req.credits is None  # default -> resolved to SUBSCRIPTION_CREDITS_PER_PERIOD in service


def test_valid_expires() -> None:
    req = AdminSubscriptionGrantRequest.model_validate(_base_expires())
    assert req.expiresAt is not None
    assert req.days is None


def test_both_expires_and_days_rejected() -> None:
    payload = _base_days() | {"expiresAt": _future()}
    with pytest.raises(ValidationError):
        AdminSubscriptionGrantRequest.model_validate(payload)


def test_neither_expires_nor_days_rejected() -> None:
    payload = {"userId": str(uuid.uuid4()), "idempotencyKey": "k-1"}
    with pytest.raises(ValidationError):
        AdminSubscriptionGrantRequest.model_validate(payload)


def test_expires_naive_rejected() -> None:
    naive = (datetime.datetime.now() + datetime.timedelta(days=5)).replace(tzinfo=None).isoformat()
    payload = {"userId": str(uuid.uuid4()), "expiresAt": naive, "idempotencyKey": "k-1"}
    with pytest.raises(ValidationError):
        AdminSubscriptionGrantRequest.model_validate(payload)


def test_expires_in_past_rejected() -> None:
    past = (datetime.datetime.now(tz=datetime.UTC) - datetime.timedelta(days=1)).isoformat()
    payload = {"userId": str(uuid.uuid4()), "expiresAt": past, "idempotencyKey": "k-1"}
    with pytest.raises(ValidationError):
        AdminSubscriptionGrantRequest.model_validate(payload)


def test_expires_equal_now_rejected() -> None:
    # expiresAt == now() is not strictly future (<=) -> rejected. Use a just-past instant to make
    # the "<= now()" boundary deterministic without racing the validator's own now().
    now = (datetime.datetime.now(tz=datetime.UTC) - datetime.timedelta(milliseconds=1)).isoformat()
    payload = {"userId": str(uuid.uuid4()), "expiresAt": now, "idempotencyKey": "k-1"}
    with pytest.raises(ValidationError):
        AdminSubscriptionGrantRequest.model_validate(payload)


@pytest.mark.parametrize("days", [0, -1, -30])
def test_nonpositive_days_rejected(days: int) -> None:
    payload = {"userId": str(uuid.uuid4()), "days": days, "idempotencyKey": "k-1"}
    with pytest.raises(ValidationError):
        AdminSubscriptionGrantRequest.model_validate(payload)


@pytest.mark.parametrize("credits", [-1, -100])
def test_negative_credits_rejected(credits: int) -> None:
    payload = _base_days() | {"credits": credits}
    with pytest.raises(ValidationError):
        AdminSubscriptionGrantRequest.model_validate(payload)


def test_credits_zero_allowed() -> None:
    req = AdminSubscriptionGrantRequest.model_validate(_base_days() | {"credits": 0})
    assert req.credits == 0


def test_missing_user_id_rejected() -> None:
    payload = {"days": 30, "idempotencyKey": "k-1"}
    with pytest.raises(ValidationError):
        AdminSubscriptionGrantRequest.model_validate(payload)


def test_missing_idempotency_key_rejected() -> None:
    payload = {"userId": str(uuid.uuid4()), "days": 30}
    with pytest.raises(ValidationError):
        AdminSubscriptionGrantRequest.model_validate(payload)


def test_empty_idempotency_key_rejected() -> None:
    payload = _base_days() | {"idempotencyKey": ""}
    with pytest.raises(ValidationError):
        AdminSubscriptionGrantRequest.model_validate(payload)


def test_extra_field_rejected() -> None:
    payload = _base_days() | {"adminToken": "leak"}
    with pytest.raises(ValidationError):
        AdminSubscriptionGrantRequest.model_validate(payload)
