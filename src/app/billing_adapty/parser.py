"""Pure defensive parsing of an Adapty webhook payload (ADR-029 §3 / ADR-047, billing-adapty/03).

Adapty does not version its payload strictly: the same logical field can arrive under different
keys across SDK / payload versions. These functions read a best-effort value from the parsed JSON
object without ever raising on missing / wrongly-typed fields (nested access is ``isinstance``
guarded). The body is parsed manually (no Pydantic) so a malformed verification ping yields an
``ignored`` outcome rather than a 422 (which Adapty would retry forever).

ADR-047 reworks this parser to the REAL Adapty wire format: the per-event id is ``profile_event_id``
(not ``event_id``), business fields live primarily in ``event_properties`` (``ep``) with the flat
top-level keys kept as a fallback (Dashboard "flattened" view / older payloads), id-like fields can
arrive as a bare ``int`` (coerced to ``str``), and the event semantics are resolved through
``classify_event`` (GRANTING / EXPIRING / NOOP) rather than a flat frozenset membership — because
``access_level_updated`` is conditional on ``is_active`` / ``access_level_id``.
"""

from __future__ import annotations

import datetime
import uuid
from dataclasses import dataclass
from typing import Any

# Recognised, normalised (lower-case) Adapty event types (ADR-047 §B — replaces ADR-029 §4).
GRANTING_EVENTS = frozenset({"trial_started", "subscription_started", "subscription_renewed"})
EXPIRING_EVENTS = frozenset({"subscription_expired", "subscription_cancelled"})
NOOP_EVENTS = frozenset({"subscription_renewal_cancelled", "trial_renewal_cancelled"})
CONDITIONAL_EVENTS = frozenset({"access_level_updated"})
KNOWN_EVENTS = GRANTING_EVENTS | EXPIRING_EVENTS | NOOP_EVENTS | CONDITIONAL_EVENTS

# Event semantics (the output of ``classify_event``).
SEM_GRANTING = "granting"
SEM_EXPIRING = "expiring"
SEM_NOOP = "noop"

# The access level that counts as "premium access granted" for a conditional access_level_updated.
ACCESS_LEVEL_PREMIUM = "premium"


@dataclass(frozen=True)
class ParsedEvent:
    """A defensively parsed Adapty event. ``customer_user_id`` is already a validated UUID."""

    event_id: str
    event_type: str
    customer_user_id: uuid.UUID
    vendor_product_id: str | None
    expires_at: datetime.datetime | None
    transaction_id: str | None
    original_transaction_id: str | None
    is_active: bool | None
    access_level_id: str | None
    will_renew: bool | None


def _as_dict(value: Any) -> dict[str, Any]:
    """Return ``value`` if it is a dict, else an empty dict (safe nested access)."""
    return value if isinstance(value, dict) else {}


def _first_str(*candidates: Any) -> str | None:
    """First candidate that is a non-empty string, or a non-bool int coerced to str, else None.

    Adapty id-like fields (``profile_event_id`` / ``transaction_id`` / ``original_transaction_id``)
    can arrive as a bare integer (e.g. ``410003298316682`` without quotes). A ``bool`` is rejected
    explicitly (``isinstance(True, int)`` is True in Python) so a stray ``True``/``False`` never
    becomes the string ``"True"``.
    """
    for candidate in candidates:
        if isinstance(candidate, str) and candidate:
            return candidate
        if isinstance(candidate, int) and not isinstance(candidate, bool):
            return str(candidate)
    return None


def _first_bool(*candidates: Any) -> bool | None:
    """First candidate that is strictly a ``bool``, else None.

    Strict on purpose (ADR-047): ``1`` / ``0`` / ``"true"`` must NOT collapse to a bool — only a
    real JSON boolean counts, otherwise the field is treated as "not present".
    """
    for candidate in candidates:
        if isinstance(candidate, bool):
            return candidate
    return None


def parse_event_id(body: dict[str, Any]) -> str | None:
    """Per-event id: ``profile_event_id`` first (ADR-047), then ep / legacy fallbacks."""
    props = _as_dict(body.get("event_properties"))
    return _first_str(
        body.get("profile_event_id"),
        props.get("profile_event_id"),
        body.get("event_id"),
        body.get("id"),
    )


def parse_event_type(body: dict[str, Any]) -> str:
    """Event type, lower-cased; read defensively from several keys (wire-format unconfirmed)."""
    props = _as_dict(body.get("event_properties"))
    raw = _first_str(
        body.get("event_type"),
        body.get("event"),
        props.get("event_type"),
        body.get("type"),
    )
    return raw.lower() if raw is not None else ""


