"""Unit: ADR-050 — pure CloudPayments-format parsing + product classification (no I/O).

Exercises the pure functions of ``app.billing_cloudpayments.parser``:
- ``classify_product`` deterministic order (token-map priority > interval > token-name >
  sub-name > unknown);
- defensive field parsers (``TransactionId`` / ``AccountId`` UPPER->lower UUID / ``Data`` as a
  JSON-string / ``billing_interval_count`` str->int / gate normalisation / strict bool);
- ``parse_amount`` reads the top-level ``Amount`` (audit-only — NEVER sizes a grant, proven at
  the service layer);
- ``sanitize_payload`` allowlist projection carries NO card data (PII by-design exclusion, §7).
"""

from __future__ import annotations

import uuid

import pytest

from app.billing_cloudpayments import parser
from app.billing_cloudpayments.parser import ParsedPayment

_UID_UPPER = "B284721F-C3E0-4446-B00F-3C6A21F32535"
_UID = uuid.UUID(_UID_UPPER.lower())
_TOKENS = frozenset({"100_tokens_9.99", "2000_Tokens_99.99"})


# --------------------------- classify_product (ADR-050 §3) ---------------------------


def test_classify_token_map_wins_over_interval() -> None:
    # Rule 1: an id present in the operator token map is tokens even if an interval is present.
    assert parser.classify_product("100_tokens_9.99", "year", _TOKENS) == parser.KIND_TOKENS


@pytest.mark.parametrize("unit", ["year", "month", "week", "day"])
def test_classify_interval_unit_is_subscription(unit: str) -> None:
    # Rule 2: a recurrence interval (not in the token map) => subscription.
    assert parser.classify_product("yearly_49.99_nottrial", unit, _TOKENS) == (
        parser.KIND_SUBSCRIPTION
    )


def test_classify_token_name_pattern_no_interval() -> None:
    # Rule 3: ^\d+_tokens by name (not in the map, no interval) => tokens (amount still map-only).
    assert parser.classify_product("999_tokens_pack", None, _TOKENS) == parser.KIND_TOKENS


@pytest.mark.parametrize(
    "product_id",
    ["yearly_49.99_nottrial", "week_6.99_nottrial", "monthly_trial", "some_year_plan"],
)
def test_classify_subscription_name_pattern(product_id: str) -> None:
    # Rule 4: subscription by name (interval keyword or _trial/_nottrial suffix), no interval field.
    assert parser.classify_product(product_id, None, _TOKENS) == parser.KIND_SUBSCRIPTION


@pytest.mark.parametrize("product_id", ["randomsku", "gift_card", "consumable_extra"])
def test_classify_unknown(product_id: str) -> None:
    # Rule 5: no signal at all => unknown (service maps to ignored/unknown_product, WARNING).
    assert parser.classify_product(product_id, None, _TOKENS) == parser.KIND_UNKNOWN


# --------------------------- Data JSON-string parsing ---------------------------


def test_parse_data_from_json_string() -> None:
    data = parser._parse_data({"Data": '{"product_id":"p1","billing_interval_unit":"year"}'})
    assert data == {"product_id": "p1", "billing_interval_unit": "year"}


def test_parse_data_accepts_already_dict() -> None:
    assert parser._parse_data({"Data": {"product_id": "p1"}}) == {"product_id": "p1"}


@pytest.mark.parametrize("bad", ["{not json", "[1,2,3]", '"a string"', "null", ""])
def test_parse_data_malformed_or_non_object_is_none(bad: str) -> None:
    assert parser._parse_data({"Data": bad}) is None


def test_parse_data_absent_is_none() -> None:
    assert parser._parse_data({}) is None


# --------------------------- userId (UPPER -> lower UUID) / None-guard ---------------------------


def test_parse_user_id_upper_account_id_normalised_lower() -> None:
    assert parser.parse_user_id({"AccountId": _UID_UPPER}, {}) == _UID


