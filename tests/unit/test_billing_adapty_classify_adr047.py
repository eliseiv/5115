"""Unit: ADR-047 event classification + strict bool / int-id parsing (no I/O).

Exercises the pure parser additions of ADR-047:
- ``classify_event`` table (GRANTING / EXPIRING / NOOP), including the conditional
  ``access_level_updated`` resolved on ``is_active`` / ``access_level_id``;
- ``_first_bool`` strictness: ``1`` (int) / ``"true"`` (str) must NOT collapse to a premium grant
  (``parse_is_active`` -> None -> NOOP) — the CURRENT behavior is pinned by these tests;
- new id parsers accept a bare ``int`` and coerce to ``str``;
- new id-source ordering (``profile_event_id`` first; ``event_properties`` over flat).
"""

from __future__ import annotations

import datetime
import uuid

import pytest

from app.billing_adapty import parser
from app.billing_adapty.parser import ParsedEvent

_UID = uuid.UUID("11111111-1111-1111-1111-111111111111")


def _event(
    *,
    event_type: str,
    is_active: bool | None = None,
    access_level_id: str | None = None,
) -> ParsedEvent:
    return ParsedEvent(
        event_id="evt",
        event_type=event_type,
        customer_user_id=_UID,
        vendor_product_id="week_6.99_nottrial",
        expires_at=None,
        transaction_id="410003298316682",
        original_transaction_id=None,
        is_active=is_active,
        access_level_id=access_level_id,
        will_renew=None,
    )


# --------------------------- event constants (ADR-047 replaces ADR-029) ---------------------------


def test_event_constant_sets() -> None:
    assert set(parser.GRANTING_EVENTS) == {
        "trial_started",
        "subscription_started",
        "subscription_renewed",
    }
    assert set(parser.EXPIRING_EVENTS) == {"subscription_expired", "subscription_cancelled"}
    assert set(parser.NOOP_EVENTS) == {
        "subscription_renewal_cancelled",
        "trial_renewal_cancelled",
    }
    assert set(parser.CONDITIONAL_EVENTS) == {"access_level_updated"}
    assert set(parser.KNOWN_EVENTS) == (
        parser.GRANTING_EVENTS
        | parser.EXPIRING_EVENTS
        | parser.NOOP_EVENTS
        | parser.CONDITIONAL_EVENTS
    )


# --------------------------- classify_event table ---------------------------


@pytest.mark.parametrize(
    "event_type",
    ["trial_started", "subscription_started", "subscription_renewed"],
)
def test_classify_granting_events(event_type: str) -> None:
    assert parser.classify_event(_event(event_type=event_type)) == parser.SEM_GRANTING


@pytest.mark.parametrize("event_type", ["subscription_expired", "subscription_cancelled"])
def test_classify_expiring_events(event_type: str) -> None:
    assert parser.classify_event(_event(event_type=event_type)) == parser.SEM_EXPIRING


@pytest.mark.parametrize(
    "event_type", ["subscription_renewal_cancelled", "trial_renewal_cancelled"]
)
def test_classify_noop_events(event_type: str) -> None:
    assert parser.classify_event(_event(event_type=event_type)) == parser.SEM_NOOP


def test_classify_access_level_updated_premium_active_grants() -> None:
    e = _event(event_type="access_level_updated", is_active=True, access_level_id="premium")
    assert parser.classify_event(e) == parser.SEM_GRANTING


def test_classify_access_level_updated_inactive_expires() -> None:
    e = _event(event_type="access_level_updated", is_active=False, access_level_id="premium")
    assert parser.classify_event(e) == parser.SEM_EXPIRING


def test_classify_access_level_updated_active_non_premium_is_noop() -> None:
    e = _event(event_type="access_level_updated", is_active=True, access_level_id="basic")
    assert parser.classify_event(e) == parser.SEM_NOOP


def test_classify_access_level_updated_unknown_is_active_is_noop() -> None:
    # is_active not a real bool -> parse yields None -> NOOP (do NOT revoke access).
    e = _event(event_type="access_level_updated", is_active=None, access_level_id="premium")
    assert parser.classify_event(e) == parser.SEM_NOOP


# --------------------------- _first_bool / parse_is_active strictness ---------------------------


@pytest.mark.parametrize("raw_is_active", [1, "true", "True", 0, "false", "1"])
def test_parse_is_active_non_bool_is_none(raw_is_active: object) -> None:
    # ADR-047: only a real JSON boolean counts; 1 / "true" / "1" must NOT collapse to a bool.
    body = {"event_properties": {"is_active": raw_is_active}}
    assert parser.parse_is_active(body) is None


