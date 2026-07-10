"""Unit: pure defensive parsing of an Adapty webhook payload (ADR-029 §3) + product-tier map.

These exercise ``app.billing_adapty.parser`` (no I/O) and ``Settings.adapty_product_tokens`` /
``_tier_for`` (pure config). The parser must NEVER raise on a missing / wrongly-typed field and
must recognise the documented alternative key names (id, profile.customer_user_id, user_id,
event_properties.product_id, profile.expires_at). expires_at must accept both the trailing-'Z'
and the explicit-offset ISO8601 form, and degrade an unparseable value to ``None`` (event still
applied).
"""

from __future__ import annotations

import datetime
import uuid

import pytest

from app.billing_adapty import parser
from app.billing_adapty.service import AdaptyWebhookService
from app.config import Settings

_UID = "11111111-1111-1111-1111-111111111111"


# --------------------------- event_id ---------------------------


def test_parse_event_id_primary_key() -> None:
    assert parser.parse_event_id({"event_id": "evt-1"}) == "evt-1"


def test_parse_event_id_alternative_id_key() -> None:
    # Adapty may send the id under "id" on some payload versions.
    assert parser.parse_event_id({"id": "evt-alt"}) == "evt-alt"


def test_parse_event_id_prefers_event_id_over_id() -> None:
    assert parser.parse_event_id({"event_id": "primary", "id": "secondary"}) == "primary"


def test_parse_event_id_missing_returns_none() -> None:
    assert parser.parse_event_id({}) is None


def test_parse_event_id_empty_string_is_none() -> None:
    assert parser.parse_event_id({"event_id": "", "id": ""}) is None


def test_parse_event_id_numeric_is_coerced_to_str() -> None:
    # ADR-047: id-like fields may arrive as a bare int -> coerced to str (was None before).
    assert parser.parse_event_id({"event_id": 123}) == "123"


def test_parse_event_id_list_value_is_none() -> None:
    # A non-string / non-int value (list) is still ignored -> None.
    assert parser.parse_event_id({"event_id": [1, 2]}) is None


# --------------------------- event_type ---------------------------


def test_parse_event_type_lowercased() -> None:
    assert parser.parse_event_type({"event_type": "SUBSCRIPTION_STARTED"}) == "subscription_started"


def test_parse_event_type_missing_returns_empty() -> None:
    assert parser.parse_event_type({}) == ""


def test_parse_event_type_numeric_is_coerced_and_lowered() -> None:
    # ADR-047: the shared _first_str helper now coerces a bare int; event_type=5 -> "5"
    # (harmless: "5" is not in KNOWN_EVENTS so it still echoes as ignored).
    assert parser.parse_event_type({"event_type": 5}) == "5"


def test_parse_event_type_list_value_returns_empty() -> None:
    assert parser.parse_event_type({"event_type": [1]}) == ""


# --------------------------- customer_user_id ---------------------------


def test_parse_customer_user_id_top_level() -> None:
    assert parser.parse_customer_user_id({"customer_user_id": _UID}) == uuid.UUID(_UID)


def test_parse_customer_user_id_from_profile() -> None:
    body = {"profile": {"customer_user_id": _UID}}
    assert parser.parse_customer_user_id(body) == uuid.UUID(_UID)


def test_parse_customer_user_id_from_user_id_alias() -> None:
    assert parser.parse_customer_user_id({"user_id": _UID}) == uuid.UUID(_UID)


def test_parse_customer_user_id_source_priority() -> None:
    # top-level customer_user_id wins over profile / user_id.
    body = {
        "customer_user_id": _UID,
        "profile": {"customer_user_id": "22222222-2222-2222-2222-222222222222"},
        "user_id": "33333333-3333-3333-3333-333333333333",
    }
    assert parser.parse_customer_user_id(body) == uuid.UUID(_UID)


def test_parse_customer_user_id_missing_returns_none() -> None:
    assert parser.parse_customer_user_id({}) is None


def test_parse_customer_user_id_non_uuid_returns_none() -> None:
    assert parser.parse_customer_user_id({"customer_user_id": "not-a-uuid"}) is None


def test_parse_customer_user_id_profile_not_a_dict_is_safe() -> None:
    # profile arriving as a non-dict must not raise.
    assert parser.parse_customer_user_id({"profile": "oops", "user_id": _UID}) == uuid.UUID(_UID)


# --------------------------- vendor_product_id ---------------------------


def test_parse_vendor_product_id_from_event_properties() -> None:
    body = {"event_properties": {"vendor_product_id": "pro_monthly"}}
    assert parser.parse_vendor_product_id(body) == "pro_monthly"


def test_parse_vendor_product_id_from_product_id_alias() -> None:
    body = {"event_properties": {"product_id": "pro_yearly"}}
    assert parser.parse_vendor_product_id(body) == "pro_yearly"