def test_parse_user_id_fallbacks_to_data_user_id() -> None:
    assert parser.parse_user_id({}, {"user_id": _UID_UPPER}) == _UID


def test_parse_user_id_both_absent_is_none() -> None:
    assert parser.parse_user_id({}, {}) is None


@pytest.mark.parametrize("account", ["", "not-a-uuid", "1234"])
def test_parse_user_id_non_uuid_is_none(account: str) -> None:
    assert parser.parse_user_id({"AccountId": account}, {}) is None


# --------------------------- transaction id / gate / interval count ---------------------------


def test_parse_transaction_id_present_and_absent() -> None:
    assert parser.parse_transaction_id({"TransactionId": "t-1"}) == "t-1"
    assert parser.parse_transaction_id({}) is None


@pytest.mark.parametrize(
    "status, op, expected",
    [
        ("Completed", "Payment", True),
        ("completed", "payment", True),
        ("Pending", "Payment", False),
        ("Completed", "Refund", False),
        ("Declined", "Payment", False),
    ],
)
def test_gate_case_insensitive(status: str, op: str, expected: bool) -> None:
    s = parser.parse_status({"Status": status})
    o = parser.parse_operation_type({"OperationType": op})
    assert parser.parse_gate(s, o) is expected


@pytest.mark.parametrize("raw, expected", [("1", 1), ("3", 3), ("0", 1), ("x", 1), (2, 2)])
def test_parse_billing_interval_count(raw: object, expected: int) -> None:
    assert parser.parse_billing_interval_count({"billing_interval_count": raw}) == expected


def test_parse_billing_interval_count_absent_defaults_one() -> None:
    assert parser.parse_billing_interval_count({}) == 1


# --------------------------- amount (audit-only) / test_mode strictness ---------------------------


def test_parse_amount_reads_top_level() -> None:
    assert parser.parse_amount({"Amount": 3990}) == 3990
    assert parser.parse_amount({"Amount": 3990.0}) == 3990


def test_parse_amount_absent_or_bool_is_none() -> None:
    assert parser.parse_amount({}) is None
    assert parser.parse_amount({"Amount": True}) is None


@pytest.mark.parametrize("raw", [1, "true", 0, "false"])
def test_parse_test_mode_non_bool_is_none(raw: object) -> None:
    assert parser.parse_test_mode({"TestMode": raw}) is None


def test_parse_test_mode_real_bool() -> None:
    assert parser.parse_test_mode({"TestMode": False}) is False


# --------------------------- sanitize_payload: NO card PII ---------------------------


def _parsed(**overrides: object) -> ParsedPayment:
    base: dict[str, object] = {
        "transaction_id": "t-1",
        "user_id": _UID,
        "product_id": "yearly_49.99_nottrial",
        "status": "completed",
        "operation_type": "payment",
        "billing_interval_unit": "year",
        "billing_interval_count": 1,
        "billing_phase": "regular",
        "subscription_id": "sub-1",
        "is_trial_initial": None,
        "is_trial_conversion": None,
        "is_initial_payment": None,
        "amount": 3990,
        "currency": "RUB",
        "test_mode": False,
        "kind": parser.KIND_SUBSCRIPTION,
    }
    base.update(overrides)
    return ParsedPayment(**base)  # type: ignore[arg-type]


def test_sanitize_payload_allowlist_only_no_card_fields() -> None:
    sanitized = parser.sanitize_payload(_parsed())
    assert set(sanitized) == {
        "transactionId",
        "productId",
        "kind",
        "status",
        "operationType",
        "amount",
        "currency",
        "testMode",
        "billingIntervalUnit",
        "billingIntervalCount",
        "billingPhase",
        "subscriptionId",
    }
    # PII by-design exclusion: no PAN fragment / issuer / card type keys or values.
    blob = str(sanitized)
    for forbidden in ("CardFirstSix", "CardLastFour", "Issuer", "CardType", "220024", "8808"):
        assert forbidden not in blob