@pytest.mark.parametrize("raw_is_active", [True, False])
def test_parse_is_active_real_bool(raw_is_active: bool) -> None:
    body = {"event_properties": {"is_active": raw_is_active}}
    assert parser.parse_is_active(body) is raw_is_active


def test_is_active_int_one_does_not_premium_grant() -> None:
    # Pin the end-to-end consequence at the classify boundary: is_active=1(int) for a premium
    # access_level_updated must NOT grant (parse -> None -> NOOP).
    body = {
        "event_properties": {
            "is_active": 1,
            "access_level_id": "premium",
        }
    }
    parsed_is_active = parser.parse_is_active(body)
    assert parsed_is_active is None
    e = _event(
        event_type="access_level_updated",
        is_active=parsed_is_active,
        access_level_id="premium",
    )
    assert parser.classify_event(e) == parser.SEM_NOOP


@pytest.mark.parametrize("raw_will_renew", [1, "true", 0, "false"])
def test_parse_will_renew_non_bool_is_none(raw_will_renew: object) -> None:
    body = {"event_properties": {"will_renew": raw_will_renew}}
    assert parser.parse_will_renew(body) is None


def test_parse_will_renew_real_bool() -> None:
    assert parser.parse_will_renew({"event_properties": {"will_renew": False}}) is False


# --------------------------- profile_event_id / id parsing ---------------------------


def test_parse_event_id_prefers_profile_event_id() -> None:
    body = {"profile_event_id": "pe-1", "event_id": "legacy", "id": "legacy2"}
    assert parser.parse_event_id(body) == "pe-1"


def test_parse_event_id_numeric_profile_event_id_coerced_to_str() -> None:
    assert parser.parse_event_id({"profile_event_id": 410003298316682}) == "410003298316682"


def test_parse_event_id_falls_back_to_event_id_then_id() -> None:
    assert parser.parse_event_id({"event_id": "evt-legacy"}) == "evt-legacy"
    assert parser.parse_event_id({"id": "id-legacy"}) == "id-legacy"


def test_parse_event_id_from_event_properties() -> None:
    assert parser.parse_event_id({"event_properties": {"profile_event_id": "pe-ep"}}) == "pe-ep"


def test_parse_event_id_bool_is_rejected() -> None:
    # isinstance(True, int) is True in Python; a stray bool must NOT become the id "True".
    assert parser.parse_event_id({"profile_event_id": True}) is None


# --------------------------- transaction_id / original_transaction_id ---------------------------


def test_parse_transaction_id_numeric_coerced() -> None:
    body = {"event_properties": {"transaction_id": 410003298316682}}
    assert parser.parse_transaction_id(body) == "410003298316682"


def test_parse_transaction_id_top_level_fallback() -> None:
    assert parser.parse_transaction_id({"transaction_id": "txn-flat"}) == "txn-flat"


def test_parse_transaction_id_missing_is_none() -> None:
    assert parser.parse_transaction_id({}) is None


def test_parse_original_transaction_id_numeric_coerced() -> None:
    body = {"event_properties": {"original_transaction_id": 99887766}}
    assert parser.parse_original_transaction_id(body) == "99887766"


def test_parse_original_transaction_id_missing_is_none() -> None:
    assert parser.parse_original_transaction_id({}) is None


# --------------------------- access_level_id / customer_user_id / expires_at additions ---------


def test_parse_access_level_id_from_ep_then_flat() -> None:
    assert parser.parse_access_level_id({"event_properties": {"access_level_id": "premium"}}) == (
        "premium"
    )
    assert parser.parse_access_level_id({"access_level_id": "flat"}) == "flat"


def test_parse_customer_user_id_from_event_properties() -> None:
    body = {"event_properties": {"customer_user_id": str(_UID)}}
    assert parser.parse_customer_user_id(body) == _UID


def test_parse_expires_at_prefers_subscription_expires_at() -> None:
    body = {
        "event_properties": {
            "subscription_expires_at": "2026-07-07T00:00:00Z",
            "expires_at": "2030-01-01T00:00:00Z",
        }
    }
    parsed = parser.parse_expires_at(body)
    assert parsed == datetime.datetime(2026, 7, 7, 0, 0, tzinfo=datetime.UTC)


def test_parse_expires_at_top_level_subscription_expires_at() -> None:
    body = {"subscription_expires_at": "2026-07-07T00:00:00Z"}
    parsed = parser.parse_expires_at(body)
    assert parsed == datetime.datetime(2026, 7, 7, 0, 0, tzinfo=datetime.UTC)