def parse_customer_user_id(body: dict[str, Any]) -> uuid.UUID | None:
    """customer_user_id (our userId UUID) from the documented source order (ADR-047 §A).

    Sources: ``customer_user_id`` -> ``profile.customer_user_id`` ->
    ``event_properties.customer_user_id`` -> ``user_id``. A value that is absent or not a valid UUID
    is treated as "user not found" by the caller (until iOS calls ``Adapty.identify`` the real
    payload only carries Adapty's ``profile_id``).
    """
    profile = _as_dict(body.get("profile"))
    props = _as_dict(body.get("event_properties"))
    raw = _first_str(
        body.get("customer_user_id"),
        profile.get("customer_user_id"),
        props.get("customer_user_id"),
        body.get("user_id"),
    )
    if raw is None:
        return None
    try:
        return uuid.UUID(raw)
    except ValueError:
        return None


def parse_vendor_product_id(body: dict[str, Any]) -> str | None:
    props = _as_dict(body.get("event_properties"))
    return _first_str(
        props.get("vendor_product_id"),
        props.get("product_id"),
        body.get("vendor_product_id"),
        body.get("product_id"),
    )


def parse_expires_at(body: dict[str, Any]) -> datetime.datetime | None:
    """ISO8601 ``expires_at`` -> tz-aware datetime; unparseable -> None (event still applied).

    ADR-047: ``subscription_expires_at`` (the real Adapty field) is tried first, both inside
    ``event_properties`` and at top-level, before the legacy ``expires_at`` / ``profile`` keys.
    """
    props = _as_dict(body.get("event_properties"))
    profile = _as_dict(body.get("profile"))
    raw = _first_str(
        props.get("subscription_expires_at"),
        props.get("expires_at"),
        body.get("subscription_expires_at"),
        body.get("expires_at"),
        profile.get("expires_at"),
    )
    if raw is None:
        return None
    # Accept a trailing 'Z' (Python <3.11 fromisoformat rejected it; 3.12 accepts, but normalise
    # defensively for any 'Z' variant). Unparseable -> None, not an error.
    candidate = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        parsed = datetime.datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.UTC)
    return parsed


def parse_transaction_id(body: dict[str, Any]) -> str | None:
    """Apple ``transaction_id`` — unique per billing period (ADR-047, primary grant idem key)."""
    props = _as_dict(body.get("event_properties"))
    return _first_str(props.get("transaction_id"), body.get("transaction_id"))


def parse_original_transaction_id(body: dict[str, Any]) -> str | None:
    """``original_transaction_id`` — stable across the whole subscription chain (idem fallback)."""
    props = _as_dict(body.get("event_properties"))
    return _first_str(props.get("original_transaction_id"), body.get("original_transaction_id"))


def parse_is_active(body: dict[str, Any]) -> bool | None:
    """``is_active`` — strictly a JSON boolean, else None (ADR-047)."""
    props = _as_dict(body.get("event_properties"))
    return _first_bool(props.get("is_active"), body.get("is_active"))


def parse_access_level_id(body: dict[str, Any]) -> str | None:
    """``access_level_id`` (e.g. ``"premium"``) for conditional access_level_updated mapping."""
    props = _as_dict(body.get("event_properties"))
    return _first_str(props.get("access_level_id"), body.get("access_level_id"))


def parse_will_renew(body: dict[str, Any]) -> bool | None:
    """``will_renew`` — strictly a JSON boolean, else None. Audit/log only; NOT persisted."""
    props = _as_dict(body.get("event_properties"))
    return _first_bool(props.get("will_renew"), body.get("will_renew"))


def classify_event(event: ParsedEvent) -> str:
    """Resolve event semantics (ADR-047 §B): one of ``SEM_GRANTING|SEM_EXPIRING|SEM_NOOP``.

    Only events in ``KNOWN_EVENTS`` reach here (unknown types are echoed as ``ignored`` earlier in
    ``handle()``). ``access_level_updated`` is conditional on ``is_active`` / ``access_level_id``:
    premium-active -> granting, inactive -> expiring, otherwise (active-but-not-premium or unknown
    ``is_active``) -> noop (do NOT revoke access).
    """
    event_type = event.event_type
    if event_type in GRANTING_EVENTS:
        return SEM_GRANTING
    if event_type in EXPIRING_EVENTS:
        return SEM_EXPIRING
    if event_type in NOOP_EVENTS:
        return SEM_NOOP
    # CONDITIONAL_EVENTS == {"access_level_updated"}.
    if event.is_active is True and event.access_level_id == ACCESS_LEVEL_PREMIUM:
        return SEM_GRANTING
    if event.is_active is False:
        return SEM_EXPIRING
    return SEM_NOOP