def test_parse_vendor_product_id_top_level_fallback() -> None:
    assert parser.parse_vendor_product_id({"vendor_product_id": "pro_top"}) == "pro_top"


def test_parse_vendor_product_id_missing_returns_none() -> None:
    assert parser.parse_vendor_product_id({}) is None


# --------------------------- expires_at ---------------------------


def test_parse_expires_at_trailing_z() -> None:
    body = {"event_properties": {"expires_at": "2026-07-12T00:00:00Z"}}
    parsed = parser.parse_expires_at(body)
    assert parsed == datetime.datetime(2026, 7, 12, 0, 0, tzinfo=datetime.UTC)


def test_parse_expires_at_explicit_offset() -> None:
    body = {"event_properties": {"expires_at": "2026-07-12T03:00:00+03:00"}}
    parsed = parser.parse_expires_at(body)
    assert parsed is not None
    # 03:00 +03:00 == 00:00 UTC.
    assert parsed.astimezone(datetime.UTC) == datetime.datetime(
        2026, 7, 12, 0, 0, tzinfo=datetime.UTC
    )


def test_parse_expires_at_naive_assumed_utc() -> None:
    body = {"event_properties": {"expires_at": "2026-07-12T00:00:00"}}
    parsed = parser.parse_expires_at(body)
    assert parsed == datetime.datetime(2026, 7, 12, 0, 0, tzinfo=datetime.UTC)


def test_parse_expires_at_from_profile_fallback() -> None:
    body = {"profile": {"expires_at": "2026-07-12T00:00:00Z"}}
    assert parser.parse_expires_at(body) is not None


def test_parse_expires_at_unparseable_returns_none() -> None:
    body = {"event_properties": {"expires_at": "tomorrow-ish"}}
    assert parser.parse_expires_at(body) is None


def test_parse_expires_at_missing_returns_none() -> None:
    assert parser.parse_expires_at({}) is None


# --------------------------- known-event classification ---------------------------


def test_known_events_set_contents() -> None:
    # ADR-047 reworked the event sets: trial_started joins GRANTING, *_renewal_cancelled form NOOP,
    # and access_level_updated is the conditional event. KNOWN_EVENTS is their union.
    assert set(parser.KNOWN_EVENTS) == {
        "trial_started",
        "subscription_started",
        "subscription_renewed",
        "subscription_cancelled",
        "subscription_expired",
        "subscription_renewal_cancelled",
        "trial_renewal_cancelled",
        "access_level_updated",
    }
    assert set(parser.GRANTING_EVENTS) == {
        "trial_started",
        "subscription_started",
        "subscription_renewed",
    }
    assert set(parser.EXPIRING_EVENTS) == {"subscription_cancelled", "subscription_expired"}
    assert set(parser.NOOP_EVENTS) == {
        "subscription_renewal_cancelled",
        "trial_renewal_cancelled",
    }
    assert set(parser.CONDITIONAL_EVENTS) == {"access_level_updated"}


# --------------------------- product-tier resolution (_tier_for) ---------------------------


def _settings(product_tokens: str = "{}", grant: int = 1000) -> Settings:
    return Settings(
        ADAPTY_WEBHOOK_SECRET="x",
        ADAPTY_PRODUCT_TOKENS=product_tokens,
        ADAPTY_SUBSCRIPTION_TOKENS_GRANT=grant,
    )


def _service(settings: Settings) -> AdaptyWebhookService:
    # _tier_for only touches settings; the session/wallet/audit deps are unused for it.
    return AdaptyWebhookService(None, None, None, settings)  # type: ignore[arg-type]


def test_tier_for_mapped_product_exact_number() -> None:
    svc = _service(_settings('{"pro_monthly": 5000}', grant=1000))
    assert svc._tier_for("pro_monthly") == 5000


def test_tier_for_unmapped_product_falls_back_to_grant() -> None:
    svc = _service(_settings('{"pro_monthly": 5000}', grant=1234))
    assert svc._tier_for("pro_yearly") == 1234


def test_tier_for_none_product_falls_back_to_grant() -> None:
    svc = _service(_settings("{}", grant=777))
    assert svc._tier_for(None) == 777


def test_adapty_product_tokens_malformed_json_is_empty_map() -> None:
    assert _settings("{not json").adapty_product_tokens() == {}


def test_adapty_product_tokens_excludes_bool_and_nonpositive() -> None:
    s = _settings('{"a": 10, "b": true, "c": 0, "d": -5, "e": "x"}')
    assert s.adapty_product_tokens() == {"a": 10}


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("subscription_started", True),
        ("subscription_renewed", True),
        ("subscription_cancelled", True),
        ("subscription_expired", True),
        ("subscription_paused", False),
        ("", False),
    ],
)
def test_event_recognition(raw: str, expected: bool) -> None:
    assert (raw in parser.KNOWN_EVENTS) == expected
